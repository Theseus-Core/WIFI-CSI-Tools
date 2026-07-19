#!/usr/bin/env python3
# -*-coding:utf-8-*-

import sys
import os
import numpy as np
import argparse
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QSlider, QLabel, QFileDialog, QSpinBox)
from PyQt5.QtCore import Qt, QTimer
import pyqtgraph as pg

class CSIViewerWindow(QWidget):
    def __init__(self, file_path=None):
        super().__init__()
        self.setWindowTitle("WiFi CSI Data Player & Visualizer")
        self.resize(1280, 720)
        
        # 数据变量
        self.csi_complex = None
        self.csi_amplitude = None
        self.timestamps = None
        self.pkt_index = None
        self.total_frames = 0
        self.current_frame_idx = 0
        
        # 定时器（用于自动播放）
        self.play_timer = QTimer()
        self.play_timer.timeout.connect(self.next_frame)
        
        # 初始化界面
        self.init_ui()
        
        # 如果启动时传入了文件，直接加载
        if file_path and os.path.exists(file_path):
            self.load_npz_data(file_path)
        else:
            self.open_file_dialog()

    def init_ui(self):
        # 主布局
        main_layout = QVBoxLayout(self)
        
        # ====== 1. 图表显示区域 (左右并排) ======
        charts_layout = QHBoxLayout()
        
        # 左图：当前帧子载波幅度曲线
        self.plot_frame = pg.PlotWidget()
        self.plot_frame.setTitle("Current Frame Amplitude (Subcarriers)")
        self.plot_frame.setLabel('left', 'Amplitude')
        self.plot_frame.setLabel('bottom', 'Subcarrier Index')
        self.curve_frame = self.plot_frame.plot(pen=pg.mkPen('g', width=1.5))
        charts_layout.addWidget(self.plot_frame, stretch=1)
        
        # 右图：时空热力图 (瀑布图)
        self.plot_waterfall = pg.PlotWidget()
        self.plot_waterfall.setTitle("CSI Amplitude Waterfall (Time vs Subcarriers)")
        self.plot_waterfall.setLabel('left', 'Subcarrier Index')
        self.plot_waterfall.setLabel('bottom', 'Relative Frame Window')
        self.image_item = pg.ImageItem()
        self.plot_waterfall.addItem(self.image_item)
        
        # 为热力图添加漂亮的时变伪彩色 (Lookup Table)
        cmap = pg.colormap.get('viridis')
        self.image_item.setLookupTable(cmap.getLookupTable())
        charts_layout.addWidget(self.plot_waterfall, stretch=1)
        
        main_layout.addLayout(charts_layout, stretch=1)
        
        # ====== 2. 进度条区域 ======
        slider_layout = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(100)
        self.slider.sliderMoved.connect(self.slider_changed) # 拖拽时更新
        self.slider.sliderPressed.connect(self.pause)        # 拖拽时先暂停播放
        
        self.lbl_frame_info = QLabel("Frame: 0 / 0  (Time: 00:00:00.000)")
        self.lbl_frame_info.setMinimumWidth(250)
        
        slider_layout.addWidget(self.slider, stretch=1)
        slider_layout.addWidget(self.lbl_frame_info)
        main_layout.addLayout(slider_layout)
        
        # ====== 3. 控制面板区域 ======
        controls_layout = QHBoxLayout()
        
        self.btn_open = QPushButton("Open .npz File")
        self.btn_open.clicked.connect(self.open_file_dialog)
        controls_layout.addWidget(self.btn_open)
        
        self.btn_play = QPushButton("Play")
        self.btn_play.clicked.connect(self.toggle_play)
        controls_layout.addWidget(self.btn_play)
        
        controls_layout.addWidget(QLabel("FPS:"))
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(1, 100)
        self.spin_fps.setValue(20) # 默认每秒播放20帧
        self.spin_fps.valueChanged.connect(self.update_fps)
        controls_layout.addWidget(self.spin_fps)
        
        controls_layout.addStretch(1) # 弹簧右对齐
        main_layout.addLayout(controls_layout)

    def open_file_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select CSI .npz File", "data", "WiFi CSI Data (*.npz)")
        if file_path:
            self.load_npz_data(file_path)

    def load_npz_data(self, file_path):
        self.pause()
        print(f"[Loader] Loading {file_path} into memory...")
        
        try:
            with np.load(file_path, allow_pickle=True) as data:
                # 兼容处理你的 DataSaveThread 存储格式
                self.timestamps = data['timestamp']
                self.pkt_index = data['pkt_index']
                complex_data = data['csi_complex']
                
                # 处理由于子载波异常变长导致的 object 数组退化问题
                if complex_data.dtype == object:
                    print("[Warning] Irregular subcarrier lengths detected. Aligning data...")
                    min_len = min(len(row) for row in complex_data)
                    self.csi_complex = np.array([row[:min_len] for row in complex_data], dtype=np.complex64)
                else:
                    self.csi_complex = complex_data
                
                # 预先一次性计算幅度值，避免播放时重复计算导致卡顿
                self.csi_amplitude = np.abs(self.csi_complex)
                
            self.total_frames = self.csi_amplitude.shape[0]
            print(f"[Loader] Load success. Total frames: {self.total_frames}, Subcarriers: {self.csi_amplitude.shape[1]}")
            
            # 更新控制控件状态
            self.slider.setMaximum(self.total_frames - 1)
            self.current_frame_idx = 0
            self.slider.setValue(0)
            
            # 自适应右侧热力图的色彩对比度上限
            max_amp = float(np.percentile(self.csi_amplitude, 98)) # 取98分位数防噪点过亮
            self.image_item.setLevels([0, max_amp])
            
            self.update_display()
            
        except Exception as e:
            print(f"[Error] Failed to load npz file: {e}")
            self.lbl_frame_info.setText("Error loading file!")

    def update_display(self):
        if self.csi_amplitude is None or self.total_frames == 0:
            return
            
        idx = self.current_frame_idx
        
        # 1. 更新单个帧的曲线
        amp_row = self.csi_amplitude[idx]
        self.curve_frame.setData(amp_row)
        
        # 2. 更新右侧热力图 (动态展示当前帧前后共 200 帧的切片窗口)
        window_size = 200
        start_win = max(0, idx - window_size // 2)
        end_win = min(self.total_frames, start_win + window_size)
        
        # 截取局部的时空矩阵并转置以适配 pyqtgraph 的 ImageItem 坐标系 [Time, Subcarrier]
        waterfall_matrix = self.csi_amplitude[start_win:end_win].T
        self.image_item.setImage(waterfall_matrix)
        
        # 3. 更新文本标签和进度条位置
        ts = self.timestamps[idx]
        time_str = os.datetime = np.datetime64(int(ts*1000), 'ms').astype(str).split('T')[-1] # 提取时分秒
        pkt = self.pkt_index[idx]
        self.lbl_frame_info.setText(f"Frame: {idx+1}/{self.total_frames}  [Pkt: {pkt}]  Time: {time_str[:-3]}")
        
        # 阻止信号触发死循环
        self.slider.blockSignals(True)
        self.slider.setValue(idx)
        self.slider.blockSignals(False)

    def slider_changed(self, value):
        self.current_frame_idx = value
        self.update_display()

    def toggle_play(self):
        if self.play_timer.isActive():
            self.pause()
        else:
            if self.csi_amplitude is not None:
                fps = self.spin_fps.value()
                self.play_timer.start(int(1000 / fps))
                self.btn_play.setText("Pause")
                self.btn_play.setStyleSheet("background-color: #ffaa00; font-weight: bold;")

    def pause(self):
        self.play_timer.stop()
        self.btn_play.setText("Play")
        self.btn_play.setStyleSheet("")

    def next_frame(self):
        if self.current_frame_idx < self.total_frames - 1:
            self.current_frame_idx += 1
            self.update_display()
        else:
            self.pause() # 放完了自动暂停

    def update_fps(self, fps):
        if self.play_timer.isActive():
            self.play_timer.start(int(1000 / fps))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='WiFi CSI Data Player')
    parser.add_argument('-f', '--file', dest='file', action='store', help='Path to the .npz file')
    args = parser.parse_args()

    app = QApplication(sys.argv)
    viewer = CSIViewerWindow(file_path=args.file)
    viewer.show()
    sys.exit(app.exec_())