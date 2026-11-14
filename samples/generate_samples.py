#!/usr/bin/env python3
"""生成演示用虚构数据（与生产数据目录隔离）。

输出到 samples/data/：
  demo_grid.nc          升序纬度 + 0~360 经度，含缺测空洞，变量 tas
  demo_grid_desc.nc     降序纬度 + -180~180 经度，变量 temperature
  demo_slow_cancel.nc   可控慢提取：每 5 天休眠 2s（共约 12s），用于演示取消
  demo_slow_timeout.nc  可控慢提取：每 2 天休眠 5s（共约 75s），用于演示 60s 超时
  stations.csv          10 个虚构站点（同时覆盖两种经度约定）
  stations.json         同样站点的 JSON 形式

慢提取由全局属性驱动（仅本服务识别，真实文件忽略这些属性）：
  demo_slow=1, demo_delay_seconds=<秒>, demo_chunk_steps=<时间步>
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd
import xarray as xr

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data")


def _make_grid(
    times: pd.DatetimeIndex,
    lats: np.ndarray,
    lons: np.ndarray,
    var_name: str,
    with_nan_hole: bool,
) -> xr.Dataset:
    rng = np.random.default_rng(42)
    # 构造带纬度梯度的虚构温度场（摄氏度），叠加日变化与随机噪声
    base = 15.0 + 25.0 * np.cos(np.radians(lats))[None, :, None]
    season = 3.0 * np.sin(np.linspace(0, np.pi, len(times)))[:, None, None]
    noise = rng.normal(0, 0.4, size=(len(times), len(lats), len(lons)))
    values = (base + season + noise).astype("float32")
    if with_nan_hole:
        # 中部区域前 5 天设为缺测，演示 CSV 留空
        values[:5, len(lats) // 2, len(lons) // 2] = np.nan

    da = xr.DataArray(
        values,
        dims=("time", "lat", "lon"),
        coords={"time": times, "lat": lats, "lon": lons},
        name=var_name,
        attrs={"units": "degC", "long_name": "fictional daily near-surface air temperature"},
    )
    return da.to_dataset()


def write_nc(ds: xr.Dataset, path: str) -> None:
    ds.to_netcdf(path, format="NETCDF4", engine="netcdf4")
    print(f"written {path} ({os.path.getsize(path)} bytes)")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    times = pd.date_range("2026-01-01", periods=45, freq="D")

    # 1) 升序纬度、0~360 经度（步长 20 度），含缺测
    lats_asc = np.arange(-60.0, 61.0, 20.0)
    lons_360 = np.arange(0.0, 360.0, 20.0)
    ds1 = _make_grid(times, lats_asc, lons_360, "tas", with_nan_hole=True)
    write_nc(ds1, os.path.join(OUT, "demo_grid.nc"))

    # 2) 降序纬度、-180~180 经度（步长 30 度）
    lats_desc = np.arange(60.0, -61.0, -30.0)
    lons_180 = np.arange(-180.0, 180.0, 30.0)
    ds2 = _make_grid(times, lats_desc, lons_180, "temperature", with_nan_hole=False)
    write_nc(ds2, os.path.join(OUT, "demo_grid_desc.nc"))

    # 3) 慢提取-可取消：30 天，每 5 步休眠 2 秒 => 约 12 秒
    slow_times = pd.date_range("2026-02-01", periods=30, freq="D")
    ds3 = _make_grid(
        slow_times,
        np.arange(-40.0, 41.0, 20.0),
        np.arange(0.0, 360.0, 30.0),
        "tas",
        with_nan_hole=False,
    )
    ds3.attrs = {
        "demo_slow": 1,
        "demo_delay_seconds": 2.0,
        "demo_chunk_steps": 5,
    }
    write_nc(ds3, os.path.join(OUT, "demo_slow_cancel.nc"))

    # 4) 慢提取-超时：30 天，每 2 步休眠 5 秒 => 约 75 秒，超过 60s 限制
    ds4 = _make_grid(
        slow_times,
        np.arange(-40.0, 41.0, 20.0),
        np.arange(0.0, 360.0, 30.0),
        "tas",
        with_nan_hole=False,
    )
    ds4.attrs = {
        "demo_slow": 1,
        "demo_delay_seconds": 5.0,
        "demo_chunk_steps": 2,
    }
    write_nc(ds4, os.path.join(OUT, "demo_slow_timeout.nc"))

    # 5) 虚构站点（部分经度用 0~360，部分用 -180~180，含缺测空洞附近点）
    stations = [
        ("ST01", 39.90, 116.40),   # 北京
        ("ST02", 31.23, 121.47),   # 上海
        ("ST03", 22.32, 114.17),   # 香港
        ("ST04", -33.87, 151.21),  # 悉尼
        ("ST05", 51.51, -0.13),    # 伦敦（负经度）
        ("ST06", 40.71, -74.01),   # 纽约
        ("ST07", -23.55, -46.63),  # 圣保罗
        ("ST08", 1.35, 103.82),    # 新加坡
        ("ST09", 0.0, 180.0),      # 日期变更线
        ("ST10", 0.0, 359.9),      # 与变更线几乎同一点（另一种经度写法）
    ]
    csv_lines = ["id,lat,lon"]
    json_rows = []
    for sid, lat, lon in stations:
        csv_lines.append(f"{sid},{lat},{lon}")
        json_rows.append({"id": sid, "lat": lat, "lon": lon})

    csv_path = os.path.join(OUT, "stations.csv")
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(csv_lines) + "\n")
    print(f"written {csv_path}")

    json_path = os.path.join(OUT, "stations.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"stations": json_rows}, fh, ensure_ascii=False, indent=2)
    print(f"written {json_path}")


if __name__ == "__main__":
    main()
