"""球面最近格点匹配与逐站点时序提取。"""

from __future__ import annotations

import math
import os
from typing import Callable

import numpy as np
import pandas as pd
import xarray as xr

EARTH_RADIUS_M = 6_371_008.8


def _wrap_180(lon: np.ndarray) -> np.ndarray:
    """将经度规整到 (-180, 180]，仅用于距离计算。"""
    return ((np.asarray(lon, dtype=float) + 180.0) % 360.0) - 180.0


def nearest_grid_indices(
    station_lats: np.ndarray,
    station_lons: np.ndarray,
    grid_lats: np.ndarray,
    grid_lons: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按球面距离（haversine）选最近格点。

    返回 (lat 索引数组, lon 索引数组, 距离米数组)，均按站点顺序排列。
    兼容降序纬度（取 argmin 天然适配）与 0~360 度经度（先做规整）。
    """
    s_lat = np.radians(np.asarray(station_lats, dtype=float))
    s_lon = np.radians(_wrap_180(np.asarray(station_lons, dtype=float)))
    g_lat = np.radians(np.asarray(grid_lats, dtype=float))
    g_lon = np.radians(_wrap_180(np.asarray(grid_lons, dtype=float)))

    lat_idx = np.empty(s_lat.shape, dtype=np.int64)
    lon_idx = np.empty(s_lat.shape, dtype=np.int64)
    distances = np.empty(s_lat.shape, dtype=np.float64)

    for i in range(s_lat.shape[0]):
        # 先按纬度方向选最近的一条纬线，再在该纬线上按经度选点
        dlat = np.abs(g_lat - s_lat[i])
        candidate_lat = int(np.argmin(dlat))

        dlon = (g_lon - s_lon[i] + math.pi) % (2.0 * math.pi) - math.pi
        hav = (
            np.sin(dlat[candidate_lat] / 2.0) ** 2
            + np.cos(g_lat[candidate_lat])
            * np.cos(s_lat[i])
            * np.sin(dlon / 2.0) ** 2
        )
        hav = np.clip(hav, 0.0, 1.0)
        lon_i = int(np.argmin(hav))
        dist = 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(hav[lon_i]))

        lat_idx[i] = candidate_lat
        lon_idx[i] = lon_i
        distances[i] = dist
    return lat_idx, lon_idx, distances


def _format_time(ts: np.datetime64) -> str:
    """整日时间戳输出 YYYY-MM-DD，否则输出带时分秒的 ISO 串。"""
    ts_pd = pd.Timestamp(ts)
    if (
        ts_pd.hour == 0
        and ts_pd.minute == 0
        and ts_pd.second == 0
        and ts_pd.microsecond == 0
    ):
        return ts_pd.strftime("%Y-%m-%d")
    return ts_pd.isoformat()


def _interruptible_sleep(
    seconds: float, stop_event, check: Callable[[], None]
) -> None:
    """可被取消事件立即打断的休眠，每 50ms 检查一次。"""
    import time

    deadline = time.monotonic() + seconds
    while True:
        check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def extract_station_series(
    nc_path: str,
    variable: str,
    stations,
    output_csv: str,
    *,
    chunk_size: int = 10,
    demo_slow: bool = False,
    demo_delay: float = 0.0,
    demo_chunk: int = 1,
    stop_event=None,
    deadline_monotonic: float | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """提取站点时序并原子发布 CSV。

    - 缺测（NaN）写为空字段。
    - 先写临时文件，成功关闭后 os.replace 原子替换 output_csv；
      任何中途异常/取消都不会产生半成品结果文件。
    - 每个时间块之间检查取消与 60s 截止时间；演示慢提取按块休眠。
    """
    import time

    from .netcdf_guard import locked_netcdf  # noqa: E402

    def check_stop() -> None:
        if stop_event is not None and stop_event.is_set():
            raise TaskInterrupted("cancelled")
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TaskInterrupted("timeout")

    tmp_path = f"{output_csv}.tmp.{os.getpid()}"
    # HDF5 串行锁仅在真正访问文件（打开/逐块读取/关闭）时持有；
    # 拼装 CSV、演示休眠与取消等待均在锁外，避免阻塞其他任务与上传校验。
    with locked_netcdf():
        ds = xr.open_dataset(nc_path)
    try:
        da = ds[variable]
        grid_lat = np.asarray(ds["lat"].values, dtype=float)
        grid_lon = np.asarray(ds["lon"].values, dtype=float)
        times = ds["time"].values

        s_lat = np.array([s.lat for s in stations], dtype=float)
        s_lon = np.array([s.lon for s in stations], dtype=float)
        lat_idx, lon_idx, distances = nearest_grid_indices(
            s_lat, s_lon, grid_lat, grid_lon
        )

        ntime = times.shape[0]
        written = 0
        # 演示慢提取：每跨过 demo_chunk 个时间步休眠一次（与读取分块大小无关）
        slow_step = max(1, demo_chunk)
        next_sleep_at = slow_step
        with open(tmp_path, "w", newline="", encoding="utf-8") as fh:
            fh.write("station_id,date,grid_lat,grid_lon,temperature\n")
            for start in range(0, ntime, chunk_size):
                check_stop()
                end = min(start + chunk_size, ntime)
                # 仅本块的实际读取需要 HDF5 锁
                with locked_netcdf():
                    block = da.isel(time=slice(start, end))
                    block_values = np.asarray(block.values)  # 触发读取并物化
                # 以下为纯内存计算与文件写入，不触碰 HDF5
                for ti in range(start, end):
                    date_str = _format_time(times[ti])
                    for k, st in enumerate(stations):
                        value = block_values[ti - start, lat_idx[k], lon_idx[k]]
                        if np.isnan(value):
                            temp_str = ""
                        else:
                            temp_str = repr(float(value))
                        glat = float(grid_lat[lat_idx[k]])
                        glon = float(grid_lon[lon_idx[k]])
                        fh.write(
                            f"{st.station_id},{date_str},"
                            f"{glat!r},{glon!r},"
                            f"{temp_str}\n"
                        )
                    written += 1
                if progress_cb is not None:
                    progress_cb(written, ntime)

                if demo_slow and demo_delay > 0:
                    # 本块可能一次跨过多个休眠边界，逐次补齐休眠
                    while end >= next_sleep_at and next_sleep_at <= ntime:
                        _interruptible_sleep(demo_delay, stop_event, check_stop)
                        next_sleep_at += slow_step
                check_stop()
        # 文件已完整写入并关闭后再原子发布
        os.replace(tmp_path, output_csv)
    except TaskInterrupted:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    finally:
        with locked_netcdf():
            ds.close()


class TaskInterrupted(Exception):
    """计算被取消或超时打断（不发布任何结果）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
