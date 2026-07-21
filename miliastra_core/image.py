from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from miliastra_core.export.gia import GiaExportSettings, build_gia_from_plan, raster_plan_to_gia_objects
from miliastra_core.export.quantization import QuantizationMode
from miliastra_core.raster.models import (
    ColorMergeMode,
    RasterAlgorithmSettings,
    RasterPlan,
    ResampleMode,
    ScanStrategy,
)
from miliastra_core.raster.pipeline import build_raster_plan as _build_raster_plan
from miliastra_core.raster.resize import resize_image

from miliastra_core.export.builder import DEFAULT_ENTITY_ID_START, build_gia
from miliastra_core.export.decoration import DEFAULT_WRAPPER_TEMPLATE_ID, MAX_DECORATIONS_PER_PARENT

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
DEFAULT_TEMPLATE_GIA = ROOT_DIR / "UI" / "白模解析.gia"
GRID_STEP_M = 0.01  # 仅为旧 UI 兼容，不再是算法和默认导出的全局规则。

COLLISION_MODE_NATIVE = "native"
COLLISION_MODE_NATIVE_AND_CLIMB = "native_and_climb"
COLLISION_MODE_OFF = "off"
_COLLISION_MODE_FLAGS = {
    COLLISION_MODE_NATIVE: (True, False),
    COLLISION_MODE_NATIVE_AND_CLIMB: (True, True),
    COLLISION_MODE_OFF: (False, False),
}


@dataclass(frozen=True)
class ImageGiaSettings:
    """兼容旧页面的一体化设置。

    新代码应优先调用 ``algorithm_settings()`` 生成 RasterPlan，再用
    ``gia_export_settings()`` 导出。这样只改碰撞、ID、量化或物理尺寸时，
    可以复用同一个 RasterPlan，不再重跑图像算法。
    """

    target_width_m: float
    target_height_m: float
    max_pixels: int
    alpha_threshold: int
    alpha_threshold_num: int
    template_id: int
    block_height_m: float | None = None
    entity_id_start: int = DEFAULT_ENTITY_ID_START + 100_000
    merge_rectangles: bool = False
    color_tolerance: int = 0
    background_rgb: tuple[int, int, int] | None = None
    background_tolerance: int = 0
    collision_mode: str = COLLISION_MODE_OFF
    enable_out_of_range_run: bool = False
    out_of_range_display_mode: int = 0

    # 新增图像算法参数
    max_width_px: int | None = None
    max_height_px: int | None = None
    palette_enabled: bool = False
    palette_colors: int = 64
    merge_color_mode: str = ColorMergeMode.RANGE.value
    resample_mode: str = ResampleMode.LANCZOS.value
    scan_strategy: str = ScanStrategy.BEST_OF_BOTH.value

    # 新增纯导出参数，修改它们不应使 RasterPlan 失效
    quantization_mode: str = QuantizationMode.NONE.value
    quantization_step_m: float = GRID_STEP_M
    decoration_packaging: bool = True
    max_decorations_per_parent: int = MAX_DECORATIONS_PER_PARENT
    wrapper_template_id: int = DEFAULT_WRAPPER_TEMPLATE_ID
    wrapper_static: bool = False
    wrapper_collision: bool = False
    wrapper_climb: bool = False
    wrapper_enable_out_of_range_run: bool = False
    wrapper_out_of_range_display_mode: int = 0

    def algorithm_settings(self) -> RasterAlgorithmSettings:
        return RasterAlgorithmSettings(
            max_pixels=int(self.max_pixels),
            max_width_px=self.max_width_px,
            max_height_px=self.max_height_px,
            alpha_threshold=int(self.alpha_threshold),
            palette_enabled=bool(self.palette_enabled),
            palette_colors=int(self.palette_colors),
            merge_rectangles=bool(self.merge_rectangles),
            merge_color_mode=ColorMergeMode(self.merge_color_mode),
            color_tolerance=int(self.color_tolerance),
            include_alpha_in_color_distance=self.alpha_threshold_num == -1,
            background_rgb=self.background_rgb,
            background_tolerance=int(self.background_tolerance),
            resample_mode=ResampleMode(self.resample_mode),
            scan_strategy=ScanStrategy(self.scan_strategy),
        )

    def gia_export_settings(self) -> GiaExportSettings:
        collision, climb = collision_mode_flags(self.collision_mode)
        fixed_opacity = None
        if self.alpha_threshold_num != -1:
            alpha_byte = max(0, min(255, int(self.alpha_threshold_num)))
            fixed_opacity = alpha_byte / 255.0 * 100.0
        return GiaExportSettings(
            target_width_m=float(self.target_width_m),
            target_height_m=float(self.target_height_m),
            template_id=int(self.template_id),
            block_height_m=self.block_height_m,
            entity_id_start=int(self.entity_id_start),
            collision=collision,
            climb=climb,
            enable_out_of_range_run=bool(self.enable_out_of_range_run),
            out_of_range_display_mode=int(self.out_of_range_display_mode),
            fixed_opacity_percent=fixed_opacity,
            quantization_mode=QuantizationMode(self.quantization_mode),
            quantization_step_m=float(self.quantization_step_m),
            decoration_packaging=bool(self.decoration_packaging),
            max_decorations_per_parent=int(self.max_decorations_per_parent),
            wrapper_template_id=int(self.wrapper_template_id),
            wrapper_static=bool(self.wrapper_static),
            wrapper_collision=collision if self.decoration_packaging else bool(self.wrapper_collision),
            wrapper_climb=climb if self.decoration_packaging else bool(self.wrapper_climb),
            wrapper_enable_out_of_range_run=bool(self.wrapper_enable_out_of_range_run),
            wrapper_out_of_range_display_mode=int(self.wrapper_out_of_range_display_mode),
        )


