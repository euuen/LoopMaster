"""SWD memory reader via pyOCD + DAPLink — non-intrusive MEM-AP access."""

import struct
from typing import Optional

from src.core.models import (
    BaseType, StructType, ArrayType, PointerType, EnumType, TypedefType, TypeInfo,
)


class SWDBackend:
    def __init__(self):
        self._session = None
        self._target = None
        self._ap = None          # MEM-AP (AHB-AP) for direct non-intrusive reads
        self._connected = False
        self._swd_freq = 4_000_000
        self._decoder = None
        # 预计算的读取计划缓存: {id(variables): [(name, plan), ...]}
        self._plan_cache_key = 0
        self._plan_cache: list[tuple[str, tuple]] = []

    @staticmethod
    def scan_probes() -> list[dict]:
        from pyocd.probe.aggregator import DebugProbeAggregator
        probes = DebugProbeAggregator.get_all_connected_probes()
        result = []
        for p in probes:
            result.append({
                "uid": p.unique_id,
                "name": p.product_name,
                "vendor": p.vendor_name,
            })
        return result

    def connect(self, target: str = None, pack: str = None, freq: int = 4_000_000,
                connect_mode: str = "attach", probe_index: int = 0) -> bool:
        from pyocd.probe.aggregator import DebugProbeAggregator
        from pyocd.core.session import Session

        try:
            probes = DebugProbeAggregator.get_all_connected_probes()
            if not probes:
                print("未找到探针")
                return False

            if probe_index < len(probes):
                dap_probe = probes[probe_index]
            else:
                dap_probe = probes[0]

            self._swd_freq = freq

            opts = {
                "frequency": freq,
                "connect_mode": "attach",  # 始终用 attach，手动处理 reset
            }
            if target:
                opts["target_override"] = target

            self._session = Session(probe=dap_probe, options=opts)
            self._session.open()
            self._target = self._session.target

            # 复位启动：先复位再运行
            if connect_mode == "reset" and self._target:
                try:
                    self._target.reset_and_halt()
                    self._target.resume()
                except Exception as e:
                    print(f"复位后启动失败: {e}")

            # Get MEM-AP for direct non-intrusive memory access
            if self._target.aps:
                self._ap = list(self._target.aps.values())[0]
            else:
                print("未找到 MEM-AP")
                self._session.close()
                return False

            self._connected = True
            self._plan_cache_key = 0  # 新连接，清除计划缓存
            return True
        except Exception as e:
            print(f"SWDBackend 连接失败: {e}")
            self._connected = False
            return False

    def disconnect(self):
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        self._target = None
        self._ap = None
        self._connected = False
        self._plan_cache_key = 0

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ap is not None

    @property
    def target_name(self) -> str:
        if self._target:
            try:
                return self._target.part_number or "cortex_m"
            except Exception:
                pass
        return ""

    @property
    def swd_freq_khz(self) -> int:
        """实际 SWD 频率 (kHz)。"""
        return self._swd_freq // 1000

    # ---- 底层读取 (不再加锁，主线程专用) ----

    def read(self, address: int, width: int) -> int:
        """通过 MEM-AP 直接读取内存。"""
        if not self._ap:
            raise RuntimeError("未连接探针")
        ap = self._ap

        if width == 1:
            word_addr = address & ~0x3
            shift = (address & 0x3) * 8
            val = ap.read_memory(word_addr, transfer_size=32)
            return (val >> shift) & 0xFF
        elif width == 2:
            word_addr = address & ~0x3
            shift = (address & 0x3) * 8
            val = ap.read_memory(word_addr, transfer_size=32)
            if shift <= 16:
                return (val >> shift) & 0xFFFF
            else:
                high = ap.read_memory(word_addr + 4, transfer_size=32)
                return ((high & 0xFF) << 8) | ((val >> 24) & 0xFF)
        elif width == 4:
            return ap.read_memory(address, transfer_size=32)
        elif width == 8:
            low = ap.read_memory(address, transfer_size=32)
            high = ap.read_memory(address + 4, transfer_size=32)
            return (high << 32) | low
        else:
            raise ValueError(f"不支持的读取宽度: {width}")

    def write(self, address: int, value: int, width: int) -> None:
        """通过 MEM-AP 直接写入内存。"""
        if not self._ap:
            raise RuntimeError("未连接探针")
        ap = self._ap

        if width == 4:
            ap.write_memory(address, value, transfer_size=32)
        elif width == 2:
            word_addr = address & ~0x3
            shift = (address & 0x3) * 8
            mask = 0xFFFF << shift
            old = ap.read_memory(word_addr, transfer_size=32)
            ap.write_memory(word_addr, (old & ~mask) | ((value & 0xFFFF) << shift), transfer_size=32)
        elif width == 1:
            word_addr = address & ~0x3
            shift = (address & 0x3) * 8
            mask = 0xFF << shift
            old = ap.read_memory(word_addr, transfer_size=32)
            ap.write_memory(word_addr, (old & ~mask) | ((value & 0xFF) << shift), transfer_size=32)
        elif width == 8:
            ap.write_memory(address, value & 0xFFFFFFFF, transfer_size=32)
            ap.write_memory(address + 4, (value >> 32) & 0xFFFFFFFF, transfer_size=32)
        else:
            raise ValueError(f"不支持的写入宽度: {width}")

    def read_variable(self, address: int, type_info: TypeInfo) -> float:
        if self._decoder is None:
            self._decoder = _TypeDecoder(self)
        return self._decoder.decode(address, type_info)

    # ---- 流水线批量读取（跨样本流水线，消除 USB 往返延迟） ----

    def read_block_pipelined(self, block_start: int, block_words: int,
                             block_plans: list[tuple], num_samples: int = 8):
        """流水线批量读取 — 一次发出 N 个 block read 命令，再批量收结果。

        消除每个样本独立的 USB 往返 (~2ms)，将 N 次读取的 USB 延迟合并为一次。
        返回: [[val0, val1, ...], ...] 每个样本一组值，顺序与 block_plans 一致。
        """
        from pyocd.coresight.ap import MEM_AP_CSW, MEM_AP_TAR, MEM_AP_DRW, CSW_SIZE32

        if not self._ap:
            raise RuntimeError("未连接探针")

        ap = self._ap
        dp = self._session.target.dp

        ap_addr = ap.address.address
        reg_off = ap._reg_offset
        csw_val = ap._csw | CSW_SIZE32
        dr_addr = ap_addr + reg_off + MEM_AP_DRW
        csw_reg = reg_off + MEM_AP_CSW
        tar_reg = reg_off + MEM_AP_TAR

        # CSW 只写一次，所有样本共用
        ap.write_reg(csw_reg, csw_val)

        # 发出 N 个延迟读取
        cbs = []
        for _ in range(num_samples):
            ap.write_reg(tar_reg, block_start)
            cbs.append(dp.read_ap_multiple(dr_addr, block_words, now=False))

        # 批量收结果并提取变量值
        results = []
        for cb in cbs:
            words = cb()
            if not isinstance(words, list):
                words = list(words)
            sample_vals = []
            for wa, bo, w, wc, sgn, flt, _buf in block_plans:
                idx = (wa - block_start) // 4
                if wc <= 1:
                    val = _extract_val(words, word_idx=idx, byte_offset=bo,
                                      width=w, is_signed=sgn, is_float=flt)
                else:
                    val = float(self.read(wa + bo, w))
                sample_vals.append(val)
            results.append(sample_vals)

        return results

    # ---- 批量读取（预计算计划，快速路径） ----

    def read_batch(self, variables: list[tuple[str, int, TypeInfo]]) -> dict[str, float]:
        """批量读取 — 预计算计划 + 逐变量快速提取，无 isinstance 开销。"""
        if not variables:
            return {}
        if not self._ap:
            raise RuntimeError("未连接探针")

        ap = self._ap

        # 预计算读取计划（缓存：变量列表不变时复用）
        var_key = id(variables)
        if var_key != self._plan_cache_key or not self._plan_cache:
            if self._decoder is None:
                self._decoder = _TypeDecoder(self)
            self._plan_cache = [
                (name, self._decoder.make_plan(addr, ti))
                for name, addr, ti in variables
            ]
            self._plan_cache_key = var_key

        result = {}
        for name, (wa, bo, w, wc, sgn, flt) in self._plan_cache:
            try:
                if wc <= 1:
                    # 单字读取 — 一次 read_memory + 内联提取
                    raw = ap.read_memory(wa, transfer_size=32)
                    result[name] = _extract_val(raw, byte_offset=bo, width=w,
                                                is_signed=sgn, is_float=flt)
                else:
                    # 跨字变量 — 回退到 read() 处理边界
                    result[name] = float(self.read(wa + bo, w))
            except Exception:
                result[name] = float('nan')

        return result


