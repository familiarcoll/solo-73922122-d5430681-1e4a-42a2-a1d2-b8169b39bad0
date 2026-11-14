"""API 端到端测试：上传、状态、取消、下载、并发上限、重启恢复。"""

import json
import os
import time

import pytest


def _post_task(client, nc_path, variable="tas", stations=None):
    c, _ = client
    if stations is None:
        stations = {"stations": [{"id": "S1", "lat": 10.0, "lon": 20.0}]}
    with open(nc_path, "rb") as fh:
        return c.post(
            "/api/tasks",
            files={"file": ("grid.nc", fh, "application/x-netcdf")},
            data={
                "variable": variable,
                "stations_json": json.dumps(stations),
            },
        )


def _wait_terminal(client, task_id, timeout=30.0):
    c, _ = client
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/tasks/{task_id}")
        state = r.json()["state"]
        if state in {"completed", "failed", "cancelled", "timeout"}:
            return r.json()
        time.sleep(0.1)
    raise AssertionError("任务未在限定时间内结束")


def test_full_flow_completed_and_download(client, paths):
    r = _post_task(client, paths["grid"])
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    final = _wait_terminal(client, task_id)
    assert final["state"] == "completed"
    assert final["download"] == f"/api/tasks/{task_id}/download"

    dl = client[0].get(f"/api/tasks/{task_id}/download")
    assert dl.status_code == 200
    assert "attachment" in dl.headers["content-disposition"]
    lines = dl.text.strip().splitlines()
    assert lines[0] == "station_id,date,grid_lat,grid_lon,temperature"
    assert len(lines) == 1 + 8  # 8 天 × 1 站


def test_csv_station_upload(client, paths):
    c, _ = client
    with open(paths["grid"], "rb") as fh:
        r = c.post(
            "/api/tasks",
            files={
                "file": ("grid.nc", fh, "application/x-netcdf"),
                "stations_file": ("st.csv", b"id,lat,lon\nA,1,2\nB,3,4\n", "text/csv"),
            },
            data={"variable": "tas"},
        )
    assert r.status_code == 201
    final = _wait_terminal(client, r.json()["task_id"])
    assert final["state"] == "completed"
    dl = c.get(f"/api/tasks/{r.json()['task_id']}/download")
    assert len(dl.text.strip().splitlines()) == 1 + 2 * 8


def test_rejects_bad_variable(client, paths):
    r = _post_task(client, paths["grid"], variable="missing")
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_rejects_non_gregorian(client, paths):
    r = _post_task(client, paths["noleap"])
    assert r.status_code == 400


def test_rejects_too_many_stations(client, paths):
    from app import main as main_mod

    object.__setattr__(main_mod.settings, "max_stations", 2)
    stations = {"stations": [
        {"id": str(i), "lat": 0.0, "lon": 0.0} for i in range(3)
    ]}
    with open(paths["grid"], "rb") as fh:
        r = client[0].post(
            "/api/tasks",
            files={"file": ("g.nc", fh, "application/x-netcdf")},
            data={"variable": "tas", "stations_json": json.dumps(stations)},
        )
    assert r.status_code == 400
    assert "上限" in r.json()["detail"]


def test_rejects_oversized_upload(client, paths):
    from app import main as main_mod

    object.__setattr__(main_mod.settings, "max_upload_bytes", 128)
    r = _post_task(client, paths["grid"])
    assert r.status_code == 400
    assert "32MiB" in r.json()["detail"] or "上限" in r.json()["detail"]


def test_parallel_limit_rejects_third_and_releases_slot(client, paths):
    c, _ = client
    ids = []
    # 慢任务每个约 12s，足够占住槽位
    for _ in range(2):
        r = _post_task(client, paths["slow_cancel"])
        assert r.status_code == 201
        ids.append(r.json()["task_id"])
    third = _post_task(client, paths["slow_cancel"])
    assert third.status_code == 409

    # 取消两个任务，槽位释放后新任务应被接受
    for tid in ids:
        c.post(f"/api/tasks/{tid}/cancel")
    for tid in ids:
        final = _wait_terminal(client, tid, timeout=20)
        assert final["state"] == "cancelled"

    again = _post_task(client, paths["grid"])
    assert again.status_code == 201
    assert _wait_terminal(client, again.json()["task_id"])["state"] == "completed"


def test_cancel_blocks_download_and_no_late_result(client, paths):
    c, _ = client
    r = _post_task(client, paths["slow_cancel"])
    tid = r.json()["task_id"]
    time.sleep(1.0)
    cr = c.post(f"/api/tasks/{tid}/cancel")
    assert cr.json()["state"] == "cancelling"
    final = _wait_terminal(client, tid, timeout=20)
    assert final["state"] == "cancelled"

    dl = c.get(f"/api/tasks/{tid}/download")
    assert dl.status_code == 409
    # 等待一段时间确认不会有迟到结果
    time.sleep(2.0)
    assert c.get(f"/api/tasks/{tid}").json()["state"] == "cancelled"


def test_timeout_marked_and_download_denied(client, paths, monkeypatch):
    from app import main as main_mod

    # Settings 是 frozen dataclass，需绕过 __setattr__（manager 共享同一实例）
    object.__setattr__(main_mod.settings, "task_timeout_seconds", 3.0)
    assert main_mod.manager.settings is main_mod.settings
    r = _post_task(client, paths["slow_timeout"])
    final = _wait_terminal(client, r.json()["task_id"], timeout=20)
    assert final["state"] == "timeout"
    dl = client[0].get(f"/api/tasks/{r.json()['task_id']}/download")
    assert dl.status_code == 409


def test_restart_marks_unfinished_failed_but_keeps_completed(client, paths):
    c, main_mod = client
    r = _post_task(client, paths["grid"])
    done_id = r.json()["task_id"]
    assert _wait_terminal(client, done_id)["state"] == "completed"

    # 再造一个正在运行的慢任务
    slow = _post_task(client, paths["slow_cancel"])
    slow_id = slow.json()["task_id"]
    time.sleep(0.5)

    # 用同一数据目录新建 TaskManager，模拟重启
    from app.task_manager import TaskManager

    new_manager = TaskManager(main_mod.settings)
    assert new_manager.get(done_id).state == "completed"
    assert new_manager.get(slow_id).state == "failed"
    assert "重启" in (new_manager.get(slow_id).error or "")

    # 已完成结果仍可下载
    fh = new_manager.result_stream(done_id)
    content = fh.read()
    fh.close()
    assert b"station_id,date" in content

    # 重启后槽位已释放，可接受新任务
    assert new_manager._active == 0


def test_404_for_unknown_task(client):
    r = client[0].get("/api/tasks/nonexistent")
    assert r.status_code == 404


def test_missing_stations_rejected(client, paths):
    with open(paths["grid"], "rb") as fh:
        r = client[0].post(
            "/api/tasks",
            files={"file": ("g.nc", fh, "application/x-netcdf")},
            data={"variable": "tas"},
        )
    assert r.status_code == 400
