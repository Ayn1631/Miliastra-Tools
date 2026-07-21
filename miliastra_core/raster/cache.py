from __future__ import annotations

import json
from pathlib import Path

from miliastra_core._io import atomic_write_text

from .models import RasterPlan


class RasterPlanCache:
    """磁盘缓存只按源图哈希 + 算法参数键控，不包含任何导出参数。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, cache_key: str) -> Path:
        return self.directory / f"{cache_key}.raster-plan.json"

    def load(self, cache_key: str) -> RasterPlan | None:
        path = self.path_for(cache_key)
        if not path.exists():
            return None
        return RasterPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def store(self, plan: RasterPlan) -> Path:
        path = self.path_for(plan.cache_key)
        return atomic_write_text(path, plan.to_json())
