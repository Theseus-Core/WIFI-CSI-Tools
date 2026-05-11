<script setup>
import { onMounted, onBeforeUnmount, reactive, ref } from 'vue';
import * as echarts from 'echarts';
import { EventsOn } from '../wailsjs/runtime/runtime';
import { OpenSerialPort, CloseSerialPort } from '../wailsjs/go/main/App';

const serialConfig = reactive({
  port: 'COM3',
  baud: 115200,
  isConnected: false,
  statusMsg: '等待连接'
});

const settings = reactive({
  step: 4, // 默认步长改为4（参考你的PyQt代码，降低渲染密度）
  windowLength: 50, // X轴显示的历史点数
  fps: 20 // 渲染帧率 (50ms刷新一次)
});

let ampChart, phaseChart;
let timestamps = [];
let ampSeriesMap = new Map();
let phaseSeriesMap = new Map();

// 【核心优化1】数据缓冲池：接收到的数据先扔到这里，不阻塞接收过程
let packetBuffer = []; 
let renderTimer = null;

// 提取公共配置项
const getCommonOption = (title) => ({
  title: { 
    text: title, 
    textStyle: { color: '#ccc', fontSize: 14 },
    left: 'center',
    top: 10
  },
  animation: false, // 必须关闭动画，否则高频更新极度卡顿
  tooltip: { trigger: 'axis' },
  xAxis: { 
    type: 'category', 
    data: [], 
    axisLine: { lineStyle: { color: '#555' } } 
  },
  yAxis: { 
    type: 'value', 
    scale: true,
    splitLine: { lineStyle: { color: '#222' } },
    axisLine: { lineStyle: { color: '#555' } }
  },
  grid: { top: 60, bottom: 40, left: 60, right: 30 }
});

// 连接操作
const handleConnect = async () => {
  serialConfig.statusMsg = "正在尝试连接...";
  const res = await OpenSerialPort(serialConfig.port, serialConfig.baud);
  if (res.includes("成功")) {
    serialConfig.isConnected = true;
    serialConfig.statusMsg = res;
  } else {
    serialConfig.statusMsg = res;
  }
};

// 断开操作
const handleDisconnect = async () => {
  await CloseSerialPort();
  serialConfig.isConnected = false;
  serialConfig.statusMsg = "已断开连接";
  clearAllData();
};

const clearAllData = () => {
  timestamps = [];
  ampSeriesMap.clear();
  phaseSeriesMap.clear();
  packetBuffer = []; // 清空缓冲池

  if (ampChart && phaseChart) {
    ampChart.clear(); 
    phaseChart.clear();
    ampChart.setOption(getCommonOption('CSI 幅度 (Amplitude)'));
    phaseChart.setOption(getCommonOption('CSI 相位 (Phase)'));
  }
};

onMounted(() => {
  initCharts();
  
  // 监听后端数据：不再直接渲染，而是极速塞入缓冲池
  EventsOn("csi-data", (packet) => {
    if (!serialConfig.isConnected) return;
    packetBuffer.push(packet);
  });

  EventsOn("serial-closed", () => {
    serialConfig.isConnected = false;
    serialConfig.statusMsg = "串口已关闭";
    clearAllData();
  });

  // 【核心优化2】定时渲染引擎 (参考 PyQt 的 timer.start)
  // 将计算和渲染彻底从接收事件中剥离
  renderTimer = setInterval(processBufferAndRender, 1000 / settings.fps);
});

onBeforeUnmount(() => {
  if (renderTimer) clearInterval(renderTimer);
  window.removeEventListener('resize', resizeCharts);
});

const resizeCharts = () => {
  if (ampChart) ampChart.resize();
  if (phaseChart) phaseChart.resize();
};

const initCharts = () => {
  const opts = { renderer: 'canvas' };
  ampChart = echarts.init(document.getElementById('amp-chart'), null, opts);
  phaseChart = echarts.init(document.getElementById('phase-chart'), null, opts);
  
  ampChart.setOption(getCommonOption('CSI 幅度 (Amplitude)'));
  phaseChart.setOption(getCommonOption('CSI 相位 (Phase)'));
  
  window.addEventListener('resize', resizeCharts);
};

// 【核心优化3】批量处理数据并渲染
const processBufferAndRender = () => {
  if (packetBuffer.length === 0 || !serialConfig.isConnected) return;

  // 1. 批量消费缓冲池中的数据
  const packetsToProcess = [...packetBuffer];
  packetBuffer = []; // 立即清空，让接收端继续塞数据

  for (const packet of packetsToProcess) {
    const now = new Date().toLocaleTimeString().split(' ')[0];
    timestamps.push(now);
    if (timestamps.length > settings.windowLength) timestamps.shift();

    updateMapData(ampSeriesMap, packet.amplitude);
    updateMapData(phaseSeriesMap, packet.phase);
  }

  // 2. 数据处理完毕，执行一次统一渲染
  refreshCharts();
};