def collision_mode_flags(mode: str) -> tuple[bool, bool]:
    try:
        return _COLLISION_MODE_FLAGS[str(mode).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"不支持 collision_mode={mode!r}，可选值：{sorted(_COLLISION_MODE_FLAGS)}") from exc


def clamp_scale(value: float) -> float:
    return max(0.000001, min(50.0, float(value)))


def meters_to_grid_units(value: float) -> int:
    """旧 UI 兼容函数。新的默认导出不会调用它。"""
    return max(1, int(round(float(value) / GRID_STEP_M)))


def grid_units_to_meters(value: int | float) -> float:
    return float(value) * GRID_STEP_M


def quantize_scale(value: float) -> float:
    return grid_units_to_meters(meters_to_grid_units(clamp_scale(value)))


def load_rgba_image(raw: bytes) -> Image.Image:
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def scale_image_for_parsing(image: Image.Image, scale_percent: int) -> Image.Image:
    return scale_image_for_parsing_xy(image, scale_percent, scale_percent)


def scale_image_for_parsing_xy(image: Image.Image, scale_x_percent: int, scale_y_percent: int) -> Image.Image:
    width = max(1, int(round(image.width * max(1, int(scale_x_percent)) / 100.0)))
    height = max(1, int(round(image.height * max(1, int(scale_y_percent)) / 100.0)))
    if (width, height) == image.size:
        return image
    return image.resize((width, height), Image.Resampling.NEAREST)


def resize_for_pixel_budget(
    image: Image.Image,
    max_pixels: int,
    target_width_units: int | None = None,
    target_height_units: int | None = None,
) -> Image.Image:
    """兼容旧签名，但不再把 0.01m 网格当成图像像素上限。"""
    del target_width_units, target_height_units
    return resize_image(image.convert("RGBA"), max_pixels=max(1, int(max_pixels)))


def grid_layout(target_width_m: float, target_height_m: float, width_px: int, height_px: int) -> dict[str, Any]:
    """连续浮点布局。字段名保留 units 仅为了兼容旧页面。"""
    target_width_units = float(target_width_m) / GRID_STEP_M
    target_height_units = float(target_height_m) / GRID_STEP_M
    cell_width_units = target_width_units / max(1, width_px)
    cell_height_units = target_height_units / max(1, height_px)
    return {
        "target_width_units": target_width_units,
        "target_height_units": target_height_units,
        "cell_width_units": cell_width_units,
        "cell_height_units": cell_height_units,
        "actual_width_units": target_width_units,
        "actual_height_units": target_height_units,
        "first_x_units": (-target_width_units + cell_width_units) / 2.0,
        "first_z_units": (target_height_units - cell_height_units) / 2.0,
        "center_offset_m": {"x": 0.0, "z": 0.0},
    }


def build_raster_plan(image: Image.Image, settings: ImageGiaSettings | RasterAlgorithmSettings) -> RasterPlan:
    algorithm = settings.algorithm_settings() if isinstance(settings, ImageGiaSettings) else settings
    return _build_raster_plan(image, algorithm)


