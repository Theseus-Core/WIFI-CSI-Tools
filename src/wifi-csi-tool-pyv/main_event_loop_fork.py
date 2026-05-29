#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0

import sys
import csv
import json
import argparse
import numpy as np
import serial
from os import path
from io import StringIO
from collections import defaultdict
from functools import wraps
import time
import threading
from collections import deque
import os
from PyQt5.Qt import *
from pyqtgraph import PlotWidget
from PyQt5 import QtCore
import pyqtgraph as pg
from pyqtgraph import ScatterPlotItem
from PyQt5.QtCore import QThread
CALLBACK_FREQ = 1
# ----------------- 事件驱动框架 -----------------
class CSIEventBus:
    """CSI数据事件总线，支持多回调注册"""
    
    def __init__(self):
        self._callbacks = defaultdict(list)
        self._callback_names = set()
        
    def register(self, event_name, callback, priority=0):
        """
        注册回调函数
        :param event_name: 事件名称，如 'on_csi_data', 'on_frame_start'
        :param callback: 回调函数
        :param priority: 优先级，数值越大越先执行
        """
        if not callable(callback):
            raise ValueError(f"Callback {callback} is not callable")
        
        self._callbacks[event_name].append({
            'callback': callback,
            'priority': priority,
            'name': getattr(callback, '__name__', str(callback))
        })
        # 按优先级排序
        self._callbacks[event_name].sort(key=lambda x: x['priority'], reverse=True)
        
    def unregister(self, event_name, callback):
        """取消注册回调函数"""
        if event_name in self._callbacks:
            self._callbacks[event_name] = [
                cb for cb in self._callbacks[event_name] 
                if cb['callback'] != callback
            ]
            
    def emit(self, event_name, data):
        """
        触发事件
        :param event_name: 事件名称
        :param data: 事件数据
        :return: 所有回调的返回值列表
        """
        results = []
        for cb_info in self._callbacks.get(event_name, []):
            try:
                start_time = time.perf_counter()
                result = cb_info['callback'](data)
                elapsed = time.perf_counter() - start_time
                
                # 性能警告：如果回调执行时间超过5ms
                if elapsed > 0.005:
                    print(f"Performance warning: callback '{cb_info['name']}' took {elapsed*1000:.2f}ms")
                    
                results.append(result)
            except Exception as e:
                print(f"Error in callback {cb_info['name']}: {e}")
                import traceback
                traceback.print_exc()
        return results

# 全局事件总线实例
csi_event_bus = CSIEventBus()

def on_csi_event(event_name, priority=0):
    """
    装饰器：注册CSI事件回调
    
    使用示例:
        @on_csi_event('on_csi_data')
        def my_handler(data):
            print(f"Received {len(data['complex_data'])} subcarriers")
            return data
            
        @on_csi_event('on_csi_data', priority=10)
        def high_priority_handler(data):
            # 高优先级，先执行
            pass
    """
    def decorator(func):
        csi_event_bus.register(event_name, func, priority=priority)
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator

# ----------------- 数据处理管道 -----------------
class CSIDataProcessor:
    """CSI数据处理器，负责数据解析和预处理"""
    
    def __init__(self):
        self.stats = {
            'total_packets': 0,
            'valid_packets': 0,
            'invalid_packets': 0,
            'avg_processing_time': 0.0
        }
        
    def parse_raw_line(self, raw_line):
        """
        解析原始串口数据行
        :return: 解析后的数据字典，失败返回None
        """
        try:
            strings = str(raw_line)
            if not strings:
                return None
                
            strings = strings.lstrip("b'").rstrip("\\r\\n'")
            
            # 查找 data 数组
            data_start = strings.find('[')
            data_end = strings.find(']')
            
            if data_start == -1 or data_end == -1 or data_end <= data_start:
                return None
            
            # 提取数据
            data_str = strings[data_start + 1:data_end]
            
            # 解析包序号（如果没有则自动生成）
            pkt_index = None
            if 'index:' in strings:
                try:
                    pkt_index = int(strings.split('index:')[1].split()[0])
                except:
                    pass
            
            # 转换数据
            csi_raw_data = [int(x.strip()) for x in data_str.split(',') if x.strip()]
            
            return {
                'raw_line': strings,
                'data_str': data_str,
                'raw_data': csi_raw_data,
                'pkt_index': pkt_index,
                'data_len': len(csi_raw_data) // 2
            }
        except Exception as e:
            return None
    
    def process_csi_data(self, parsed_data):
        """
        处理CSI数据，转换为复数、幅度、相位
        :return: 处理后的数据字典
        """
        start_time = time.perf_counter()
        
        raw_arr = np.array(parsed_data['raw_data'], dtype=np.float32)
        real_parts = raw_arr[1::2]  # 奇数索引是实部
        imag_parts = raw_arr[0::2]  # 偶数索引是虚部
        
        complex_data = real_parts + 1j * imag_parts
        amplitude = np.abs(complex_data)
        phase = np.angle(complex_data)
        
        # 更新统计
        self.stats['total_packets'] += 1
        self.stats['valid_packets'] += 1
        elapsed = time.perf_counter() - start_time
        self.stats['avg_processing_time'] = (
            self.stats['avg_processing_time'] * (self.stats['valid_packets'] - 1) + elapsed
        ) / self.stats['valid_packets']
        
        return {
            'complex_data': complex_data,
            'amplitude': amplitude,
            'phase': phase,
            'pkt_index': parsed_data.get('pkt_index'),
            'data_len': len(complex_data),
            'raw_data': parsed_data['raw_data'],
            'timestamp': time.time()
        }
    
    def get_stats(self):
        """获取处理统计信息"""
        return self.stats.copy()

