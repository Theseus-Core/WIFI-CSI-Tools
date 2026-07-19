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
        
        # 设置白色背景
        
        
        # 1. 幅度历史图 (占据大部分区域)
        self.plotWidget_multi_data = PlotWidget(self)
        self.plotWidget_multi_data.setGeometry(QtCore.QRect(0, 0, 1280, 900))
        self.plotWidget_multi_data.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis)
        self.plotWidget_multi_data.addLegend()
        self.plotWidget_multi_data.setTitle('Subcarrier Amplitude Data')
        self.plotWidget_multi_data.setLabel('left', 'Amplitude')
        self.plotWidget_multi_data.setLabel('bottom', 'Time (Cumulative Packet Count)')
      
        self.plotWidget_multi_data.getAxis('bottom').setPen('w')
        self.plotWidget_multi_data.getAxis('left').setPen('w')
        self.plotWidget_multi_data.getAxis('bottom').setTextPen('w')
        self.plotWidget_multi_data.getAxis('left').setTextPen('w')  # 修正拼写错误
        
        # 独立分离出 AGC 和 FFT 曲线
        self.agc_curve = self.plotWidget_multi_data.plot([], name='AGC Gain', pen=(255,165,0))  # 橙色
        self.fft_curve = self.plotWidget_multi_data.plot([], name='FFT Gain', pen=(255,0,255))  # 品红色
        
        self.amp_curves = []
        for i in range(CSI_DATA_COLUMNS):
            curve = self.plotWidget_multi_data.plot([], pen=(200, 200, 200))
            curve.setVisible(False)
            self.amp_curves.append(curve)

        # 缓存状态，防止重复计算
        self.cached_colors = []
        self.last_data_len = 0

        # 定时器刷新 UI
        self.timer = pg.QtCore.QTimer()
        self.timer.timeout.connect(self.update_data)
        self.timer.start(50)  # 50ms (20帧/秒)

    def update_data(self):
        # 1. 快速获取数据快照并释放锁
        with data_lock:
            data_len = current_data_len
            if data_len == 0:
                return
            colors_snapshot = current_colors
            amp_history = csi_amplitude_history.copy()
            agc_hist = agc_history.copy()
            fft_hist = fft_history.copy()

        # 2. 如果载波长度或颜色发生变化，更新画笔和视图范围
        if data_len != self.last_data_len or colors_snapshot != self.cached_colors:
            self.cached_colors = colors_snapshot
            self.last_data_len = data_len

            # 隐藏所有历史曲线，重新按步长显示
            for c in self.amp_curves:
                c.setVisible(False)
            
            for i in range(0, data_len, DISPLAY_STEP):
                color = colors_snapshot[i] if i < len(colors_snapshot) else (200,200,200)
                self.amp_curves[i].setPen(color)
                self.amp_curves[i].setVisible(True)

        # 3. 更新历史曲线
        self.agc_curve.setData(agc_hist)
        self.fft_curve.setData(fft_hist)

        for i in range(0, data_len, DISPLAY_STEP):
            self.amp_curves[i].setData(amp_history[:, i])


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
            colors.append((100, 100, 100))  # 深灰色，在白色背景下更清晰
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

        # ---------------- 核心性能优化区 ----------------
        raw_arr = np.array(csi_raw_data, dtype=np.float32)
        real_parts = raw_arr[1::2]  # 奇数索引是实部
        imag_parts = raw_arr[0::2]  # 偶数索引是虚部
        new_complex_row = real_parts + 1j * imag_parts
        
        sub_len = len(new_complex_row)

        new_amp_row = np.abs(new_complex_row)
        
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
            elif csi_data_len == 234:
                colors = generate_subcarrier_colors((0,77), (78,155), (156,233), sub_len)
            elif csi_data_len == 228:
                colors = generate_subcarrier_colors((0,28), (29,57), (57,113), sub_len)
            elif csi_data_len == 490:
                colors = generate_subcarrier_colors((0,61), (62,122), (123,245), sub_len)
            elif csi_data_len == 128:
                colors = generate_subcarrier_colors((0,31), (32,63), None, sub_len)
            elif csi_data_len == 256:
                colors = generate_subcarrier_colors((0,32), (32,63), (64,128), sub_len)
            elif csi_data_len == 512:
                colors = generate_subcarrier_colors((0,63), (64,127), (128,256), sub_len)
            elif csi_data_len == 384:
                colors = generate_subcarrier_colors((0,63), (64,127), (128,192), sub_len)
            elif 0 < csi_data_len <= 612:
                colors = generate_subcarrier_colors((0,raw_len//2), (raw_len//2+1,raw_len-1), None, sub_len)
            else:
                colors = [(100,100,100)] * sub_len
            
            print(len(colors))
            print(colors)
            with data_lock:
                current_colors = colors
                current_data_len = sub_len

        # 加锁更新全局历史数组
        with data_lock:
            csi_amplitude_history[:-1] = csi_amplitude_history[1:]
            csi_amplitude_history[-1, :sub_len] = new_amp_row
            
            agc_history[:-1] = agc_history[1:]
            agc_history[-1] = agc_gain
            
            fft_history[:-1] = fft_history[1:]
            fft_history[-1] = fft_gain

    ser.close()

class SubThread(QThread):
    def __init__(self, serial_port, save_file_name, log_file_name):
        super().__init__()
        self.serial_port = serial_port
        self.save_file_name = save_file_name
        self.log_file_name = log_file_name
        self.csv_writer = None
        self.log_file_fd = None

    def run(self):
        save_file_fd = open(self.save_file_name, 'w', newline='')
        self.log_file_fd = open(self.log_file_name, 'w')
        self.csv_writer = csv.writer(save_file_fd)
        # 使用新的表头写入 CSV
        self.csv_writer.writerow(DATA_COLUMNS_NAMES)
        
        csi_data_read_parse(self.serial_port, self.csv_writer, self.log_file_fd)
        
        save_file_fd.close()
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