def export_raster_plan_to_gia(
    plan: RasterPlan,
    settings: ImageGiaSettings | GiaExportSettings,
    *,
    template_path: Path = DEFAULT_TEMPLATE_GIA,
) -> tuple[bytes, dict[str, Any], str]:
    export_settings = settings.gia_export_settings() if isinstance(settings, ImageGiaSettings) else settings
    return build_gia_from_plan(plan, export_settings, template_path=template_path, build_gia=build_gia)


def image_to_objects(image: Image.Image, settings: ImageGiaSettings) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    plan = build_raster_plan(image, settings)
    objects, export_summary = raster_plan_to_gia_objects(plan, settings.gia_export_settings())
    block_height = export_summary["block_height_m"]
    summary = {
        "source_size_px": list(plan.source_size_px),
        "sampled_size_px": list(plan.sampled_size_px),
        "requested_size_m": {"width": settings.target_width_m, "height": settings.target_height_m},
        "actual_size_m": {"width": settings.target_width_m, "height": settings.target_height_m},
        "cell_size_m": export_summary["cell_size_m"],
        "grid_step_m": settings.quantization_step_m if settings.quantization_mode == QuantizationMode.GRID.value else None,
        "center_offset_m": {"x": 0.0, "z": 0.0},
        "block_height_m": block_height,
        "block_height_mode": "manual" if settings.block_height_m is not None else "auto_min_cell_size",
        "alpha_threshold": settings.alpha_threshold,
        "template_id": settings.template_id,
        "entity_id_start": settings.entity_id_start,
        "object_count": len(objects),
        "merge_rectangles": settings.merge_rectangles,
        "merge_color_mode": settings.merge_color_mode,
        "color_tolerance": settings.color_tolerance,
        "palette_enabled": settings.palette_enabled,
        "palette_colors": settings.palette_colors if settings.palette_enabled else None,
        "background_rgb": list(settings.background_rgb) if settings.background_rgb is not None else None,
        "background_tolerance": settings.background_tolerance,
        "collision_mode": settings.collision_mode,
        "enable_out_of_range_run": settings.enable_out_of_range_run,
        "out_of_range_display_mode": settings.out_of_range_display_mode,
        "raster_cache_key": plan.cache_key,
        "coordinate_rule": "continuous floating-point center coordinates; quantization is export policy",
        "size_priority": "requested physical size is exact unless an explicit export quantization policy is enabled",
    }
    return objects, summary


def build_image_final_preview(image: Image.Image, settings: ImageGiaSettings) -> tuple[Image.Image, dict[str, Any]]:
    plan = build_raster_plan(image, settings)
    width, height = plan.sampled_size_px
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    fixed_alpha = None if settings.alpha_threshold_num == -1 else max(0, min(255, settings.alpha_threshold_num))
    for rect in plan.rectangles:
        r, g, b, a = rect.rgba
        if fixed_alpha is not None:
            a = fixed_alpha
        draw.rectangle((rect.x, rect.y, rect.x + rect.width - 1, rect.y + rect.height - 1), fill=(r, g, b, a))
    transparent = sum(1 for value in canvas.getchannel("A").getdata() if value == 0)
    return canvas, {
        "sampled_size_px": [width, height],
        "object_count": len(plan.rectangles),
        "transparent_pixel_count": transparent,
        "merge_rectangles": settings.merge_rectangles,
        "raster_cache_key": plan.cache_key,
    }


def build_image_gia_bytes(
    *,
    image: Image.Image,
    settings: ImageGiaSettings,
    template_path: Path = DEFAULT_TEMPLATE_GIA,
) -> tuple[bytes, dict[str, Any], str]:
    plan = build_raster_plan(image, settings)
    data, export_summary, objects_json = export_raster_plan_to_gia(plan, settings, template_path=template_path)
    _, image_summary = image_to_objects_from_plan(plan, settings)
    return data, {"image": image_summary, "gia": export_summary.get("gia", {}), "file_size": len(data)}, objects_json


def image_to_objects_from_plan(plan: RasterPlan, settings: ImageGiaSettings) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    objects, export_summary = raster_plan_to_gia_objects(plan, settings.gia_export_settings())
    summary = {
        "source_size_px": list(plan.source_size_px),
        "sampled_size_px": list(plan.sampled_size_px),
        "requested_size_m": {"width": settings.target_width_m, "height": settings.target_height_m},
        "actual_size_m": {"width": settings.target_width_m, "height": settings.target_height_m},
        "cell_size_m": export_summary["cell_size_m"],
        "block_height_m": export_summary["block_height_m"],
        "object_count": len(objects),
        "raster_cache_key": plan.cache_key,
        "algorithm": plan.algorithm_settings.canonical_dict(),
        "export": export_summary,
    }
    return objects, summary
