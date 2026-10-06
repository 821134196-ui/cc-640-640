import time


def now_ms() -> int:
    """服务端时钟（毫秒）。费用判定只允许使用这个时间。"""
    return int(time.time() * 1000)
