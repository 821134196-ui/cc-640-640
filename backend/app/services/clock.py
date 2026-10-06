"""服务端时钟。

业务时间一律以服务端事件落库时间（server_time）为准，客户端时间只作记录，
不参与费用判定——避免“取消和出发几乎同时发生”时，双方各用客户端时间算出不同费用。

测试与演示可以把时钟固定/拨快，用于构造临界场景。
"""
import threading
import time

_lock = threading.Lock()
_fixed = None  # type: ignore[assignment]
_offset = 0.0


def now() -> float:
    with _lock:
        if _fixed is not None:
            return _fixed
        return time.time() + _offset


def set_fixed(ts: float | None) -> None:
    """固定时钟（传 None 恢复真实时钟）。"""
    global _fixed
    with _lock:
        _fixed = ts


def advance(seconds: float) -> None:
    global _offset, _fixed
    with _lock:
        if _fixed is not None:
            _fixed += seconds
        else:
            _offset += seconds


def reset() -> None:
    global _fixed, _offset
    with _lock:
        _fixed = None
        _offset = 0.0
