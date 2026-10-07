"""GPT JSON normalization, preserving the source conversion algorithms."""
from __future__ import annotations
from typing import Any, Callable
from .geometry import rotate_scene_about_x, clear_object_overlaps
OUT_OF_RANGE_DISPLAY_MODES = {"default": 0, "permanent": 1, "permanent_highest_precision": 2}

def parse_hex_color(value: Any) -> Any:
    """Turn GPT-style #RRGGBB into the list format accepted by parse_color."""
    if not isinstance(value, str):
        return value
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"color must be #RRGGBB, got {value!r}")
    try:
        return [int(text[index:index + 2], 16) for index in range(0, 6, 2)]
    except ValueError as exc:
        raise ValueError(f"color must be #RRGGBB, got {value!r}") from exc

def normalize_objects(
    data: list[dict[str, Any]] | dict[str, Any],
    *,
    default_template_id: int | None,
    default_collision: bool | None,
    default_climb: bool | None,
    default_out_of_range_run: bool | None,
    default_out_of_range_display: str | None,
    global_rotation_x_degrees: float,
    auto_optimize_object_overlaps: bool,
    object_overlap_clearance: float,
    object_surface_tolerance: float,
    multi_face_scale_increment: float,
    object_overlap_max_passes: int,
    optimization_progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int | str]]:
    """Accept both legacy lists and GPT model dictionaries without altering floats."""

    if not isinstance(data, (list, dict)):
        raise ValueError("模型 JSON 根节点必须为列表或字典")
    source_format = "list" if isinstance(data, list) else "model_dictionary"
    raw_items: list[dict[str, Any]] = []
    if isinstance(data, list):
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"object #{index} must be a JSON object")
            raw_items.append(dict(item))
    else:
        for name, source in data.items():
            if not isinstance(name, str) or not isinstance(source, dict):
                raise ValueError("each model dictionary entry must be a name mapped to an object")
            item = dict(source)
            item.setdefault("name", name)
            raw_items.append(item)

    objects: list[dict[str, Any]] = []
    for item in raw_items:
        # GPT models use an explicit template_id, otherwise the selected default primitive.
        if default_template_id is not None and "template_id" not in item:
            item["template_id"] = default_template_id
        if default_collision is not None:
            item.setdefault("collision", default_collision)
        if default_climb is not None:
            item.setdefault("climb", default_climb)
        if default_out_of_range_run is not None:
            item.setdefault("out_of_range_run", default_out_of_range_run)
        if default_out_of_range_display is not None:
            item.setdefault("out_of_range_display", default_out_of_range_display)
        display_mode = item.get("out_of_range_display")
        if display_mode is not None and display_mode not in OUT_OF_RANGE_DISPLAY_MODES:
            raise ValueError(
                "out_of_range_display must be default, permanent, or permanent_highest_precision"
            )
        if "color" in item:
            item["color"] = parse_hex_color(item["color"])
        objects.append(item)

    global_rotation_center = rotate_scene_about_x(objects, global_rotation_x_degrees)
    moved_overlapping_objects = 0
    scaled_multi_face_objects = 0
    if auto_optimize_object_overlaps:
        moved_overlapping_objects, scaled_multi_face_objects = clear_object_overlaps(
            objects,
            clearance=object_overlap_clearance,
            surface_tolerance=object_surface_tolerance,
            multi_face_scale_increment=multi_face_scale_increment,
            max_passes=object_overlap_max_passes,
            progress_callback=optimization_progress_callback,
        )

    return objects, {
        "source_format": source_format,
        "source_count": len(raw_items),
        "moved_overlapping_objects": moved_overlapping_objects,
        "scaled_multi_face_objects": scaled_multi_face_objects,
        "global_rotation_center": global_rotation_center,
    }
