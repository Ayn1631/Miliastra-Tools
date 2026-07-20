from __future__ import annotations

import time
from typing import Any

import streamlit as st

from UI.task_queue import (
    QueuedAction,
    TaskExecutionError,
    TaskQueueError,
    inspect_task,
    load_task_result,
    queue_enabled,
    submit_task,
)


def _progress_widgets(prefix: str):
    started_at = time.perf_counter()
    progress = st.progress(0, text=f"{prefix}：准备中 · 0%")
    info = st.empty()
    last_percent = -1
    last_update_at = 0.0

    def update(percent: int, message: str) -> None:
        nonlocal last_percent, last_update_at
        normalized = max(0, min(100, int(percent)))
        now = time.perf_counter()
        if normalized not in (0, 100) and normalized == last_percent and now - last_update_at < 0.15:
            return
        elapsed = now - started_at
        progress.progress(
            normalized,
            text=f"{message} · {normalized}% · 已用 {elapsed:.1f}s",
        )
        info.markdown(
            f"**当前任务：** {message}  ｜  **进度：** {normalized}%  ｜  **已用：** {elapsed:.1f}s"
        )
        last_percent = normalized
        last_update_at = now

    return progress, info, update, started_at


def run_heavy_action(
    prefix: str,
    action: QueuedAction,
    *,
    use_queue: bool = True,
) -> Any:
    """Run synchronously in development or through the single RQ worker in production."""
    progress, info, update, started_at = _progress_widgets(prefix)
    try:
        if not use_queue or not queue_enabled():
            value = action(update)
        else:
            ticket = submit_task(action, prefix)
            info.info(f"任务已提交：`{ticket.job_id}`。正在等待 Worker。")
            while True:
                snapshot = inspect_task(ticket)
                if snapshot.status == "queued":
                    position = snapshot.queue_position or 1
                    update(0, f"排队等待中，当前位置 {position}")
                elif snapshot.status in {"started", "deferred", "scheduled"}:
                    update(snapshot.progress, snapshot.message)
                elif snapshot.status == "finished":
                    update(100, "正在读取完成结果")
                    value = load_task_result(ticket)
                    break
                elif snapshot.status in {"failed", "stopped", "canceled"}:
                    detail = snapshot.error or snapshot.message
                    raise TaskExecutionError(f"{prefix}任务{snapshot.status}：{detail}")
                else:
                    update(snapshot.progress, snapshot.message)
                time.sleep(0.35)
    except TaskQueueError as exc:
        elapsed = time.perf_counter() - started_at
        progress.progress(100, text=f"{prefix}：处理失败 · 已用 {elapsed:.1f}s")
        info.error(str(exc))
        raise
    except Exception as exc:
        elapsed = time.perf_counter() - started_at
        progress.progress(100, text=f"{prefix}：处理失败 · 已用 {elapsed:.1f}s")
        info.error(f"{prefix}失败：{exc}")
        raise

    elapsed = time.perf_counter() - started_at
    progress.progress(100, text=f"{prefix}：完成 · 100% · 已用 {elapsed:.1f}s")
    info.success(f"{prefix}完成，已用 {elapsed:.1f}s。")
    return value

