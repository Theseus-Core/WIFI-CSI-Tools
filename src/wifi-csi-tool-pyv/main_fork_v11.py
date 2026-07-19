#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0

import sys
import json
import argparse
import numpy as np
import serial
from os import path
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
from PyQt5.QtCore import QThread, pyqtSignal, QObject, QMetaObject, Qt, Q_ARG

CALLBACK_FREQ = 1

# 全局UI窗口引用
g_ui_window = None

# ----------------- 可拖动面板类 -----------------
class DraggablePanel(QFrame):
    """可拖动的浮动面板"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.dragging = False
        self.drag_position = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if self.dragging and event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self.drag_position)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = False
            event.accept()

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
    
    def __init__(self, port, log_file_fd):
        super().__init__()
        self.port = port
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

# 使用NumPy数组替代deque以提高性能
g_slide_window = None  # 将在第一次使用时初始化
window_size = 50
init_max_std = 0
sw_emj_list = ["🤔","😀","🤓"]
sw_emj_list_idx = 0
sleep_flag = 0

timer = float(0)
dataset = []

# 状态机全局变量
recording_state = False
above_threshold_count = 0
below_threshold_count = 0
session_cache = []
history_buffer = deque(maxlen=3)

@on_csi_event('on_csi_data', priority=10)
def print_stats(data):
    global init_max_std
    global g_slide_window
    global sw_emj_list_idx
    global sleep_flag
    global g_ui_window
    global timer
    global dataset
    global recording_state
    global above_threshold_count
    global below_threshold_count
    global session_cache
    global history_buffer
    
    data_len = data['data_len']
    amplitude = data['amplitude']  # shape: (data_len,)
    
    # 定义需要剔除的子载波索引（恒为0的子载波）
    ZERO_SUBCARRIERS_TO_REMOVE = [57, 58, 59]  # 根据检测结果配置
    
    # 创建需要保留的子载波掩码
    keep_mask = np.ones(data_len, dtype=bool)
    for idx in ZERO_SUBCARRIERS_TO_REMOVE:
        if idx < data_len:  # 确保索引在范围内
            keep_mask[idx] = False
    
    # 应用掩码，剔除恒为0的子载波
    amplitude = amplitude[keep_mask]
    
    # 如果没有有效子载波，直接返回
    if len(amplitude) == 0:
        return
    
    # 初始化NumPy滚动窗口
    if g_slide_window is None:
        # 预分配内存 (window_size, max_subcarriers)
        g_slide_window = np.zeros((window_size, len(amplitude)), dtype=np.float32)
        g_slide_window_idx = 0
        g_slide_window_filled = False
    else:
        g_slide_window_idx = getattr(print_stats, 'idx', 0)
        g_slide_window_filled = getattr(print_stats, 'filled', False)
        
        # 检查当前窗口的列数是否与新数据匹配
        if g_slide_window.shape[1] != len(amplitude):
            # 如果不匹配，重新初始化窗口
            g_slide_window = np.zeros((window_size, len(amplitude)), dtype=np.float32)
            g_slide_window_idx = 0
            g_slide_window_filled = False
    
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
    
    #计算极差
    range_vals = np.max(window_data, axis=0) - np.min(window_data, axis=0)

    
    max_std_idx = np.argmax(std_vals)  # 获取标准差最大的子载波索引
    min_std_val = np.min(std_vals)      # 最小标准差
    max_std_val = np.max(std_vals)      # 最大标准差
    avg_std_val = np.mean(std_vals)     # 平均标准差
    
    max_range_idx = np.argmax(range_vals)  # 获取极差最大的子载波索引
    min_range_val = np.min(range_vals)      # 最小极差
    max_range_val = np.max(range_vals)      # 最大极差
    avg_range_val = np.mean(range_vals)     # 平均极差
    
    # 将当前帧数据和强度放入历史缓冲区
    history_buffer.append((np.array(window_data), avg_std_val))
    
    threshold = 3
    
    if avg_std_val > threshold:
        above_threshold_count += 1
        below_threshold_count = 0
    else:
        above_threshold_count = 0
        below_threshold_count += 1
        
    if not recording_state:
        if above_threshold_count >= 3:
            recording_state = True
            # 初始化 session_cache，包含历史缓冲区内的3帧
            session_cache = list(history_buffer)
            print(f"[State Machine] Transition to RECORDING state. Initialized with {len(session_cache)} historical frames.")
    else:
        # 在录制状态中，持续收集数据
        session_cache.append((np.array(window_data), avg_std_val))
        
        if below_threshold_count >= 5:
            # 退出录制状态
            recording_state = False
            if session_cache:
                N = len(session_cache)
                num_to_keep = max(1, int(np.ceil(0.30 * N)))
                
                # 按照强度（avg_std_val）降序排序
                sorted_cache = sorted(session_cache, key=lambda x: x[1], reverse=True)
                
                # 保留强度最高的前25%
                kept_samples = sorted_cache[:num_to_keep]
                for sample, val in kept_samples:
                    dataset.append(sample)
                    
                timer += 1  # 动作计数增加
                print(f"[State Machine] Transition to CALM state. Session finished. Total frames: {N}, Kept top 25%: {num_to_keep}, Total dataset samples: {len(dataset)}")
            
            session_cache = []
            above_threshold_count = 0
            below_threshold_count = 0

    # 更新UI显示统计数据（线程安全）
    if g_ui_window:
        # 更新平均标准差
        QMetaObject.invokeMethod(g_ui_window, "update_stat_value",
                                 Qt.QueuedConnection,
                                 Q_ARG(str, "avg_std"),
                                 Q_ARG(float, avg_std_val))
        
        # 使用 update_stat_value_str 更新录制状态和数据集大小
        if recording_state:
            status_str = f"🔴 Rec ({len(session_cache)}f) | Total: {len(dataset)}"
        else:
            status_str = f"🟢 Calm | Total: {len(dataset)}"
            
        QMetaObject.invokeMethod(g_ui_window, "update_stat_value_str",
                                 Qt.QueuedConnection,
                                 Q_ARG(str, "is_wave"),
                                 Q_ARG(str, status_str))

@on_csi_event('on_error')
def handle_error(error_data):
    """处理错误"""
    print(f"Error: {error_data}")

# ----------------- GUI 窗口类 -----------------
class CSIDataGraphicalWindow(QWidget):
    def __init__(self,dataset_tag:str):
        self.dataset_tag = dataset_tag
        super().__init__()
        self.resize(1280, 900)
        self.setup_ui()
        self.setup_stats_panel()  # 添加统计面板
        
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
    
    def setup_stats_panel(self):
        """创建可拖动的统计数据显示面板"""
        # 使用可拖动面板类
        self.stats_panel = DraggablePanel(self)
        
        # 设置面板样式和大小
        self.stats_panel.setFixedSize(320, 280)
        self.stats_panel.setStyleSheet("""
            QFrame {
                background-color: rgba(0, 0, 0, 220);
                border: 2px solid #00ff00;
                border-radius: 10px;
                font-family: monospace;
            }
            QLabel {
                color: #00ff00;
                font-size: 13px;
                padding: 3px;
            }
        """)
        
        # 初始位置（右上角）
        self.stats_panel.move(self.width() - self.stats_panel.width() - 10, 10)
        
        layout = QVBoxLayout(self.stats_panel)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # 添加拖动提示条
        drag_handle = QLabel("⋮⋮ 可拖动面板 ⋮⋮")
        drag_handle.setStyleSheet("""
            QLabel {
                color: #ffff00;
                font-size: 11px;
                background-color: rgba(255, 255, 0, 0.2);
                border-radius: 5px;
                padding: 5px;
            }
        """)
        drag_handle.setAlignment(Qt.AlignCenter)
        layout.addWidget(drag_handle)
        
        # 标题
        title = QLabel("📊 CSI Statistics Monitor")
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: #ffff00;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background-color: #00ff00;")
        layout.addWidget(line)
        
        # 统计数据项
        self.stats_labels = {}
        stats_items = [
            ("avg_std", "📊 Avg StdDev:", "0.0000"),
            ("is_wave", "🎯 Last Action:", "False"),
        ]
        
        for stat_id, display_name, default_value in stats_items:
            hlayout = QHBoxLayout()
            
            name_label = QLabel(display_name)
            name_label.setMinimumWidth(120)
            name_label.setStyleSheet("color: #00ff00;")
            
            value_label = QLabel(default_value)
            value_label.setStyleSheet("color: #ffff00; font-weight: bold; font-size: 14px;")
            value_label.setAlignment(Qt.AlignRight)
            value_label.setWordWrap(True)
            
            hlayout.addWidget(name_label)
            hlayout.addWidget(value_label)
            layout.addLayout(hlayout)
            
            self.stats_labels[stat_id] = value_label
        
        # 添加状态指示
        layout.addStretch()
        
        status_layout = QHBoxLayout()
        status_led = QLabel("●")
        status_led.setStyleSheet("color: #00ff00; font-size: 12px;")
        status_text = QLabel("System Running")
        status_text.setStyleSheet("color: #00ff00; font-size: 11px;")
        
        status_layout.addWidget(status_led)
        status_layout.addWidget(status_text)
        status_layout.addStretch()
        layout.addLayout(status_layout)
        
        # 添加关闭按钮（可选）
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(255, 0, 0, 0.5);
                color: white;
                border-radius: 10px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(255, 0, 0, 0.8);
            }
        """)
        close_btn.clicked.connect(self.toggle_stats_panel)
        
        # 将关闭按钮放在右上角
        close_btn.setParent(self.stats_panel)
        close_btn.move(self.stats_panel.width() - 25, 5)
    
    def toggle_stats_panel(self):
        """切换统计面板显示/隐藏"""
        if self.stats_panel.isVisible():
            self.stats_panel.hide()
        else:
            self.stats_panel.show()
    
    def resizeEvent(self, event):
        """窗口大小改变时调整面板位置"""
        if hasattr(self, 'stats_panel') and self.stats_panel.isVisible():
            # 如果面板在右侧，保持其相对位置
            if self.stats_panel.x() > self.width() / 2:
                self.stats_panel.move(self.width() - self.stats_panel.width() - 10, self.stats_panel.y())
        super().resizeEvent(event)
        
    @pyqtSlot(str, float)
    def update_stat_value(self, stat_name, value):
        """更新浮点数类型的统计值（可从其他线程调用）"""
        if stat_name in self.stats_labels:
            if isinstance(value, float):
                self.stats_labels[stat_name].setText(f"{value:.4f}")
            else:
                self.stats_labels[stat_name].setText(str(value))
    
    @pyqtSlot(str, str)
    def update_stat_value_str(self, stat_name, value):
        """更新字符串类型的统计值（可从其他线程调用）"""
        if stat_name in self.stats_labels:
            self.stats_labels[stat_name].setText(value)
    
    @pyqtSlot(str, int)
    def update_stat_value_int(self, stat_name, value):
        """更新整数类型的统计值（可从其他线程调用）"""
        if stat_name in self.stats_labels:
            self.stats_labels[stat_name].setText(f"{value}")
    
    def setup_event_handlers(self):
        """设置UI相关的事件处理器"""
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

    def closeEvent(self, event):
        """窗口关闭时的处理"""
        global dataset, recording_state, session_cache
        
        # 如果当前仍在录制状态，强制结算剩余缓存数据
        if recording_state and session_cache:
            N = len(session_cache)
            num_to_keep = max(1, int(np.ceil(0.25 * N)))
            sorted_cache = sorted(session_cache, key=lambda x: x[1], reverse=True)
            for sample, val in sorted_cache[:num_to_keep]:
                dataset.append(sample)
            print(f"[Close Event] Flushing remaining recording session: {N} frames -> kept top 25% ({num_to_keep} frames).")
            session_cache = []
            recording_state = False
            
        # 保存 dataset 到 npz 文件
        if dataset:
            save_path = f'data/{self.dataset_tag}_{time.strftime("%Y%m%d_%H%M%S")}.npz'
            np.savez(save_path, 
                     dataset=np.array(dataset),
                     )
            print(f"Dataset saved to {save_path}")

        else:
            print("Dataset is empty, nothing to save")
        
        # 接受关闭事件
        event.accept()


