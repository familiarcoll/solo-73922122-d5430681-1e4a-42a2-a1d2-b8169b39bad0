"""受理交接回归测试：登记写入/线程启动失败不得留下任务或占用槽位。"""

from __future__ import annotations

import json
import os
import time

from app.task_manager import TaskManager

TERMINAL = {"completed", "failed", "cancelled", "timeout"}


def _post(client, nc_path):
    c, _ = client
    with open(nc_path, "rb") as fh:
        return c.post(
            "/api/tasks",
            files={"file": ("grid.nc", fh, "application/x-netcdf")},
            data={
                "variable": "tas",
                "stations_json": json.dumps(
                    {"stations": [{"id": "S1", "lat": 10.0, "lon": 20.0}]}
                ),
            },
        )


def _wait_terminal(client, task_id, timeout=30.0):
    c, _ = client
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = c.get(f"/api/tasks/{task_id}").json()["state"]
        if state in TERMINAL:
            return c.get(f"/api/tasks/{task_id}").json()
        time.sleep(0.05)
    raise AssertionError("任务未在限定时间内结束")


def _upload_files(settings) -> list[str]:
    if not os.path.isdir(settings.upload_dir):
        return []
    return sorted(
        p for p in os.listdir(settings.upload_dir)
        if os.path.isfile(os.path.join(settings.upload_dir, p))
    )


def _registry(settings) -> dict:
    if not os.path.exists(settings.registry_path):
        return {}
    with open(settings.registry_path, encoding="utf-8") as fh:
        return json.load(fh)


def _assert_no_residue(client, task_id=None):
    c, main_mod = client
    mgr = main_mod.manager
    assert mgr._active == 0
    assert mgr.list_tasks() == []
    assert _registry(main_mod.settings) == {}
    assert _upload_files(main_mod.settings) == []
    # 失败请求返回的 201 主体中出现的 ID（若有）也不得可查询
    if task_id:
        assert c.get(f"/api/tasks/{task_id}").status_code == 404
    assert c.get("/api/tasks").json() == {"tasks": []}


def test_registry_write_failure_on_admission_leaves_nothing(client, paths, monkeypatch):
    c, main_mod = client
    mgr = main_mod.manager

    def boom():
        raise OSError("registry disk full")

    monkeypatch.setattr(mgr, "_persist_locked", boom)
    r = _post(client, paths["grid"])
    assert r.status_code == 503
    assert "受理失败" in r.json()["detail"]
    # 回滚后内存态、注册表、暂存目录都应干净，且槽位未被占用
    assert mgr._active == 0
    assert mgr.list_tasks() == []
    assert _upload_files(main_mod.settings) == []

    # 恢复写入能力后，服务仍可受理并完成真实任务
    monkeypatch.undo()
    ok = _post(client, paths["grid"])
    assert ok.status_code == 201, ok.text
    assert _wait_terminal(client, ok.json()["task_id"])["state"] == "completed"


def test_thread_start_failure_rolls_back_and_frees_slot(client, paths, monkeypatch):
    c, main_mod = client
    mgr = main_mod.manager

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(mgr, "_thread_factory", FailingThread)
    r = _post(client, paths["grid"])
    assert r.status_code == 503
    assert "受理失败" in r.json()["detail"]
    _assert_no_residue(client)

    # 失败受理不得留下 admitted=false 的注册表记录，重启后也不恢复
    restarted = TaskManager(main_mod.settings)
    assert restarted.list_tasks() == []
    assert restarted._active == 0

    # 恢复线程工厂后仍可正常受理，证明槽位没有泄漏
    monkeypatch.undo()
    ok = _post(client, paths["grid"])
    assert ok.status_code == 201, ok.text
    final = _wait_terminal(client, ok.json()["task_id"])
    assert final["state"] == "completed"
    # 成功受理仍交付完整 CSV
    dl = c.get(f"/api/tasks/{ok.json()['task_id']}/download")
    assert dl.status_code == 200
    assert len(dl.text.strip().splitlines()) == 1 + 8


def test_commit_write_failure_stops_gated_worker(client, paths, monkeypatch):
    """线程已启动但提交落盘失败：被 gate 拦住的工作线程不得运行。"""
    c, main_mod = client
    mgr = main_mod.manager
    original = mgr._persist_locked
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:  # 第一次登记成功，第二次提交失败
            raise OSError("registry fsync failed")
        return original()

    monkeypatch.setattr(mgr, "_persist_locked", flaky)
    r = _post(client, paths["grid"])
    assert r.status_code == 503
    assert calls["n"] >= 2
    # 放行后线程发现 admitted=False 立即退出，不会落 running/completed
    time.sleep(0.3)
    assert mgr.list_tasks() == []
    assert mgr._active == 0
    assert _registry(main_mod.settings) == {}
    assert _upload_files(main_mod.settings) == []

    monkeypatch.undo()
    ok = _post(client, paths["grid"])
    assert ok.status_code == 201
    assert _wait_terminal(client, ok.json()["task_id"])["state"] == "completed"