# ----------------- 数据读取与分发线程 -----------------
class CSIDataReader(QThread):
    """CSI数据读取线程，负责从串口读取并分发数据"""
    
    def __init__(self, port, csv_writer, log_file_fd):
        super().__init__()
        self.port = port
        self.csv_writer = csv_writer
        self.log_file_fd = log_file_fd
        self.processor = CSIDataProcessor()
        self.is_running = True
        self.serial = None
        
    def run(self):
        try:
            self.serial = serial.Serial(
                port=self.port, 
                baudrate=921600, 
                bytesize=8, 
                parity='N', 
                stopbits=1,
                timeout=1
            )
        except Exception as e:
            print(f'Open port failed: {e}')
            csi_event_bus.emit('on_error', {'error': str(e), 'type': 'serial_open'})
            return
        
        print('Port open success')
        csi_event_bus.emit('on_serial_ready', {'port': self.port})
        
        pkt_counter = 0
        
        while self.is_running:
            try:
                raw_line = self.serial.readline()
                if not raw_line:
                    continue
                
                # 触发原始数据事件
                csi_event_bus.emit('on_raw_data', {'raw_line': raw_line})
                
                # 解析数据
                parsed = self.processor.parse_raw_line(raw_line)
                if not parsed:
                    self.log_file_fd.write(f'Parse failed: {raw_line}\n')
                    self.log_file_fd.flush()
                    csi_event_bus.emit('on_parse_error', {'raw_line': raw_line})
                    continue
                
                # 添加包序号
                if parsed['pkt_index'] is None:
                    parsed['pkt_index'] = pkt_counter
                    pkt_counter += 1
                
                # 保存到CSV
                if self.csv_writer:
                    self.csv_writer.writerow([
                        parsed['pkt_index'], 
                        parsed['data_len'], 
                        f"[{parsed['data_str']}]"
                    ])
                
                # 处理CSI数据
                processed_data = self.processor.process_csi_data(parsed)
                
                # 触发CSI数据事件（主要事件）
                csi_event_bus.emit('on_csi_data', processed_data)
                
                # 每100包输出统计信息
                if self.processor.stats['valid_packets'] % CALLBACK_FREQ == 0:
                    stats = self.processor.get_stats()
                    csi_event_bus.emit('on_stats_update', stats)
                    
            except Exception as e:
                self.log_file_fd.write(f'Error in read loop: {e}\n')
                self.log_file_fd.flush()
                csi_event_bus.emit('on_error', {'error': str(e), 'type': 'read_loop'})
        
        if self.serial:
            self.serial.close()
    
    def stop(self):
        self.is_running = False
        if self.serial:
            self.serial.close()

# ----------------- UI 更新回调（使用装饰器注册） -----------------
# 全局变量（保持与原代码兼容）
csi_amplitude_history = np.zeros((200, 490), dtype=np.float32)
csi_phase_history = np.zeros((200, 490), dtype=np.float32)
csi_complex_latest = np.zeros(490, dtype=np.complex64)
agc_history = np.zeros(200, dtype=np.float32)
fft_history = np.zeros(200, dtype=np.float32)
current_colors = []
current_data_len = 0
data_lock = threading.Lock()
DISPLAY_STEP = 4

