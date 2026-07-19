
import argparse
import math
import sys
import serial
import threading
import queue
import numpy as np
from abc import ABC, abstractmethod
import re
from collections import deque

import pyqtgraph as pg
from pyqtgraph.Qt import QtWidgets, QtCore


class IFrontParser(ABC):
    @abstractmethod
    def parse(self, line: str) -> list[int]:
        pass


class DefaultFrontParser(IFrontParser):
    """解析数据格式: xxxxxx data:[Q1,I1,Q2,I2,...] 其中Q(偶数索引)是虚部，I(奇数索引)是实部"""
    
    def parse(self, line: str) -> list[int]:
        # 匹配 data:[...] 格式
        match = re.search(r'data:\s*\[([^\]]+)\]', line)
        if not match:
            return None
        
        data_str = match.group(1)
        data_list = []
        for x in data_str.split(','):
            x = x.strip()
            try:
                data_list.append(int(x))
            except ValueError:
                continue
        
        # 检查数据长度是否为偶数（QI对）
        if len(data_list) % 2 != 0:
            print(f"[警告] 数据长度不是偶数: {len(data_list)}")
            return None
            
        return data_list


def serial_thread_func(
    port: str, 
    baud: int, 
    q: queue.Queue, 
    front_parser: IFrontParser,
):
    with serial.Serial(port, baud, timeout=1) as ser:
        print(f"[串口] 打开 {port} @ {baud}bps")
        
        while True:
            try:
                line = ser.readline().decode('utf-8', errors='ignore')
                if not line:
                    continue
                
                csi_frame: list[int] = front_parser.parse(line)
                if csi_frame is None:
                    continue
                
                q.put(csi_frame)
                
            except Exception as e:
                print("[串口] 错误:", e)
                continue


class SimpleWindow:
    """滑动窗口，用于计算平均幅度"""
    
    def __init__(self, size: int):
        self.size = size
        self.items = deque(maxlen=size)
    
    def get_average(self) -> float:
        if len(self.items) == 0:
            return 0.0
        return sum(self.items) / len(self.items)
    
    def put(self, item: float) -> None:
        self.items.append(item)


class MainWindow(QtWidgets.QMainWindow):
    
    def __init__(self, data_queue: queue.Queue, max_channels: int = 3, history_length: int = 300):
        super().__init__()
        
        self.index = 0
        self.max_points = history_length
        self.max_channels = max_channels
        
        # 存储多个子载波的数据
        self.x_data = deque(maxlen=self.max_points)
        self.y_data_list = [deque(maxlen=self.max_points) for _ in range(max_channels)]
        
        self.data_queue = data_queue
        
        # 设置窗口
        self.setWindowTitle("WiFi CSI 幅度实时监测")
        self.resize(1200, 800)
        
        # 创建多个绘图区域
        self.plot_widgets = []
        self.curves = []
        
        layout = QtWidgets.QVBoxLayout()
        
        for i in range(max_channels):
            plot_widget = pg.PlotWidget()
            plot_widget.setTitle(f"子载波 {i+1} 幅度")
            plot_widget.setLabel('left', '幅度')
            plot_widget.setLabel('bottom', '时间 (采样点)')
            plot_widget.setBackground('w')
            
            # 使用不同颜色
            colors = ['r', 'g', 'b', 'c', 'm', 'y']
            curve = plot_widget.plot(pen=colors[i % len(colors)], pen_width=2)
            
            self.plot_widgets.append(plot_widget)
            self.curves.append(curve)
            layout.addWidget(plot_widget)
        
        container = QtWidgets.QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        
        # 定时器更新绘图
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_plot)
        self.timer.start(50)  # 20 FPS
        
        # 滑动窗口用于平滑显示
        self.slide_windows = [SimpleWindow(size=10) for _ in range(max_channels)]
        
    def calculate_magnitude(self, csi_frame: list) -> list[float]:
        """
        计算每个子载波的幅度
        数据格式: [Q1, I1, Q2, I2, Q3, I3, ...]
        
        参数:
            csi_frame: 包含QI对的数据列表
            
        返回:
            每个子载波的幅度值列表
        """
        magnitudes = []
        
        # 每两个数据为一组 (虚部, 实部)
        for i in range(0, len(csi_frame), 2):
            if i + 1 < len(csi_frame):
                Q = csi_frame[i]      # 虚部
                I = csi_frame[i + 1]  # 实部
                
                # 计算幅度: sqrt(I^2 + Q^2)
                magnitude = math.sqrt(I*I + Q*Q)
                magnitudes.append(magnitude)
        
        # 确保返回指定数量的子载波
        while len(magnitudes) < self.max_channels:
            magnitudes.append(0.0)
            
        return magnitudes[:self.max_channels]
    
    def update_plot(self):
        """更新所有绘图"""
        # 一次性处理队列中的所有数据
        while not self.data_queue.empty():
            csi_frame = self.data_queue.get()
            magnitudes = self.calculate_magnitude(csi_frame)
            
            self.index += 1
            
            # 更新x轴数据
            self.x_data.append(self.index)
            
            # 更新每个子载波的y轴数据和平滑窗口
            for i in range(self.max_channels):
                if i < len(magnitudes):
                    # 应用滑动平均平滑
                    smoothed_value = self.slide_windows[i].get_average()
                    if smoothed_value > 0:
                        # 如果有历史数据，使用加权平均
                        alpha = 0.7  # 平滑因子
                        final_value = alpha * magnitudes[i] + (1 - alpha) * smoothed_value
                    else:
                        final_value = magnitudes[i]
                    
                    self.slide_windows[i].put(final_value)
                    self.y_data_list[i].append(final_value)
                else:
                    self.y_data_list[i].append(0)
        
        # 更新每个子载波的图表
        if len(self.x_data) > 0:
            # 转换为相对时间
            t0 = self.x_data[0]
            x = [i - t0 for i in self.x_data]
            
            for i in range(self.max_channels):
                if len(self.y_data_list[i]) > 0:
                    # 只显示最后max_points个点
                    y_data = list(self.y_data_list[i])
                    if len(x) > len(y_data):
                        x_display = x[-len(y_data):]
                    else:
                        x_display = x[:len(y_data)]
                    
                    self.curves[i].setData(x_display, y_data)
                    
                    # 自动调整y轴范围
                    if len(y_data) > 0:
                        max_y = max(y_data)
                        min_y = min(y_data)
                        if max_y > min_y:
                            self.plot_widgets[i].setYRange(min_y - 0.1*(max_y-min_y), 
                                                           max_y + 0.1*(max_y-min_y))


def parse_args():
    parser = argparse.ArgumentParser(description="WiFi CSI 幅度可视化")
    parser.add_argument("--port", default="COM3", help="串口号")
    parser.add_argument("--baud", type=int, default=921600, help="波特率")
    parser.add_argument("--max-channels", type=int, default=3, help="最大子载波数量")
    parser.add_argument("--max-history", type=int, default=300, help="最大历史数据点数")
    return parser.parse_args()


if __name__ == "__main__":
    # 创建队列和解析器
    data_queue = queue.Queue(maxsize=1000)
    parser = DefaultFrontParser()
    args = parse_args()
    
    # 启动串口接收线程
    thread = threading.Thread(
        target=serial_thread_func,
        args=(args.port, args.baud, data_queue, parser),
        daemon=True
    )
    thread.start()
    
    # 启动GUI
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow(data_queue, args.max_channels, args.max_history)
    win.show()
    sys.exit(app.exec())