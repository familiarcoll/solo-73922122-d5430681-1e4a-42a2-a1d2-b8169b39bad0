"""提取逻辑单测：最近格点、降序纬度、经度约定、缺测留空、取消/超时。"""

import threading
import time

import numpy as np
import pandas as pd
import pytest

from app.extraction import (
    TaskInterrupted,
    extract_station_series,
    nearest_grid_indices,
)
from app.validation import Station, inspect_netcdf


def test_nearest_grid_basic():
    grid_lat = np.array([0.0, 10.0, 20.0])
    grid_lon = np.array([0.0, 10.0, 20.0])
    lat_i, lon_i, dist = nearest_grid_indices(
        np.array([9.4]), np.array([10.6]), grid_lat, grid_lon
    )
    assert lat_i[0] == 1
    assert lon_i[0] == 1
    assert dist[0] > 0


def test_nearest_grid_descending_lat():
    grid_lat = np.array([20.0, 10.0, 0.0])  # 降序
    grid_lon = np.array([0.0, 10.0])
    lat_i, _, _ = nearest_grid_indices(
        np.array([11.0]), np.array([0.5]), grid_lat, grid_lon
    )
    assert lat_i[0] == 1  # 物理上 10 度纬线最近，其降序索引为 1


def test_longitude_conventions_equivalent():
    # 站点 179.9 与 -180.1 应映射到几乎同一格点
    grid_lat = np.array([0.0])
    grid_lon = np.arange(0.0, 360.0, 10.0)
    lat_i, lon_a, _ = nearest_grid_indices(
        np.array([0.0]), np.array([179.9]), grid_lat, grid_lon
    )
    _, lon_b, _ = nearest_grid_indices(
        np.array([0.0]), np.array([-180.1]), grid_lat, grid_lon
    )
    assert abs(lon_a[0] - lon_b[0]) <= 1


def test_extract_writes_csv_with_grid_coords_and_blank_nan(paths, tmp_path):
    # nan 空洞位于中央格点（lat=0, lon=180），站点直接对准它
    stations = [
        Station("S1", 0.0, 180.0),
        Station("S2", 59.0, 359.0),
    ]
    out = str(tmp_path / "out.csv")
    extract_station_series(
        paths["grid_nan"], "tas", stations, out, chunk_size=4
    )
    import csv

    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0].keys() == {
        "station_id",
        "date",
        "grid_lat",
        "grid_lon",
        "temperature",
    }
    # 8 天 × 2 站
    assert len(rows) == 16
    assert all(r["station_id"] in {"S1", "S2"} for r in rows)
    # 日期格式为 YYYY-MM-DD
    assert rows[0]["date"] == "2026-01-01"
    # 缺测行温度字段为空
    nan_rows = [r for r in rows if r["temperature"] == ""]
    assert nan_rows, "缺测值应输出为空字段"
    # 格点坐标来自原文件（0~360 经度）
    assert all(-60.0 <= float(r["grid_lat"]) <= 60.0 for r in rows)
    assert all(0.0 <= float(r["grid_lon"]) < 360.0 for r in rows)


def test_extract_descending_lat_grid(paths, tmp_path):
    stations = [Station("D1", 59.0, -179.0)]
    out = str(tmp_path / "desc_out.csv")
    extract_station_series(
        paths["grid_desc"], "temperature", stations, out
    )
    df = pd.read_csv(out)
    assert len(df) == 8
    # 降序文件的原始纬度坐标
    assert df["grid_lat"].iloc[0] == 60.0
    assert df["grid_lon"].iloc[0] == -180.0


def test_extract_cancel_stops_and_publishes_nothing(paths, tmp_path):
    stations = [Station(f"S{i}", 0.0, float(i * 30)) for i in range(5)]
    out = str(tmp_path / "cancel.csv")
    event = threading.Event()

    def cancel_soon():
        time.sleep(1.0)
        event.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    with pytest.raises(TaskInterrupted, match="cancelled"):
        extract_station_series(
            paths["slow_cancel"],
            "tas",
            stations,
            out,
            demo_slow=True,
            demo_delay=2.0,
            demo_chunk=5,
            stop_event=event,
        )
    # 取消后既无结果文件也无临时文件
    import glob

    assert not glob.glob(str(tmp_path / "cancel.csv*"))


def test_extract_timeout(paths, tmp_path):
    stations = [Station("S1", 0.0, 0.0)]
    out = str(tmp_path / "timeout.csv")
    deadline = time.monotonic() + 1.0
    with pytest.raises(TaskInterrupted, match="timeout"):
        extract_station_series(
            paths["slow_cancel"],
            "tas",
            stations,
            out,
            demo_slow=True,
            demo_delay=2.0,
            demo_chunk=2,
            stop_event=threading.Event(),
            deadline_monotonic=deadline,
        )
    import glob

    assert not glob.glob(str(tmp_path / "timeout.csv*"))


def test_demo_attributes_detected(paths):
    info = inspect_netcdf(paths["slow_cancel"], "tas")
    assert info.demo_slow is True
    assert info.demo_delay == 2.0
    assert info.demo_chunk == 5