# 颜色生成函数（保持不变）
def generate_subcarrier_colors(red_range, green_range, yellow_range, total_num):
    colors = []
    for i in range(total_num):
        if red_range and red_range[0] <= i <= red_range[1]:
            intensity = int(255 * (i - red_range[0]) / (red_range[1] - red_range[0]))
            colors.append((intensity, 0, 0))
        elif green_range and green_range[0] <= i <= green_range[1]:
            intensity = int(255 * (i - green_range[0]) / (green_range[1] - green_range[0]))
            colors.append((0, intensity, 0))
        elif yellow_range and yellow_range[0] <= i <= yellow_range[1]:
            intensity = int(255 * (i - yellow_range[0]) / (yellow_range[1] - yellow_range[0]))
            colors.append((0, intensity, intensity))
        else:
            colors.append((200, 200, 200))
    return colors

@on_csi_event('on_csi_data', priority=100)
def update_ui_data(processed_data):
    """更新UI数据缓冲区"""
    global current_colors, current_data_len
    global csi_amplitude_history, csi_phase_history, csi_complex_latest
    global agc_history, fft_history
    
    sub_len = processed_data['data_len']
    new_complex = processed_data['complex_data']
    new_amp = processed_data['amplitude']
    new_phase = processed_data['phase']
    
    # 初始化颜色（仅第一次）
    with data_lock:
        if current_data_len == 0:
            raw_len = len(processed_data['raw_data'])
            if sub_len == 106:
                colors = generate_subcarrier_colors((0,25), (27,53), None, sub_len)
            elif sub_len == 114:
                colors = generate_subcarrier_colors((0,27), (29,56), None, sub_len)
            elif sub_len == 117:
                colors = generate_subcarrier_colors((0,38), (39,78), (79,116), sub_len)
            elif sub_len == 52:
                colors = generate_subcarrier_colors((0,12), (13,26), None, sub_len)
            elif sub_len == 234:
                colors = generate_subcarrier_colors((0,77), (78,155), (156,233), sub_len)
            elif sub_len == 228:
                colors = generate_subcarrier_colors((0,28), (29,57), (57,113), sub_len)
            elif sub_len == 490:
                colors = generate_subcarrier_colors((0,61), (62,122), (123,245), sub_len)
            elif sub_len == 128:
                colors = generate_subcarrier_colors((0,31), (32,63), None, sub_len)
            elif sub_len == 256:
                colors = generate_subcarrier_colors((0,32), (32,63), (64,128), sub_len)
            elif sub_len == 512:
                colors = generate_subcarrier_colors((0,63), (64,127), (128,256), sub_len)
            elif sub_len == 384:
                colors = generate_subcarrier_colors((0,63), (64,127), (128,192), sub_len)
            elif 0 < sub_len <= 612:
                colors = generate_subcarrier_colors((0,raw_len//2), (raw_len//2+1,raw_len-1), None, sub_len)
            else:
                colors = [(200,200,200)] * sub_len
            
            current_colors = colors
            current_data_len = sub_len
        
        # 更新历史数据
        csi_amplitude_history[:-1] = csi_amplitude_history[1:]
        csi_amplitude_history[-1, :sub_len] = new_amp
        
        csi_phase_history[:-1] = csi_phase_history[1:]
        csi_phase_history[-1, :sub_len] = new_phase
        
        csi_complex_latest[:sub_len] = new_complex

from collections import deque
import numpy as np

# 使用NumPy数组替代deque以提高性能
g_slide_window = None  # 将在第一次使用时初始化
window_size = 50
init_max_std = 0
sw_emj_list = ["🤔","😀","🤓"]
sw_emj_list_idx = 0
sleep_flag = 0

@on_csi_event('on_csi_data', priority=10)
def print_stats(data):
    global init_max_std
    global g_slide_window
    global sw_emj_list_idx
    global sleep_flag
    
    data_len = data['data_len']
    amplitude = data['amplitude']  # shape: (data_len,)
    
    #print(f"Received {data_len} subcarriers")
    
    # 初始化NumPy滚动窗口
    if g_slide_window is None:
        # 预分配内存 (window_size, max_subcarriers)
        g_slide_window = np.zeros((window_size, len(amplitude)), dtype=np.float32)
        g_slide_window_idx = 0
        g_slide_window_filled = False
    else:
        g_slide_window_idx = getattr(print_stats, 'idx', 0)
        g_slide_window_filled = getattr(print_stats, 'filled', False)
    
    # 添加新数据到窗口（循环覆盖）
    g_slide_window[g_slide_window_idx] = amplitude
    g_slide_window_idx = (g_slide_window_idx + 1) % window_size
    
    if g_slide_window_idx == 0:
        g_slide_window_filled = True
    
    # 保存状态
    print_stats.idx = g_slide_window_idx
    print_stats.filled = g_slide_window_filled
    
    # 显示收集进度
    current_size = window_size if g_slide_window_filled else g_slide_window_idx
    #print(f"Collecting data... ({current_size}/{window_size})")
    
    if current_size < window_size:
        return
    
    # 窗口已满，进行统计分析
    # 获取完整的窗口数据（按时间顺序）
    if g_slide_window_filled:
        # 重新排列为时间顺序（当前索引是最新的，之前的是更旧的）
        indices = np.roll(np.arange(window_size), -g_slide_window_idx)
        window_data = g_slide_window[indices]
    else:
        window_data = g_slide_window[:g_slide_window_idx]
    
    # 计算每个子载波的统计量 (axis=0 表示沿时间轴)
    mean_vals = np.mean(window_data, axis=0)      # 每个子载波的均值
    std_vals = np.std(window_data, axis=0)        # 每个子载波的标准差
    var_vals = np.var(window_data, axis=0)        # 每个子载波的方差
    
    max_std_idx = np.argmax(std_vals)  # 获取标准差最大的子载波索引
    
    if std_vals[max_std_idx] > init_max_std:
        
        init_max_std = std_vals[max_std_idx]
        print(init_max_std)
    if std_vals[max_std_idx] > 2 and sleep_flag > 50:
        print(f"{sw_emj_list[sw_emj_list_idx]} action triggered by subcarrier ")
        sleep_flag = 0
        sw_emj_list_idx = (sw_emj_list_idx + 1) % len(sw_emj_list)

    
    sleep_flag += 1




    
    

   
        

'''
@on_csi_event('on_csi_data', priority=60)
def my_custom_handler(data):
    # 处理CSI数据
    
    # 可以触发新的事件
    csi_event_bus.emit('my_custom_event', data)
    return processed_result
'''






@on_csi_event('on_error')
def handle_error(error_data):
    """处理错误"""
    print(f"Error: {error_data}")
    

# ----------------- GUI 窗口类（保持不变但集成事件） -----------------
class CSIDataGraphicalWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.resize(1280, 900)
        self.setup_ui()
        
        self.cached_colors = []
        self.cached_brushes = []
        self.last_data_len = 0
        
        # 定时器刷新 UI
        self.timer = pg.QtCore.QTimer()
        self.timer.timeout.connect(self.update_display)
        self.timer.start(50)
        
        # 注册UI更新回调（通过事件总线）
        self.setup_event_handlers()
    
    def setup_ui(self):
        # 1. 最后一帧相位图 (左上)
        self.plotWidget_ted = PlotWidget(self)
        self.plotWidget_ted.setGeometry(QtCore.QRect(0, 0, 640, 300))
        self.plotWidget_ted.setYRange(-2*np.pi, 2*np.pi)
        self.plotWidget_ted.addLegend()
        self.plotWidget_ted.setTitle('Phase Data - Last Frame')
        self.plotWidget_ted.setLabel('left', 'Phase (rad)')
        self.plotWidget_ted.setLabel('bottom', 'Subcarrier Index')
        self.curve = self.plotWidget_ted.plot([], name='CSI Row Data', pen='r')
        
        # 2. 幅度历史图 (中间)
        self.plotWidget_multi_data = PlotWidget(self)
        self.plotWidget_multi_data.setGeometry(QtCore.QRect(0, 300, 1280, 300))
        self.plotWidget_multi_data.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis)
        self.plotWidget_multi_data.addLegend()
        self.plotWidget_multi_data.setTitle('Subcarrier Amplitude Data')
        self.plotWidget_multi_data.setLabel('left', 'Amplitude')
        self.plotWidget_multi_data.setLabel('bottom', 'Time (Cumulative Packet Count)')
        
        self.agc_curve = self.plotWidget_multi_data.plot([], name='AGC Gain', pen=(255,255,0))
        self.fft_curve = self.plotWidget_multi_data.plot([], name='FFT Gain', pen=(255,0,255))
        
        self.amp_curves = []
        for i in range(490):
            curve = self.plotWidget_multi_data.plot([], pen=(200, 200, 200))
            curve.setVisible(False)
            self.amp_curves.append(curve)
        
        # 3. 相位历史图 (下方)
        self.plotWidget_phase_data = PlotWidget(self)
        self.plotWidget_phase_data.setGeometry(QtCore.QRect(0, 600, 1280, 300))
        self.plotWidget_phase_data.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis)
        self.plotWidget_phase_data.addLegend()
        self.plotWidget_phase_data.setTitle('Subcarrier Phase Data')
        self.plotWidget_phase_data.setLabel('left', 'Phase (rad)')
        self.plotWidget_phase_data.setLabel('bottom', 'Time (Cumulative Packet Count)')
        
        self.phase_curves = []
        for i in range(490):
            curve = self.plotWidget_phase_data.plot([], pen=(200, 200, 200))
            curve.setVisible(False)
            self.phase_curves.append(curve)
        
        # 4. IQ 散点图 (右上)
        self.plotWidget_iq = PlotWidget(self)
        self.plotWidget_iq.setGeometry(QtCore.QRect(640, 0, 640, 300))
        self.plotWidget_iq.setLabel('left', 'Q (Imag)')
        self.plotWidget_iq.setLabel('bottom', 'I (Real)')
        self.plotWidget_iq.setTitle('IQ Plot - Last Frame')
        self.plotWidget_iq.getViewBox().setRange(QtCore.QRectF(-30, -30, 60, 60))
        self.plotWidget_iq.getViewBox().setAspectLocked(True)
        
        self.iq_scatter = ScatterPlotItem(size=6)
        self.plotWidget_iq.addItem(self.iq_scatter)
    
    def setup_event_handlers(self):
        """设置UI相关的事件处理器"""
        # 注意：UI更新在主线程，数据更新在后台线程，需要线程安全
        pass
    
    def update_display(self):
        """定时器触发的UI更新"""
        with data_lock:
            data_len = current_data_len
            if data_len == 0:
                return
            colors_snapshot = current_colors
            complex_latest = csi_complex_latest[:data_len].copy()
            amp_history = csi_amplitude_history.copy()
            phase_history = csi_phase_history.copy()
            agc_hist = agc_history.copy()
            fft_hist = fft_history.copy()
        
        # 更新视图范围
        if data_len != self.last_data_len or colors_snapshot != self.cached_colors:
            self.plotWidget_ted.setXRange(0, data_len)
            
            self.cached_brushes = [pg.mkBrush(c) for c in colors_snapshot]
            self.cached_colors = colors_snapshot
            self.last_data_len = data_len
            
            for c in self.amp_curves + self.phase_curves:
                c.setVisible(False)
            
            for i in range(0, data_len, DISPLAY_STEP):
                color = colors_snapshot[i] if i < len(colors_snapshot) else (200,200,200)
                self.amp_curves[i].setPen(color)
                self.amp_curves[i].setVisible(True)
                self.phase_curves[i].setPen(color)
                self.phase_curves[i].setVisible(True)
        
        # 更新显示
        self.curve.setData(np.angle(complex_latest))
        self.agc_curve.setData(agc_hist)
        self.fft_curve.setData(fft_hist)
        
        for i in range(0, data_len, DISPLAY_STEP):
            self.amp_curves[i].setData(amp_history[:, i])
            self.phase_curves[i].setData(phase_history[:, i])
        
        real_parts = np.real(complex_latest)
        imag_parts = np.imag(complex_latest)
        self.iq_scatter.setData(x=real_parts, y=imag_parts, brush=self.cached_brushes)


