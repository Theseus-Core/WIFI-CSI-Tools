package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"math"
	"regexp"
	"strconv"
	"strings"
	"sync"

	"github.com/tarm/serial"
	"github.com/wailsapp/wails/v2/pkg/runtime"
)

type DataPacket struct {
	Data      []int16   `json:"data"`
	Amplitude []float32 `json:"amplitude"`
	Phase     []float32 `json:"phase"`
}

type App struct {
	ctx        context.Context
	portStream io.ReadWriteCloser
	mu         sync.Mutex // 保证串口操作安全
}

func NewApp() *App { return &App{} }

func (a *App) startup(ctx context.Context) { a.ctx = ctx }

// OpenSerialPort 打开串口
func (a *App) OpenSerialPort(portName string, baudRate int) string {
	a.mu.Lock()
	defer a.mu.Unlock()

	if a.portStream != nil {
		return "串口已在运行中"
	}

	config := &serial.Config{Name: portName, Baud: baudRate}
	stream, err := serial.OpenPort(config)
	if err != nil {
		return fmt.Sprintf("连接失败: %v", err)
	}

	a.portStream = stream
	go a.readSerialLoop(stream)

	return fmt.Sprintf("成功连接至 %s", portName)
}

// CloseSerialPort 断开串口
func (a *App) CloseSerialPort() string {
	a.mu.Lock()
	defer a.mu.Unlock()

	if a.portStream != nil {
		a.portStream.Close()
		a.portStream = nil
		return "已断开连接"
	}
	return "未连接串口"
}

func (a *App) readSerialLoop(stream io.ReadWriteCloser) {
	// 结束后通知前端已断开
	defer func() {
		runtime.EventsEmit(a.ctx, "serial-closed", true)
	}()

	re := regexp.MustCompile(`data:\[(.*?)\]`)
	scanner := bufio.NewScanner(stream)
	buf := make([]byte, 128*1024)
	scanner.Buffer(buf, 128*1024)

	for scanner.Scan() {
		line := scanner.Text()
		
		match := re.FindStringSubmatch(line)
		if len(match) > 1 {
			intValues := a.parseInt16Array(match[1])
			amps, phases := a.calculateCSI(intValues)
			packet := DataPacket{
				Data:      intValues,
				Amplitude: amps,
				Phase:     phases,
			}
			runtime.EventsEmit(a.ctx, "csi-data", packet)
		}
	}
}

func (a *App) calculateCSI(data []int16) ([]float32, []float32) {
	count := len(data) / 2
	amps := make([]float32, 0, count)
	phases := make([]float32, 0, count)
	for i := 0; i < len(data)-1; i += 2 {
		imag, real := float64(data[i]), float64(data[i+1])
		amps = append(amps, float32(math.Sqrt(real*real+imag*imag)))
		phases = append(phases, float32(math.Atan2(imag, real)))
	}
	return amps, phases
}

func (a *App) parseInt16Array(input string) []int16 {
	strValues := strings.Split(input, ",")
	result := make([]int16, 0, len(strValues))
	for _, s := range strValues {
		trimmed := strings.TrimSpace(s)
		if val, err := strconv.ParseInt(trimmed, 10, 16); err == nil {
			result = append(result, int16(val))
		}
	}
	return result
}