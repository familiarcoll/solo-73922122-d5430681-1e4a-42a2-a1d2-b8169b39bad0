"""任务生命周期管理：并发限制、取消、60s 超时、持久化与重启恢复。"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import states
from .config import Settings
from .extraction import TaskInterrupted, extract_station_series
from .validation import GridInfo, Station


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskRecord:
    task_id: str
    state: str
    variable: str
    station_count: int
    upload_path: str
    result_path: str | None = None
    error: str | None = None
    progress_time: int = 0
    total_time: int = 0
    created_at: str = field(default_factory=_utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    # 受理是否已完整完成（写入注册表 + 工作线程启动成功）。
    # 未完成受理的残留记录在重启恢复时会被丢弃，而不是伪装成 failed 任务。
    admitted: bool = True
    # 仅运行期使用，不写入注册表
    stop_event: threading.Event | None = field(default=None, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    slot_held: bool = field(default=False, repr=False)
    # 工作线程在此事件放行前不得触碰任何状态/注册表
    admission_gate: threading.Event | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        out = {
            "task_id": self.task_id,
            "state": self.state,
            "variable": self.variable,
            "station_count": self.station_count,
            "progress_time": self.progress_time,
            "total_time": self.total_time,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "download": (
                f"/api/tasks/{self.task_id}/download"
                if self.state == states.COMPLETED
                else None
            ),
        }
        return out


class TaskManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._active = 0
        # 可替换的线程工厂：故障注入用例可借此让 start() 失败
        self._thread_factory = threading.Thread
        os.makedirs(settings.upload_dir, exist_ok=True)
        os.makedirs(settings.result_dir, exist_ok=True)
        self._recover_from_registry()
        self._cleanup_orphan_tempfiles()

    # --------------------------- 持久化 / 恢复 ---------------------------

    def _persist_locked(self) -> None:
        payload = {
            tid: {**rec.to_dict(), "admitted": rec.admitted}
            for tid, rec in self._tasks.items()
        }
        tmp = f"{self.settings.registry_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.settings.registry_path)

    def _recover_from_registry(self) -> None:
        """重启后未完成任务一律记 FAILED；已完成结果保持可下载。

        受理未完成的记录（登记写入或线程启动失败后残留）直接丢弃：
        它们从未真正进入任务生命周期，不得因此变成可查询的 failed 任务。
        """
        # 上次受理中途失败可能遗留注册表临时文件，一并清理
        self._safe_remove(f"{self.settings.registry_path}.tmp")
        path = self.settings.registry_path
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return

        now = _utc_now_iso()
        for task_id, data in payload.items():
            if not data.get("admitted", True):
                # 失败受理的残留记录：重启后既不恢复也不占位
                continue
            state = data.get("state")
            result_basename = None
            if state == states.COMPLETED:
                result_basename = f"{task_id}.csv"
                result_path = os.path.join(
                    self.settings.result_dir, result_basename
                )
                if not os.path.exists(result_path):
                    # 结果文件丢失，无法再下载
                    state = states.FAILED
                    data["error"] = "重启后结果文件缺失"
                    result_basename = None
            rec = TaskRecord(
                task_id=task_id,
                state=state if state in states.TERMINAL_STATES else states.FAILED,
                variable=data.get("variable", ""),
                station_count=data.get("station_count", 0),
                # 重启后的任务不再持有上传文件（原文件已随任务结束删除或随进程失效）
                upload_path="",
                result_path=(
                    os.path.join(self.settings.result_dir, result_basename)
                    if result_basename
                    else None
                ),
                error=(
                    data.get("error")
                    if state == states.FAILED
                    else ("服务重启，任务未完成" if state not in states.TERMINAL_STATES else None)
                ),
                progress_time=data.get("progress_time", 0),
                total_time=data.get("total_time", 0),
                created_at=data.get("created_at", now),
                started_at=data.get("started_at"),
                finished_at=now,
                admitted=True,
            )
            self._tasks[task_id] = rec
        self._persist_locked()

    def _cleanup_orphan_tempfiles(self) -> None:
        """清理上次崩溃遗留的临时结果文件与无主上传文件。"""
        for pattern in ("*.tmp.*",):
            for p in glob.glob(os.path.join(self.settings.result_dir, pattern)):
                self._safe_remove(p)
        live_uploads = {rec.upload_path for rec in self._tasks.values()}
        for p in glob.glob(os.path.join(self.settings.upload_dir, "*")):
            if os.path.isfile(p) and p not in live_uploads:
                self._safe_remove(p)

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    # --------------------------- 任务受理 ---------------------------

    def create_task(
        self,
        grid: GridInfo,
        stations: list[Station],
        saved_upload_path: str,
    ) -> TaskRecord:
        """满额（已达并行上限）直接拒绝，不排队。

        受理是一次全有或全无的交接：只有当「登记写入」与「工作线程启动」
        都成功，任务才对查询可见并占用并行槽位。任一步失败都会撤销登记、
        释放槽位、删除暂存文件，并抛出 AdmissionFailed；调用方据此返回 5xx，
        不会有任何可查询的残留任务。
        """
        with self._lock:
            if self._active >= self.settings.max_parallel_tasks:
                raise CapacityExhausted(
                    f"并行任务已满（{self.settings.max_parallel_tasks} 个），请稍后重试"
                )
            task_id = uuid.uuid4().hex
            rec = TaskRecord(
                task_id=task_id,
                state=states.PENDING,
                variable=grid.variable,
                station_count=len(stations),
                upload_path=saved_upload_path,
                stop_event=threading.Event(),
                admitted=False,
                slot_held=True,
                admission_gate=threading.Event(),
            )
            self._tasks[task_id] = rec
            self._active += 1

            try:
                # 1) 登记写入：失败则整体回滚
                self._persist_locked()
                # 2) 线程启动：工作线程先等待 gate，受理成功后才允许运行
                worker = self._thread_factory(
                    target=self._run_after_admission,
                    name=f"extract-{task_id}",
                    args=(rec, grid, stations),
                    daemon=True,
                )
                worker.start()
            except BaseException as exc:
                self._rollback_admission_locked(rec)
                raise AdmissionFailed(f"任务受理失败: {exc}") from exc

            # 3) 两道关卡都通过：正式提交（提交本身也要落盘，失败同样回滚）
            try:
                rec.worker = worker
                rec.admitted = True
                self._persist_locked()
            except BaseException as exc:
                self._rollback_admission_locked(rec)
                raise AdmissionFailed(f"任务受理失败: {exc}") from exc
            rec.admission_gate.set()
            return rec

    def _rollback_admission_locked(self, rec: TaskRecord) -> None:
        """撤销一次未完成的受理：登记、槽位、暂存文件全部不留痕。"""
        rec.admitted = False
        self._tasks.pop(rec.task_id, None)
        if rec.slot_held:
            self._active -= 1
            rec.slot_held = False
        # 放行可能已创建的工作线程，让它在 _run_after_admission 中立即退出
        if rec.admission_gate is not None:
            rec.admission_gate.set()
        try:
            self._persist_locked()
        except Exception:
            # 注册表本身不可写时，至少保证内存态与暂存文件干净；
            # 磁盘上残留的 admitted=false 记录会在重启恢复时被丢弃
            self._safe_remove(f"{self.settings.registry_path}.tmp")
        self._safe_remove(rec.upload_path)

    # --------------------------- 工作线程 ---------------------------

    def _run_after_admission(
        self, rec: TaskRecord, grid: GridInfo, stations: list[Station]
    ) -> None:
        """等待受理提交后再进入计算；未获放行则静默退出。"""
        gate = rec.admission_gate
        if gate is not None:
            gate.wait()
            if not rec.admitted:
                return
        self._run(rec, grid, stations)

    def _run(
        self, rec: TaskRecord, grid: GridInfo, stations: list[Station]
    ) -> None:
        """计算主流程。

        保证：取消/失败/超时均等实际计算停止、xarray 数据集与文件关闭后，
        才清理暂存并写入终态；全程不触碰其他任务的资源。
        """
        result_path = os.path.join(self.settings.result_dir, f"{rec.task_id}.csv")
        deadline = time.monotonic() + self.settings.task_timeout_seconds
        terminal: str | None = None
        error_msg: str | None = None

        with self._lock:
            rec.state = states.RUNNING
            rec.started_at = _utc_now_iso()
            rec.total_time = grid.ntime
            self._persist_locked()

        def progress(done: int, total: int) -> None:
            with self._lock:
                rec.progress_time = done
                rec.total_time = total
                self._persist_locked()

        try:
            extract_station_series(
                rec.upload_path,
                grid.variable,
                stations,
                result_path,
                chunk_size=self.settings.chunk_size,
                demo_slow=grid.demo_slow,
                demo_delay=grid.demo_delay,
                demo_chunk=grid.demo_chunk,
                stop_event=rec.stop_event,
                deadline_monotonic=deadline,
                progress_cb=progress,
            )
            terminal = states.COMPLETED
        except TaskInterrupted as exc:
            # 取消已受理则绝不发布迟到结果；超时同理
            if rec.stop_event is not None and rec.stop_event.is_set():
                terminal = states.CANCELLED
            else:
                terminal = (
                    states.CANCELLED if exc.reason == "cancelled" else states.TIMEOUT
                )
            if terminal == states.TIMEOUT:
                error_msg = (
                    f"计算超过 {self.settings.task_timeout_seconds:.0f} 秒限制"
                )
        except Exception as exc:  # noqa: BLE001 - 任何提取失败都要落终态
            terminal = states.FAILED
            error_msg = f"{type(exc).__name__}: {exc}"
        # 到此处 extract 内部的 with/finally 已保证数据集与文件句柄关闭

        with self._lock:
            # 清理必须发生在实际计算停止、文件关闭之后
            if terminal == states.COMPLETED:
                if not os.path.exists(result_path):
                    terminal = states.FAILED
                    error_msg = "结果文件未生成"
                else:
                    rec.result_path = result_path
            else:
                self._safe_remove(result_path)

            rec.state = terminal
            rec.error = error_msg
            rec.finished_at = _utc_now_iso()
            rec.stop_event = None
            rec.worker = None
            if rec.slot_held:
                self._active -= 1
                rec.slot_held = False
            self._persist_locked()

        # 任务结束后释放上传文件（结果 CSV 是唯一交付物）；
        # 重启恢复的记录 upload_path 为空，跳过即可
        if rec.upload_path:
            self._safe_remove(rec.upload_path)

    # --------------------------- 查询 / 取消 / 下载 ---------------------------

    def request_cancel(self, task_id: str) -> TaskRecord:
        """受理取消：置 cancelling 并通知工作线程；终态任务返回当前状态。"""
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise TaskNotFound(task_id)
            if rec.state in states.TERMINAL_STATES:
                return rec
            rec.state = states.CANCELLING
            if rec.stop_event is not None:
                rec.stop_event.set()
            self._persist_locked()
            return rec

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise TaskNotFound(task_id)
            return rec

    def result_stream(self, task_id: str):
        """只有 COMPLETED 任务能拿到结果文件句柄。"""
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise TaskNotFound(task_id)
            if rec.state != states.COMPLETED or not rec.result_path:
                raise ResultNotReady(task_id, rec.state)
            if not os.path.exists(rec.result_path):
                raise ResultNotReady(task_id, rec.state)
            return open(rec.result_path, "rb")

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return [rec.to_dict() for rec in self._tasks.values()]


class CapacityExhausted(RuntimeError):
    """并行槽位已满。"""


class AdmissionFailed(RuntimeError):
    """受理交接失败（登记写入或工作线程启动出错），未留下任何任务。"""


class TaskNotFound(KeyError):
    def __init__(self, task_id: str):
        super().__init__(task_id)
        self.task_id = task_id


class ResultNotReady(RuntimeError):
    def __init__(self, task_id: str, state: str):
        super().__init__(f"任务 {task_id} 当前状态 {state}，结果尚不可下载")
        self.task_id = task_id
        self.state = state