def _extract_val(words, word_idx: int = 0, byte_offset: int = 0,
                 width: int = 4, word_count: int = 1,
                 is_signed: bool = False, is_float: bool = False) -> float:
    """从字数组中提取变量值，自动处理跨字边界。

    当 words 是单个 int 时按遗留路径处理；是 list 时从 word_idx 开始读取 word_count 个字。
    """
    if isinstance(words, int):
        # 单字快速路径
        raw = (words >> (byte_offset * 8)) & ((1 << (width * 8)) - 1)
    else:
        # 多字: 组合 word_count 个 32-bit 字为一个大整数
        val = 0
        for k in range(word_count):
            w = words[word_idx + k]
            val |= (w & 0xFFFFFFFF) << (k * 32)
        raw = (val >> (byte_offset * 8)) & ((1 << (width * 8)) - 1)

    if is_float and width == 4:
        return struct.unpack('<f', struct.pack('<I', raw))[0]

    if is_signed:
        if width == 1:
            return float(raw - 256 if raw >= 128 else raw)
        if width == 2:
            return float(raw - 65536 if raw >= 32768 else raw)
        if width == 4:
            return float(raw - 4294967296 if raw >= 2147483648 else raw)
        if width == 8:
            return float(raw - (1 << 63) if raw >= (1 << 63) else raw)

    return float(raw)