const updateMapData = (map, data) => {
  for (let i = 0; i < data.length; i++) {
    if (!map.has(i)) map.set(i, []);
    const h = map.get(i);
    h.push(data[i]);
    // 限制历史窗口长度
    if (h.length > settings.windowLength) {
      // 一次性删掉多余的，防止累积
      h.splice(0, h.length - settings.windowLength);
    }
  }
};

const refreshCharts = () => {
  const getSeries = (map) => {
    const res = [];
    map.forEach((data, i) => {
      // 【核心优化4】降采样渲染：只有符合步长的子载波才生成线段 (参考 PyQt 的 DISPLAY_STEP)
      if (i % settings.step === 0) {
        res.push({
          name: `SC${i}`, 
          type: 'line', 
          symbol: 'none',
          sampling: 'lttb', // 开启大据量降采样算法
          lineStyle: { width: 1.5 },
          data: data // 直接引用数组，不要用 [...data] 展开，减少内存开销
        });
      }
    });
    return res;
  };
  
  // setOption 默认合并，传递整个覆盖对象即可
  ampChart.setOption({ xAxis: { data: timestamps }, series: getSeries(ampSeriesMap) });
  phaseChart.setOption({ xAxis: { data: timestamps }, series: getSeries(phaseSeriesMap) });
};
</script>

<template>
  <div class="container">
    <div class="header">
      <div class="logo">📡 WiFi-CSI Analyzer</div>
      <div class="settings-bar">
        <div class="section">
          <input v-model="serialConfig.port" :disabled="serialConfig.isConnected" placeholder="端口(COM/tty)"/>
          <select v-model.number="serialConfig.baud" :disabled="serialConfig.isConnected">
            <option :value="115200">115200</option>
            <option :value="921600">921600</option>
            <option :value="2000000">2000000</option>
          </select>
          <button v-if="!serialConfig.isConnected" @click="handleConnect" class="btn-open">打开串口</button>
          <button v-else @click="handleDisconnect" class="btn-close">断开连接</button>
        </div>

        <div class="divider"></div>

        <div class="section">
          <label>显示步长(Step):</label>
          <input type="number" v-model.number="settings.step" :disabled="serialConfig.isConnected" min="1" max="128" title="值越大，显示的线条越少，性能越高" />
          <label>历史长度:</label>
          <input type="number" v-model.number="settings.windowLength" :disabled="serialConfig.isConnected" min="10" max="500" />
        </div>

        <div class="status" :class="{ active: serialConfig.isConnected }">{{ serialConfig.statusMsg }}</div>
      </div>
    </div>

    <div class="chart-area">
      <div v-if="!serialConfig.isConnected" class="empty-overlay">
        请选择串口并点击“打开串口”开始采集
      </div>
      <div id="amp-chart" class="chart"></div>
      <div id="phase-chart" class="chart"></div>
    </div>
  </div>
</template>

<style scoped>
/* 样式与原版完全保持一致 */
.container { display: flex; flex-direction: column; height: 100vh; background: #0f0f0f; color: #eee; overflow: hidden; }
.header { background: #1a1a1a; border-bottom: 1px solid #333; padding: 5px 20px; }
.logo { font-size: 12px; color: #888; margin-bottom: 5px; }
.settings-bar { display: flex; align-items: center; gap: 15px; padding-bottom: 5px; }
.section { display: flex; align-items: center; gap: 8px; font-size: 13px; }
input, select { background: #262626; border: 1px solid #444; color: #fff; padding: 5px 8px; border-radius: 4px; width: 90px; }
input:disabled, select:disabled { background: #1a1a1a; color: #666; border-color: #333; cursor: not-allowed; }
button { padding: 6px 15px; border: none; border-radius: 4px; color: white; cursor: pointer; font-weight: bold; transition: background 0.2s; }
.btn-open { background: #007acc; }
.btn-open:hover { background: #0098ff; }
.btn-close { background: #c62828; }
.btn-close:hover { background: #ff3d00; }
.divider { width: 1px; height: 24px; background: #333; }
.status { margin-left: auto; font-size: 12px; color: #ff9800; background: rgba(255,152,0,0.1); padding: 4px 10px; border-radius: 12px; }
.status.active { color: #4caf50; background: rgba(76,175,80,0.1); }
.chart-area { flex: 1; display: flex; flex-direction: column; padding: 15px; gap: 15px; position: relative; }
.empty-overlay { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 10; background: rgba(0,0,0,0.6); padding: 20px 40px; border-radius: 8px; border: 1px dashed #444; color: #666; pointer-events: none; }
.chart { flex: 1; background: #161616; border-radius: 6px; border: 1px solid #222; min-height: 200px; }
</style>