from __future__ import annotations

import os
import re
import shutil
import time

from UI.task_queue import jobs_root


_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def cleanup_expired_jobs() -> int:
    retention = max(600, int(os.environ.get("I2GIA_JOB_FILE_TTL_SECONDS", "86400")))
    cutoff = time.time() - retention
    removed = 0
    root = jobs_root().resolve()
    for path in root.iterdir():
        if not path.is_dir() or not _JOB_ID_PATTERN.fullmatch(path.name):
            continue
        resolved = path.resolve()
        if resolved.parent != root or resolved.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(resolved)
        removed += 1
    return removed


if __name__ == "__main__":
    print(f"removed_expired_job_directories={cleanup_expired_jobs()}")
