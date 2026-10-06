"""服务端写串行化。

“取消和出发几乎同时发生”时，必须由服务端给出唯一先后顺序，不能各用客户端时间。
SQLite 的库级锁在单文件下本身互斥，但为了让“取状态→算费用→追加事件→记账”
成为不可交错的原子判定，这里在应用层用一把可重入写锁把变更操作串起来。

说明：本系统设计为单 uvicorn worker（见 run.sh）。该锁在单进程内有效；
若横向扩为多进程/多机，应改用数据库级行锁/唯一序号表或串行化队列。
"""
import threading

write_lock = threading.RLock()


class SerializedWrite:
    """FastAPI 依赖：在整个请求处理期间持有写锁。"""

    def __enter__(self):
        write_lock.acquire()
        return self

    def __exit__(self, *exc):
        write_lock.release()
        return False


def write_locked():
    """FastAPI 依赖：整个变更请求持有写锁，保证服务端事件全序。"""
    with write_lock:
        yield

