from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


EDGE_OPERATORS = ("Canny", "Sobel", "Scharr", "Laplacian", "Prewitt")
SOURCE_EDGE = "edge_detection"
SOURCE_LINE_ART = "line_art"
COLOR_MODE_SPECIFIED = "specified_rgb"
COLOR_MODE_AUTO_NONZERO = "auto_nonzero"
COLOR_MODE_PASSTHROUGH = "passthrough"


@dataclass(frozen=True)
class LineCleanupConfig:
    alpha_threshold: int = 1
    close_kernel: int = 1
    remove_small_components: int = 4
    exclude_curve_length_enabled: bool = True
    exclude_curve_length_px: float = 8.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> LineCleanupConfig:
        source = value or {}
        return cls(
            alpha_threshold=int(source.get("alpha_threshold", 1)),
            close_kernel=int(source.get("close_kernel", 1)),
            remove_small_components=int(source.get("remove_small_components", 4)),
            exclude_curve_length_enabled=bool(source.get("exclude_curve_length_enabled", True)),
            exclude_curve_length_px=float(source.get("exclude_curve_length_px", 8.0)),
        )


def default_operator_params(operator: str) -> dict[str, int | float]:
    if operator == "Canny":
        return {"low_threshold": 50, "high_threshold": 150, "aperture_size": 3}
    if operator == "Sobel":
        return {"threshold": 45, "kernel_size": 3}
    if operator == "Scharr":
        return {"threshold": 55}
    if operator == "Laplacian":
        return {"threshold": 35, "kernel_size": 3}
    return {"threshold": 45}


@dataclass(frozen=True)
class EdgeProcessingConfig:
    operator: str = "Canny"
    operator_params: dict[str, int | float] = field(default_factory=lambda: default_operator_params("Canny"))
    blur_kernel: int = 3
    cleanup: LineCleanupConfig = field(default_factory=LineCleanupConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> EdgeProcessingConfig:
        source = value or {}
        operator = str(source.get("operator", "Canny"))
        if operator not in EDGE_OPERATORS:
            operator = "Canny"
        params = source.get("operator_params")
        return cls(
            operator=operator,
            operator_params=dict(params) if isinstance(params, dict) else default_operator_params(operator),
            blur_kernel=int(source.get("blur_kernel", 3)),
            cleanup=LineCleanupConfig.from_dict(source.get("cleanup")),
        )


@dataclass(frozen=True)
class SketchProcessingConfig:
    """Complete edge-detection and uploaded-line-art processing settings."""

    source_mode: str = SOURCE_EDGE
    edge: EdgeProcessingConfig = field(default_factory=EdgeProcessingConfig)
    line_color_mode: str = COLOR_MODE_AUTO_NONZERO
    line_rgb: tuple[int, int, int] = (0, 0, 0)
    line_rgb_tolerance: int = 16
    nonzero_threshold: int = 0
    auto_explore_polarity: bool = True
    line_cleanup: LineCleanupConfig = field(default_factory=LineCleanupConfig)

    @property
    def active_cleanup(self) -> LineCleanupConfig:
        return self.edge.cleanup if self.source_mode == SOURCE_EDGE else self.line_cleanup

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> SketchProcessingConfig:
        source = value or {}
        if "edge" not in source and any(key in source for key in ("operator", "operator_params", "blur_kernel")):
            return cls(source_mode=SOURCE_EDGE, edge=EdgeProcessingConfig.from_dict(source))
        source_mode = str(source.get("source_mode", SOURCE_EDGE))
        if source_mode not in (SOURCE_EDGE, SOURCE_LINE_ART):
            source_mode = SOURCE_EDGE
        color_mode = str(source.get("line_color_mode", COLOR_MODE_AUTO_NONZERO))
        if color_mode not in (COLOR_MODE_SPECIFIED, COLOR_MODE_AUTO_NONZERO, COLOR_MODE_PASSTHROUGH):
            color_mode = COLOR_MODE_AUTO_NONZERO
        raw_rgb = source.get("line_rgb", (0, 0, 0))
        if not isinstance(raw_rgb, (list, tuple)) or len(raw_rgb) != 3:
            raw_rgb = (0, 0, 0)
        return cls(
            source_mode=source_mode,
            edge=EdgeProcessingConfig.from_dict(source.get("edge")),
            line_color_mode=color_mode,
            line_rgb=tuple(max(0, min(255, int(channel))) for channel in raw_rgb),
            line_rgb_tolerance=int(source.get("line_rgb_tolerance", 16)),
            nonzero_threshold=int(source.get("nonzero_threshold", 0)),
            auto_explore_polarity=bool(source.get("auto_explore_polarity", True)),
            line_cleanup=LineCleanupConfig.from_dict(source.get("line_cleanup")),
        )
