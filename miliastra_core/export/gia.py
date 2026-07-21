from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from miliastra_core.raster.models import RasterPlan

from .decoration import (
    DEFAULT_WRAPPER_TEMPLATE_ID,
    MAX_DECORATIONS_PER_PARENT,
    geometry_bounds,
)
from .quantization import QuantizationMode, QuantizationPolicy


@dataclass(frozen=True)
class GiaExportSettings:
    target_width_m: float
    target_height_m: float
    template_id: int
    block_height_m: float | None = None
    entity_id_start: int = 1_078_500_000
    collision: bool = False
    climb: bool = False
    enable_out_of_range_run: bool = False
    out_of_range_display_mode: int = 0
    fixed_opacity_percent: float | None = None
    quantization_mode: QuantizationMode = QuantizationMode.NONE
    quantization_step_m: float = 0.01
    decoration_packaging: bool = True
    max_decorations_per_parent: int = MAX_DECORATIONS_PER_PARENT
    wrapper_template_id: int = DEFAULT_WRAPPER_TEMPLATE_ID
    wrapper_static: bool = False
    wrapper_collision: bool = False
    wrapper_climb: bool = False
    wrapper_enable_out_of_range_run: bool = False
    wrapper_out_of_range_display_mode: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantization_mode", QuantizationMode(self.quantization_mode))
        if self.target_width_m <= 0 or self.target_height_m <= 0:
            raise ValueError("目标物理宽高必须 > 0")
        if self.block_height_m is not None and self.block_height_m <= 0:
            raise ValueError("block_height_m 必须为 None 或 > 0")
        if self.fixed_opacity_percent is not None and not 0 <= self.fixed_opacity_percent <= 100:
            raise ValueError("fixed_opacity_percent 必须位于 0..100")
        if self.climb and not self.collision:
            raise ValueError("启用攀爬时必须同时启用碰撞")
        if not 1 <= int(self.max_decorations_per_parent) <= MAX_DECORATIONS_PER_PARENT:
            raise ValueError(f"max_decorations_per_parent 必须位于 1..{MAX_DECORATIONS_PER_PARENT}")
        if self.wrapper_climb and not self.wrapper_collision:
            raise ValueError("空模型启用攀爬时必须同时启用碰撞")
        if int(self.wrapper_out_of_range_display_mode) not in (0, 1, 2):
            raise ValueError("wrapper_out_of_range_display_mode 必须为 0、1 或 2")

    @property
    def quantization(self) -> QuantizationPolicy:
        return QuantizationPolicy(self.quantization_mode, self.quantization_step_m)


def raster_plan_to_gia_objects(plan: RasterPlan, settings: GiaExportSettings) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    width_px, height_px = plan.sampled_size_px
    cell_width = settings.target_width_m / width_px
    cell_height = settings.target_height_m / height_px
    block_height = settings.block_height_m if settings.block_height_m is not None else min(cell_width, cell_height)
    q = settings.quantization
    objects: list[dict[str, Any]] = []
    entity_id = int(settings.entity_id_start)
    for rect in plan.rectangles:
        center_x = -settings.target_width_m / 2 + (rect.x + rect.width / 2) * cell_width
        center_z = settings.target_height_m / 2 - (rect.y + rect.height / 2) * cell_height
        scale_x = rect.width * cell_width
        scale_z = rect.height * cell_height
        r, g, b, a = rect.rgba
        opacity = settings.fixed_opacity_percent
        if opacity is None:
            opacity = a / 255.0 * 100.0
        objects.append(
            {
                "entity_id": entity_id,
                "name": f"ImageRect_{rect.y:04d}_{rect.x:04d}_{rect.width}x{rect.height}",
                "template_id": int(settings.template_id),
                "position": q.vec3((center_x, 0.0, center_z)),
                "rotation": [0.0, 0.0, 0.0],
                "scale": q.positive_vec3((scale_x, block_height, scale_z)),
                "color": {"rgb": [r, g, b], "opacity": float(opacity)},
                "collision": bool(settings.collision) and not settings.decoration_packaging,
                "climb": bool(settings.climb) and not settings.decoration_packaging,
                "enable_out_of_range_run": bool(settings.enable_out_of_range_run),
                "out_of_range_display_mode": int(settings.out_of_range_display_mode),
            }
        )
        entity_id += 1
    summary = {
        "raster_cache_key": plan.cache_key,
        "sampled_size_px": list(plan.sampled_size_px),
        "object_count": len(objects),
        "decoration_object_count": len(objects),
        "requested_size_m": {"width": settings.target_width_m, "height": settings.target_height_m},
        "cell_size_m": {"width": cell_width, "height": cell_height},
        "block_height_m": block_height,
        "quantization": {
            "mode": settings.quantization_mode.value,
            "step_m": settings.quantization_step_m if settings.quantization_mode is QuantizationMode.GRID else None,
        },
    }
    return objects, summary


def build_gia_from_plan(
    plan: RasterPlan,
    settings: GiaExportSettings,
    *,
    template_path: str | Path,
    build_gia: Callable[..., dict[str, Any]],
) -> tuple[bytes, dict[str, Any], str]:
    objects, image_summary = raster_plan_to_gia_objects(plan, settings)
    if not objects:
        raise ValueError("RasterPlan 没有可导出的可见矩形")
    template_path = Path(template_path)
    if not template_path.exists():
        raise FileNotFoundError(f"template GIA not found: {template_path}")
    with tempfile.TemporaryDirectory(prefix="miliastra_gia_") as tmp:
        tmp_dir = Path(tmp)
        objects_path = tmp_dir / "objects.json"
        output_path = tmp_dir / "output.gia"
        summary_path = tmp_dir / "summary.json"
        objects_json = json.dumps(objects, ensure_ascii=False, indent=2) + "\n"
        objects_path.write_text(objects_json, encoding="utf-8")
        # 必须基于最终量化后的图片块求精确 AABB。包装层会在三轴目标尺寸上
        # 统一减去 0.004，并使用最终父缩放反算子装饰物的局部变换。
        bounds = geometry_bounds(objects)
        parent_position = list(bounds.bottom_center)
        parent_scale = list(bounds.size)
        build_summary = build_gia(
            template_path=template_path,
            objects_path=objects_path,
            output_path=output_path,
            summary_path=summary_path,
            entity_id_start=settings.entity_id_start,
            decoration_packaging=bool(settings.decoration_packaging),
            max_decorations_per_parent=int(settings.max_decorations_per_parent),
            wrapper_template_id=int(settings.wrapper_template_id),
            wrapper_static=bool(settings.wrapper_static),
            # 图片装饰物的子碰撞固定关闭；高层碰撞选项只控制精确 AABB 父空模型。
            wrapper_collision=bool(settings.collision) if settings.decoration_packaging else False,
            wrapper_climb=bool(settings.climb) if settings.decoration_packaging else False,
            wrapper_enable_out_of_range_run=bool(settings.wrapper_enable_out_of_range_run),
            wrapper_out_of_range_display_mode=int(settings.wrapper_out_of_range_display_mode),
            decoration_parent_position=parent_position if settings.decoration_packaging else None,
            decoration_parent_scale=parent_scale if settings.decoration_packaging else None,
        )
        data = output_path.read_bytes()
    return data, {"image": image_summary, "gia": build_summary, "file_size": len(data)}, objects_json
