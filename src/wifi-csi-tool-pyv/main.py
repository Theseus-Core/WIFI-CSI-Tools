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

from PyQt5.Qt import *
from pyqtgraph import PlotWidget
from PyQt5 import QtCore
import pyqtgraph as pg
from pyqtgraph import ScatterPlotItem
from PyQt5.QtCore import QThread
import threading

# ----------------- 全局常量与变量 -----------------
CSI_DATA_INDEX = 200  # 历史缓冲区大小
CSI_DATA_COLUMNS = 490  # 最大子载波数量

# 【修改】适配新版发送端的数据表头
DATA_COLUMNS_NAMES = ['index', 'len', 'data']

# 使用线程锁保护共享数据
data_lock = threading.Lock()

# 共享历史数据缓冲区
csi_amplitude_history = np.zeros((CSI_DATA_INDEX, CSI_DATA_COLUMNS), dtype=np.float32)
csi_phase_history = np.zeros((CSI_DATA_INDEX, CSI_DATA_COLUMNS), dtype=np.float32)
csi_complex_latest = np.zeros(CSI_DATA_COLUMNS, dtype=np.complex64)

agc_history = np.zeros(CSI_DATA_INDEX, dtype=np.float32)
fft_history = np.zeros(CSI_DATA_INDEX, dtype=np.float32)

current_colors = []
current_data_len = 0

# 【性能关键】降低渲染密度的步长。设为4意味着每隔4个子载波画一条曲线，有效防止UI卡死。
DISPLAY_STEP = 4

class csi_data_graphical_window(QWidget):
    def __init__(self):
        super().__init__()
        self.resize(1280, 900)

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

        # 独立分离出 AGC 和 FFT 曲线
        self.agc_curve = self.plotWidget_multi_data.plot([], name='AGC Gain', pen=(255,255,0))
        self.fft_curve = self.plotWidget_multi_data.plot([], name='FFT Gain', pen=(255,0,255))
        
        self.amp_curves = []
        for i in range(CSI_DATA_COLUMNS):
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
        for i in range(CSI_DATA_COLUMNS):
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

        # 缓存状态，防止重复计算
        self.cached_colors = []
        self.cached_brushes = []
        self.last_data_len = 0

        # 定时器刷新 UI
        self.timer = pg.QtCore.QTimer()
        self.timer.timeout.connect(self.update_data)
        self.timer.start(50) # 100ms (10帧/秒) 已经足够人眼观测

    def update_data(self):
        # 1. 快速获取数据快照并释放锁
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

        # 2. 如果载波长度或颜色发生变化，更新画笔和视图范围
        if data_len != self.last_data_len or colors_snapshot != self.cached_colors:
            self.plotWidget_ted.setXRange(0, data_len)
            
            self.cached_brushes = [pg.mkBrush(c) for c in colors_snapshot]
            self.cached_colors = colors_snapshot
            self.last_data_len = data_len

            # 隐藏所有历史曲线，重新按步长显示
            for c in self.amp_curves + self.phase_curves:
                c.setVisible(False)
            
            for i in range(0, data_len, DISPLAY_STEP):
                color = colors_snapshot[i] if i < len(colors_snapshot) else (200,200,200)
                self.amp_curves[i].setPen(color)
                self.amp_curves[i].setVisible(True)
                self.phase_curves[i].setPen(color)
                self.phase_curves[i].setVisible(True)

        # 3. 更新最后一帧相位
        self.curve.setData(np.angle(complex_latest))

        # 4. 更新历史曲线
        self.agc_curve.setData(agc_hist)
        self.fft_curve.setData(fft_hist)

        for i in range(0, data_len, DISPLAY_STEP):
            self.amp_curves[i].setData(amp_history[:, i])
            self.phase_curves[i].setData(phase_history[:, i])

        # 5. 更新 IQ 散点图
        real_parts = np.real(complex_latest)
        imag_parts = np.imag(complex_latest)
        self.iq_scatter.setData(x=real_parts, y=imag_parts, brush=self.cached_brushes)


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

