"""校验逻辑单测：网格结构、公历、站点解析。"""

import numpy as np
import pytest

from app.validation import (
    ValidationError,
    inspect_netcdf,
    parse_stations_csv,
    parse_stations_json,
)


def test_accepts_regular_grid_ascending_360(paths):
    info = inspect_netcdf(paths["grid"], "tas")
    assert info.ntime == 8
    assert info.lat_descending is False


def test_accepts_descending_lat_and_180_lon(paths):
    info = inspect_netcdf(paths["grid_desc"], "temperature")
    assert info.lat_descending is True


def test_rejects_non_gregorian_calendar(paths):
    with pytest.raises(ValidationError, match="公历"):
        inspect_netcdf(paths["noleap"], "tas")


def test_rejects_missing_variable(paths):
    with pytest.raises(ValidationError, match="不存在"):
        inspect_netcdf(paths["grid"], "no_such_var")


def test_rejects_irregular_lat(tmp_path):
    import xarray as xr

    times = np.arange("2026-01-01", "2026-01-04", dtype="datetime64[D]")
    ds = xr.Dataset(
        {
            "tas": (
                ("time", "lat", "lon"),
                np.zeros((3, 3, 4), dtype="float32"),
            )
        },
        coords={
            "time": times,
            "lat": [0.0, 10.0, 30.0],  # 非等间距
            "lon": [0.0, 10.0, 20.0, 30.0],
        },
    )
    p = str(tmp_path / "irregular.nc")
    ds.to_netcdf(p)
    with pytest.raises(ValidationError, match="等间距"):
        inspect_netcdf(p, "tas")


def test_rejects_wrong_dims(tmp_path):
    import xarray as xr

    # 三个维度都存在，但变量维度顺序错误
    ds = xr.Dataset(
        {"tas": (("lat", "time", "lon"), np.zeros((2, 3, 4), dtype="float32"))},
        coords={
            "time": np.arange("2026-01-01", "2026-01-04", dtype="datetime64[D]"),
            "lat": [0.0, 1.0],
            "lon": [0.0, 10.0, 20.0, 30.0],
        },
    )
    p = str(tmp_path / "wrongdim.nc")
    ds.to_netcdf(p)
    with pytest.raises(ValidationError, match="维度必须严格"):
        inspect_netcdf(p, "tas")


def test_rejects_corrupt_file(tmp_path):
    p = str(tmp_path / "bad.nc")
    with open(p, "wb") as fh:
        fh.write(b"not a netcdf file at all")
    with pytest.raises(ValidationError):
        inspect_netcdf(p, "tas")


def test_station_json_and_csv_aliases():
    stations = parse_stations_json(
        [{"name": "A", "latitude": 10.0, "longitude": 20.0}], 100
    )
    assert stations[0].station_id == "A"

    csv_text = "ID,LAT,LONG\nB,30,40\n"
    stations2 = parse_stations_csv(csv_text, 100)
    assert stations2[0].station_id == "B"
    assert stations2[0].lat == 30.0


def test_station_limit_duplicate_and_range():
    with pytest.raises(ValidationError, match="超过上限"):
        parse_stations_json(
            [{"id": str(i), "lat": 0, "lon": 0} for i in range(3)], 2
        )
    with pytest.raises(ValidationError, match="重复"):
        parse_stations_json(
            [
                {"id": "X", "lat": 0, "lon": 0},
                {"id": "X", "lat": 1, "lon": 1},
            ],
            10,
        )
    with pytest.raises(ValidationError, match="纬度越界"):
        parse_stations_json([{"id": "X", "lat": 91, "lon": 0}], 10)
    with pytest.raises(ValidationError, match="为空"):
        parse_stations_json([], 10)
