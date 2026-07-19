#!/usr/bin/env python3
# -*-coding:utf-8-*-

import sys
import serial
import numpy as np
import threading
import argparse

# ----------------- 全局变量 -----------------
data_lock = threading.Lock()
CSI_DATA_COLUMNS = 490  # 最大子载波数量
current_data_len = 0
csi_complex_latest = np.zeros(CSI_DATA_COLUMNS, dtype=np.complex64)

# ----------------- CSI 解析函数 -----------------
def parse_csi_line(line: str):
    """
    解析一行 CSI 数据: index:X len:Y data:[...]
    返回: pkt_index, csi_data_len, csi_raw_data
    """
    line = line.strip()
    if "data:[" not in line:
        return None
    
    try:
        prefix, data_str = line.split("data:[")
        data_str = data_str.replace("]", "").strip()
        prefix_parts = prefix.strip().split()
        pkt_index = int(prefix_parts[0].split(":")[1])
        csi_data_len = int(prefix_parts[1].split(":")[1])
        csi_raw_data = [int(x) for x in data_str.split(',') if x.strip()]
        if len(csi_raw_data) != csi_data_len:
            print(f"警告: 帧 {pkt_index} 声明长度 {csi_data_len} 但实际收到 {len(csi_raw_data)}")
            return None
        return pkt_index, csi_data_len, csi_raw_data
    except Exception as e:
        print(f"解析错误: {line}\n异常: {e}")
        return None

# ----------------- CSI 处理函数 -----------------
def process_csi_data(pkt_index, csi_raw_data):
    """
    将 CSI 原始数据转成复数数组, 并检查幅值
    """
    raw_arr = np.array(csi_raw_data, dtype=np.float32)
    if len(raw_arr) % 2 != 0:
        print(f"警告: 帧 {pkt_index} 的原始数据长度 {len(raw_arr)} 为奇数, 跳过处理")
        return
    
    real_parts = raw_arr[1::2]  # 奇数索引是实部
    imag_parts = raw_arr[0::2]  # 偶数索引是虚部
    new_complex_row = real_parts + 1j * imag_parts

    # 检查幅值
    amp_row = np.abs(new_complex_row)
    zero_indices = np.where(amp_row == 0)[0]
    if zero_indices.size > 0:
        print(f"警告: 帧 {pkt_index} 有幅值为0的子载波 {zero_indices.tolist()}")
        print(f"原始数据长度: {len(csi_raw_data)}, 子载波数量: {len(new_complex_row)}")
        print(f"原始数据前20个值: {csi_raw_data[:20]} ...")

    # 更新最新复数数组（线程安全）
    with data_lock:
        global current_data_len, csi_complex_latest
        current_data_len = len(new_complex_row)
        csi_complex_latest[:current_data_len] = new_complex_row

# ----------------- 串口读取函数 -----------------
def read_csi_serial(port: str, baudrate=921600):
    try:
        ser = serial.Serial(port=port, baudrate=baudrate, bytesize=8, parity='N', stopbits=1)
        print(f"串口 {port} 打开成功")
    except Exception as e:
        print(f"打开串口 {port} 出错: {e}")
        return
    
    frame_count = 0
    while True:
        try:
            line_bytes = ser.readline()
            if not line_bytes:
                continue
            line = line_bytes.decode(errors='ignore').strip()
            parsed = parse_csi_line(line)
            if parsed is None:
                continue
            pkt_index, csi_data_len, csi_raw_data = parsed
            process_csi_data(pkt_index, csi_raw_data)
            frame_count += 1
            if frame_count % 50 == 0:
                print(f"已处理 {frame_count} 帧数据")
        except KeyboardInterrupt:
            print("检测到键盘中断, 程序退出")
            break
        except Exception as e:
            print(f"读取或处理数据时出错: {e}")
    
    ser.close()

# ----------------- 主程序 -----------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='读取串口 CSI 数据并排查异常幅值')
    parser.add_argument('-p', '--port', dest='port', action='store', required=True,
                        help='串口号')
    parser.add_argument('-s', '--store', dest='store_file', action='store', default='./csi_data.csv',
                        help='保存文件路径（本脚本不使用，仅保留参数兼容）')
    parser.add_argument('-l', '--log', dest='log_file', action='store', default='./csi_data_log.txt',
                        help='日志文件路径（本脚本不使用，仅保留参数兼容）')

    args = parser.parse_args()

    read_csi_serial(args.port)