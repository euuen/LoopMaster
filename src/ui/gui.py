"""LoopMaster Scope -- non-intrusive MCU variable oscilloscope."""

import sys
import csv
import json
import logging
import subprocess
import os
import time
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPushButton, QSpinBox, QLabel, QFileDialog, QStatusBar, QMenuBar,
    QMenu, QMessageBox, QApplication, QLineEdit, QHeaderView,
    QTreeWidget, QTreeWidgetItem, QFrame, QTabWidget, QListWidget,
    QListWidgetItem, QAbstractItemView, QTableWidget, QTableWidgetItem,
    QComboBox,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont, QPalette, QColor
from PySide6.QtWidgets import QGraphicsProxyWidget

import numpy as np
import pyqtgraph as pg

from src.parser.readelf import parse_symbol_table, parse_debug_info
from src.parser.variable_inventory import VariableInventory
from src.parser.elf_parser import ELFParser
from src.parser.map_parser import parse_map_file
from src.core.collector import DataCollector
from src.core.mem_backend import SWDBackend, _extract_val
from src.core.models import (
    Variable, TypeInfo, BaseType, StructType, ArrayType,
    PointerType, EnumType, TypedefType, FuncType,
)

# ---- Constants ----

COLORS = [
    "#00ff88", "#ff4488", "#44aaff", "#ffaa00", "#aa44ff",
    "#00ccff", "#ff6600", "#66ff44", "#ff44aa", "#44ffcc",
]

PRESET_FRAME_RATES = [12, 24, 30, 60, 120]
FRAME_RATE_DEFAULT = 60
TIME_WINDOW_DEFAULT = 10
BUFFER_SECONDS = 300
MAX_STRUCT_DEPTH = 6

ROLE_PATH = Qt.UserRole
ROLE_ADDR = Qt.UserRole + 1
ROLE_TYPE = Qt.UserRole + 2

logger = logging.getLogger("loopmaster")


def setup_logging(log_path: str = "loopmaster.log"):
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)-7s] %(message)s',
        datefmt='%H:%M:%S',
    )
    # 文件输出 — 完整日志
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # 控制台输出 — INFO 以上
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.info("LoopMaster 启动")


# ---- Helpers ----

def resolve_type(ti: Optional[TypeInfo]) -> Optional[TypeInfo]:
    while isinstance(ti, TypedefType):
        ti = ti.underlying_type
    return ti


def format_type(ti: Optional[TypeInfo]) -> str:
    if ti is None:
        return "?"
    if isinstance(ti, BaseType):
        return ti.name
    if isinstance(ti, StructType):
        prefix = "union " if ti.is_union else "struct "
        name = ti.name if ti.name else "<anonymous>"
        return f"{prefix}{name}"
    if isinstance(ti, ArrayType):
        elem = format_type(ti.element_type)
        return f"{elem}[{ti.count}]"
    if isinstance(ti, PointerType):
        pointed = format_type(ti.pointed_type) if ti.pointed_type else "void"
        return f"{pointed}*"
    if isinstance(ti, EnumType):
        return f"enum {ti.name}"
    if isinstance(ti, TypedefType):
        return ti.name
    if isinstance(ti, FuncType):
        return "func"
    return "?"


