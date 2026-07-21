from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class ColorMergeMode(str, Enum):
    """矩形扩展时的颜色接受策略。"""

    EXACT = "exact"
    SEED = "seed"
    DYNAMIC_MEAN = "dynamic_mean"
    RANGE = "range"


class ScanStrategy(str, Enum):
    HORIZONTAL_FIRST = "horizontal_first"
    VERTICAL_FIRST = "vertical_first"
    BEST_OF_BOTH = "best_of_both"


class ResampleMode(str, Enum):
    NEAREST = "nearest"
    BOX = "box"
    BILINEAR = "bilinear"
    BICUBIC = "bicubic"
    LANCZOS = "lanczos"


@dataclass(frozen=True)
class RasterAlgorithmSettings:
    """只影响图像分析结果的参数。

    改动此对象需要重算 RasterPlan。GIA/GIL 的 ID、分组、坐标量化等
    导出参数不在这里，因此调整导出不会触发图像算法重跑。
    """

    max_pixels: int = 4096
    max_width_px: int | None = None
    max_height_px: int | None = None
    alpha_threshold: int = 1
    palette_enabled: bool = False
    palette_colors: int = 64
    merge_rectangles: bool = True
    merge_color_mode: ColorMergeMode = ColorMergeMode.RANGE
    color_tolerance: int = 0
    include_alpha_in_color_distance: bool = True
    background_rgb: tuple[int, int, int] | None = None
    background_tolerance: int = 0
    resample_mode: ResampleMode = ResampleMode.LANCZOS
    scan_strategy: ScanStrategy = ScanStrategy.BEST_OF_BOTH

    def __post_init__(self) -> None:
        object.__setattr__(self, "merge_color_mode", ColorMergeMode(self.merge_color_mode))
        object.__setattr__(self, "resample_mode", ResampleMode(self.resample_mode))
        object.__setattr__(self, "scan_strategy", ScanStrategy(self.scan_strategy))
        if self.background_rgb is not None:
            object.__setattr__(self, "background_rgb", tuple(int(v) for v in self.background_rgb))
        if self.max_pixels < 1:
            raise ValueError("max_pixels 必须 >= 1")
        if self.max_width_px is not None and self.max_width_px < 1:
            raise ValueError("max_width_px 必须为 None 或 >= 1")
        if self.max_height_px is not None and self.max_height_px < 1:
            raise ValueError("max_height_px 必须为 None 或 >= 1")
        if not 1 <= self.alpha_threshold <= 256:
            raise ValueError("alpha_threshold 必须位于 1..256，且采用包含判定 alpha >= threshold")
        if not 2 <= self.palette_colors <= 256:
            raise ValueError("palette_colors 必须位于 2..256")
        if not 0 <= self.color_tolerance <= 255:
            raise ValueError("color_tolerance 必须位于 0..255")
        if not 0 <= self.background_tolerance <= 255:
            raise ValueError("background_tolerance 必须位于 0..255")
        if self.background_rgb is not None:
            if len(self.background_rgb) != 3 or any(not 0 <= int(v) <= 255 for v in self.background_rgb):
                raise ValueError("background_rgb 必须是三个 0..255 的整数")

    def canonical_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["merge_color_mode"] = self.merge_color_mode.value
        data["resample_mode"] = self.resample_mode.value
        data["scan_strategy"] = self.scan_strategy.value
        if self.background_rgb is not None:
            data["background_rgb"] = list(self.background_rgb)
        return data

    def digest(self) -> str:
        payload = json.dumps(self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RasterRect:
    x: int
    y: int
    width: int
    height: int
    rgba: tuple[int, int, int, int]

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class RasterPlan:
    source_size_px: tuple[int, int]
    sampled_size_px: tuple[int, int]
    rectangles: tuple[RasterRect, ...]
    algorithm_settings: RasterAlgorithmSettings
    source_sha256: str
    sampled_rgba_sha256: str

    @property
    def cache_key(self) -> str:
        h = hashlib.sha256()
        h.update(self.source_sha256.encode("ascii"))
        h.update(b"\0")
        h.update(self.algorithm_settings.digest().encode("ascii"))
        return h.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source_size_px": list(self.source_size_px),
            "sampled_size_px": list(self.sampled_size_px),
            "source_sha256": self.source_sha256,
            "sampled_rgba_sha256": self.sampled_rgba_sha256,
            "cache_key": self.cache_key,
            "algorithm_settings": self.algorithm_settings.canonical_dict(),
            "rectangles": [
                {
                    "x": r.x,
                    "y": r.y,
                    "width": r.width,
                    "height": r.height,
                    "rgba": list(r.rgba),
                }
                for r in self.rectangles
            ],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RasterPlan":
        raw_settings = dict(data["algorithm_settings"])
        raw_settings["merge_color_mode"] = ColorMergeMode(raw_settings["merge_color_mode"])
        raw_settings["resample_mode"] = ResampleMode(raw_settings["resample_mode"])
        raw_settings["scan_strategy"] = ScanStrategy(raw_settings["scan_strategy"])
        if raw_settings.get("background_rgb") is not None:
            raw_settings["background_rgb"] = tuple(raw_settings["background_rgb"])
        settings = RasterAlgorithmSettings(**raw_settings)
        rects = tuple(
            RasterRect(
                x=int(item["x"]),
                y=int(item["y"]),
                width=int(item["width"]),
                height=int(item["height"]),
                rgba=tuple(int(v) for v in item["rgba"]),
            )
            for item in data["rectangles"]
        )
        return cls(
            source_size_px=tuple(int(v) for v in data["source_size_px"]),
            sampled_size_px=tuple(int(v) for v in data["sampled_size_px"]),
            rectangles=rects,
            algorithm_settings=settings,
            source_sha256=str(data["source_sha256"]),
            sampled_rgba_sha256=str(data["sampled_rgba_sha256"]),
        )

    @classmethod
    def from_json(cls, text: str) -> "RasterPlan":
        return cls.from_dict(json.loads(text))