def csi_data_read_parse(port: str, csv_writer, log_file_fd):
    global current_colors, current_data_len

    try:
        ser = serial.Serial(port=port, baudrate=921600, bytesize=8, parity='N', stopbits=1)
    except Exception as e:
        print(f'Open port failed: {e}')
        return
    
    print('Port open success')
    count = 0

    while True:
        try:
            
            strings = str(ser.readline())
            raw_line = strings
            
            if not strings:
                break
            strings = strings.lstrip("b'").rstrip("\\r\\n'")
            
            # 【修改】暴力提取 data 部分，找到 [] 闭合的数据
            #print(strings)
            
            # 查找 data: 或者直接查找 [ 和 ]
            data_start = strings.find('[')
            data_end = strings.find(']')
            
            if data_start == -1 or data_end == -1 or data_end <= data_start:
                log_file_fd.write(strings + '\n')
                log_file_fd.flush()
                print("No valid data array found, skipping...")
                continue
            
            # 提取 data 数组字符串
            data_str = strings[data_start + 1:data_end]
            
            # 使用包序号计数器
            if not hasattr(csi_data_read_parse, 'pkt_counter'):
                csi_data_read_parse.pkt_counter = 0
            pkt_index = csi_data_read_parse.pkt_counter
            csi_data_read_parse.pkt_counter += 1
            
            # 将字符串转换为整数数组
            csi_raw_data = [int(x.strip()) for x in data_str.split(',') if x.strip()]
            csi_data_len = len(csi_raw_data) // 2  # I/Q 配对

        except Exception as e:
            log_file_fd.write('parse error\n' + strings + '\n')
            continue

        if csi_data_len == 0:
            log_file_fd.write('csi_data_len is zero\n' + strings + '\n')
            continue

        # 原有的 fft_gain 和 agc_gain 处理（保持原样）
        fft_gain = 0.0
        agc_gain = 0.0

        # 原有的 CSV 写入（保持原样）
        csv_writer.writerow([pkt_index, csi_data_len, f"[{data_str}]"])

        # ---------------- 核心性能优化区（完全保持原样） ----------------
        raw_arr = np.array(csi_raw_data, dtype=np.float32)
        real_parts = raw_arr[1::2] # 奇数索引是实部
        imag_parts = raw_arr[0::2] # 偶数索引是虚部
        new_complex_row = real_parts + 1j * imag_parts
        
        sub_len = len(new_complex_row)

        new_amp_row = np.abs(new_complex_row)
        new_phase_row = np.angle(new_complex_row)
        
        if count == 0:
            count = 1
            raw_len = len(csi_raw_data)
            print(csi_data_len)
            if csi_data_len == 106:
                colors = generate_subcarrier_colors((0,25), (27,53), None, sub_len)
            elif csi_data_len == 114:
                colors = generate_subcarrier_colors((0,27), (29,56), None, sub_len)
            elif csi_data_len == 117:
                colors = generate_subcarrier_colors((0,38), (39,78), (79,116), sub_len)
            elif csi_data_len == 52:
                colors = generate_subcarrier_colors((0,12), (13,26), None, sub_len)
            elif csi_data_len == 234 :
                colors = generate_subcarrier_colors((0,77), (78,155), (156,233), sub_len)
            elif csi_data_len == 228 :
                colors = generate_subcarrier_colors((0,28), (29,57), (57,113), sub_len)
            elif csi_data_len == 490 :
                colors = generate_subcarrier_colors((0,61), (62,122), (123,245), sub_len)
            elif csi_data_len == 128 :
                colors = generate_subcarrier_colors((0,31), (32,63), None, sub_len)
            elif csi_data_len == 256 :
                colors = generate_subcarrier_colors((0,32), (32,63), (64,128), sub_len)
            elif csi_data_len == 512 :
                colors = generate_subcarrier_colors((0,63), (64,127), (128,256), sub_len)
            elif csi_data_len == 384 :
                colors = generate_subcarrier_colors((0,63), (64,127), (128,192), sub_len)
            elif 0 < csi_data_len <= 612:
                colors = generate_subcarrier_colors((0,raw_len//2), (raw_len//2+1,raw_len-1), None, sub_len)
            else:
                colors = [(200,200,200)] * sub_len
            
            print(len(colors))
            print(colors)
            with data_lock:
                current_colors = colors
                current_data_len = sub_len

        # 加锁更新全局历史数组（完全保持原样）
        with data_lock:
            csi_amplitude_history[:-1] = csi_amplitude_history[1:]
            csi_amplitude_history[-1, :sub_len] = new_amp_row

            csi_phase_history[:-1] = csi_phase_history[1:]
            csi_phase_history[-1, :sub_len] = new_phase_row

            csi_complex_latest[:sub_len] = new_complex_row
            
            agc_history[:-1] = agc_history[1:]
            agc_history[-1] = agc_gain
            
            fft_history[:-1] = fft_history[1:]
            fft_history[-1] = fft_gain

    ser.close()
"""
def csi_data_read_parse(port: str, csv_writer, log_file_fd):
    global current_colors, current_data_len

    try:
        ser = serial.Serial(port=port, baudrate=921600, bytesize=8, parity='N', stopbits=1)
    except Exception as e:
        print(f'Open port failed: {e}')
        return
    
    print('Port open success')
    count = 0

    while True:
        try:
            
            strings = str(ser.readline())
            raw_line = strings
            
            if not strings:
                break
            strings = strings.lstrip("b'").rstrip("\\r\\n'")
            
            # 【修改】寻找新版 C 代码的特征字符 "data:["
            print(strings)
            if "data:[" not in strings:
                log_file_fd.write(strings + '\n')
                log_file_fd.flush()
                print("not f")
                continue

            # 【修改】解析格式： index:X len:Y data:[Z,Z,Z]
            prefix, data_str = strings.split("data:[")
            data_str = data_str.replace("]", "").strip()
            
            prefix_parts = prefix.strip().split()
            pkt_index = int(prefix_parts[0].split(":")[1])
            csi_data_len = int(prefix_parts[1].split(":")[1])
            
            # 将字符串转换为整数数组
            csi_raw_data = [int(x) for x in data_str.split(',') if x.strip()]

        except Exception as e:
            log_file_fd.write('parse error\n' + strings + '\n')
            continue

        if csi_data_len != len(csi_raw_data):
            log_file_fd.write('csi_data_len is not equal\n' + strings + '\n')
            continue

        # 【修改】因为 C 端去掉了这两个数据，为了保持原图表不崩溃，赋默认值 0
        fft_gain = 0.0
        agc_gain = 0.0

        # 【修改】将精简后的数据存入 CSV
        csv_writer.writerow([pkt_index, csi_data_len, f"[{data_str}]"])

        # ---------------- 核心性能优化区 ----------------
        raw_arr = np.array(csi_raw_data, dtype=np.float32)
        real_parts = raw_arr[1::2] # 奇数索引是实部
        imag_parts = raw_arr[0::2] # 偶数索引是虚部
        new_complex_row = real_parts + 1j * imag_parts
        
        sub_len = len(new_complex_row)

        new_amp_row = np.abs(new_complex_row)
        new_phase_row = np.angle(new_complex_row)
        
        if count == 0:
            count = 1
            raw_len = len(csi_raw_data)
            if csi_data_len == 106:
                colors = generate_subcarrier_colors((0,25), (27,53), None, sub_len)
            elif csi_data_len == 114:
                colors = generate_subcarrier_colors((0,27), (29,56), None, sub_len)
            elif csi_data_len == 52:
                colors = generate_subcarrier_colors((0,12), (13,26), None, sub_len)
            elif csi_data_len == 234 :
                colors = generate_subcarrier_colors((0,28), (29,56), (60,116), sub_len)
            elif csi_data_len == 228 :
                colors = generate_subcarrier_colors((0,28), (29,57), (57,113), sub_len)
            elif csi_data_len == 490 :
                colors = generate_subcarrier_colors((0,61), (62,122), (123,245), sub_len)
            elif csi_data_len == 128 :
                colors = generate_subcarrier_colors((0,31), (32,63), None, sub_len)
            elif csi_data_len == 256 :
                colors = generate_subcarrier_colors((0,32), (32,63), (64,128), sub_len)
            elif csi_data_len == 512 :
                colors = generate_subcarrier_colors((0,63), (64,127), (128,256), sub_len)
            elif csi_data_len == 384 :
                colors = generate_subcarrier_colors((0,63), (64,127), (128,192), sub_len)
            elif 0 < csi_data_len <= 612:
                colors = generate_subcarrier_colors((0,raw_len//2), (raw_len//2+1,raw_len-1), None, sub_len)
            else:
                colors = [(200,200,200)] * sub_len
            
            with data_lock:
                current_colors = colors
                current_data_len = sub_len

        # 加锁更新全局历史数组
        with data_lock:
            csi_amplitude_history[:-1] = csi_amplitude_history[1:]
            csi_amplitude_history[-1, :sub_len] = new_amp_row

            csi_phase_history[:-1] = csi_phase_history[1:]
            csi_phase_history[-1, :sub_len] = new_phase_row

            csi_complex_latest[:sub_len] = new_complex_row
            
            agc_history[:-1] = agc_history[1:]
            agc_history[-1] = agc_gain
            
            fft_history[:-1] = fft_history[1:]
            fft_history[-1] = fft_gain

    ser.close()
"""

class SubThread(QThread):
    def __init__(self, serial_port, save_file_name, log_file_name):
        super().__init__()
        self.serial_port = serial_port
        save_file_fd = open(save_file_name, 'w', newline='')
        self.log_file_fd = open(log_file_name, 'w')
        self.csv_writer = csv.writer(save_file_fd)
        # 【修改】使用新的表头写入 CSV
        self.csv_writer.writerow(DATA_COLUMNS_NAMES)

    def run(self):
        csi_data_read_parse(self.serial_port, self.csv_writer, self.log_file_fd)

    def __del__(self):
        self.wait()
        self.log_file_fd.close()


if __name__ == '__main__':
    if sys.version_info < (3, 6):
        print(' Python version should >= 3.6')
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

    subthread = SubThread(args.port, args.store_file, args.log_file)
    window = csi_data_graphical_window()
    
    subthread.start()
    window.show()

    sys.exit(app.exec())