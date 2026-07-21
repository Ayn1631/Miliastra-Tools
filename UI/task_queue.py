from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import cloudpickle

if TYPE_CHECKING:
    from redis import Redis
    from rq import Queue


ProgressCallback = Callable[[int, str], None]
QueuedAction = Callable[[ProgressCallback], Any]
_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class TaskQueueError(RuntimeError):
    """Base error for queue submission, status polling, and result loading."""


class TaskQueueFullError(TaskQueueError):
    pass


class TaskQueueUnavailableError(TaskQueueError):
    pass


class TaskExecutionError(TaskQueueError):
    pass


@dataclass(frozen=True)
class TaskTicket:
    job_id: str
    label: str


@dataclass(frozen=True)
class TaskSnapshot:
    job_id: str
    status: str
    progress: int
    message: str
    queue_position: int | None
    error: str | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def queue_enabled() -> bool:
    return _env_bool("I2GIA_QUEUE_ENABLED", False)


def runtime_root() -> Path:
    configured = os.environ.get("I2GIA_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / ".i2gia-runtime").resolve()


def jobs_root() -> Path:
    path = runtime_root() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validated_job_id(job_id: str) -> str:
    normalized = str(job_id).strip().lower()
    if not _JOB_ID_PATTERN.fullmatch(normalized):
        raise TaskQueueError(f"invalid internal job id: {job_id!r}")
    return normalized


def job_directory(job_id: str) -> Path:
    normalized = _validated_job_id(job_id)
    root = jobs_root()
    path = (root / normalized).resolve()
    if path.parent != root.resolve():
        raise TaskQueueError("job directory escaped the configured runtime root")
    return path


def payload_path(job_id: str) -> Path:
    return job_directory(job_id) / "payload.pkl"


def result_path(job_id: str) -> Path:
    return job_directory(job_id) / "result.pkl"


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        # Windows development environments may not support POSIX permissions.
        pass


def prepare_task(action: QueuedAction, label: str) -> TaskTicket:
    """Persist an internal callable without putting image/result bytes in Redis."""
    job_id = uuid.uuid4().hex
    directory = job_directory(job_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    temporary = directory / "payload.pkl.tmp"
    with temporary.open("wb") as handle:
        cloudpickle.dump(action, handle, protocol=cloudpickle.DEFAULT_PROTOCOL)
    os.replace(temporary, payload_path(job_id))
    _chmod_private(payload_path(job_id))
    manifest = {
        "job_id": job_id,
        "label": str(label),
        "state": "prepared",
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _chmod_private(manifest_path)
    return TaskTicket(job_id=job_id, label=str(label))


def redis_connection() -> Redis:
    try:
        from redis import Redis
    except ImportError as exc:
        raise TaskQueueUnavailableError(
            "任务队列需要 redis 和 rq；本地同步模式无需安装这两个依赖"
        ) from exc
    url = os.environ.get("I2GIA_REDIS_URL", "redis://127.0.0.1:6379/0")
    return Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )


def task_queue(connection: Redis | None = None) -> Queue:
    try:
        from rq import Queue
    except ImportError as exc:
        raise TaskQueueUnavailableError(
            "任务队列需要 redis 和 rq；本地同步模式无需安装这两个依赖"
        ) from exc
    timeout = max(60, int(os.environ.get("I2GIA_JOB_TIMEOUT_SECONDS", "1800")))
    return Queue(
        os.environ.get("I2GIA_QUEUE_NAME", "i2gia"),
        connection=connection or redis_connection(),
        default_timeout=timeout,
    )


def submit_task(action: QueuedAction, label: str) -> TaskTicket:
    from UI.task_worker import execute_serialized_job

    connection = redis_connection()
    queue = task_queue(connection)
    try:
        connection.ping()
        maximum = max(1, int(os.environ.get("I2GIA_QUEUE_MAX_LENGTH", "20")))
        if queue.count >= maximum:
            raise TaskQueueFullError(
                f"任务队列已满（{queue.count}/{maximum}），请等待前面的任务完成。"
            )
        ticket = prepare_task(action, label)
        retention = max(600, int(os.environ.get("I2GIA_RESULT_TTL_SECONDS", "86400")))
        timeout = max(60, int(os.environ.get("I2GIA_JOB_TIMEOUT_SECONDS", "1800")))
        queue.enqueue(
            execute_serialized_job,
            ticket.job_id,
            job_id=ticket.job_id,
            description=ticket.label,
            job_timeout=timeout,
            result_ttl=retention,
            failure_ttl=retention,
            meta={
                "label": ticket.label,
                "progress": 0,
                "message": "任务已进入队列",
            },
        )
        return ticket
    except TaskQueueError:
        raise
    except Exception as exc:
        raise TaskQueueUnavailableError(f"无法连接任务队列：{exc}") from exc


def _status_text(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def inspect_task(ticket: TaskTicket) -> TaskSnapshot:
    try:
        from rq.job import Job
    except ImportError as exc:
        raise TaskQueueUnavailableError(
            "任务队列需要 redis 和 rq；本地同步模式无需安装这两个依赖"
        ) from exc
    connection = redis_connection()
    queue = task_queue(connection)
    try:
        job = Job.fetch(ticket.job_id, connection=connection)
        status = _status_text(job.get_status(refresh=True))
        meta = dict(job.meta or {})
        position = queue.get_job_position(ticket.job_id) if status == "queued" else None
        error = None
        if status == "failed":
            error = str(job.exc_info or "任务执行失败")
        return TaskSnapshot(
            job_id=ticket.job_id,
            status=status,
            progress=max(0, min(100, int(meta.get("progress", 0)))),
            message=str(meta.get("message") or status),
            queue_position=(int(position) + 1 if position is not None else None),
            error=error,
        )
    except Exception as exc:
        raise TaskQueueUnavailableError(f"读取任务状态失败：{exc}") from exc


def load_task_result(ticket: TaskTicket) -> Any:
    path = result_path(ticket.job_id)
    if not path.is_file():
        raise TaskExecutionError("任务已结束，但结果文件不存在。")
    try:
        with path.open("rb") as handle:
            return cloudpickle.load(handle)
    except Exception as exc:
        raise TaskExecutionError(f"读取任务结果失败：{exc}") from exc
