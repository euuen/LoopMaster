# LoopMaster

非侵入式 MCU 变量示波器 —— 通过 SWD 实时采样 ARM 微控制器内存变量，支持波形显示和结构体成员展开。

## 功能

- **静态分析**：解析 ELF/AXF 固件，提取符号表、全局变量（含 DWARF 类型信息）、结构体/联合体内存布局
- **实时示波器**：通过 CMSIS-DAP/DAPLink 探针非侵入式读取变量，实时绘制波形
- **结构体展开**：支持递归展开结构体成员，可单独勾选每个成员进行监控
- **采样率预设**：1/10/50/100/200/500/1000 Hz 一键切换，支持自定义

## 安装

```bash
cd D:\PythonProjects\LoopMaster
.venv\Scripts\activate
pip install -r requirements.txt
```

requirements:

```
rich>=3.0
PyYAML>=6.0
openpyxl>=3.0
pyocd>=0.36
pyside6>=6.6
pyqtgraph>=0.13
numpy>=1.24
```

## 使用方法

### 启动 GUI

```bash
# 直接启动（无需参数，启动后导入文件）
python main.py scope

# 带 ELF 文件启动
python main.py scope input_files/Gimbal_G0B1.elf

# 带 CMSIS-Pack 和指定目标
python main.py scope input_files/Gimbal_G0B1.elf --pack path/to/STM32G0xx.pack --target stm32g0b1retx
```

### GUI 操作流程

1. **导入固件**：点击左侧面板顶部的 `Import ELF/AXF` 按钮，选择 .elf/.axf 文件
2. **连接探针**：Probe → Scan Probes 扫描，然后 Connect 连接目标芯片
3. **选择变量**：在左侧树中勾选要监控的变量；展开结构体可勾选单个成员
4. **设置采样率**：点击预设按钮（1/10/50/100/200/500/1k Hz）或使用微调框自定义
5. **开始/停止**：点击绿色 START 按钮开始采样，红色 STOP 停止
6. **导出**：Export CSV 导出时间序列数据

### 状态指示

- 状态栏 ● 红色 = 探针未连接，● 绿色 = 已连接
- 状态栏右侧显示实际采样率

### CLI 命令

```bash
# 查看 ELF 基本信息
python main.py info input_files/Gimbal_G0B1.elf

# 列出符号表
python main.py symbols input_files/Gimbal_G0B1.elf
python main.py symbols input_files/Gimbal_G0B1.elf --internal       # 含内部符号
python main.py symbols input_files/Gimbal_G0B1.elf -o csv --output-file output/symbols.csv

# 列出全局变量（含 DWARF 类型信息）
python main.py variables input_files/Gimbal_G0B1.elf
python main.py variables input_files/Gimbal_G0B1.elf --filter gimbal
python main.py variables input_files/Gimbal_G0B1.elf --sort name
python main.py variables input_files/Gimbal_G0B1.elf -o json --output-file output/vars.json
python main.py variables input_files/Gimbal_G0B1.elf -o excel --output-file output/vars.xlsx

# 查看结构体内存布局
python main.py struct input_files/Gimbal_G0B1.elf --list            # 列出所有结构体
python main.py struct input_files/Gimbal_G0B1.elf -n __DMA_HandleTypeDef
python main.py struct input_files/Gimbal_G0B1.elf -n __DMA_HandleTypeDef -o json
```

## 硬件要求

- **调试探针**：CMSIS-DAP / DAPLink（自动检测）
- **目标芯片**：ARM Cortex-M 系列（SWD 连接）
- **接线**：SWCLK + SWDIO + GND

## 配置

`config/settings.yaml`：

```yaml
scope:
  swd_freq: 4000000      # SWD 时钟频率 (Hz)
  sample_rate: 100        # 默认采样率
  buffer_seconds: 10      # 默认缓冲区时长（秒）

parser:
  dwarf:
    max_type_depth: 10    # 类型展开最大深度

  output:
    default_format: table # table/csv/json/excel
    csv_delimiter: ","
    excel_sheet_name: Variables
```

## 文件夹结构

```
LoopMaster/
  main.py                  # 入口
  config/settings.yaml     # 配置
  input_files/             # 固件文件 (.elf, .map)
  output/                  # 输出文件
  src/
    core/                  # 核心：数据模型、采样引擎、SWD 后端
    parser/                # ELF/DWARF 解析、变量清单、结构体布局
    ui/                    # CLI + GUI
    utils/                 # 导出器 (CSV/JSON/Excel/Rich)
```