def test_admission_failure_does_not_consume_parallel_slot(client, paths, monkeypatch):
    c, main_mod = client
    mgr = main_mod.manager

    # 先占 1 个槽位（慢任务）
    slow = _post(client, paths["slow_cancel"])
    assert slow.status_code == 201

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread pool exhausted")

    monkeypatch.setattr(mgr, "_thread_factory", FailingThread)
    bad = _post(client, paths["grid"])
    assert bad.status_code == 503
    monkeypatch.undo()

    # 若失败受理错误占用槽位，此处会 409；正确实现应仍余 1 个槽位
    good = _post(client, paths["grid"])
    assert good.status_code == 201, good.text
    assert _wait_terminal(client, good.json()["task_id"])["state"] == "completed"

    # 此时慢任务 + 已完成任务，槽位应只剩 1，再建第二个慢任务占满，第三个 409
    second_slow = _post(client, paths["slow_cancel"])
    assert second_slow.status_code == 201
    assert _post(client, paths["slow_cancel"]).status_code == 409

    for tid in (slow.json()["task_id"], second_slow.json()["task_id"]):
        c.post(f"/api/tasks/{tid}/cancel")
        assert _wait_terminal(client, tid, timeout=20)["state"] == "cancelled"


def test_restart_ignores_residual_unadmitted_registry_record(client, paths, tmp_path):
    """模拟磁盘上遗留 admitted=false 的半截记录：重启后不得恢复或占位。"""
    c, main_mod = client
    settings = main_mod.settings
    residual_id = "deadbeefdeadbeefdeadbeefdeadbeef"
    payload = {
        residual_id: {
            "task_id": residual_id,
            "state": "pending",
            "variable": "tas",
            "station_count": 1,
            "progress_time": 0,
            "total_time": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "download": None,
            "admitted": False,
        }
    }
    with open(settings.registry_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    restarted = TaskManager(settings)
    assert restarted.list_tasks() == []
    assert restarted._active == 0
    # 重启后的注册表也不再包含该失败受理记录
    assert residual_id not in _registry(settings)

    # 两个并行槽位都可用：用一个慢任务占住，再成功受理第二个
    import shutil

    from app.validation import Station, inspect_netcdf

    upload_a = str(tmp_path / "upload_a.nc")
    upload_b = str(tmp_path / "upload_b.nc")
    shutil.copyfile(paths["slow_cancel"], upload_a)
    shutil.copyfile(paths["slow_cancel"], upload_b)

    first = restarted.create_task(
        inspect_netcdf(upload_a, "tas"),
        [Station("S1", 0.0, 0.0)],
        upload_a,
    )
    second = restarted.create_task(
        inspect_netcdf(upload_b, "tas"),
        [Station("S1", 0.0, 0.0)],
        upload_b,
    )
    assert first.admitted and second.admitted
    assert restarted._active == 2
    for rec in (first, second):
        restarted.request_cancel(rec.task_id)
    deadline = time.time() + 20
    while time.time() < deadline and restarted._active:
        time.sleep(0.05)
    assert restarted._active == 0


def test_successful_admission_persists_admitted_flag(client, paths):
    c, main_mod = client
    r = _post(client, paths["grid"])
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    assert _wait_terminal(client, task_id)["state"] == "completed"
    assert _registry(main_mod.settings)[task_id]["admitted"] is True


def test_two_parallel_slow_tasks_both_complete_full_csv(client, paths):
    """成功受理的两个并行任务都应完成，并各自开放完整 CSV。"""
    c, _ = client
    ids = []
    for _ in range(2):
        r = _post(client, paths["slow_cancel"])
        assert r.status_code == 201, r.text
        ids.append(r.json()["task_id"])

    for tid in ids:
        final = _wait_terminal(client, tid, timeout=30)
        assert final["state"] == "completed"
        dl = c.get(f"/api/tasks/{tid}/download")
        assert dl.status_code == 200
        lines = dl.text.strip().splitlines()
        assert lines[0] == "station_id,date,grid_lat,grid_lon,temperature"
        assert len(lines) == 1 + 30
