"""Data collector with numpy ring buffer and background sampling thread."""

import threading
import time
from typing import Optional

import numpy as np

from src.core.models import Variable, TypeInfo
from src.core.mem_backend import SWDBackend


class RingBuffer:
    """Pre-allocated numpy ring buffer with O(1) append, O(log n) search, O(k) tail."""

    __slots__ = ('_data', '_head', '_count', '_maxlen')

    def __init__(self, maxlen: int):
        self._data = np.zeros(maxlen, dtype=np.float64)
        self._head = 0       # next write position
        self._count = 0
        self._maxlen = maxlen

    def append(self, val: float):
        self._data[self._head] = val
        self._head = (self._head + 1) % self._maxlen
        if self._count < self._maxlen:
            self._count += 1

    @property
    def count(self) -> int:
        return self._count

    def __len__(self) -> int:
        return self._count

    def _phys_idx(self, logical_idx: int) -> int:
        if logical_idx < 0:
            logical_idx = self._count + logical_idx
        return (self._head - self._count + logical_idx) % self._maxlen

    def logical_slice(self, start: int, stop: int) -> np.ndarray:
        """Return data[start:stop] as contiguous numpy array (view if possible)."""
        if start < 0:
            start = max(0, self._count + start)
        if stop < 0:
            stop = max(0, self._count + stop)
        start = max(0, min(start, self._count))
        stop = max(0, min(stop, self._count))
        if start >= stop:
            return np.array([], dtype=np.float64)

        p_start = (self._head - self._count + start) % self._maxlen
        p_end = (self._head - self._count + stop) % self._maxlen

        if p_end == 0 and stop - start == self._count:
            p_end = self._maxlen

        if p_start < p_end:
            return self._data[p_start:p_end]  # view, no copy
        else:
            # Wrapped: must copy to make contiguous
            return np.concatenate([self._data[p_start:], self._data[:p_end]])

    def find_ge(self, value: float) -> int:
        """Return logical index of first element >= value.

        Uses binary search directly on physical memory views (no copy).
        """
        if self._count == 0:
            return -1

        p_start = (self._head - self._count) % self._maxlen
        p_end = self._head

        if p_start < p_end:
            # Single contiguous segment — searchsorted on view (O(log n), no copy)
            idx = np.searchsorted(self._data[p_start:p_end], value)
            return idx if idx < self._count else -1
        else:
            # Wrapped: two segments. Search first, then second if needed.
            seg1_len = self._maxlen - p_start
            idx = np.searchsorted(self._data[p_start:], value)
            if idx < seg1_len:
                return idx
            idx2 = np.searchsorted(self._data[:p_end], value)
            if idx2 < p_end:
                return seg1_len + idx2
            return -1

    def all_data(self) -> np.ndarray:
        """Return all data as contiguous array (copy only if wrapped)."""
        if self._count == 0:
            return np.array([], dtype=np.float64)
        p_start = (self._head - self._count) % self._maxlen
        p_end = self._head
        if p_end == 0 and self._count == self._maxlen:
            p_end = self._maxlen
        if p_start < p_end:
            return self._data[p_start:p_end]  # view
        else:
            return np.concatenate([self._data[p_start:], self._data[:p_end]])


class DataCollector:
    def __init__(self):
        self._backend: Optional[SWDBackend] = None
        self._variables: list[tuple[str, int, TypeInfo]] = []
        self._buffers: dict[str, RingBuffer] = {}
        self._timestamps: Optional[RingBuffer] = None
        self._sample_rate = 100
        self._buffer_size = 1000
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._actual_rate = 0.0
        self._sample_count = 0
        self._t0 = 0.0

    def set_backend(self, backend: SWDBackend):
        self._backend = backend

    def configure(self, sample_rate: int, buffer_seconds: float = 10.0):
        self._sample_rate = sample_rate
        self._buffer_size = max(int(sample_rate * buffer_seconds), 1000)

    def set_variables(self, variables: list[tuple[str, int, TypeInfo]]):
        with self._lock:
            self._variables = variables
            self._buffers = {v[0]: RingBuffer(self._buffer_size) for v in variables}
            self._timestamps = RingBuffer(self._buffer_size)

    def add_variable(self, name: str, address: int, type_info: TypeInfo):
        with self._lock:
            self._variables.append((name, address, type_info))
            self._buffers[name] = RingBuffer(self._buffer_size)

    def remove_variable(self, name: str):
        with self._lock:
            self._variables = [(n, a, t) for n, a, t in self._variables if n != name]
            if name in self._buffers:
                del self._buffers[name]

    @property
    def variable_names(self) -> list[str]:
        return [v[0] for v in self._variables]

    def start(self):
        if self._running:
            return
        if not self._backend or not self._backend.is_connected:
            raise RuntimeError("Backend not connected")
        self._running = True
        self._sample_count = 0
        self._t0 = 0.0
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def actual_rate(self) -> float:
        return self._actual_rate

    def get_data(self, tail_seconds: float = None) -> dict[str, tuple['np.ndarray', 'np.ndarray']]:
        """Returns {name: (timestamps, values)} as numpy arrays.

        If tail_seconds is provided, returns only data from the last N seconds.
        """
        with self._lock:
            if self._timestamps is None or self._timestamps.count == 0:
                return {}

            ts_buf = self._timestamps

            if tail_seconds is not None and tail_seconds > 0:
                last_val = ts_buf._data[ts_buf._phys_idx(-1)]
                cutoff = last_val - tail_seconds
                start_idx = ts_buf.find_ge(cutoff)
                if start_idx < 0:
                    start_idx = 0
            else:
                start_idx = 0

            ts_arr = ts_buf.logical_slice(start_idx, ts_buf.count)

            result = {}
            for name, buf in self._buffers.items():
                vals_arr = buf.logical_slice(start_idx, buf.count)
                n = min(len(ts_arr), len(vals_arr))
                result[name] = (ts_arr[:n], vals_arr[:n])
            return result

    def _sample_loop(self):
        t0 = time.perf_counter()
        sample_interval = 1.0 / self._sample_rate
        rate_window = 10

        while self._running:
            loop_start = time.perf_counter()

            try:
                variables_snapshot = list(self._variables)
                if variables_snapshot:
                    data = self._backend.read_batch(variables_snapshot)
                    now = time.perf_counter() - t0
                    with self._lock:
                        self._timestamps.append(now)
                        for name, val in data.items():
                            if name in self._buffers:
                                self._buffers[name].append(val)

                    self._sample_count += 1
                    if self._sample_count % rate_window == 0:
                        elapsed = now
                        self._actual_rate = self._sample_count / elapsed if elapsed > 0 else 0

            except Exception as e:
                import sys
                print(f"[Collector] sample error: {e}", file=sys.stderr, flush=True)

            # Maintain sample rate
            elapsed = time.perf_counter() - loop_start
            sleep_time = sample_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