if __name__ == '__main__':
    if sys.version_info < (3, 6):
        print('Python version should >= 3.6')
        exit()
    
    parser = argparse.ArgumentParser(
        description='Read CSI data from serial port and display it graphically')
    parser.add_argument('-p', '--port', dest='port', action='store', required=True,
                        help='Serial port number of csv_recv device')
    parser.add_argument('-l', '--log', dest='log_file', action='store', default='./csi_data_log.txt',
                        help='Save other serial data the bad CSI data to a log file')
    parser.add_argument("-t", "--tag", dest="tag", action="store", required=True, default="", help="Add label to dataset")
    args = parser.parse_args()
    
    app = QApplication(sys.argv)
    
    # 准备日志文件
    log_file_fd = open(args.log_file, 'w')
    
    # 创建数据读取线程（不再需要CSV写入器）
    reader = CSIDataReader(args.port, log_file_fd)
    
    # 创建UI窗口
    window = CSIDataGraphicalWindow(args.tag)
    g_ui_window = window  # 设置全局引用
    
    # 启动线程
    reader.start()
    window.show()
    
    # 程序退出时清理
    def cleanup():
        reader.stop()
        reader.wait()
        log_file_fd.close()
    
    app.aboutToQuit.connect(cleanup)
    
    sys.exit(app.exec())