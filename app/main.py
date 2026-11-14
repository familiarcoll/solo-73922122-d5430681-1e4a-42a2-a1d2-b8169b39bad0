"""FastAPI 入口：上传 NetCDF + 站点，异步提取并提供状态/取消/下载。"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .config import Settings
from .task_manager import (
    CapacityExhausted,
    ResultNotReady,
    TaskManager,
    TaskNotFound,
)
from .validation import (
    ValidationError,
    inspect_netcdf,
    parse_stations_csv,
    parse_stations_json,
)

settings = Settings()
manager: TaskManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.result_dir, exist_ok=True)
    manager = TaskManager(settings)
    yield


app = FastAPI(
    title="逐日温度网格 → 站点时序提取服务",
    description=(
        "上传不超过 32MiB 的 NetCDF（time/lat/lon 三维规则经纬网、标准公历）"
        "与最多 100 个站点，按球面距离匹配最近格点并导出 CSV。\n\n"
        "并行任务上限 2 个，单任务计算时限 60 秒，支持取消与状态查询。"
    ),
    version=__version__,
    lifespan=lifespan,
)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok", "version": __version__}


@app.post("/api/tasks", tags=["任务"], status_code=201)
async def create_task(
    file: UploadFile = File(..., description="NetCDF 文件（≤32MiB，time/lat/lon 三维）"),
    variable: str = Form(..., description="温度变量名，例如 tas / t2m / temperature"),
    stations_file: UploadFile | None = File(
        default=None, description="站点 CSV（表头 id,lat,lon，支持别名）"
    ),
    stations_json: str | None = Form(
        default=None,
        description='站点 JSON，如 [{"id":"S1","lat":39.9,"lon":116.4}]',
    ),
):
    """受理提取任务；并行满额返回 409，输入不合法返回 400。"""
    assert manager is not None

    # 1. 站点参数（两种形式二选一）
    if stations_file is not None and stations_json:
        raise _bad_request("stations_file 与 stations_json 只能提供一种")
    try:
        if stations_file is not None:
            raw = await stations_file.read()
            stations = parse_stations_csv(
                raw.decode("utf-8"), settings.max_stations
            )
        elif stations_json:
            try:
                payload = json.loads(stations_json)
            except json.JSONDecodeError as exc:
                raise _bad_request(f"stations_json 不是合法 JSON: {exc}") from exc
            stations = parse_stations_json(payload, settings.max_stations)
        else:
            raise _bad_request("必须通过 stations_file 或 stations_json 提供站点列表")
    except ValidationError as exc:
        raise _bad_request(str(exc)) from exc

    # 2. 流式落盘并强制 32MiB 上限（先写独立暂存文件，校验失败即删除）
    task_token = os.urandom(8).hex()
    upload_path = os.path.join(settings.upload_dir, f"incoming-{task_token}.nc")
    size = 0
    try:
        with open(upload_path, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise _bad_request(
                        f"文件超过 {settings.max_upload_bytes} 字节（32MiB）上限"
                    )
                fh.write(chunk)
        if size == 0:
            raise _bad_request("上传文件为空")

        # 3. 同步校验网格结构（快速失败，不占用并行槽位）
        try:
            grid = inspect_netcdf(upload_path, variable)
        except ValidationError as exc:
            raise _bad_request(str(exc)) from exc

        # 4. 占用槽位并启动后台计算
        try:
            rec = manager.create_task(grid, stations, upload_path)
        except CapacityExhausted as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        if os.path.exists(upload_path):
            os.remove(upload_path)
        raise
    except Exception:
        if os.path.exists(upload_path):
            os.remove(upload_path)
        raise

    return JSONResponse(status_code=201, content=rec.to_dict())


@app.get("/api/tasks", tags=["任务"])
def list_tasks():
    assert manager is not None
    return {"tasks": manager.list_tasks()}


@app.get("/api/tasks/{task_id}", tags=["任务"])
def get_task(task_id: str):
    assert manager is not None
    try:
        return manager.get(task_id).to_dict()
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.post("/api/tasks/{task_id}/cancel", tags=["任务"])
def cancel_task(task_id: str):
    """受理取消；工作线程实际停止后状态转为 cancelled。"""
    assert manager is not None
    try:
        rec = manager.request_cancel(task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return rec.to_dict()


@app.get("/api/tasks/{task_id}/download", tags=["任务"])
def download_task(task_id: str):
    """仅 COMPLETED 任务可下载，其余状态返回 409。"""
    assert manager is not None
    try:
        fh = manager.result_stream(task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except ResultNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def iterfile():
        try:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            fh.close()

    return StreamingResponse(
        iterfile(),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="stations_temperature_{task_id}.csv"'
            )
        },
    )