if __name__ == '__main__':
    if sys.version_info < (3, 6):
        print('Python version should >= 3.6')
        exit()
    
    parser = argparse.ArgumentParser(
        description='Read CSI data from serial port and display it graphically')
    parser.add_argument('-p', '--port', dest='port', action='store', required=True,
                        help='Serial port number of csv_recv device')
    parser.add_argument('-s', '--store', dest='store_file', action='store', default='./csi_data.csv',
                        help='Save the data printed by the serial port to a file')
    parser.add_argument('-l', '--log', dest='log_file', action='store', default='./csi_data_log.txt',
                        help='Save other serial data the bad CSI data to a log file')
    
    args = parser.parse_args()
    
    app = QApplication(sys.argv)
    
    # 准备文件
    save_file_fd = open(args.store_file, 'w', newline='')
    log_file_fd = open(args.log_file, 'w')
    csv_writer = csv.writer(save_file_fd)
    csv_writer.writerow(['index', 'len', 'data'])
    
    # 创建数据读取线程
    reader = CSIDataReader(args.port, csv_writer, log_file_fd)
    
    # 创建UI窗口
    window = CSIDataGraphicalWindow()
    
    # 启动线程
    reader.start()
    window.show()
    
    # 程序退出时清理
    def cleanup():
        reader.stop()
        reader.wait()
        save_file_fd.close()
        log_file_fd.close()
    
    app.aboutToQuit.connect(cleanup)
    
    sys.exit(app.exec())