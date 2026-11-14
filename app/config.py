from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    """运行参数，均可通过环境变量覆盖。"""

    data_dir: str = os.environ.get("APP_DATA_DIR", "data")
    max_upload_bytes: int = _env_int("APP_MAX_UPLOAD_BYTES", 32 * 1024 * 1024)
    max_stations: int = _env_int("APP_MAX_STATIONS", 100)
    max_parallel_tasks: int = _env_int("APP_MAX_PARALLEL_TASKS", 2)
    task_timeout_seconds: float = _env_float("APP_TASK_TIMEOUT_SECONDS", 60.0)
    # 每个时间块的步数，演示用慢提取按块休眠
    chunk_size: int = _env_int("APP_CHUNK_SIZE", 10)

    @property
    def upload_dir(self) -> str:
        return os.path.join(self.data_dir, "uploads")

    @property
    def result_dir(self) -> str:
        return os.path.join(self.data_dir, "results")

    @property
    def registry_path(self) -> str:
        return os.path.join(self.data_dir, "registry.json")
