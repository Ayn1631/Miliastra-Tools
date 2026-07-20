from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import cloudpickle
from rq import get_current_job

from UI.task_queue import payload_path, result_path


def _configure_worker_threads() -> None:
    thread_count = max(1, int(os.environ.get("I2GIA_OPENCV_THREADS", "2")))
    try:
        import cv2

        cv2.setNumThreads(thread_count)
    except (ImportError, ValueError):
        pass


def _update_progress(percent: int, message: str) -> None:
    job = get_current_job()
    if job is None:
        return
    job.meta["progress"] = max(0, min(100, int(percent)))
    job.meta["message"] = str(message)
    job.save_meta()


def execute_serialized_job(job_id: str) -> dict[str, Any]:
    """RQ entrypoint. Only executes callables written into the controlled job root."""
    _configure_worker_threads()
    source = payload_path(job_id)
    destination = result_path(job_id)
    if not source.is_file():
        raise FileNotFoundError(f"queued payload does not exist: {source}")

    _update_progress(1, "Worker 已接收任务")
    with source.open("rb") as handle:
        action = cloudpickle.load(handle)
    if not callable(action):
        raise TypeError("queued payload is not callable")

    last_percent = -1
    last_update = 0.0

    def progress(percent: int, message: str) -> None:
        nonlocal last_percent, last_update
        normalized = max(0, min(100, int(percent)))
        now = time.monotonic()
        if normalized not in (0, 100) and normalized == last_percent and now - last_update < 0.2:
            return
        _update_progress(normalized, message)
        last_percent = normalized
        last_update = now

    value = action(progress)
    progress(98, "正在保存任务结果")
    temporary = destination.with_suffix(".pkl.tmp")
    with temporary.open("wb") as handle:
        cloudpickle.dump(value, handle, protocol=cloudpickle.DEFAULT_PROTOCOL)
    os.replace(temporary, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    progress(100, "任务完成")
    return {
        "job_id": str(job_id),
        "result_file": str(Path(destination).name),
    }

