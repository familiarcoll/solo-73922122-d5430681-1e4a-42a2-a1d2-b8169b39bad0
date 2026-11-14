"""输入校验：NetCDF 网格结构、公历、站点参数。"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any

import numpy as np

from .netcdf_guard import locked_netcdf

# 仅接受标准公历（CF time 中的标准/公历命名）
_STANDARD_CALENDARS = frozenset(
    {"standard", "gregorian", "proleptic_gregorian", ""}
)
_DIM_NAMES = ("time", "lat", "lon")


class ValidationError(ValueError):
    """输入不满足契约，API 层映射为 HTTP 400。"""


@dataclass(frozen=True)
class Station:
    station_id: str
    lat: float
    lon: float


@dataclass(frozen=True)
class GridInfo:
    """检查通过后得到的网格元数据。"""

    variable: str
    ntime: int
    nlat: int
    nlon: int
    lat_descending: bool
    demo_slow: bool
    demo_delay: float
    demo_chunk: int


def _read_calendar(path: str) -> str:
    """在解码前直接读 time 变量的 calendar 属性，非公历可快速拒绝。"""
    import netCDF4  # 延迟导入，降低模块加载成本

    try:
        with locked_netcdf():
            with netCDF4.Dataset(path, "r") as ds:
                if "time" not in ds.variables:
                    raise ValidationError("NetCDF 缺少 time 坐标变量")
                cal = getattr(ds.variables["time"], "calendar", "standard")
                units = getattr(ds.variables["time"], "units", "")
        if not units:
            raise ValidationError("time 变量缺少 CF units 属性")
        return str(cal).strip().lower()
    except OSError as exc:
        raise ValidationError(f"无法以 netCDF4/HDF5 格式打开文件: {exc}") from exc


def _check_regular_axis(values: np.ndarray, name: str) -> None:
    """坐标必须一维、有限、严格单调且等间距（规则网格）。"""
    if values.ndim != 1 or values.size < 2:
        raise ValidationError(f"{name} 必须是长度至少为 2 的一维坐标")
    arr = np.asarray(values, dtype=float)
    if not np.isfinite(arr).all():
        raise ValidationError(f"{name} 坐标包含非有限值")
    diffs = np.diff(arr)
    if not (np.all(diffs > 0) or np.all(diffs < 0)):
        raise ValidationError(f"{name} 坐标必须严格单调（升序或降序）")
    if not np.allclose(diffs, diffs[0], rtol=0.0, atol=1e-8):
        raise ValidationError(f"{name} 坐标必须等间距（规则网格）")


def inspect_netcdf(path: str, variable: str) -> GridInfo:
    """校验上传的 NetCDF 是否满足：time/lat/lon 三维规则经纬网 + 标准公历。"""
    import xarray as xr

    calendar = _read_calendar(path)
    if calendar not in _STANDARD_CALENDARS:
        raise ValidationError(
            f"仅支持标准公历（standard/gregorian），收到 calendar={calendar!r}"
        )

    try:
        with locked_netcdf():
            ds = xr.open_dataset(path, decode_times=True)
            try:
                return _validate_dataset(ds, variable)
            finally:
                ds.close()
    except (OSError, ValueError) as exc:
        raise ValidationError(f"xarray 无法打开该 NetCDF: {exc}") from exc


def _validate_dataset(ds, variable: str) -> "GridInfo":
    for dim in _DIM_NAMES:
        if dim not in ds.dims:
            raise ValidationError(f"NetCDF 缺少维度 {dim!r}")
    if variable not in ds.data_vars:
        raise ValidationError(
            f"变量 {variable!r} 不存在，可用变量: {sorted(ds.data_vars)}"
        )

    da = ds[variable]
    if tuple(da.dims) != _DIM_NAMES:
        raise ValidationError(
            f"变量 {variable!r} 的维度必须严格为 {_DIM_NAMES}，"
            f"实际为 {da.dims}"
        )
    if da.ndim != 3:
        raise ValidationError(f"变量 {variable!r} 必须是三维数据")

    for coord in ("time", "lat", "lon"):
        if coord not in ds.coords:
            raise ValidationError(f"缺少坐标变量 {coord!r}")

    time_vals = ds["time"].values
    if not np.issubdtype(time_vals.dtype, np.datetime64):
        raise ValidationError(
            "time 坐标无法按 CF 时间解码为公历日期，请确认 units/calendar"
        )

    lat = np.asarray(ds["lat"].values, dtype=float)
    lon = np.asarray(ds["lon"].values, dtype=float)
    _check_regular_axis(lat, "lat")
    _check_regular_axis(lon, "lon")
    if lat.min() < -90.0 or lat.max() > 90.0:
        raise ValidationError("lat 必须落在 [-90, 90] 区间")
    if lon.min() < -180.0 or lon.max() > 360.0:
        raise ValidationError("lon 必须落在 [-180, 360] 区间")

    ntime, nlat, nlon = da.shape
    if ntime == 0:
        raise ValidationError("time 维度长度为 0")

    # 可选演示属性（虚构样例使用，真实文件忽略）
    demo_slow = bool(ds.attrs.get("demo_slow", False))
    demo_delay = float(ds.attrs.get("demo_delay_seconds", 0.0))
    demo_chunk = int(ds.attrs.get("demo_chunk_steps", max(1, ntime // 4)))
    demo_delay = max(0.0, demo_delay)
    demo_chunk = max(1, demo_chunk)

    return GridInfo(
        variable=variable,
        ntime=ntime,
        nlat=nlat,
        nlon=nlon,
        lat_descending=bool(lat[1] < lat[0]),
        demo_slow=demo_slow,
        demo_delay=demo_delay,
        demo_chunk=demo_chunk,
    )


# ----------------------------- 站点参数 -----------------------------


def _as_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"无法解析为数字: {value!r}") from exc
    if not np.isfinite(out):
        raise ValidationError(f"坐标必须为有限数字: {value!r}")
    return out


_LAT_ALIASES = ("lat", "latitude", "y")
_LON_ALIASES = ("lon", "long", "longitude", "x")
_ID_ALIASES = ("id", "station_id", "station", "name")


def _pick(row: dict[str, Any], aliases: tuple[str, ...], label: str) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    raise ValidationError(f"站点记录缺少 {label} 字段，可接受名称: {aliases}")


def _rows_to_stations(rows: list[dict[str, Any]], max_stations: int) -> list[Station]:
    if not rows:
        raise ValidationError("站点列表为空")
    if len(rows) > max_stations:
        raise ValidationError(f"站点数量超过上限 {max_stations}（收到 {len(rows)}）")

    stations: list[Station] = []
    seen: set[str] = set()
    for idx, row in enumerate(rows):
        sid = str(_pick(row, _ID_ALIASES, "站点 ID")).strip()
        if not sid:
            raise ValidationError(f"第 {idx + 1} 个站点 ID 为空")
        if len(sid) > 64:
            raise ValidationError(f"站点 ID 长度不能超过 64 字符: {sid!r}")
        if sid in seen:
            raise ValidationError(f"站点 ID 重复: {sid!r}")
        lat = _as_float(_pick(row, _LAT_ALIASES, "纬度"))
        lon = _as_float(_pick(row, _LON_ALIASES, "经度"))
        if not (-90.0 <= lat <= 90.0):
            raise ValidationError(f"站点 {sid!r} 纬度越界: {lat}")
        if not (-180.0 <= lon <= 360.0):
            raise ValidationError(f"站点 {sid!r} 经度越界: {lon}")
        seen.add(sid)
        stations.append(Station(station_id=sid, lat=lat, lon=lon))
    return stations


def parse_stations_json(payload: Any, max_stations: int) -> list[Station]:
    """接受 ["stations": [...]] 包装或直接的列表。"""
    if isinstance(payload, dict) and "stations" in payload:
        payload = payload["stations"]
    if not isinstance(payload, list):
        raise ValidationError("stations JSON 必须是数组（或含 stations 数组的对象）")
    return _rows_to_stations(payload, max_stations)


def parse_stations_csv(text: str, max_stations: int) -> list[Station]:
    """解析 CSV 文本，表头支持 id/lat/lon 及常见别名（大小写不敏感）。"""
    if not text.strip():
        raise ValidationError("站点 CSV 内容为空")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValidationError("站点 CSV 缺少表头")
    rows = [dict(r) for r in reader]
    return _rows_to_stations(rows, max_stations)
