from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import streamlit.components.v1 as components
from PIL import Image

from miliastra_core.sketch_config import EdgeProcessingConfig, SketchProcessingConfig
from miliastra_core.sketch import (
    Pixel,
    Point2,
    normalize_pixel_path,
    rasterize_normalized_paths,
)


_COMPONENT_DIR = Path(__file__).resolve().parent / "components" / "edge_protection_picker"
_edge_protection_picker = components.declare_component(
    "edge_protection_picker",
    path=str(_COMPONENT_DIR),
)

PROTECTION_BRANCH_COLORS = (
    "#00B86B",
    "#FF6B35",
    "#6C63FF",
    "#E83E8C",
    "#00A7E1",
    "#F2B705",
    "#8E5CFF",
    "#D64545",
)


def protection_branch_color(index: int) -> str:
    return PROTECTION_BRANCH_COLORS[max(0, int(index)) % len(PROTECTION_BRANCH_COLORS)]


def new_protection_branch(
    *,
    index: int,
    defaults: SketchProcessingConfig | EdgeProcessingConfig,
) -> dict[str, Any]:
    branch_id = uuid.uuid4().hex[:10]
    processing_defaults = (
        defaults
        if isinstance(defaults, SketchProcessingConfig)
        else SketchProcessingConfig(edge=defaults)
    )
    return {
        "id": branch_id,
        "name": f"保护分支 {index}",
        "color": protection_branch_color(index - 1),
        "scale_percent": 100,
        "config": processing_defaults.to_dict(),
        "protected_curves": [],
        "deleted_curves": [],
        "preview_signature": None,
        "preview_stage": None,
        "preview_image": None,
        "preview_curves": None,
        "picker_version": 0,
    }


def protection_branch_signature(branches: Sequence[dict[str, Any]]) -> str:
    payload = [
        {
            "id": str(branch.get("id", "")),
            "scale_percent": int(branch.get("scale_percent", 100)),
            "config": branch.get("config", {}),
            "protected_curves": branch.get("protected_curves", []),
            "deleted_curves": branch.get("deleted_curves", []),
        }
        for branch in branches
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _collect_branch_paths(
    branches: Sequence[dict[str, Any]],
    field_name: str,
) -> list[list[Point2]]:
    paths: list[list[Point2]] = []
    for branch in branches:
        for item in branch.get(field_name, []):
            points = item.get("points") if isinstance(item, dict) else None
            if not isinstance(points, list):
                continue
            normalized: list[Point2] = []
            for point in points:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    continue
                normalized.append((float(point[0]), float(point[1])))
            if normalized:
                paths.append(normalized)
    return paths


def collect_protected_paths(
    branches: Sequence[dict[str, Any]],
) -> list[list[Point2]]:
    return _collect_branch_paths(branches, "protected_curves")


def collect_deleted_paths(
    branches: Sequence[dict[str, Any]],
) -> list[list[Point2]]:
    return _collect_branch_paths(branches, "deleted_curves")


def build_protected_mask(
    branches: Sequence[dict[str, Any]],
    image_size: tuple[int, int],
) -> np.ndarray:
    return rasterize_normalized_paths(collect_protected_paths(branches), image_size)


def build_deleted_mask(
    branches: Sequence[dict[str, Any]],
    image_size: tuple[int, int],
) -> np.ndarray:
    return rasterize_normalized_paths(collect_deleted_paths(branches), image_size)


def build_curve_items_mask(
    curve_items: Sequence[dict[str, Any]],
    image_size: tuple[int, int],
) -> np.ndarray:
    """把独立保存的归一化曲线条目映射为主画布掩码。"""
    return rasterize_normalized_paths(
        _collect_branch_paths(
            [{"curves": list(curve_items)}],
            "curves",
        ),
        image_size,
    )


def curve_identifier(points: Sequence[Point2]) -> str:
    encoded = json.dumps(
        [[round(float(x), 6), round(float(y), 6)] for x, y in points],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def build_curve_candidates(
    curves: Sequence[Sequence[Pixel]],
    image_size: tuple[int, int],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for curve in curves:
        normalized = normalize_pixel_path(curve, image_size)
        if not normalized:
            continue
        candidates.append(
            {
                "id": curve_identifier(normalized),
                "points": [[float(x), float(y)] for x, y in normalized],
            }
        )
    return candidates


def protected_path_groups(
    branches: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for index, branch in enumerate(branches):
        paths = collect_protected_paths([branch])
        if not paths:
            continue
        groups.append(
            {
                "branch_id": str(branch.get("id", "")),
                "name": str(branch.get("name", f"保护分支 {index + 1}")),
                "color": str(
                    branch.get("color") or protection_branch_color(index)
                ),
                "paths": [
                    [[float(x), float(y)] for x, y in path]
                    for path in paths
                ],
            }
        )
    return groups


def protect_curve(
    branch: dict[str, Any],
    points: Sequence[Point2],
) -> bool:
    """幂等地保护一条曲线；返回 True 表示新增，False 表示已存在。"""
    curve_id = curve_identifier(points)
    curves = list(branch.get("protected_curves", []))
    already_exists = any(
        isinstance(item, dict) and item.get("id") == curve_id
        for item in curves
    )
    if already_exists:
        return False
    curves.append(
        {
            "id": curve_id,
            "points": [[float(x), float(y)] for x, y in points],
        }
    )
    branch["protected_curves"] = curves
    return True


def _image_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_edge_protection_picker(
    branch_image: Image.Image,
    result_image: Image.Image,
    *,
    candidate_curves: Sequence[dict[str, Any]],
    result_candidate_curves: Sequence[dict[str, Any]],
    selected_curve_ids: Sequence[str],
    deleted_curve_ids: Sequence[str],
    active_existing_paths: Sequence[dict[str, Any]],
    active_existing_deleted_paths: Sequence[dict[str, Any]],
    existing_path_groups: Sequence[dict[str, Any]],
    active_branch_id: str,
    active_branch_name: str,
    active_branch_color: str,
    editor_mode: str = "protect",
    key: str,
) -> dict[str, Any] | None:
    def encode(value: Any) -> str:
        # Streamlit 会对 list/dict 参数执行 dataframe 检测并导入 pandas。
        # 组件协议统一使用 JSON 字符串，彻底绕开该依赖与 Fragment 导入竞争。
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    value = _edge_protection_picker(
        branch_image_data_url=_image_data_url(branch_image),
        branch_image_width=int(branch_image.width),
        branch_image_height=int(branch_image.height),
        result_image_data_url=_image_data_url(result_image),
        result_image_width=int(result_image.width),
        result_image_height=int(result_image.height),
        candidate_curves_json=encode(list(candidate_curves)),
        result_candidate_curves_json=encode(list(result_candidate_curves)),
        selected_curve_ids_json=encode([str(value) for value in selected_curve_ids]),
        deleted_curve_ids_json=encode([str(value) for value in deleted_curve_ids]),
        active_existing_paths_json=encode(list(active_existing_paths)),
        active_existing_deleted_paths_json=encode(
            list(active_existing_deleted_paths)
        ),
        existing_path_groups_json=encode(list(existing_path_groups)),
        active_branch_id=str(active_branch_id),
        active_branch_name=str(active_branch_name),
        active_branch_color=str(active_branch_color),
        editor_mode=str(editor_mode),
        key=key,
        default=None,
    )
    return value if isinstance(value, dict) else None