class _TypeDecoder:
    """类型解码器 — decode() 用于单个读取，make_plan() 用于预计算批量读取计划。"""

    def __init__(self, backend: SWDBackend):
        self._backend = backend

    def decode(self, address: int, ti: TypeInfo) -> float:
        if ti is None:
            return float(self._backend.read(address, 4))
        if isinstance(ti, TypedefType):
            return self.decode(address, ti.underlying_type)
        if isinstance(ti, BaseType):
            return self._decode_base(address, ti)
        if isinstance(ti, PointerType):
            return float(self._backend.read(address, 4))
        if isinstance(ti, EnumType):
            return float(self._backend.read(address, ti.size or 4))
        if isinstance(ti, ArrayType):
            if ti.element_type:
                return self.decode(address, ti.element_type)
            return float(self._backend.read(address, 4))
        if isinstance(ti, StructType):
            return float(self._backend.read(address, 4))
        return float(self._backend.read(address, 4))

    def _decode_base(self, address: int, bt: BaseType) -> float:
        size = bt.byte_size
        if size <= 0:
            return float('nan')
        raw = self._backend.read(address, min(size, 8))
        encoding = bt.encoding
        name = bt.name.lower()

        if encoding == "float" or "float" in name or "double" in name:
            if size == 4:
                data = struct.pack('<I', raw & 0xFFFFFFFF)
                return struct.unpack('<f', data)[0]

        if encoding.startswith("signed") or (name.startswith("int") and "uint" not in name):
            if size == 1:
                return float(struct.unpack('<b', struct.pack('<B', raw & 0xFF))[0])
            if size == 2:
                return float(struct.unpack('<h', struct.pack('<H', raw & 0xFFFF))[0])
            if size == 4:
                return float(struct.unpack('<i', struct.pack('<I', raw & 0xFFFFFFFF))[0])

        return float(raw)

    def make_plan(self, address: int, ti: TypeInfo) -> tuple:
        """预计算读取参数，返回 (word_addr, byte_offset, width, word_count, is_signed, is_float)。

        word_count 表示此变量跨越的 32-bit 字数，用于跨字边界读取。
        """
        if ti is None:
            wa = address & ~0x3
            bo = address & 0x3
            return (wa, bo, 4, 1, False, False)
        if isinstance(ti, TypedefType):
            return self.make_plan(address, ti.underlying_type)
        if isinstance(ti, BaseType):
            return self._plan_base(address, ti)
        if isinstance(ti, PointerType) or isinstance(ti, EnumType):
            w = 4
            if isinstance(ti, EnumType) and ti.size:
                w = ti.size
            wa = address & ~0x3
            bo = address & 0x3
            wc = (bo + w + 3) // 4
            return (wa, bo, w, wc, False, False)
        if isinstance(ti, ArrayType):
            if ti.element_type:
                return self.make_plan(address, ti.element_type)
            wa = address & ~0x3
            bo = address & 0x3
            return (wa, bo, 4, 1, False, False)
        # StructType, FuncType, etc. — fallback
        wa = address & ~0x3
        bo = address & 0x3
        return (wa, bo, 4, 1, False, False)

    def _plan_base(self, address: int, bt: BaseType) -> tuple:
        size = bt.byte_size
        if size <= 0:
            wa = address & ~0x3
            bo = address & 0x3
            return (wa, bo, 4, 1, False, False)
        width = min(size, 8)
        wa = address & ~0x3
        bo = address & 0x3
        wc = (bo + width + 3) // 4  # 需要多少个 32-bit 字
        encoding = bt.encoding
        name = bt.name.lower()
        is_float = encoding == "float" or "float" in name or "double" in name
        is_signed = encoding.startswith("signed") or (name.startswith("int") and "uint" not in name)
        return (wa, bo, width, wc, is_signed, is_float)
