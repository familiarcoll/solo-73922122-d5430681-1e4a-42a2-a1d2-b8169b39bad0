"""netCDF4/HDF5 访问串行化。

manylinux 版 netCDF4 wheel 链接的 HDF5 未启用线程安全（--disable-threadsafe），
多线程同时打开/读取不同文件会在 HDF5 内部触发段错误。
本服务最多两个任务并行，使用一把全局 RLock 串行化所有 HDF5 句柄的
打开、读取与关闭，保证并行任务仍可调度（等待 I/O 时释放 GIL 但持有本锁），
同时杜绝并发进入 HDF5。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

netcdf_lock = threading.RLock()


@contextmanager
def locked_netcdf():
    with netcdf_lock:
        yield
