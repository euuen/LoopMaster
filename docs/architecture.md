# LoopMaster 架构文档

> 从 ELF 解析到 GUI 显示的完整数据流、逻辑流与状态变化

---

## 目录

1. [项目文件结构](#1-项目文件结构)
2. [数据模型：类型系统](#2-数据模型类型系统)
3. [第 1 阶段：固件解析（一次性）](#3-第-1-阶段固件解析一次性)
4. [第 2 阶段：变量清单生成（一次性）](#4-第-2-阶段变量清单生成一次性)
5. [第 3 阶段：GUI 树构建（按需触发）](#5-第-3-阶段gui-树构建按需触发)
6. [第 4 阶段：GUI 交互 → 状态变化](#6-第-4-阶段gui-交互--状态变化)
7. [第 5 阶段：采样管道启动](#7-第-5-阶段采样管道启动)
8. [第 6 阶段：快路径构建](#8-第-6-阶段快路径构建)
9. [第 7 阶段：采样循环（运行时心脏）](#9-第-7-阶段采样循环运行时心脏)
10. [第 8 阶段：数据读取后端](#10-第-8-阶段数据读取后端)
11. [第 9 阶段：数据显示](#11-第-9-阶段数据显示)
12. [第 10 阶段：空闲预览](#12-第-10-阶段空闲预览)
13. [第 11 阶段：配置持久化](#13-第-11-阶段配置持久化)
14. [完整依赖图谱](#14-完整依赖图谱)
15. [关键设计决策汇总](#15-关键设计决策汇总)

---

## 1. 项目文件结构

```
LoopMaster/
├── main.py                          # 入口：CLI vs GUI 路由
├── loopmaster.json                  # GUI 配置持久化
├── config/settings.yaml             # 默认配置
│
└── src/
    ├── core/
    │   ├── models.py                # 数据模型定义
    │   ├── mem_backend.py           # SWD 内存读取后端
    │   └── collector.py             # RingBuffer + 采样控制器
    │
    ├── parser/
    │   ├── elf_parser.py            # ELFParser 封装
    │   ├── readelf.py               # arm-none-eabi-readelf 调用 + DWARF 解析
    │   ├── variable_inventory.py    # ELF + DWARF 合并变量清单
    │   ├── struct_layout.py         # 结构体内存布局引擎
    │   └── map_parser.py            # .map 文件解析
    │
    ├── ui/
    │   ├── cli.py                   # 命令行界面
    │   └── gui.py                   # PySide6 示波器 GUI
    │
    └── utils/
        └── exporters.py             # CSV/JSON/Excel/Rich 输出
```

---

## 2. 数据模型：类型系统

> **文件**：`src/core/models.py`（共 78 行）

所有调试类型通过 `TypeInfo` 联合类型表示，定义了一种可以递归描述 C 语言变量类型的 DSL：

```python
# models.py:71
TypeInfo = Union[
    BaseType,     # 基础类型: int, float, char...
    StructType,   # 结构体/联合体: 含成员列表，可递归展开
    ArrayType,    # 数组: 元素类型 + 计数
    PointerType,  # 指针: 指向的类型 + 指针自身大小
    EnumType,     # 枚举: 名值对列表
    TypedefType,  # typedef 别名: 指向底层真实类型
    FuncType,     # 函数类型
]
```

核心数据类：

```python
# models.py:73-78
@dataclass
class Variable:
    name: str                          # 变量名
    address: int                       # 编译时固定的 RAM 地址
    size: int                          # 字节大小
    type_info: Optional[TypeInfo] = None   # 完整的类型树（DWARF 提供）
    symbol: Optional[Symbol] = None    # ELF 符号表的原始条目
    file_name: str = ""                # 源文件路径
```

### 一句话

> `Variable` 是贯穿整个系统的核心数据结构——`address` 是和探针通讯的硬件坐标，`type_info` 是驱动 GUI 树展开和采样解码的类型蓝图。

---

## 3. 第 1 阶段：固件解析（一次性）

### 3.1 入口

```
gui.py:575  _on_import_elf()
  → QFileDialog 选择 .elf/.axf 文件
  → self._elf_path = Path(path)                     # gui.py:576
  → self._load_variables()                          # gui.py:577
```

### 3.2 三步解析

```
gui.py:581-596  _load_variables()
  │
  ├─ elf = ELFParser(self._elf_path)                # gui.py:582
  │  elf.open()
  │
  ├─ dwarf_db = parse_debug_info(self._elf_path)    # gui.py:584
  │
  ├─ map_path = self._find_map_file()               # gui.py:587-589
  │  symbol_to_file = parse_map_file(map_path)
  │
  └─ inventory = VariableInventory(elf, dwarf_db, symbol_to_file)
     self._variables = inventory.generate()         # gui.py:596
     ← 结果保存在实例变量中
```

### 3.3 ELF 符号解析

> **文件**：`src/parser/readelf.py`，行 89-116
> **函数**：`parse_symbol_table(filepath)`

```python
# readelf.py:89-116
def parse_symbol_table(filepath):
    text = run_readelf(filepath, "-s")       # 调用 arm-none-eabi-readelf -s
    symbols = []
    for line in text.splitlines():
        m = _SYM_RE.match(line)             # 正则解析符号行
        symbols.append(Symbol(
            name=name,                      # 变量名
            address=hex,                    # 地址
            size=int,                       # 大小
            sym_type=type,                  # OBJECT / FUNC / NOTYPE
            binding=bind,                   # LOCAL / GLOBAL
        ))
    return symbols
```

> 一句话：调用 `arm-none-eabi-readelf -s` 解析 `.symtab` 节，获得所有符号的**名称、地址、大小、类型**。

### 3.4 DWARF 调试信息解析

> **文件**：`src/parser/readelf.py`，行 195-270
> **函数**：`parse_debug_info(filepath)`

```python
# readelf.py:195-270
def parse_debug_info(filepath):
    text = run_readelf(filepath, "-wi")     # 调用 arm-none-eabi-readelf -wi
    if "Contents of the .debug_info section" not in text:
        return DwarfDB()                    # 返回空数据库

    # 解析所有 DIE（Debug Information Entry），构建树
    all_dies: dict[int, RawDie] = {}
    for line in text.splitlines():
        m = _DIE_RE.match(line)
        if m:
            level, offset, tag = m.groups()
            # 构造 DIE 树（含父子关系）

    # Pass 1: 构建类型索引（py:224-226）
    for offset, die in all_dies.items():
        db.types[offset] = _die_to_type_info(die, all_dies, set())

    # Pass 2: 收集命名的 StructType（py:228-231）
    for offset, die in all_dies.items():
        ti = db.types.get(offset)
        if isinstance(ti, StructType) and ti.name:
            db.structs[ti.name] = ti

    # Pass 2.5: 链接 typedef → 匿名 struct（py:234-241）
    for offset, die in all_dies.items():
        ti = db.types.get(offset)
        if isinstance(ti, TypedefType):
            ut = ti.underlying_type
            if isinstance(ut, StructType) and ti.name not in db.structs:
                db.structs[ti.name] = ut

    # Pass 3: 收集全局变量（py:243-270）
    for offset, die in all_dies.items():
        if die.tag != "DW_TAG_variable":
            continue
        addr = _parse_location(die.attrs.get("DW_AT_location", ""))
        if addr == 0:
            continue                        # 过滤局部变量（栈/寄存器）
        db.variables.append(Variable(name, addr, size, type_info, file_name))

    return db
```

### 3.5 DWARF 类型树构建（`_die_to_type_info`）

> **文件**：`src/parser/readelf.py`，行 287-430
> **函数**：`_die_to_type_info(die, all_dies, visiting)`

```python
# readelf.py:287-430
def _die_to_type_info(die, all_dies, visiting):
    tag = die.tag                                   # DIE 的标签

    if tag == "DW_TAG_base_type":                   # → BaseType          py:295-300
        return BaseType(name, byte_size, encoding)

    if tag in ("DW_TAG_structure_type",             # → StructType        py:302-323
               "DW_TAG_union_type"):
        members = []
        for child in die.children:
            if child.tag in ("DW_TAG_member", "DW_TAG_inheritance"):
                m_type = _die_to_type_info(all_dies[type_ref], ...)
                members.append(MemberInfo(name, offset, m_type))
        return StructType(name, size, members, is_union)

    if tag == "DW_TAG_enumeration_type":            # → EnumType          py:381-392
        return EnumType(name, size, enumerators)

    if tag == "DW_TAG_typedef":                     # → TypedefType       py:393-397
        underlying = _die_to_type_info(all_dies[type_ref], ...)
        return TypedefType(name, underlying)

    if tag == "DW_TAG_pointer_type":                # → PointerType       py:398-406
        pointed = _die_to_type_info(all_dies[type_ref], ...)
        return PointerType(pointed_type=pointed, size=4)    # ARM 32-bit

    if tag == "DW_TAG_array_type":                  # → ArrayType         py:408-423
        return ArrayType(element_type, count, total_size)

    if tag == "DW_TAG_subroutine_type":             # → FuncType          py:427-432
        return FuncType(return_type)
```

> 一句话：DWARF DIE 树递归遍历，把每个编译单元的类型定义翻译成 `TypeInfo` 对象树。

### 3.6 局部变量过滤

> **文件**：`src/parser/readelf.py`，行 502-508
> **函数**：`_parse_location(raw)`

```python
# readelf.py:502-508
def _parse_location(raw):
    m = _OP_ADDR_RE.search(raw)     # 只匹配 "DW_OP_addr: 20000000"
    if m:
        return int(m.group(1), 16)
    return 0                        # 栈/寄存器表达式 → 返回 0
```

> 一句话：只解析有固定地址（`DW_OP_addr`）的变量，栈上的局部变量和寄存器变量一律返回 0 并被过滤掉。

### 3.7 第 1 阶段状态变化

```
之前:
  self._elf_path = None
  self._variables = []

之后:
  self._elf_path = Path("firmware.elf")
  self._variables = [
    Variable(name="pid_x",  address=0x20000100, type_info=BaseType("float",4)),
    Variable(name="pid_y",  address=0x20000104, type_info=BaseType("float",4)),
    Variable(name="pid_ctrl", address=0x20000110, type_info=StructType("PID", ...)),
    Variable(name="p_ptr",  address=0x20000130, type_info=PointerType(pointed=StructType("PID"))),
    ...
  ]
```

---

## 4. 第 2 阶段：变量清单生成（一次性）

> **文件**：`src/parser/variable_inventory.py`，行 16-113
> **函数**：`VariableInventory.generate()`

```python
# variable_inventory.py:16-113
def generate(self):
    symbols = self.elf_parser.get_symbols()         # ELF 符号表

    # DWARF 变量按地址和名称建索引
    for v in self.dwarf_db.variables:
        dwarf_by_addr[v.address] = v
        dwarf_by_name[v.name] = v

    variables = []
    for sym in symbols:
        if sym.sym_type != "OBJECT":                # 只取全局数据变量
            continue
        if sym.address == 0:
            continue

        # 优先取 DWARF 的类型信息
        dv = dwarf_by_addr.get(sym.address) or dwarf_by_name.get(sym.name)
        if dv and dv.type_info:
            type_info = dv.type_info                # DWARF 提供的完整类型
            size = dv.size
        else:
            size = sym.size                         # 只有符号表大小

        # LTO 去重、地址去重（py:49-68）
        ...

        variables.append(Variable(name, address, size, type_info, sym, file_name))

    # 补充 DWARF-only 变量（py:85-111）
    ...

    variables.sort(key=lambda v: v.address)
    return variables
```

> 一句话：ELF 符号表提供（名称+地址），DWARF 提供（类型信息+源文件），VariableInventory 按地址匹配合并两者，处理 LTO 去重。

---

## 5. 第 3 阶段：GUI 树构建（按需触发）

### 5.1 入口

```
gui.py:620-680  _populate_tree()
  │
  ├─ 触发时机：
  │   ├─ _load_variables() 完成后（gui.py:598）
  │   ├─ 搜索框输入变更（gui.py:647，_on_filter_changed）
  │   └─ 启动时加载配置后
  │
  ├─ self._tree.clear()                            # gui.py:629
  ├─ self._registry.clear()                        # gui.py:630
  │
  ├─ 过滤                                   # gui.py:632-645
  │
  ├─ 按 file_name 分组创建文件夹节点         # gui.py:649-671
  │
  ├─ 对每个变量: _add_variable_item(v)       # gui.py:657/671
  │
  └─ 恢复上次勾选（从 loopmaster.json）        # gui.py:677-681
```

### 5.2 `_add_variable_item`——GUI 树的核心展开逻辑

> **文件**：`src/ui/gui.py`，行 695-774

```python
# gui.py:695-774
def _add_variable_item(self, v, depth, parent_item, path_prefix):
    concrete = resolve_type(v.type_info)            # py:698
    #           └── gui.py:80-83 只解包 TypedefType，不透传 PointerType
    is_struct = isinstance(concrete, StructType)    # py:699

    if is_struct and depth < MAX_STRUCT_DEPTH and concrete.members:
        # ── 展开结构体 ──
        item = QTreeWidgetItem(...)
        item.setData(ROLE_PATH, full_path)          # py:708
        item.setData(ROLE_ADDR, v.address)          # py:709
        item.setData(ROLE_TYPE, v.type_info)        # py:710

        for member in sorted(concrete.members, key=lambda m: m.offset):
            member_addr = v.address + member.offset # py:725 ← 静态地址
            member_concrete = resolve_type(member.type_info)

            if isinstance(member_concrete, StructType) and depth+1 < MAX_STRUCT_DEPTH:
                # 递归展开嵌套结构体                             # py:729-734
                ...
            else:
                # 叶子节点                                       # py:736-759
                child.setData(ROLE_PATH, member_path)
                child.setData(ROLE_ADDR, member_addr)
                child.setData(ROLE_TYPE, member.type_info)
                self._registry[member_path] = (member_addr, member.type_info)
                #                     ↑ 路径字符串 → (固定地址, 类型)

    else:
        # ── 叶子节点（非结构体，或深度超限）                      # py:761-774
        item = QTreeWidgetItem(...)
        item.setData(ROLE_PATH, full_path)
        item.setData(ROLE_ADDR, v.address)
        item.setData(ROLE_TYPE, v.type_info)
        self._registry[full_path] = (v.address, v.type_info)
```

### 5.3 `resolve_type`——类型解包

> **文件**：`src/ui/gui.py`，行 80-83

```python
# gui.py:80-83
def resolve_type(ti):
    """解包 TypedefType，返回最终的真实类型。"""
    while isinstance(ti, TypedefType):
        ti = ti.underlying_type
    return ti
```

> 一句话：递归解包所有 `typedef` 链，暴露最内层的真实类型（但当前不解包 `PointerType`）。

### 5.4 第 3 阶段状态变化

```
之前:
  self._registry = {}                  # 空的
  self._tree = (空 QTreeWidget)

之后:
  self._registry = {
    "pid_x":           (0x20000100, BaseType("float")),
    "pid_y":           (0x20000104, BaseType("float")),
    "pid_ctrl":        (0x20000108, StructType("PID_TypeDef")),
    "pid_ctrl.Target": (0x20000108, BaseType("float")),   ← 结构体成员展开
    "pid_ctrl.Measure":(0x2000010C, BaseType("float")),
    "p_ptr":           (0x20000120, PointerType(StructType("PID"))),
    ...
  }

  self._tree = QTreeWidget 填充了变量名、地址、类型列
```

---

## 6. 第 4 阶段：GUI 交互 → 状态变化

### 6.1 用户勾选变量

> **文件**：`src/ui/gui.py`，行 783-793

```python
# gui.py:783-793
def _on_selection_changed(self):
    # 遍历所有选中的 QTreeWidgetItem
    selected_paths = set()
    for item in self._tree.selectedItems():
        path = item.data(0, ROLE_PATH)
        if path is not None:
            selected_paths.add(path)

    self._monitored = selected_paths                # ★ 核心状态更新
    self._update_selected_list()                    # 更新 tab 标题
    self._idle_read()                               # 触发即时预览
```

### 6.2 搜索过滤

> **文件**：`src/ui/gui.py`，行 645-647

```python
# gui.py:645-647
def _on_filter_changed(self):
    self._populate_tree()                           # 重建整棵树
```

> 一句话：搜索框每次输入都重建 GUI 树（含恢复勾选状态）。

### 6.3 第 4 阶段状态变化

```
之前:
  self._monitored = set()               # 空

用户勾选了 pid_x 和 pid_ctrl.Target:
  self._monitored = {"pid_x", "pid_ctrl.Target"}

  tab 标题: "示波器 (2)"
  实时值表格: 显示 pid_x 和 pid_ctrl.Target 的当前值
```

---

## 7. 第 5 阶段：采样管道启动

> **文件**：`src/ui/gui.py`，行 923-996

```python
# gui.py:923-996
def _on_start(self):
    # 前置检查
    if not self._elf_path:                          # py:924
    if not self._backend.is_connected:              # py:927

    # 从 _registry 构建 _monitor_list
    self._monitor_list = []                         # py:931
    for path in sorted(self._monitored):            # py:932
        info = self._registry.get(path)             # py:933 ← 从 bridge 取(addr,ti)
        addr, ti = info
        self._monitor_list.append((path, addr, ti))

    # 配置采集器
    self._collector.configure(rate, buffer_seconds) # py:947
    self._collector.set_variables(self._monitor_list)   # py:948
    # → 为每个变量创建 RingBuffer

    # 构建快路径（关键！）
    self._setup_fast_path()                         # py:950

    # 启动采样循环
    if rate >= 500:                                 # 高频模式
        QTimer.singleShot(0, self._tight_sample_loop)
    else:                                           # 低频模式
        self._sample_timer.setInterval(interval_ms)
        self._sample_timer.start()
```

### 第 5 阶段状态变化

```
之前:
  self._monitor_list = []
  collector._buffers = {}
  collector._running = False

之后:
  self._monitor_list = [
    ("pid_x",           0x20000100, BaseType("float")),
    ("pid_ctrl.Target", 0x20000108, BaseType("float")),
  ]
  collector._buffers = {
    "pid_x":            RingBuffer(30000),
    "pid_ctrl.Target":  RingBuffer(30000),
  }
  collector._running = True
```

> 一句话：`_monitored`（纯路径字符串） + `_registry`（路径→地址/类型） → `_monitor_list`（(路径,固定地址,类型)元组列表）→ 创建 RingBuffer → 构建快路径。

---

## 8. 第 6 阶段：快路径构建

> **文件**：`src/ui/gui.py`，行 1055-1101
> **函数**：`_setup_fast_path()`

这是整个系统**性能最关键**的函数。它在采样开始前把所有运行时决策做完。

### 8.1 缓存热引用

```python
# gui.py:1059-1066
c = self._collector
ap = self._backend._ap                  # MEM-AP 对象
decoder = self._backend._decoder
self._fast_ap = ap                      # 消除 self._backend._ap 链
self._fast_bufs = c._buffers            # 消除 collector._buffers 字典查找
self._fast_ts = c._timestamps
```

> 一句话：把深层次属性引用提前查好存到 `self._fast_*` 上，采样循环中省去属性链遍历。

### 8.2 类型编译为原生参数

```python
# gui.py:1074-1089
for name, addr, ti in self._monitor_list:
    wa, bo, w, wc, sgn, flt = decoder.make_plan(addr, ti)  # ★ 类型→6个数字
    buf = c._buffers.get(name)                              # 对应的 RingBuffer
    all_plans.append((wa, bo, w, wc, sgn, flt, buf))

    # 分类：direct / complex / cross-word
    if wc <= 1 and bo == 0 and w == 4 and not sgn and not flt:
        self._fast_direct.append((buf, wa))
    elif wc <= 1:
        self._fast_complex.append((buf, wa, bo, w, sgn, flt))
    else:
        self._fast_complex.append((buf, addr, 0, w, sgn, flt, True))  # 7元素
```

> 一句话：`make_plan` 把类型信息编译成 6 个原生数字，然后按复杂度分入 direct/complex/跨字三类，采样循环直接遍历无需 `isinstance`。

### 8.3 合并块读取

```python
# gui.py:1092-1100
if len(all_plans) >= 2:
    all_plans.sort(key=lambda x: x[0])              # 按地址排序
    first_wa = all_plans[0][0]
    last_end = last_plan[0] + last_plan[3] * 4
    total_words = (last_end - first_wa) // 4        # 跨度
    if 2 <= total_words <= 64:                      # 跨度在合理范围内
        self._fast_block = (first_wa, total_words, all_plans)
```

> 一句话：如果多个变量的地址在 64 个字（256 字节）内，合并为单次 `read_memory_block32` 调用，N 次 USB 事务变 1 次。

### 8.4 第 6 阶段状态变化

```
之前:
  self._fast_direct = []
  self._fast_complex = []
  self._fast_block = None

之后（假设 pid_x 和 pid_ctrl.Target）:
  self._fast_direct = [
    (RingBuffer("pid_x"),            0x20000100),
    (RingBuffer("pid_ctrl.Target"),  0x20000108),
  ]                                   ← 两者都是对齐 uint32 → direct
  self._fast_complex = []
  self._fast_block = (0x20000100, 3, [(0x20000100,0,4,1,F,F,buf_pid_x),
                                      (0x20000108,0,4,1,F,F,buf_target)])
  # 一次读 3 个字（0x20000100-0x2000010B），覆盖两个变量
```

---

## 9. 第 7 阶段：采样循环（运行时心脏）

LoopMaster 有两种采样模式，根据采样率自动选择。

### 9.1 低频模式（< 500Hz）：QTimer 精确间隔

> **文件**：`src/ui/gui.py`，行 1106-1174
> **定时器**：gui.py:141-142，`_sample_timer.timeout.connect(_on_sample_tick)`

```python
# gui.py:1106-1174
def _on_sample_tick(self):
    tick_start = time.perf_counter()
    ap = self._fast_ap
    block = self._fast_block

    # ── 优先：块读取 ──
    if block is not None:
        block_start, block_words, block_plans = block
        words = ap.read_memory_block32(block_start, block_words)  # 1次USB
        for wa, bo, w, wc, sgn, flt, buf in block_plans:
            idx = (wa - block_start) // 4
            buf.append(_extract_val(words, idx, bo, w, sgn, flt)) # 纯CPU
        ts_deque.append(tick_start - t0)
        return

    # ── 回退：逐个读取 ──
    for buf, wa in self._fast_direct:         # 对齐 uint32 路径
        buf.append(float(ap.read_memory(wa, 32)))
    for item in self._fast_complex:           # 偏移/有符号/浮点/跨字
        ...
```

> 一句话：每个定时器周期执行一次采样，优先块读取（1 次 USB 拿全部变量），失败则按 direct/complex 分类逐个读。

### 9.2 高频模式（≥ 500Hz）：无限制自调度

> **文件**：`src/ui/gui.py`，行 1179-1278
> **函数**：`_tight_sample_loop()`

```python
# gui.py:1179-1278
def _tight_sample_loop(self):
    block = self._fast_block

    if block is not None:
        # ── 流水线批读取 ──
        pipe_depth = 48/32/16/8                  # 自适应深度
        all_sample_vals = BACKEND.read_block_pipelined(
            block_start, block_words, block_plans, pipe_depth)
        # 发出 pipe_depth 个延迟请求 → 批量收结果
        for i, sample_vals in enumerate(all_sample_vals):
            for buf, val in zip(block_plans, sample_vals):
                buf.append(val)

    else:
        # ── 20ms 批量窗口 ──
        batch_deadline = time.perf_counter() + 0.020
        while c._running and time.perf_counter() < batch_deadline:
            for buf, wa in self._fast_direct:
                buf.append(float(ap.read_memory(wa, 32)))
            ...

    # 自调度下一轮
    QTimer.singleShot(0, self._tight_sample_loop)  # 不阻塞事件循环
```

> 一句话：`singleShot(0)` 自调度，CPU 满速跑采样，没有固定间隔——块模式用流水线批处理，非块模式用 20ms 时间片窗口。

### 9.3 第 7 阶段数据流向

```
每次采样周期:
  ap.read_memory() 或 ap.read_memory_block32()
        │
        ▼
  _extract_val(words, idx, bo, w, sgn, flt) → float
        │
        ▼
  RingBuffer[变量名].append(float_value)
  RingBuffer[timestamps].append(elapsed_time)
        │
        ▼
  collector._sample_count += 1
  每 50 次采样更新 actual_rate
```

---

## 10. 第 8 阶段：数据读取后端

### 10.1 `read_batch()`——批量读取（空闲预览和采集器回退路径）

> **文件**：`src/core/mem_backend.py`，行 93-128

```python
# mem_backend.py:93-128
def read_batch(self, variables):
    # 预计算/复用读取计划
    if id(variables) != self._plan_cache_key:
        self._plan_cache = [
            (name, self._decoder.make_plan(addr, ti))
            for name, addr, ti in variables
        ]
        self._plan_cache_key = id(variables)

    result = {}
    for name, (wa, bo, w, wc, sgn, flt) in self._plan_cache:
        raw = ap.read_memory(wa, transfer_size=32)
        result[name] = _extract_val(raw, bo, w, sgn, flt)
    return result
```

> 一句话：缓存读取计划，逐变量 `read_memory` + `_extract_val`，N 个变量 = N 次 USB 事务。

### 10.2 `read_block_pipelined()`——流水线批读取

> **文件**：`src/core/mem_backend.py`，行 41-86

```python
# mem_backend.py:41-86
def read_block_pipelined(self, block_start, block_words, block_plans, num_samples):
    # 发出 N 个延迟读取请求（不等待响应）
    cbs = []
    for _ in range(num_samples):
        ap.write_reg(TAR, block_start)           # 设置地址
        cbs.append(dp.read_ap_multiple(DRW, block_words, now=False))
        #                                           ↑ 不阻塞

    # 批量收结果
    results = []
    for cb in cbs:
        words = cb()                             # 此时才等待 USB 完成
        sample_vals = [_extract_val(words, ...) for ... in block_plans]
        results.append(sample_vals)

    return results                               # [[val0,val1,...], ...]
```

> 一句话：一次性发出 N 次读请求（不等待），然后批量收 N 个结果，把 N 次 USB 往返合并为 1 次。

### 10.3 `_extract_val()`——从原始字提取数值

> **文件**：`src/core/mem_backend.py`，行 148-175

```python
# mem_backend.py:148-175
def _extract_val(words, word_idx, byte_offset, width, word_count, is_signed, is_float):
    if isinstance(words, int):                   # 单字快速路径
        raw = (words >> (byte_offset * 8)) & mask
    else:                                        # 多字拼接
        val = 0
        for k in range(word_count):
            val |= (words[word_idx + k] & 0xFFFFFFFF) << (k * 32)
        raw = (val >> (byte_offset * 8)) & mask

    if is_float and width == 4:                  # IEEE 754 浮点
        return struct.unpack('<f', struct.pack('<I', raw))[0]
    if is_signed:                                # 符号扩展
        return float(符号处理)
    return float(raw)                            # 无符号直转
```

> 一句话：纯位运算 + 条件判断，把内存中的原始字节解码为 Python float，没有 `isinstance` 类型判断。

### 10.4 `make_plan()`——类型编译为原生参数

> **文件**：`src/core/mem_backend.py`，行 328-358

```python
# mem_backend.py:328-358
def make_plan(self, address, ti):
    """返回 (word_addr, byte_offset, width, word_count, is_signed, is_float)"""
    if isinstance(ti, TypedefType):              # 递归解包
        return self.make_plan(address, ti.underlying_type)
    if isinstance(ti, BaseType):                 # 按 byte_size 和 encoding
        return self._plan_base(address, ti)
    if isinstance(ti, PointerType) or isinstance(ti, EnumType):  # 4 字节无符号
        return (wa, bo, size, wc, False, False)
    if isinstance(ti, ArrayType):               # 取第一个元素
        return self.make_plan(address, ti.element_type)
    # StructType/FuncType → fallback 4 字节
    return (wa, bo, 4, 1, False, False)
```

> 一句话：递归解包类型树，输出 6 个原生数字，让采样循环不需要理解任何类型系统。

### 10.5 SWD 底层读取

> **文件**：`src/core/mem_backend.py`，行 49-79

```python
# mem_backend.py:49-79
def read(self, address, width):
    ap = self._ap                               # AHB-AP (Memory Access Port)

    if width == 4:                              # 对齐 32-bit
        return ap.read_memory(address, transfer_size=32)

    if width == 1 or width == 2:                # 未对齐：读对齐字+移位
        word_addr = address & ~0x3
        shift = (address & 0x3) * 8
        val = ap.read_memory(word_addr, transfer_size=32)
        return (val >> shift) & mask

    if width == 8:                              # 64-bit：读两字拼接
        low = ap.read_memory(address, transfer_size=32)
        high = ap.read_memory(address + 4, transfer_size=32)
        return (high << 32) | low
```

> 一句话：通过 CoreSight AHB-AP 直接读 MCU 内存地址空间，不暂停 CPU，4 字节对齐时 1 次 USB 事务拿 32-bit 值。

---

## 11. 第 9 阶段：数据显示

### 11.1 波形更新

> **文件**：`src/ui/gui.py`，行 1282-1320
> **定时器**：gui.py:138，`_plot_timer.timeout.connect(_update_plot)`，默认 60FPS

```python
# gui.py:1282-1320
def _update_plot(self):
    # 从 RingBuffer 读取数据
    if self._auto_scroll:
        raw_data = self._collector.get_data(tail_seconds=time_window*1.5)  # py:1288
    else:
        raw_data = self._collector.get_data()                               # py:1292
    # → {name: (np.array(timestamps), np.array(values))}

    # 降采样/插值匹配 FPS
    data = self._process_display_data(raw_data)     # py:1302

    # 更新每条曲线
    for name, curve in self._plot_curves.items():   # py:1295
        if name in data:
            curve.setData(ts, vals)                 # py:1298 ← pyqtgraph 绘图

    # X 轴自动滚动
    if latest_ts > 0 and self._auto_scroll:
        self._plot.setXRange(x_min, latest_ts)      # py:1304-1307

    # 更新状态栏
    self._sb_rate.setText(f"采样: {actual}/{configured} Hz | 显示: {fps} FPS")

    # 更新实时值表格（限频 ~5Hz）
    self._update_value_table(data)                  # py:1314
```

### 11.2 从 RingBuffer 取数据

> **文件**：`src/core/collector.py`，行 108-130

```python
# collector.py:108-130
def get_data(self, tail_seconds=None):
    if tail_seconds:
        cutoff = 最新时间戳 - tail_seconds
        start_idx = self._timestamps.find_ge(cutoff)   # 二分查找 O(log n)
    else:
        start_idx = 0

    result = {}
    for name, buf in self._buffers.items():
        ts_arr = self._timestamps.logical_slice(start_idx, count)  # 无拷贝(view)
        vals_arr = buf.logical_slice(start_idx, count)
        result[name] = (ts_arr, vals_arr)
    return result
```

### 11.3 降采样/插值

> **文件**：`src/ui/gui.py`，行 1404-1437

```python
# gui.py:1404-1437
def _process_display_data(self, data):
    ratio = sample_rate / fps
    if 0.5 <= ratio <= 2.0: return data             # 比率适中，不变

    if ratio < 0.5:                                 # 采样率太低 → 线性插值
        display_ts = np.linspace(t_min, t_max, num_points)
        display_vals = np.interp(display_ts, ts, vals)

    else:                                           # 采样率太高 → 抽取
        step = max(1, int(ratio))
        indices = list(range(0, len(ts), step))
        return (ts[indices], vals[indices])

    return (display_ts, display_vals)
```

> 一句话：采样率远高帧率时降采样，远低帧率时线性插值，保证绘制的数据点数与帧率匹配。

---

## 12. 第 10 阶段：空闲预览

> **文件**：`src/ui/gui.py`，行 1444-1469
> **定时器**：gui.py:145，`_idle_timer.timeout.connect(_idle_read)`，250ms = 4Hz

```python
# gui.py:1444-1469
def _idle_read(self):
    if not self._backend.is_connected: return       # 探针未连接
    if self._collector.is_running: return           # 正在采样，不冲突

    # 从 _registry 构建读取列表
    monitor_list = []
    for path in sorted(self._monitored):
        info = self._registry.get(path)             # (addr, ti)
        monitor_list.append((path, addr, ti))

    raw = self._backend.read_batch(monitor_list)    # 批量读取

    # 填充实时值表格
    data = {}
    for name, val in raw.items():
        data[name] = ([0.0], [val])                 # 伪造时间戳
    self._update_value_table(data)
```

> 一句话：没按 START 时，每 250ms 读一次当前勾选变量的值，填入 QTableWidget 实时显示。

---

## 13. 第 11 阶段：配置持久化

### 13.1 保存

> **文件**：`src/ui/gui.py`，行 1525-1537

```python
# gui.py:1525-1537
def _save_config(self):
    cfg = {
        "elf_path": str(self._elf_path),            # ELF 文件路径
        "sample_rate": self._rate_combo.currentData(),
        "frame_rate": self._frame_rate,
        "swd_freq_index": self._swd_freq_combo.currentIndex(),
        "connect_mode_index": self._mode_combo.currentIndex(),
        "y_auto": self._y_auto_btn.isChecked(),
        "monitored_variables": sorted(self._monitored),  # ← 纯路径字符串
    }
    json.dump(cfg, open("loopmaster.json", "w"))

# gui.py:1544-1552  closeEvent()
    self._on_stop()                                 # 停止采样
    self._save_config()                             # 保存配置
    self._backend.disconnect()                      # 断开探针
```

### 13.2 恢复

> **文件**：`src/ui/gui.py`，行 148-161

```python
# gui.py:148-161  __init__()
    cfg = self._load_config()                       # 读取 loopmaster.json
    if cfg:
        elf = cfg.get("elf_path", "")
        if elf and Path(elf).exists():
            self._elf_path = Path(elf)
            self._load_variables()                  # 自动载入变量
            # 恢复采样率、帧率、SWD 频率等设置
            self._restore_selected_items(self._tree.invisibleRootItem(), saved_vars)
```

### 13.3 恢复勾选

> **文件**：`src/ui/gui.py`，行 1545-1551

```python
# gui.py:1545-1551
def _restore_selected_items(self, parent, saved):
    for i in range(parent.childCount()):
        item = parent.child(i)
        path = item.data(0, ROLE_PATH)              ← 从 QTreeWidgetItem 取路径
        if path in saved:
            item.setSelected(True)
            self._monitored.add(path)
        self._restore_selected_items(item, saved)
```

> 一句话：保存时只存路径字符串（不存地址和类型），恢复时按路径在 QTreeWidget 中匹配勾选——所以路径字符串是跨会话的唯一标识。

---

## 14. 完整依赖图谱

```
loopmaster.json                           ELF/AXF 文件 (.elf/.axf)
(持久化配置)                                  │
     ▲                                       ▼
     │                          parser/readelf.py:89
     │                          parse_symbol_table()
     │                               │
     │                          parser/readelf.py:195
     │                          parse_debug_info()
     │                               │
     │                          parser/variable_inventory.py:16
     │                          VariableInventory.generate()
     │                               │
     │                          gui.py:596
     │                          self._variables: list[Variable]
     │                               │
     │                          gui.py:620
     │                          _populate_tree()
     │                               │
     │                          gui.py:123
     │                          self._registry: dict[str → (addr, TypeInfo)]
     │                               │
     │                          QTreeWidget (GUI 显示)
     │                               │  用户勾选
     │                          gui.py:791
     │                          self._monitored: set[str]
     │                               │
     │                          gui.py:931-936
     │                          self._monitor_list: [(path, addr, ti), ...]
     │                               │
     │                          gui.py:950, 1055
     │                          _setup_fast_path()
     │                               │
     │                          gui.py:1069-1100
     │  ┌─ self._fast_direct ── self._fast_complex ── self._fast_block
     │  │        │                      │                    │
     │  │        ▼                      ▼                    ▼
     │  │  gui.py:1150         gui.py:1154-1161     gui.py:1123-1134
     │  │  ap.read_memory      ap.read_memory       ap.read_memory_block32
     │  │  + float()           + _extract_val()     + _extract_val()
     │  │        │                      │                    │
     │  │        └──────────┬───────────┘────────────────────┘
     │  │                   ▼
     │  │  RingBuffer.append(float_value)
     │  │  RingBuffer.append(timestamp)
     │  │                   │
     │  │  gui.py:1288-1292 │  (每帧 ~16.7ms)
     │  │  collector.get_data(tail_seconds)
     │  │                   │
     │  │  gui.py:1295-1298
     │  │  curve.setData(ts, vals)  ← pyqtgraph 绘图
     │  │
     │  └───── gui.py:1465 (空闲预览)
     │         swd_backend.read_batch(monitor_list)
     │                   │
     │                   ▼
     │         _update_value_table(data) ← QTableWidget
     │
     └=========== gui.py:1525 ==========
                _save_config() → loopmaster.json
```

---

## 15. 关键设计决策汇总

| 决策 | 位置 | 为什么 |
|:----|:-----|:-------|
| `_setup_fast_path()` 预计算 | gui.py:1055-1101 | 消除采样循环中的类型判断、属性查找、地址计算，让热路径只剩 `ap.read_memory` + `buf.append` |
| `make_plan` 输出 6 个数字 | mem_backend.py:328-358 | 把 `TypeInfo` 递归类型树"编译"成纯原生参数，采样循环无需理解类型系统 |
| direct/complex/block 三级分类 | gui.py:1082-1100 | 对齐 uint32 走最快路径（2 行代码），偏移/有符号走中等路径，跨字回退 |
| 块读取合并 | gui.py:1092-1100 | 相邻地址变量一次 `read_memory_block32` 替代多次 `read_memory`，N 次 USB 变 1 次 |
| 流水线批处理高频模式 | mem_backend.py:41-86 | 发出 N 个请求不等待→批量收结果，将 N 次 USB 往返合并为 1 次 |
| `_registry` 作 bridge | gui.py:759/774 | 分离 GUI 树（QTreeWidget）和采样引擎（address+TypeInfo），树重建时只需重建 registry |
| 路径字符串作唯一标识 | gui.py:791 | `_monitored` 存 `"pid_ctrl.Target"` 而非地址或对象引用，配置持久化天然支持 |
| 只解析 `DW_OP_addr` | readelf.py:502-508 | 只读固定地址的全局变量，跳过栈/寄存器变量，简化地址模型 |
| `resolve_type` 不解 PointerType | gui.py:80-83 | PointerType 需要运行时解引用读取，当前地址模型不支持，是已知限制 |

---

> 文档版本：v1.0
> 最后更新：2026-06-05
> 基于 LoopMaster 源码分析