# ---- Main Window ----

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LoopMaster Scope — MCU Variable Oscilloscope")
        self.resize(1500, 860)
        self.setMinimumSize(1100, 600)

        self._elf_path: Optional[Path] = None
        self._variables: list[Variable] = []
        self._monitored: set[str] = set()
        self._monitor_list: list[tuple[str, int, object]] = []
        self._registry: dict[str, tuple[int, TypeInfo]] = {}

        self._backend = SWDBackend()
        self._collector = DataCollector()
        self._collector.set_backend(self._backend)
        self._unlimited_mode = False
        self._probe_list: list[dict] = []
        self._pack_path: Optional[Path] = None
        self._config_path = Path("loopmaster.json")

        self._plot_curves: dict[str, pg.PlotDataItem] = {}

        # Timers — 必须在 _setup_ui 之前创建，因为 setValue 会触发 valueChanged 信号
        self._plot_timer = QTimer()
        self._plot_timer.timeout.connect(self._update_plot)

        self._sample_timer = QTimer()
        self._sample_timer.setTimerType(Qt.PreciseTimer)
        self._sample_timer.timeout.connect(self._on_sample_tick)

        self._idle_timer = QTimer()
        self._idle_timer.timeout.connect(self._idle_read)

        self._setup_menu()
        self._setup_ui()
        self._setup_statusbar()

        self._idle_timer.start(250)  # 空闲读取保持 4Hz 固定

        # 自动加载上次配置
        cfg = self._load_config()
        if cfg:
            elf = cfg.get("elf_path", "")
            if elf and Path(elf).exists():
                self._elf_path = Path(elf)
                self._load_variables()
                # 恢复设置
                if "sample_rate" in cfg:
                    self._rate_spin.setValue(cfg["sample_rate"])
                if "frame_rate" in cfg:
                    self._frame_rate_spin.setValue(cfg["frame_rate"])
                if "swd_freq_index" in cfg:
                    idx = cfg["swd_freq_index"]
                    if 0 <= idx < self._swd_freq_combo.count():
                        self._swd_freq_combo.setCurrentIndex(idx)
                if "connect_mode_index" in cfg:
                    idx = cfg["connect_mode_index"]
                    if 0 <= idx < self._mode_combo.count():
                        self._mode_combo.setCurrentIndex(idx)
                if "y_auto" in cfg:
                    self._y_auto_btn.setChecked(cfg["y_auto"])
                self.setWindowTitle(f"LoopMaster Scope — {Path(elf).name}")

    # ================================================================
    #  Menu
    # ================================================================

    def _setup_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("文件(&F)")
        act_elf = QAction("导入 ELF/AXF...", self)
        act_elf.triggered.connect(self._on_import_elf)
        file_menu.addAction(act_elf)

        act_pack = QAction("导入 CMSIS-Pack...", self)
        act_pack.triggered.connect(self._on_import_pack)
        file_menu.addAction(act_pack)

        file_menu.addSeparator()
        act_log = QAction("查看日志", self)
        act_log.triggered.connect(self._on_view_log)
        file_menu.addAction(act_log)

        act_exit = QAction("退出", self)
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        probe_menu = mb.addMenu("探针(&P)")
        act_scan = QAction("扫描探针", self)
        act_scan.triggered.connect(self._on_scan_probes_ui)
        probe_menu.addAction(act_scan)

        act_connect = QAction("连接", self)
        act_connect.triggered.connect(self._on_connect_ui)
        probe_menu.addAction(act_connect)

        act_disconnect = QAction("断开", self)
        act_disconnect.triggered.connect(self._on_disconnect_ui)
        probe_menu.addAction(act_disconnect)

    # ================================================================
    #  Status Bar
    # ================================================================

    def _setup_statusbar(self):
        self._sb = QStatusBar()
        self.setStatusBar(self._sb)

        self._led = QLabel("●")
        self._led.setStyleSheet("color: #e04040; font-size: 14px;")
        self._sb.addWidget(self._led)

        self._sb_label = QLabel("  探针: 未连接  |  目标: --")
        self._sb.addWidget(self._sb_label)

        self._sb_rate = QLabel("  |  采样率: -- Hz")
        self._sb_rate.setStyleSheet("font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 10pt;")
        self._sb.addPermanentWidget(self._sb_rate)

    # ================================================================
    #  Connection Bar
    # ================================================================

    def _setup_connection_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("connectionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.setSpacing(8)

        # 探头图标
        icon_label = QLabel("🔌")
        icon_label.setStyleSheet("font-size: 16px; padding: 0px 4px;")
        layout.addWidget(icon_label)

        # 探针选择器
        self._probe_combo = QComboBox()
        self._probe_combo.setMinimumWidth(220)
        self._probe_combo.setPlaceholderText("点击扫描发现探针...")
        layout.addWidget(self._probe_combo)

        # 扫描按钮
        self._btn_scan = QPushButton("🔄 扫描")
        self._btn_scan.setObjectName("scanBtn")
        self._btn_scan.setFixedHeight(30)
        self._btn_scan.clicked.connect(self._on_scan_probes_ui)
        layout.addWidget(self._btn_scan)

        # 分隔线
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.VLine)
        sep1.setObjectName("connSeparator")
        sep1.setFixedWidth(1)
        layout.addWidget(sep1)

        # 连接模式选择
        layout.addWidget(QLabel("模式:"))
        self._mode_combo = QComboBox()
        self._mode_combo.setFixedWidth(150)
        self._mode_combo.addItem("附加启动")
        self._mode_combo.addItem("复位启动")
        layout.addWidget(self._mode_combo)

        # 分隔线
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setObjectName("connSeparator")
        sep2.setFixedWidth(1)
        layout.addWidget(sep2)

        # SWD 速率
        layout.addWidget(QLabel("SWD:"))
        self._swd_freq_combo = QComboBox()
        self._swd_freq_combo.setFixedWidth(90)
        self._swd_freq_combo.addItems(["1 MHz", "4 MHz", "10 MHz", "20 MHz", "40 MHz"])
        self._swd_freq_combo.setCurrentIndex(1)
        layout.addWidget(self._swd_freq_combo)

        # 分隔线
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.VLine)
        sep3.setObjectName("connSeparator")
        sep3.setFixedWidth(1)
        layout.addWidget(sep3)

        # 连接/断开按钮
        self._btn_connect = QPushButton("连接")
        self._btn_connect.setObjectName("connectBtn")
        self._btn_connect.setFixedHeight(30)
        self._btn_connect.clicked.connect(self._on_connect_ui)
        layout.addWidget(self._btn_connect)

        # 状态指示
        self._conn_indicator = QLabel("●")
        self._conn_indicator.setStyleSheet("color: #e04040; font-size: 16px; padding: 0px 4px;")
        layout.addWidget(self._conn_indicator)

        self._conn_label = QLabel("未连接")
        self._conn_label.setStyleSheet("color: #8080a0; font-size: 9pt;")
        layout.addWidget(self._conn_label)

        # 详细状态
        layout.addStretch()
        self._conn_info = QLabel("")
        self._conn_info.setStyleSheet("color: #606080; font-size: 9pt;")
        layout.addWidget(self._conn_info)

        return bar

    def _update_conn_status(self, connected: bool):
        if connected:
            self._conn_indicator.setStyleSheet("color: #40e060; font-size: 16px; padding: 0px 4px;")
            self._conn_label.setText("已连接")
            self._conn_label.setStyleSheet("color: #40e060; font-weight: bold; font-size: 9pt;")
            self._btn_connect.setText("断开")
            self._btn_connect.setObjectName("disconnectBtn")
            self._conn_info.setText(
                f"目标: {self._backend.target_name}  |  "
                f"SWD: ~{self._backend.swd_freq_khz} kHz"
            )
            self._probe_combo.setEnabled(False)
            self._btn_scan.setEnabled(False)
            self._mode_combo.setEnabled(False)
            self._swd_freq_combo.setEnabled(False)
        else:
            self._conn_indicator.setStyleSheet("color: #e04040; font-size: 16px; padding: 0px 4px;")
            self._conn_label.setText("未连接")
            self._conn_label.setStyleSheet("color: #8080a0; font-size: 9pt;")
            self._btn_connect.setText("连接")
            self._btn_connect.setObjectName("connectBtn")
            self._conn_info.setText("")
            self._probe_combo.setEnabled(True)
            self._btn_scan.setEnabled(True)
            self._mode_combo.setEnabled(True)
            self._swd_freq_combo.setEnabled(True)
        # 强制样式刷新
        self._btn_connect.style().unpolish(self._btn_connect)
        self._btn_connect.style().polish(self._btn_connect)

    # ================================================================
    #  Main UI
    # ================================================================

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 连接栏插在最顶部
        self._conn_bar_widget = self._setup_connection_bar()
        root.addWidget(self._conn_bar_widget)

        # Tab 区域
        tab_container = QWidget()
        tab_container_layout = QVBoxLayout(tab_container)
        tab_container_layout.setContentsMargins(8, 8, 8, 8)
        tab_container_layout.setSpacing(0)

        self._tabs = QTabWidget()
        tab_container_layout.addWidget(self._tabs)
        root.addWidget(tab_container, stretch=1)

        # ---- Tab 1: Variable Selection ----
        self._tab_vars = QWidget()
        self._tabs.addTab(self._tab_vars, "📋  变量选择")

        tab1_layout = QVBoxLayout(self._tab_vars)
        tab1_layout.setContentsMargins(8, 8, 8, 8)
        tab1_layout.setSpacing(8)

        # Import + search bar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self._btn_import_elf = QPushButton("📂  导入 ELF/AXF...")
        self._btn_import_elf.setObjectName("importBtn")
        self._btn_import_elf.clicked.connect(self._on_import_elf)
        top_bar.addWidget(self._btn_import_elf)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("按名称搜索变量...")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        top_bar.addWidget(self._filter_edit, stretch=1)

        btn_collapse = QPushButton("全部折叠")
        btn_collapse.setObjectName("smallBtn")
        btn_collapse.clicked.connect(lambda: self._tree.collapseAll())
        top_bar.addWidget(btn_collapse)

        tab1_layout.addLayout(top_bar)

        # Variable tree
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["变量名", "地址", "类型"])
        self._tree.setColumnWidth(1, 110)
        self._tree.setColumnWidth(2, 200)
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(18)
        self._tree.setAnimated(True)
        self._tree.setSelectionMode(QAbstractItemView.MultiSelection)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        tab1_layout.addWidget(self._tree)

        hint = QLabel("提示：展开结构体可选择子成员。点击选中，Ctrl+点击多选，Shift+点击范围选择。")
        hint.setStyleSheet("color: #606080; font-size: 9pt; padding: 2px;")
        tab1_layout.addWidget(hint)

        # ---- Tab 2: Scope ----
        self._tab_scope = QWidget()
        self._tabs.addTab(self._tab_scope, "📈  示波器")

        tab2_layout = QVBoxLayout(self._tab_scope)
        tab2_layout.setContentsMargins(8, 8, 8, 8)
        tab2_layout.setSpacing(6)

        # -- Real-time value table --
        table_frame = QFrame()
        table_frame.setObjectName("panel")
        table_frame.setMaximumHeight(140)
        table_layout = QVBoxLayout(table_frame)
        table_layout.setContentsMargins(8, 6, 8, 6)

        table_header = QLabel("实时数值")
        table_header.setStyleSheet("font-weight: bold; font-size: 10pt; color: #c0c0e0; padding: 2px;")
        table_layout.addWidget(table_header)

        self._value_table = QTableWidget()
        self._value_table.setColumnCount(3)
        self._value_table.setHorizontalHeaderLabels(["变量", "数值", "类型"])
        self._value_table.setAlternatingRowColors(True)
        self._value_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._value_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._value_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._value_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._value_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._value_table.setMaximumHeight(100)
        self._value_table.setRowCount(0)
        table_layout.addWidget(self._value_table)

        tab2_layout.addWidget(table_frame)

        # -- Scrolling plot --
        self._plot_widget = pg.GraphicsLayoutWidget()
        self._plot = self._plot_widget.addPlot()
        self._plot.setLabel("left", "数值", color="#b0b0b0")
        self._plot.setLabel("bottom", "时间", units="s", color="#b0b0b0")
        self._plot.showGrid(x=True, y=True, alpha=0.12)
        legend = self._plot.addLegend()
        if legend:
            legend.setBrush(pg.mkBrush(30, 30, 45, 200))
            legend.setPen(pg.mkPen(color=(80, 80, 100), width=1))
        self._plot.setAutoVisible(y=True)
        self._plot.getAxis("left").setPen(pg.mkPen(color="#606060"))
        self._plot.getAxis("bottom").setPen(pg.mkPen(color="#606060"))
        self._plot.setMouseEnabled(x=True, y=True)
        self._plot.enableAutoRange(y=True)
        self._plot.setLimits(xMin=0)
        self._plot.getViewBox().sigRangeChangedManually.connect(self._on_user_interact)
        self._auto_scroll = True

        # Y-auto toggle button overlaid on plot (top-right corner)
        self._y_auto_btn = QPushButton("Y自动")
        self._y_auto_btn.setCheckable(True)
        self._y_auto_btn.setChecked(True)
        self._y_auto_btn.setFixedSize(48, 22)
        self._y_auto_btn.setStyleSheet(
            "QPushButton { background: rgba(30,30,56,200); border: 1px solid #4a4a6a;"
            " border-radius: 4px; color: #a0a0c0; font-size: 8pt; font-weight: bold; }"
            "QPushButton:checked { background: rgba(0,180,216,180); color: #0a0a18; }"
            "QPushButton:hover { border: 1px solid #00b4d8; }")
        self._y_auto_btn.toggled.connect(self._on_y_auto_toggled)
        self._y_proxy = QGraphicsProxyWidget()
        self._y_proxy.setWidget(self._y_auto_btn)
        self._plot.getViewBox().scene().addItem(self._y_proxy)
        self._y_proxy.setZValue(100)
        self._plot.getViewBox().sigResized.connect(self._position_y_btn)

        tab2_layout.addWidget(self._plot_widget, stretch=1)

        # -- Controls bar --
        ctrl = QFrame()
        ctrl.setObjectName("controlBar")
        ctrl_layout = QHBoxLayout(ctrl)
        ctrl_layout.setContentsMargins(12, 8, 12, 8)
        ctrl_layout.setSpacing(10)

        rate_group = QHBoxLayout()
        rate_group.setSpacing(6)
        rate_group.addWidget(QLabel("采样率:"))
        self._rate_spin = QSpinBox()
        self._rate_spin.setRange(1, 10000)
        self._rate_spin.setValue(100)
        self._rate_spin.setSuffix(" Hz")
        self._rate_spin.setFixedWidth(100)
        self._rate_spin.valueChanged.connect(self._on_rate_changed)
        rate_group.addWidget(self._rate_spin)
        ctrl_layout.addLayout(rate_group)

        btn_max = QPushButton("MAX")
        btn_max.setObjectName("maxBtn")
        btn_max.setFixedWidth(44)
        btn_max.setToolTip("无限制模式 — 以硬件极限速度采样")
        btn_max.clicked.connect(lambda: self._rate_spin.setValue(1000))
        ctrl_layout.addWidget(btn_max)

        ctrl_layout.addSpacing(16)

        # 分隔
        sep_fps = QFrame()
        sep_fps.setFrameShape(QFrame.VLine)
        sep_fps.setStyleSheet("color: #3a3a5a;")
        sep_fps.setFixedWidth(1)
        ctrl_layout.addWidget(sep_fps)
        ctrl_layout.addSpacing(4)

        # 帧率控制
        ctrl_layout.addWidget(QLabel("显示帧率:"))
        self._frame_rate_spin = QSpinBox()
        self._frame_rate_spin.setRange(1, 120)
        self._frame_rate_spin.setSuffix(" FPS")
        self._frame_rate_spin.setFixedWidth(85)
        self._frame_rate_spin.valueChanged.connect(self._on_frame_rate_changed)
        self._frame_rate_spin.setValue(FRAME_RATE_DEFAULT)  # connect 之后设置，确保信号触发
        ctrl_layout.addWidget(self._frame_rate_spin)

        for fps in PRESET_FRAME_RATES:
            btn = QPushButton(str(fps))
            btn.setObjectName("presetBtn")
            btn.setFixedWidth(36)
            btn.clicked.connect(lambda checked, r=fps: self._frame_rate_spin.setValue(r))
            ctrl_layout.addWidget(btn)

        ctrl_layout.addSpacing(8)

        # 分隔
        sep_tw = QFrame()
        sep_tw.setFrameShape(QFrame.VLine)
        sep_tw.setStyleSheet("color: #3a3a5a;")
        sep_tw.setFixedWidth(1)
        ctrl_layout.addWidget(sep_tw)
        ctrl_layout.addSpacing(4)

        # 时间窗口
        ctrl_layout.addWidget(QLabel("时间窗口:"))
        self._time_window_spin = QSpinBox()
        self._time_window_spin.setRange(1, 120)
        self._time_window_spin.setSuffix(" s")
        self._time_window_spin.setFixedWidth(80)
        self._time_window_spin.setValue(TIME_WINDOW_DEFAULT)
        ctrl_layout.addWidget(self._time_window_spin)

        for tw in [5, 10, 30, 60]:
            btn = QPushButton(f"{tw}s")
            btn.setObjectName("presetBtn")
            btn.setFixedWidth(36)
            btn.clicked.connect(lambda checked, r=tw: self._time_window_spin.setValue(r))
            ctrl_layout.addWidget(btn)

        ctrl_layout.addStretch()

        self._btn_start = QPushButton("  开始")
        self._btn_start.setObjectName("startBtn")
        self._btn_start.clicked.connect(self._on_start_stop)
        ctrl_layout.addWidget(self._btn_start)

        self._btn_snapshot = QPushButton("导出 CSV")
        self._btn_snapshot.setObjectName("exportBtn")
        self._btn_snapshot.clicked.connect(self._on_export)
        ctrl_layout.addWidget(self._btn_snapshot)

        tab2_layout.addWidget(ctrl)

    # ================================================================
    #  Import & Tree Population
    # ================================================================

    def _on_import_elf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 ELF/AXF", "", "ELF/AXF (*.elf *.axf *.out);;All (*.*)")
        if path:
            self._elf_path = Path(path)
            self._load_variables()
            self.setWindowTitle(f"LoopMaster Scope — {self._elf_path.name}")

    def _load_variables(self):
        try:
            elf = ELFParser(self._elf_path)
            elf.open()
            dwarf_db = parse_debug_info(self._elf_path)

            # Try to find a linker map file for accurate source file mapping
            symbol_to_file = {}
            map_path = self._find_map_file()
            if map_path:
                try:
                    symbol_to_file = parse_map_file(map_path)
                except Exception:
                    pass

            inventory = VariableInventory(elf, dwarf_db, symbol_to_file)
            self._variables = inventory.generate()
            elf.close()
        except Exception as e:
            QMessageBox.warning(self, "错误", f"解析 ELF 失败: {e}")
            return
        self._populate_tree()

    def _find_map_file(self) -> Optional[Path]:
        """Look for a .map file near the ELF."""
        elf_dir = self._elf_path.parent
        elf_stem = self._elf_path.stem

        # Same directory, same basename
        candidates = [
            elf_dir / f"{elf_stem}.map",
        ]
        # Also check common build directories relative to elf location
        for up in range(4):
            prefix = Path(*([".."] * (up + 1)))
            candidates.append(elf_dir / prefix / "build" / "Debug" / f"{elf_stem}.map")
            candidates.append(elf_dir / prefix / "build" / f"{elf_stem}.map")

        for c in candidates:
            try:
                resolved = c.resolve()
                if resolved.exists():
                    return resolved
            except (OSError, ValueError):
                pass
        return None

    def _populate_tree(self):
        text = self._filter_edit.text().lower()
        self._tree.clear()
        self._registry.clear()

        # Filter variables
        display = []
        for v in self._variables:
            if text and text not in v.name.lower():
                concrete = resolve_type(v.type_info)
                if isinstance(concrete, StructType):
                    if not self._any_member_matches(text, concrete, v.name):
                        continue
                else:
                    continue
            display.append(v)

        # Group by source file
        from collections import defaultdict
        file_groups: dict[str, list] = defaultdict(list)
        for v in display:
            fn = v.file_name or ""
            file_groups[fn].append(v)

        group_keys = sorted([k for k in file_groups if k]) + ([""] if "" in file_groups else [])

        self._tree.blockSignals(True)
        for fname in group_keys:
            vars_in_file = sorted(file_groups[fname], key=lambda v: (v.address, v.name))
            if fname:
                short_name = Path(fname).name
                folder = QTreeWidgetItem(self._tree)
                folder.setText(0, f"📄 {short_name}")
                folder.setText(1, f"{len(vars_in_file)} vars")
                folder.setText(2, fname)
                folder.setFlags(folder.flags() & ~Qt.ItemIsSelectable)
                font = folder.font(0)
                font.setBold(True)
                folder.setFont(0, font)
                folder.setForeground(0, QColor("#8090b0"))
                for v in vars_in_file:
                    self._add_variable_item(v, parent_item=folder)
            else:
                for v in vars_in_file:
                    self._add_variable_item(v)

        self._tree.blockSignals(False)
        self._tree.collapseAll()

        # 恢复上次选中的变量
        cfg = self._load_config()
        self._monitored = set()
        saved_vars = cfg.get("monitored_variables", []) if cfg else []
        if saved_vars:
            self._restore_selected_items(self._tree.invisibleRootItem(), saved_vars)
        self._update_selected_list()

    def _any_member_matches(self, text: str, st: StructType, parent_path: str) -> bool:
        for m in st.members:
            full = f"{parent_path}.{m.name}"
            if text in m.name.lower() or text in full.lower():
                return True
            inner = resolve_type(m.type_info)
            if isinstance(inner, StructType):
                if self._any_member_matches(text, inner, full):
                    return True
        return False

    def _add_variable_item(self, v: Variable, depth: int = 0,
                           parent_item: Optional[QTreeWidgetItem] = None,
                           path_prefix: str = ""):
        concrete = resolve_type(v.type_info)
        is_struct = isinstance(concrete, StructType)
        full_path = f"{path_prefix}.{v.name}" if path_prefix else v.name

        if is_struct and depth < MAX_STRUCT_DEPTH and concrete.members:
            item = QTreeWidgetItem() if parent_item is None else QTreeWidgetItem(parent_item)
            item.setText(0, v.name)
            item.setText(1, f"0x{v.address:08X}")
            item.setText(2, format_type(v.type_info))
            item.setData(0, ROLE_PATH, full_path)
            item.setData(0, ROLE_ADDR, v.address)
            item.setData(0, ROLE_TYPE, v.type_info)
            item.setFlags(item.flags() | Qt.ItemIsSelectable)

            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)

            if full_path in self._monitored:
                item.setSelected(True)

            if parent_item is None:
                self._tree.addTopLevelItem(item)

            sorted_members = sorted(concrete.members, key=lambda m: m.offset)

            for member in sorted_members:
                member_addr = v.address + member.offset
                member_concrete = resolve_type(member.type_info)
                member_is_struct = isinstance(member_concrete, StructType)

                if member_is_struct and depth + 1 < MAX_STRUCT_DEPTH and member_concrete.members:
                    display_ti = member.type_info if isinstance(member.type_info, TypedefType) else member_concrete
                    pseudo = Variable(
                        name=member.name, address=member_addr,
                        size=member_concrete.size, type_info=display_ti,
                    )
                    self._add_variable_item(pseudo, depth + 1, item, full_path)
                else:
                    member_path = f"{full_path}.{member.name}"
                    child = QTreeWidgetItem(item)
                    child.setText(0, member.name)
                    child.setText(1, f"0x{member_addr:08X}")

                    type_str = format_type(member.type_info)
                    if member.bit_size > 0:
                        type_str += f"  [:{member.bit_size}]"
                    child.setText(2, type_str)

                    if member.bit_size > 0:
                        child.setFlags(Qt.ItemIsEnabled)
                        child.setForeground(2, QColor("#8888a0"))
                    else:
                        child.setFlags(child.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                        if member_path in self._monitored:
                            child.setSelected(True)

                    child.setData(0, ROLE_PATH, member_path)
                    child.setData(0, ROLE_ADDR, member_addr)
                    child.setData(0, ROLE_TYPE, member.type_info)

                    self._registry[member_path] = (member_addr, member.type_info)

        else:
            item = QTreeWidgetItem() if parent_item is None else QTreeWidgetItem(parent_item)
            item.setText(0, v.name)
            item.setText(1, f"0x{v.address:08X}")
            item.setText(2, format_type(v.type_info))
            item.setData(0, ROLE_PATH, full_path)
            item.setData(0, ROLE_ADDR, v.address)
            item.setData(0, ROLE_TYPE, v.type_info)
            item.setFlags(item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled)

            if full_path in self._monitored:
                item.setSelected(True)

            self._registry[full_path] = (v.address, v.type_info)

            if parent_item is None:
                self._tree.addTopLevelItem(item)

    # ================================================================
    #  Multi-selection via Qt selection model
    # ================================================================

    def _on_selection_changed(self):
        """Sync Qt selection state with self._monitored."""
        selected_paths = set()
        for item in self._tree.selectedItems():
            path = item.data(0, ROLE_PATH)
            if path is not None:
                selected_paths.add(path)

        self._monitored = selected_paths
        self._update_selected_list()
        self._idle_read()

    def _update_selected_list(self):
        """Update tab title with selected count."""
        count = len(self._monitored)
        self._tabs.setTabText(1, f"📈  示波器 ({count})" if count else "📈  示波器")

    def _on_clear_selection(self):
        self._tree.clearSelection()

    def _on_filter_changed(self):
        self._populate_tree()

    # ================================================================
    #  Probe actions
    # ================================================================

    def _on_view_log(self):
        """用系统默认编辑器打开日志文件。"""
        log_path = os.path.abspath("loopmaster.log")
        if os.path.exists(log_path):
            try:
                os.startfile(log_path)
            except Exception:
                subprocess.Popen(["notepad.exe", log_path])
        else:
            QMessageBox.information(self, "日志", "日志文件尚未创建。")

    def _on_import_pack(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 CMSIS-Pack", "", "Pack (*.pack *.pdsc);;All (*.*)")
        if path:
            self._pack_path = path

    def _on_scan_probes(self):
        self._on_scan_probes_ui()

    def _on_connect(self):
        self._on_connect_ui()

    def _on_disconnect(self):
        self._on_disconnect_ui()

    def _on_scan_probes_ui(self):
        self._btn_scan.setEnabled(False)
        self._btn_scan.setText("扫描中...")
        QApplication.processEvents()
        try:
            self._probe_list = SWDBackend.scan_probes()
        except Exception as e:
            QMessageBox.warning(self, "错误", f"扫描失败: {e}")
            self._btn_scan.setEnabled(True)
            self._btn_scan.setText("🔄 扫描")
            return

        self._probe_combo.clear()
        if not self._probe_list:
            self._probe_combo.addItem("— 未找到探针 —")
            self._conn_label.setText("未找到探针")
        else:
            for i, p in enumerate(self._probe_list):
                uid_short = p["uid"][:8] if p["uid"] else "????"
                label = f"{p['name']} ({p['vendor']}) [{uid_short}]"
                self._probe_combo.addItem(label)
            self._probe_combo.setCurrentIndex(0)
            self._conn_label.setText(f"找到 {len(self._probe_list)} 个探针")

        self._btn_scan.setEnabled(True)
        self._btn_scan.setText("🔄 扫描")

    def _on_connect_ui(self):
        if self._backend.is_connected:
            self._on_disconnect_ui()
            return

        if not self._probe_list or self._probe_combo.currentIndex() < 0:
            QMessageBox.warning(self, "错误", "请先扫描探针。")
            return

        probe_index = self._probe_combo.currentIndex()
        if probe_index >= len(self._probe_list):
            probe_index = 0

        mode_text = self._mode_combo.currentText()
        connect_mode = "reset" if "复位" in mode_text else "attach"

        freq_text = self._swd_freq_combo.currentText()
        freq = int(freq_text.split()[0]) * 1_000_000

        pack = getattr(self, '_pack_path', None)
        try:
            ok = self._backend.connect(
                pack=pack, connect_mode=connect_mode, probe_index=probe_index,
                freq=freq)
        except Exception as e:
            QMessageBox.warning(self, "连接失败", str(e))
            return

        if ok:
            self._update_conn_status(True)
            self._sb_label.setText(
                f"  探针: 已连接  |  目标: {self._backend.target_name}")
            self._led.setStyleSheet("color: #40e060; font-size: 14px;")
            logger.info("探针已连接 (模式=%s, 目标=%s, SWD=%dkHz)",
                connect_mode, self._backend.target_name, self._backend.swd_freq_khz)
            self._idle_read()
        else:
            logger.warning("连接失败: 未找到目标芯片")
            QMessageBox.warning(self, "连接失败",
                "未找到目标芯片，请检查接线和供电。")

    def _on_disconnect_ui(self):
        logger.info("断开探针连接")
        self._on_stop()
        self._backend.disconnect()
        self._update_conn_status(False)
        self._sb_label.setText(
            f"  探针: 已断开  |  目标: --")
        self._led.setStyleSheet("color: #e04040; font-size: 14px;")

    # ================================================================
    #  Start / Stop
    # ================================================================

    def _on_start_stop(self):
        if self._collector.is_running:
            self._on_stop()
        else:
            self._on_start()

    def _on_start(self):
        if not self._elf_path:
            QMessageBox.warning(self, "错误", "请先导入 ELF 文件。")
            return
        if not self._backend.is_connected:
            QMessageBox.warning(self, "错误", "请先连接探针。")
            return

        self._monitor_list = []
        for path in sorted(self._monitored):
            info = self._registry.get(path)
            if info is not None:
                addr, ti = info
                self._monitor_list.append((path, addr, ti))

        if not self._monitor_list:
            QMessageBox.warning(self, "错误",
                "未选择变量，请先在变量选择标签页中选择。")
            return

        rate = self._rate_spin.value()

        self._collector.configure(rate, BUFFER_SECONDS)
        self._collector.set_variables(self._monitor_list)
        self._collector._running = True
        self._collector._sample_count = 0
        self._collector._t0 = 0.0
        self._setup_fast_path()

        # Set up value table for monitored variables
        self._value_table.setRowCount(len(self._monitor_list))
        for row, (name, _, ti) in enumerate(self._monitor_list):
            self._value_table.setItem(row, 0, QTableWidgetItem(name))
            self._value_table.setItem(row, 1, QTableWidgetItem("—"))
            self._value_table.setItem(row, 2, QTableWidgetItem(format_type(ti)))
        self._value_update_counter = 0

        self._plot.clear()
        legend = self._plot.addLegend()
        if legend:
            legend.setBrush(pg.mkBrush(30, 30, 45, 200))
            legend.setPen(pg.mkPen(color=(80, 80, 100), width=1))
        self._plot_curves.clear()
        for i, (name, _, _) in enumerate(self._monitor_list):
            color = COLORS[i % len(COLORS)]
            curve = self._plot.plot(
                [], [],
                pen=pg.mkPen(color=color, width=1.5),
                name=name,
                autoDownsample=True,
                clipToView=True,
            )
            self._plot_curves[name] = curve

        # 重置 Y 轴自适应 和 X 轴自动滚动
        self._y_auto_btn.setChecked(True)
        self._auto_scroll = True

        # 高频(>=500Hz): 紧凑批采样循环，消除 QTimer 事件开销
        # 低频(<500Hz):  精确 QTimer 按间隔触发
        if rate >= 500:
            self._unlimited_mode = True
            self._sample_timer.stop()
            logger.info("开始极限采样: %d 变量, 无限制模式", len(self._monitor_list))
            QTimer.singleShot(0, self._tight_sample_loop)
        else:
            self._unlimited_mode = False
            interval_ms = max(1, int(1000 / rate))
            self._sample_timer.setInterval(interval_ms)
            self._sample_timer.start()
            logger.info("开始采样: %d 变量, %d Hz", len(self._monitor_list), rate)

        self._btn_start.setText("  停止")
        self._btn_start.setObjectName("stopBtn")
        self._btn_start.setStyleSheet(self._btn_start.styleSheet())

        # Switch to Scope tab
        self._tabs.setCurrentIndex(1)

    def _on_stop(self):
        logger.info("停止采样 (共 %d 个样本)", self._collector._sample_count)
        self._unlimited_mode = False
        self._sample_timer.stop()
        self._collector._running = False
        self._btn_start.setText("  开始")
        self._btn_start.setObjectName("startBtn")
        self._btn_start.setStyleSheet(self._btn_start.styleSheet())
        # Clear value table
        self._value_table.setRowCount(0)
        # Reset plot auto-range
        self._plot.enableAutoRange(x=True)

    def _on_rate_changed(self, rate: int):
        """采样率变更时同步到采集器和定时器。无限制模式下可切回定时模式。"""
        self._collector._sample_rate = rate
        if not self._collector.is_running:
            return
        if self._unlimited_mode:
            if rate < 500:
                # 从无限制模式切回定时器模式
                self._unlimited_mode = False
                interval_ms = max(1, int(1000 / rate))
                self._sample_timer.setInterval(interval_ms)
                self._sample_timer.start()
                logger.info("切回定时采样: %d Hz", rate)
        else:
            if rate >= 500:
                self._unlimited_mode = True
                self._sample_timer.stop()
                logger.info("切换到极限采样模式")
                QTimer.singleShot(0, self._tight_sample_loop)
            else:
                interval_ms = max(1, int(1000 / rate))
                self._sample_timer.setInterval(interval_ms)

    def _setup_fast_path(self):
        """预计算采样热路径的全部引用，消除函数调用和字典查找。"""
        c = self._collector
        ap = self._backend._ap
        decoder = self._backend._decoder
        if decoder is None:
            from src.core.mem_backend import _TypeDecoder
            decoder = _TypeDecoder(self._backend)
            self._backend._decoder = decoder

        self._fast_ap = ap
        self._fast_bufs = c._buffers  # {name: deque}
        self._fast_ts = c._timestamps  # deque

        # 分类: 直接(float)读取 vs 需要 _extract_val 处理
        self._fast_direct = []   # [(deque, word_addr), ...]
        self._fast_complex = []  # [(deque, word_addr, bo, w, sgn, flt), ...]
        self._fast_names = []    # [name, ...]

        # 构建所有变量的读取计划
        all_plans = []  # [(wa, bo, w, wc, sgn, flt, buf), ...]
        for name, addr, ti in self._monitor_list:
            wa, bo, w, wc, sgn, flt = decoder.make_plan(addr, ti)
            buf = c._buffers.get(name)
            if buf is None:
                continue
            self._fast_names.append(name)
            all_plans.append((wa, bo, w, wc, sgn, flt, buf))
            if wc <= 1 and bo == 0 and w == 4 and not sgn and not flt:
                self._fast_direct.append((buf, wa))
            elif wc <= 1:
                self._fast_complex.append((buf, wa, bo, w, sgn, flt))
                logger.debug(f"变量 '{name}' → complex: bo={bo} w={w} sgn={sgn} flt={flt}")
            else:
                self._fast_complex.append((buf, addr, 0, w, sgn, flt, True))
                logger.debug(f"变量 '{name}' → cross-word: bo={bo} w={w} wc={wc}")

        # 尝试合并为单次块读取（将 N 次 USB 事务变为 1 次）
        self._fast_block = None
        if len(all_plans) >= 2:
            all_plans.sort(key=lambda x: x[0])  # 按 word_addr 排序
            first_wa = all_plans[0][0]
            last_plan = all_plans[-1]
            last_end = last_plan[0] + last_plan[3] * 4
            total_words = (last_end - first_wa) // 4
            if 2 <= total_words <= 64:
                self._fast_block = (first_wa, total_words, all_plans)
                logger.info(f"块读取模式: {len(all_plans)} 变量, "
                            f"范围 0x{first_wa:08X}, {total_words} 字")

    def _on_sample_tick(self):
        """极限优化采样 — 全部引用内联，零中间分配。"""
        c = self._collector
        if not c._running:
            return

        tick_start = time.perf_counter()
        ap = self._fast_ap
        ts_deque = self._fast_ts
        t0 = c._t0

        if t0 == 0.0:
            t0 = tick_start
            c._t0 = t0

        # 尝试单次块读取（将 N 次 USB 事务合并为 1 次）
        block = self._fast_block
        if block is not None:
            block_start, block_words, block_plans = block
            try:
                words = ap.read_memory_block32(block_start, block_words)
                if not isinstance(words, list):
                    words = list(words)
                for wa, bo, w, wc, sgn, flt, buf in block_plans:
                    idx = (wa - block_start) // 4
                    if wc <= 1:
                        buf.append(_extract_val(words, word_idx=idx, byte_offset=bo,
                                                width=w, is_signed=sgn, is_float=flt))
                    else:
                        # 跨字变量回退到 read()（罕见）
                        buf.append(float(self._backend.read(wa + bo, w)))
                ts_deque.append(tick_start - t0)
                c._sample_count += 1
                if c._sample_count % 50 == 0:
                    c._actual_rate = c._sample_count / (tick_start - t0) if tick_start > t0 else 0
                    tick_ms = (time.perf_counter() - tick_start) * 1000
                    logger.debug(f"[BLOCK] 耗时: {tick_ms:.1f}ms | "
                                 f"速率: {c._actual_rate:.0f}Hz | {len(block_plans)}变量→{block_words}字")
                return
            except Exception as e:
                logger.warning(f"块读取失败: {e}，回退逐个读取")
                self._fast_block = None  # 不再尝试
                # 继续执行逐个读取路径

        try:
            # 直接读取路径 (对齐 uint32 — 最常见情况)
            for buf, wa in self._fast_direct:
                buf.append(float(ap.read_memory(wa, transfer_size=32)))

            # 复杂类型路径 (偏移 / 有符号 / 浮点)
            for item in self._fast_complex:
                if len(item) == 7:  # 跨字变量
                    buf, addr, _, w, sgn, flt, _ = item
                    buf.append(float(self._backend.read(addr, w)))
                else:
                    buf, wa, bo, w, sgn, flt = item
                    raw = ap.read_memory(wa, transfer_size=32)
                    buf.append(_extract_val(raw, byte_offset=bo, width=w,
                                            is_signed=sgn, is_float=flt))
        except Exception:
            pass  # USB 偶发错误，跳过本次采样

        ts_deque.append(tick_start - t0)

        c._sample_count += 1
        if c._sample_count % 50 == 0:
            elapsed = tick_start - t0
            c._actual_rate = c._sample_count / elapsed if elapsed > 0 else 0
            tick_ms = (time.perf_counter() - tick_start) * 1000
            logger.debug(f"采样耗时: {tick_ms:.1f}ms | 速率: {c._actual_rate:.0f}Hz | "
                         f"变量: direct={len(self._fast_direct)} complex={len(self._fast_complex)}")

    def _tight_sample_loop(self):
        """无限制模式 — 自适应深度流水线批量采样。

        根据块大小自动调整流水线深度 (16-48)，将 USB 往返延迟均摊到更多样本。
        通过 singleShot(0) 交还控制权给事件循环，自然交错处理 plot 更新和用户输入。
        """
        c = self._collector
        if not c._running:
            self._unlimited_mode = False
            return

        ap = self._fast_ap
        ts_deque = self._fast_ts
        t0 = c._t0
        if t0 == 0.0:
            t0 = time.perf_counter()
            c._t0 = t0

        block = self._fast_block
        BACKEND = self._backend

        if block is not None:
            block_start, block_words, block_plans = block
            # 自适应流水线深度: 块越小深度越大，USB 开销均摊越好
            if block_words <= 8:
                pipe_depth = 48
            elif block_words <= 16:
                pipe_depth = 32
            elif block_words <= 32:
                pipe_depth = 16
            else:
                pipe_depth = 8

            try:
                all_sample_vals = BACKEND.read_block_pipelined(
                    block_start, block_words, block_plans, pipe_depth)
                now = time.perf_counter()
                for sample_vals in all_sample_vals:
                    ts_deque.append(now - t0)
                    c._sample_count += 1
                    for (_, _, _, _, _, _, buf), val in zip(block_plans, sample_vals):
                        buf.append(val)
            except Exception:
                # 流水线失败时回退单次块读取
                try:
                    words = ap.read_memory_block32(block_start, block_words)
                    if not isinstance(words, list):
                        words = list(words)
                    now = time.perf_counter()
                    for wa, bo, w, wc, sgn, flt, buf in block_plans:
                        idx = (wa - block_start) // 4
                        if wc <= 1:
                            buf.append(_extract_val(words, word_idx=idx, byte_offset=bo,
                                                    width=w, is_signed=sgn, is_float=flt))
                        else:
                            buf.append(float(BACKEND.read(wa + bo, w)))
                    ts_deque.append(now - t0)
                    c._sample_count += 1
                except Exception:
                    pass
        else:
            # 非块读取路径 — 逐个变量读取
            batch_deadline = time.perf_counter() + 0.020
            while c._running and time.perf_counter() < batch_deadline:
                try:
                    for buf, wa in self._fast_direct:
                        buf.append(float(ap.read_memory(wa, transfer_size=32)))
                    for item in self._fast_complex:
                        if len(item) == 7:
                            buf, addr, _, w, sgn, flt, _ = item
                            buf.append(float(BACKEND.read(addr, w)))
                        else:
                            buf, wa, bo, w, sgn, flt = item
                            raw = ap.read_memory(wa, transfer_size=32)
                            buf.append(_extract_val(raw, byte_offset=bo, width=w,
                                                    is_signed=sgn, is_float=flt))
                    ts_deque.append(time.perf_counter() - t0)
                    c._sample_count += 1
                except Exception:
                    pass

        # 更新实际速率
        if c._sample_count > 0:
            elapsed = time.perf_counter() - t0
            if elapsed > 0:
                c._actual_rate = c._sample_count / elapsed

        # 重新调度
        if c._running and self._unlimited_mode:
            QTimer.singleShot(0, self._tight_sample_loop)
        else:
            if not c._running:
                final_elapsed = time.perf_counter() - t0
                final_rate = c._sample_count / final_elapsed if final_elapsed > 0 else 0
                logger.info("极限采样结束: %d 样本, %.0f Hz", c._sample_count, final_rate)
            self._unlimited_mode = False

    def _update_plot(self):
        if not self._collector.is_running:
            return

        time_window = self._time_window_spin.value() if self._time_window_spin else TIME_WINDOW_DEFAULT

        if self._auto_scroll:
            # 滚动模式：只取时间窗口 + 少量边距的数据，保证高效
            raw_data = self._collector.get_data(tail_seconds=time_window * 1.5)
        else:
            # 用户手动缩放/平移：取全部历史数据
            raw_data = self._collector.get_data()

        if not raw_data:
            return

        # 插值/抽取处理
        data = self._process_display_data(raw_data)

        # 批量更新曲线
        latest_ts = 0.0
        for name, curve in self._plot_curves.items():
            if name in data:
                ts, vals = data[name]
                if len(ts) > 0:
                    curve.setData(ts, vals)
                    if ts[-1] > latest_ts:
                        latest_ts = ts[-1]

        # 自动滚动：显示最近 time_window 秒
        if latest_ts > 0 and self._auto_scroll:
            x_min = max(0, latest_ts - time_window)
            self._plot.setXRange(x_min, latest_ts, padding=0.02)

        actual = self._collector.actual_rate
        configured = self._collector._sample_rate
        fps = self._frame_rate_spin.value() if self._frame_rate_spin else 60
        scroll_mark = "" if self._auto_scroll else " (已暂停)"
        tw = self._time_window_spin.value() if self._time_window_spin else TIME_WINDOW_DEFAULT
        if self._unlimited_mode:
            self._sb_rate.setText(
                f"  |  极限采样: {actual:.0f} Hz (MAX)  |  显示: {fps} FPS  |  窗口: {tw}s{scroll_mark}")
        else:
            self._sb_rate.setText(
                f"  |  采样: {actual:.0f}/{configured} Hz  |  显示: {fps} FPS  |  窗口: {tw}s{scroll_mark}")

        # Update value table (throttled to ~5Hz to reduce flicker)
        self._value_update_counter = getattr(self, '_value_update_counter', 0) + 1
        if self._value_update_counter % max(1, fps // 5) == 0:
            self._update_value_table(data)

    def _on_user_interact(self, vb):
        """用户手动缩放/平移时关闭 Y 自适应和 X 自动滚动。"""
        if self._y_auto_btn.isChecked():
            self._y_auto_btn.setChecked(False)
        self._auto_scroll = False

    def _position_y_btn(self):
        """Keep Y-auto button at top-right of the plot."""
        vb = self._plot.getViewBox()
        bw, bh = 52, 24
        self._y_proxy.setPos(vb.width() - bw - 8, 8)

    def _on_y_auto_toggled(self, checked: bool):
        """Y 轴自适应开关。同时重置 X 轴滚动。"""
        if checked:
            self._plot.enableAutoRange(y=True)
            self._auto_scroll = True
            self._y_auto_btn.setText("Y自动")
        else:
            self._plot.enableAutoRange(y=False)
            self._y_auto_btn.setText("Y手动")

    def _on_frame_rate_changed(self, fps: int):
        """FPS 变更时更新定时器间隔。"""
        interval = max(8, int(1000 / fps))
        self._plot_timer.setInterval(interval)
        if not self._plot_timer.isActive():
            self._plot_timer.start()

    def _process_display_data(self, data: dict) -> dict:
        sample_rate = self._collector.actual_rate
        fps = self._frame_rate_spin.value() if self._frame_rate_spin else 60
        if sample_rate <= 0 or fps <= 0:
            return data

        ratio = sample_rate / fps
        if 0.5 <= ratio <= 2.0:
            return data

        result = {}
        for name, (ts, vals) in data.items():
            if len(ts) < 2:
                result[name] = (ts, vals)
                continue
            try:
                if ratio < 0.5:
                    result[name] = self._interpolate_data(ts, vals, fps)
                else:
                    result[name] = self._decimate_data(ts, vals, int(ratio))
            except Exception:
                result[name] = (ts, vals)
        return result

    @staticmethod
    def _interpolate_data(ts, vals, display_fps):
        t_min, t_max = ts[0], ts[-1]
        duration = t_max - t_min
        if duration <= 0:
            return (ts, vals)
        num_points = max(2, int(duration * display_fps))
        num_points = min(num_points, len(ts) * 10)
        if num_points <= len(ts):
            return (ts, vals)
        ts_arr = np.asarray(ts, dtype=float)
        vals_arr = np.asarray(vals, dtype=float)
        display_ts = np.linspace(t_min, t_max, num_points)
        display_vals = np.interp(display_ts, ts_arr, vals_arr)
        return (display_ts, display_vals)

    @staticmethod
    def _decimate_data(ts, vals, factor):
        step = max(1, factor)
        if step <= 1:
            return (ts, vals)
        indices = list(range(0, len(ts), step))
        if indices[-1] != len(ts) - 1:
            indices.append(len(ts) - 1)
        ts_arr = np.asarray(ts, dtype=float)
        vals_arr = np.asarray(vals, dtype=float)
        return (ts_arr[indices], vals_arr[indices])

    def _update_value_table(self, data: dict):
        """Show latest value for each monitored variable."""
        names = sorted(data.keys())
        t = self._value_table
        if t.rowCount() != len(names):
            t.setRowCount(len(names))
        for row, name in enumerate(names):
            ts, vals = data[name]
            latest = f"{vals[-1]:.4g}" if len(vals) > 0 else "—"
            # Name column
            name_item = t.item(row, 0)
            if name_item is None:
                name_item = QTableWidgetItem(name)
                t.setItem(row, 0, name_item)
            else:
                name_item.setText(name)
            # Value column
            val_item = t.item(row, 1)
            if val_item is None:
                val_item = QTableWidgetItem(latest)
                val_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(row, 1, val_item)
            else:
                val_item.setText(latest)
            # Type column from registry
            type_item = t.item(row, 2)
            if type_item is None:
                info = self._registry.get(name)
                type_str = format_type(info[1]) if info else ""
                type_item = QTableWidgetItem(type_str)
                t.setItem(row, 2, type_item)

    def _idle_read(self):
        """Single-shot read when scope is not actively sampling.
        Runs on _idle_timer (~4Hz) so values display even without pressing START.
        """
        if not self._backend.is_connected:
            return
        if self._collector.is_running:
            return
        if not self._monitored:
            if self._value_table.rowCount() > 0:
                self._value_table.setRowCount(0)
            return

        # Build monitor list from current selections
        monitor_list = []
        for path in sorted(self._monitored):
            info = self._registry.get(path)
            if info is not None:
                addr, ti = info
                monitor_list.append((path, addr, ti))

        if not monitor_list:
            return

        try:
            raw = self._backend.read_batch(monitor_list)
        except Exception:
            return

        # Convert to plot-compatible format for _update_value_table
        data = {}
        for name, val in raw.items():
            data[name] = ([0.0], [val])
        self._update_value_table(data)

    # ================================================================
    #  Export
    # ================================================================

    def _on_export(self):
        if not self._elf_path:
            QMessageBox.warning(self, "错误", "没有数据可导出。")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", "scope_data.csv", "CSV (*.csv)")
        if not path:
            return

        data = self._collector.get_data()
        if not data:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["变量名", "地址", "类型"])
                for p, (addr, ti) in self._registry.items():
                    writer.writerow([p, f"0x{addr:08X}", format_type(ti)])
            return

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            names = list(data.keys())
            writer.writerow(["时间戳"] + names)
            max_len = max(len(v[1]) for v in data.values()) if data else 0
            ts_all = list(list(data.values())[0][0]) if data else []
            for i in range(max_len):
                row = [ts_all[i] if i < len(ts_all) else ""]
                for n in names:
                    vals = data[n][1]
                    row.append(vals[i] if i < len(vals) else "")
                writer.writerow(row)

        self._sb_label.setText(f"  已导出到 {path}")

    # ================================================================
    #  Config persistence
    # ================================================================

    def _load_config(self) -> dict:
        try:
            if self._config_path.exists():
                with open(self._config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_config(self):
        cfg = {
            "elf_path": str(self._elf_path) if self._elf_path else "",
            "sample_rate": self._rate_spin.value(),
            "frame_rate": self._frame_rate_spin.value(),
            "swd_freq_index": self._swd_freq_combo.currentIndex(),
            "connect_mode_index": self._mode_combo.currentIndex(),
            "y_auto": self._y_auto_btn.isChecked(),
            "monitored_variables": sorted(self._monitored),
        }
        try:
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _restore_selected_items(self, parent: QTreeWidgetItem, saved: list[str]):
        for i in range(parent.childCount()):
            item = parent.child(i)
            path = item.data(0, ROLE_PATH)
            if path in saved:
                item.setSelected(True)
                self._monitored.add(path)
            self._restore_selected_items(item, saved)

    def closeEvent(self, event):
        self._on_stop()
        self._save_config()
        self._backend.disconnect()
        event.accept()


# ================================================================
#  Entry Point
# ================================================================

def run_scope(elf_path: str = None, pack_path: str = None, target: str = None):
    # Windows: 把系统定时器精度从 15.6ms 提升到 1ms
    import platform
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

    setup_logging()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))

    # ---- Modern Dark Palette ----
    p = app.palette()
    p.setColor(QPalette.Window, QColor(26, 26, 42))
    p.setColor(QPalette.WindowText, QColor(218, 218, 230))
    p.setColor(QPalette.Base, QColor(30, 30, 50))
    p.setColor(QPalette.AlternateBase, QColor(36, 36, 56))
    p.setColor(QPalette.Text, QColor(218, 218, 230))
    p.setColor(QPalette.Button, QColor(44, 44, 64))
    p.setColor(QPalette.ButtonText, QColor(218, 218, 230))
    p.setColor(QPalette.Highlight, QColor(0, 180, 216))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 140))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 140))
    app.setPalette(p)

    # ---- Global Stylesheet ----
    app.setStyleSheet("""
        QFrame#panel {
            background: #1e1e38;
            border: 1px solid #2a2a4a;
            border-radius: 8px;
        }
        QFrame#controlBar {
            background: #1e1e38;
            border: 1px solid #2a2a4a;
            border-radius: 8px;
        }
        QTreeWidget, QListWidget, QTableWidget {
            background: #1e1e36;
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            outline: none;
            padding: 2px;
        }
        QTableWidget::item {
            padding: 2px 6px;
            color: #c0c0d0;
        }
        QTreeWidget::item, QListWidget::item {
            padding: 3px 0px;
            border-radius: 2px;
        }
        QTreeWidget::item:hover, QListWidget::item:hover {
            background: #2a2a4e;
        }
        QTreeWidget::item:selected, QListWidget::item:selected {
            background: #0a3a5c;
        }
        QHeaderView::section {
            background: #1a1a36;
            color: #a0a0c0;
            border: none;
            border-bottom: 1px solid #2a2a4a;
            padding: 6px 8px;
            font-weight: bold;
            font-size: 10pt;
        }
        QTabWidget::pane {
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            background: #1e1e38;
        }
        QTabBar::tab {
            background: #1a1a34;
            color: #9090b0;
            padding: 10px 24px;
            border: 1px solid #2a2a4a;
            border-bottom: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            margin-right: 2px;
            font-size: 11pt;
        }
        QTabBar::tab:selected {
            background: #1e1e38;
            color: #00b4d8;
            border-bottom: 2px solid #00b4d8;
        }
        QTabBar::tab:hover:!selected {
            background: #222248;
            color: #c0c0e0;
        }
        QLineEdit {
            padding: 7px 10px;
            background: #1a1a34;
            border: 1px solid #3a3a5a;
            border-radius: 6px;
            color: #e0e0f0;
            font-size: 10pt;
        }
        QLineEdit:focus {
            border: 1px solid #00b4d8;
        }
        QPushButton#importBtn {
            padding: 10px 16px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #1a3a5c, stop:1 #152a44);
            border: 1px solid #00b4d8;
            border-radius: 6px;
            color: #00b4d8;
            font-weight: bold;
            font-size: 11pt;
        }
        QPushButton#importBtn:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #00b4d8, stop:1 #0088aa);
            color: #ffffff;
        }
        QPushButton#smallBtn {
            padding: 6px 12px;
            background: #2a2a48;
            border: 1px solid #3a3a58;
            border-radius: 5px;
            color: #c0c0d0;
            font-size: 9pt;
        }
        QPushButton#smallBtn:hover {
            background: #3a3a58;
        }
        QPushButton#presetBtn {
            padding: 4px 6px;
            background: #252542;
            border: 1px solid #363658;
            border-radius: 4px;
            color: #a0a0c0;
            font-size: 9pt;
            font-weight: bold;
        }
        QPushButton#presetBtn:hover {
            background: #00b4d8;
            color: #0a0a18;
            border: 1px solid #00b4d8;
        }
        QPushButton#maxBtn {
            padding: 4px 6px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #8a2040, stop:1 #5a1028);
            border: 1px solid #c83058;
            border-radius: 4px;
            color: #ffe0e0;
            font-size: 9pt;
            font-weight: bold;
        }
        QPushButton#maxBtn:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #c83058, stop:1 #8a2040);
            color: #ffffff;
        }
        QPushButton#startBtn {
            padding: 8px 24px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #1e7a42, stop:1 #165a30);
            border: 1px solid #208848;
            border-radius: 6px;
            color: #e0ffe0;
            font-weight: bold;
            font-size: 11pt;
        }
        QPushButton#startBtn:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #28a055, stop:1 #1e7a42);
        }
        QPushButton#stopBtn {
            padding: 8px 24px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #b82830, stop:1 #801820);
            border: 1px solid #c83038;
            border-radius: 6px;
            color: #ffe0e0;
            font-weight: bold;
            font-size: 11pt;
        }
        QPushButton#stopBtn:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #d03040, stop:1 #b82830);
        }
        QPushButton#exportBtn {
            padding: 8px 20px;
            background: #2a2a48;
            border: 1px solid #3a3a58;
            border-radius: 6px;
            color: #c0c0d0;
            font-size: 10pt;
        }
        QPushButton#exportBtn:hover {
            background: #363658;
        }
        QSpinBox {
            padding: 5px 8px;
            background: #1a1a34;
            border: 1px solid #3a3a5a;
            border-radius: 5px;
            color: #e0e0f0;
            font-size: 10pt;
        }
        QSpinBox:focus {
            border: 1px solid #00b4d8;
        }
        QSplitter::handle {
            background: #252545;
        }
        QStatusBar {
            background: #14142a;
            border-top: 1px solid #2a2a4a;
            color: #a0a0c0;
            font-size: 9pt;
            padding: 3px 8px;
        }
        QMenuBar {
            background: #161630;
            border-bottom: 1px solid #2a2a4a;
            color: #c0c0d0;
            padding: 2px;
        }
        QMenuBar::item:selected {
            background: #2a2a48;
            border-radius: 4px;
        }
        QMenu {
            background: #1e1e38;
            border: 1px solid #2a2a4a;
            color: #c0c0d0;
            padding: 4px;
        }
        QMenu::item:selected {
            background: #00b4d8;
            color: #0a0a18;
            border-radius: 4px;
        }
        QToolTip {
            background: #2a2a48;
            color: #e0e0f0;
            border: 1px solid #3a3a58;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 9pt;
        }
        QFrame#connectionBar {
            background: #141428;
            border-bottom: 1px solid #2a2a4a;
        }
        QFrame#connSeparator {
            background: #2a2a4a;
        }
        QPushButton#connectBtn {
            padding: 6px 20px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #1a7a42, stop:1 #145a30);
            border: 1px solid #208848;
            border-radius: 5px;
            color: #e0ffe0;
            font-weight: bold;
        }
        QPushButton#connectBtn:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #20a055, stop:1 #1a7a42);
        }
        QPushButton#disconnectBtn {
            padding: 6px 20px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #b82830, stop:1 #801820);
            border: 1px solid #c83038;
            border-radius: 5px;
            color: #ffe0e0;
            font-weight: bold;
        }
        QPushButton#disconnectBtn:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #d03040, stop:1 #b82830);
        }
        QPushButton#scanBtn {
            padding: 5px 14px;
            background: #2a2a48;
            border: 1px solid #3a3a58;
            border-radius: 4px;
            color: #c0c0d0;
            font-size: 9pt;
        }
        QPushButton#scanBtn:hover {
            background: #3a3a58;
        }
        QPushButton#scanBtn:disabled {
            background: #1e1e34;
            color: #606080;
        }
        QComboBox {
            padding: 5px 10px;
            background: #1a1a34;
            border: 1px solid #3a3a5a;
            border-radius: 5px;
            color: #e0e0f0;
            font-size: 9pt;
        }
        QComboBox:hover {
            border: 1px solid #4a4a6a;
        }
        QComboBox:focus, QComboBox:on {
            border: 1px solid #00b4d8;
        }
        QComboBox::drop-down {
            border: none;
            width: 20px;
        }
        QComboBox QAbstractItemView {
            background: #1e1e38;
            border: 1px solid #2a2a4a;
            color: #e0e0f0;
            selection-background-color: #00b4d8;
            selection-color: #0a0a18;
            outline: none;
        }
        QScrollBar:vertical {
            background: #16162e;
            width: 8px;
            margin: 0;
            border-radius: 4px;
        }
        QScrollBar::handle:vertical {
            background: #3a3a5a;
            min-height: 30px;
            border-radius: 4px;
        }
        QScrollBar::handle:vertical:hover {
            background: #4a4a6a;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0;
        }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
            background: none;
        }
        QScrollBar:horizontal {
            background: #16162e;
            height: 8px;
            margin: 0;
            border-radius: 4px;
        }
        QScrollBar::handle:horizontal {
            background: #3a3a5a;
            min-width: 30px;
            border-radius: 4px;
        }
        QScrollBar::handle:horizontal:hover {
            background: #4a4a6a;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            width: 0;
        }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
            background: none;
        }
    """)

    pg.setConfigOptions(
        background=(26, 26, 42),
        foreground=(190, 190, 210),
        antialias=True,
    )

    window = MainWindow()
    if elf_path:
        window._elf_path = Path(elf_path)
        window._load_variables()
        window.setWindowTitle(f"LoopMaster Scope — {Path(elf_path).name}")

    if pack_path:
        window._pack_path = pack_path

    if target:
        window._backend.connect(target=target, pack=pack_path)

    window.show()
    sys.exit(app.exec())
