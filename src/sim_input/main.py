import serial
import time
import argparse
import random
import sys

class CSITester:
    def __init__(self, port, baud):
        try:
            self.ser = serial.Serial(port, baud, timeout=1)
            print(f"[*] 成功打开串口 {port}，准备发送测试数据...")
        except Exception as e:
            print(f"[!] 无法打开串口: {e}")
            sys.exit(1)

    def generate_csi_data(self, length=20, min_val=-50, max_val=50):
        """生成模拟的 CSI 数组字符串"""
        data_array = [random.randint(min_val, max_val) for _ in range(length)]
        return f"type:CSI,rssi:-65,data:{data_array}\n"

    def test_case_fixed(self):
        """用例1：发送固定格式数据"""
        print("[+] 执行用例：固定数据")
        payload = "status:ok,data:[10,20,30,40,50]\n"
        self.ser.write(payload.encode('utf-8'))

    def test_case_long_array(self):
        """用例2：发送超长数组（测试 Go 端的缓冲区）"""
        print("[+] 执行用例：长数组数据 (128个元素)")
        payload = self.generate_csi_data(length=128)
        self.ser.write(payload.encode('utf-8'))

    def test_case_malformed(self):
        """用例3：发送格式错误的数据（测试解析鲁棒性）"""
        print("[+] 执行用例：异常格式测试")
        payloads = [
            "data:[1,2,abc,4]\n",  # 包含非数字
            "wrong_prefix:[1,2,3]\n", # 缺少 data: 关键字
            "data:[1,2,3\n" # 括号不闭合
        ]
        for p in payloads:
            self.ser.write(p.encode('utf-8'))
            time.sleep(0.1)

    def run_realtime_sim(self, interval=0.1):
        """实时模拟模式：持续发送随机波形"""
        print(f"[*] 进入实时模拟模式 (间隔: {interval}s)，按 Ctrl+C 停止...")
        try:
            count = 0
            while True:
                payload = self.generate_csi_data(length=64)
                self.ser.write(payload.encode('utf-8'))
                count += 1
                if count % 10 == 0:
                    print(f"已发送 {count} 条数据...", end='\r')
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[*] 模拟已停止")

def main():
    parser = argparse.ArgumentParser(description="WIFI-CSI 串口测试工具")
    parser.add_argument("-p", "--port", default="COM31", help="串口号 (默认: COM30)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="波特率 (默认: 115200)")
    parser.add_argument("-m", "--mode", choices=['once', 'sim'], default='once', 
                        help="模式: once (单次全用例测试), sim (实时持续模拟)")
    parser.add_argument("-i", "--interval", type=float, default=0.05, help="模拟模式下的发送间隔 (秒)")

    args = parser.parse_args()

    tester = CSITester(args.port, args.baud)

    if args.mode == 'once':
        tester.test_case_fixed()
        time.sleep(0.5)
        tester.test_case_long_array()
        time.sleep(0.5)
        tester.test_case_malformed()
        print("[*] 单次测试完成")
    else:
        tester.run_realtime_sim(args.interval)

if __name__ == "__main__":
    main()