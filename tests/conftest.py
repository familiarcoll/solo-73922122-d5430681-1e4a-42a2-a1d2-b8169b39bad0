"""pytest 夹具：隔离的数据目录、内存中构造的 NetCDF 与 TestClient。"""

from __future__ import annotations

import importlib
import os
import sys

import numpy as np
import pandas as pd
import pytest
import xarray as xr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_dataset(
    tmp_path,
    name,
    *,
    lats=None,
    lons=None,
    ntime=8,
    var="tas",
    dims=("time", "lat", "lon"),
    slow=False,
    delay=2.0,
    chunk=5,
    nan_hole=False,
    calendar="standard",
):
    if lats is None:
        lats = np.arange(-60.0, 61.0, 20.0)
    if lons is None:
        lons = np.arange(0.0, 360.0, 30.0)
    times = pd.date_range("2026-01-01", periods=ntime, freq="D")
    rng = np.random.default_rng(1)
    data = rng.normal(15.0, 2.0, size=(ntime, len(lats), len(lons))).astype("float32")
    if nan_hole:
        data[:3, len(lats) // 2, len(lons) // 2] = np.nan

    coords = {"time": times, "lat": lats, "lon": lons}
    da = xr.DataArray(data, dims=dims, coords=coords, name=var)
    ds = da.to_dataset()
    if slow:
        ds.attrs = {
            "demo_slow": 1,
            "demo_delay_seconds": delay,
            "demo_chunk_steps": chunk,
        }
    path = os.path.join(tmp_path, name)
    if calendar != "standard":
        # 直接用 netCDF4 写非标准日历
        import netCDF4

        ds.to_netcdf(path, engine="netcdf4")
        with netCDF4.Dataset(path, "r+") as ncd:
            ncd.variables["time"].calendar = calendar
    else:
        ds.to_netcdf(path, engine="netcdf4")
    return path


@pytest.fixture()
def paths(tmp_path):
    return {
        "grid": _make_dataset(str(tmp_path), "grid.nc"),
        "grid_desc": _make_dataset(
            str(tmp_path),
            "desc.nc",
            lats=np.arange(60.0, -61.0, -30.0),
            lons=np.arange(-180.0, 180.0, 30.0),
            var="temperature",
        ),
        "grid_nan": _make_dataset(str(tmp_path), "nan.nc", nan_hole=True),
        "slow_cancel": _make_dataset(
            str(tmp_path), "slow_cancel.nc", ntime=30, slow=True, delay=2.0, chunk=5
        ),
        "slow_timeout": _make_dataset(
            str(tmp_path), "slow_timeout.nc", ntime=30, slow=True, delay=5.0, chunk=2
        ),
        "noleap": _make_dataset(str(tmp_path), "noleap.nc", calendar="noleap"),
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个测试独立数据目录与 TaskManager。"""
    data_dir = str(tmp_path / "appdata")
    monkeypatch.setenv("APP_DATA_DIR", data_dir)
    monkeypatch.setenv("APP_TASK_TIMEOUT_SECONDS", "60")

    from app import config as config_mod
    from app import main as main_mod

    importlib.reload(config_mod)
    importlib.reload(main_mod)
    main_mod.settings = config_mod.Settings()

    from fastapi.testclient import TestClient

    with TestClient(main_mod.app) as c:
        yield c, main_mod
