from __future__ import annotations

import io
import json
import math
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from miliastra_core.export.builder import DEFAULT_ENTITY_ID_START, build_gia
from miliastra_core.export.decoration import (
    DEFAULT_WRAPPER_TEMPLATE_ID,
    MAX_DECORATIONS_PER_PARENT,
    geometry_bounds,
    place_objects_on_backing,
)
from miliastra_core.image import (
    COLLISION_MODE_OFF,
    DEFAULT_TEMPLATE_GIA,
    collision_mode_flags,
)


SOURCE_EDGE = "edge_detection"
SOURCE_LINE_ART = "line_art"
COLOR_MODE_SPECIFIED = "specified_rgb"
COLOR_MODE_AUTO_NONZERO = "auto_nonzero"
COLOR_MODE_PASSTHROUGH = "passthrough"
LONG_AXIS_X = "x"
LONG_AXIS_Z = "z"

ProgressCallback = Callable[[int, str], None]


@dataclass
class SketchGiaSettings:
    source_mode: str = SOURCE_EDGE
    scale_x_percent: int = 100
    scale_y_percent: int = 100
    target_width_m: float = 10.0
    target_height_m: float = 10.0

    edge_operator: str = "Canny"
    edge_params: dict[str, float | int] = field(default_factory=dict)
    blur_kernel: int = 3

    line_color_mode: str = COLOR_MODE_AUTO_NONZERO
    line_rgb: tuple[int, int, int] = (0, 0, 0)
    line_rgb_tolerance: int = 16
    nonzero_threshold: int = 0
    alpha_threshold: int = 1

    auto_explore_polarity: bool = True
    auto_thin_wide_lines: bool = True
    wide_line_search_radius: int = 4
    close_kernel: int = 1
    remove_small_components: int = 4
    exclude_curve_length_px: float = 8.0

    simplify_tolerance_px: float = 1.5
    min_segment_length_px: float = 2.0
    max_primitives: int = 500
    budget_slack_ratio: float = 0.12

    ribbon_straightness_tolerance_px: float = 1.5
    ribbon_width_variation_tolerance: float = 0.35
    ribbon_minimum_length_px: float = 2.0
    ribbon_max_primitives: int = 500
    ribbon_budget_slack_ratio: float = 0.12
    ribbon_width_scale: float = 1.0
    ribbon_minimum_width_px: float = 1.0
    ribbon_joint_overlap_px: float = 1.0
    ribbon_target_miss_ratio: float = 0.01
    ribbon_residual_min_area_px: int = 2
    ribbon_coverage_rescue_ratio: float = 0.35
    ribbon_template_id: int = 10009001

    template_id: int = 10009001
    long_axis: str = LONG_AXIS_X
    line_width_m: float = 0.01
    depth_m: float = 0.01
    max_primitive_length_m: float = 50.0

    add_white_backing: bool = True
    backing_thickness_m: float = 0.01
    backing_template_id: int = 10009001

    output_rgb: tuple[int, int, int] = (190, 190, 190)
    output_opacity: float = 100.0
    collision_mode: str = COLLISION_MODE_OFF
    enable_out_of_range_run: bool = False
    out_of_range_display_mode: int = 0
    entity_id_start: int = DEFAULT_ENTITY_ID_START + 200_000
    decoration_packaging: bool = True
    max_decorations_per_parent: int = MAX_DECORATIONS_PER_PARENT
    wrapper_template_id: int = DEFAULT_WRAPPER_TEMPLATE_ID
    wrapper_static: bool = False
    wrapper_collision: bool = False
    wrapper_climb: bool = False
    wrapper_enable_out_of_range_run: bool = False
    wrapper_out_of_range_display_mode: int = 0


@dataclass
class SketchProcessedStage:
    scaled_image: Image.Image
    cleaned_mask: np.ndarray | None
    binary_image: Image.Image
    source_summary: dict[str, Any]
    cleanup_summary: dict[str, Any]
    protected_mask: np.ndarray | None = None
    pre_short_curve_mask: np.ndarray | None = None
    pre_short_curve_image: Image.Image | None = None


@dataclass
class SketchSkeletonStage:
    skeleton_mask: np.ndarray
    skeleton_image: Image.Image
    cleanup_summary: dict[str, Any]
    protected_mask: np.ndarray | None = None
    logical_curves: list[list[Pixel]] | None = None


@dataclass(frozen=True)
class FittedStroke:
    start: tuple[float, float]
    end: tuple[float, float]
    width_px: float | None = None
    source_segment_count: int = 1


@dataclass(frozen=True)
class RibbonRect:
    start: tuple[float, float]
    end: tuple[float, float]
    width_px: float
    source_path_index: int = 0
    source_point_count: int = 2


@dataclass
class SketchAnalysisResult:
    scaled_image: Image.Image
    binary_image: Image.Image
    skeleton_image: Image.Image
    final_preview: Image.Image
    objects: list[dict[str, Any]]
    segments_px: list[tuple[tuple[float, float], tuple[float, float]]]
    strokes_px: list[FittedStroke]
    ribbon_rects_px: list[RibbonRect] = field(default_factory=list)
    geometry_mode: str = "strokes"
    summary: dict[str, Any] = field(default_factory=dict)


def _progress(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback is not None:
        callback(max(0, min(100, int(percent))), message)


def _subprogress(
    callback: ProgressCallback | None,
    start: int,
    end: int,
    prefix: str = "",
) -> ProgressCallback | None:
    """把子任务 0..100 的进度映射到父任务的指定区间。"""
    if callback is None:
        return None
    lower = max(0, min(100, int(start)))
    upper = max(lower, min(100, int(end)))

    def mapped(percent: int, message: str) -> None:
        value = lower + round((upper - lower) * max(0, min(100, int(percent))) / 100.0)
        callback(value, f"{prefix}{message}" if prefix else message)

    return mapped


def load_rgba_image(raw: bytes) -> Image.Image:
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def scale_image(image: Image.Image, scale_x_percent: int, scale_y_percent: int) -> Image.Image:
    sx = max(1, int(scale_x_percent)) / 100.0
    sy = max(1, int(scale_y_percent)) / 100.0
    width, height = image.size
    new_size = (max(1, round(width * sx)), max(1, round(height * sy)))
    if new_size == image.size:
        return image.copy()
    return image.resize(new_size, Image.Resampling.LANCZOS)


def _odd_kernel(value: int, *, minimum: int = 1, maximum: int = 31) -> int:
    value = max(minimum, min(maximum, int(value)))
    if value % 2 == 0:
        value += 1
    return min(value, maximum if maximum % 2 == 1 else maximum - 1)


def _rgba_arrays(image: Image.Image) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    rgb = rgba[..., :3]
    alpha = rgba[..., 3]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return rgb, alpha, gray


def _normalize_response(response: np.ndarray) -> np.ndarray:
    response = np.abs(response.astype(np.float32))
    maximum = float(response.max()) if response.size else 0.0
    if maximum <= 1e-12:
        return np.zeros(response.shape, dtype=np.uint8)
    return np.clip(response / maximum * 255.0, 0.0, 255.0).astype(np.uint8)


def detect_edges(image: Image.Image, settings: SketchGiaSettings) -> tuple[np.ndarray, dict[str, Any]]:
    _, alpha, gray = _rgba_arrays(image)
    blur_kernel = _odd_kernel(settings.blur_kernel)
    if blur_kernel > 1:
        gray_work = cv2.GaussianBlur(gray, (blur_kernel, blur_kernel), 0)
    else:
        gray_work = gray

    operator = str(settings.edge_operator)
    params = settings.edge_params
    if operator == "Canny":
        low = int(params.get("low_threshold", 50))
        high = int(params.get("high_threshold", 150))
        aperture = int(params.get("aperture_size", 3))
        aperture = aperture if aperture in (3, 5, 7) else 3
        response = cv2.Canny(gray_work, low, high, apertureSize=aperture, L2gradient=True)
        threshold = 0
    elif operator == "Sobel":
        kernel = int(params.get("kernel_size", 3))
        kernel = kernel if kernel in (1, 3, 5, 7) else 3
        threshold = int(params.get("threshold", 45))
        gx = cv2.Sobel(gray_work, cv2.CV_32F, 1, 0, ksize=kernel)
        gy = cv2.Sobel(gray_work, cv2.CV_32F, 0, 1, ksize=kernel)
        response = _normalize_response(cv2.magnitude(gx, gy))
    elif operator == "Scharr":
        threshold = int(params.get("threshold", 55))
        gx = cv2.Scharr(gray_work, cv2.CV_32F, 1, 0)
        gy = cv2.Scharr(gray_work, cv2.CV_32F, 0, 1)
        response = _normalize_response(cv2.magnitude(gx, gy))
    elif operator == "Laplacian":
        kernel = int(params.get("kernel_size", 3))
        kernel = kernel if kernel in (1, 3, 5, 7) else 3
        threshold = int(params.get("threshold", 35))
        response = _normalize_response(cv2.Laplacian(gray_work, cv2.CV_32F, ksize=kernel))
    elif operator == "Prewitt":
        threshold = int(params.get("threshold", 45))
        kernel_x = np.asarray([[-1, 0, 1], [-1, 0, 1], [-1, 0, 1]], dtype=np.float32)
        kernel_y = kernel_x.T
        gx = cv2.filter2D(gray_work, cv2.CV_32F, kernel_x)
        gy = cv2.filter2D(gray_work, cv2.CV_32F, kernel_y)
        response = _normalize_response(cv2.magnitude(gx, gy))
    else:
        raise ValueError(f"不支持的边缘算子：{operator}")

    if operator == "Canny":
        mask = response > 0
    else:
        mask = response >= threshold
    mask &= alpha >= int(settings.alpha_threshold)
    return mask, {
        "operator": operator,
        "blur_kernel": blur_kernel,
        "operator_params": dict(params),
        "raw_edge_pixels": int(mask.sum()),
    }


def _mask_score(mask: np.ndarray) -> float:
    if mask.size == 0:
        return float("inf")
    occupancy = float(mask.mean())
    border = np.concatenate((mask[0, :], mask[-1, :], mask[:, 0], mask[:, -1]))
    border_ratio = float(border.mean()) if border.size else 0.0
    target_occupancy = 0.08
    dense_penalty = max(0.0, occupancy - 0.35) * 12.0
    empty_penalty = max(0.0, 0.0002 - occupancy) * 100.0
    return abs(occupancy - target_occupancy) + border_ratio * 1.7 + dense_penalty + empty_penalty


def extract_line_art_mask(image: Image.Image, settings: SketchGiaSettings) -> tuple[np.ndarray, dict[str, Any]]:
    rgb, alpha, gray = _rgba_arrays(image)
    valid_alpha = alpha >= int(settings.alpha_threshold)

    if settings.line_color_mode == COLOR_MODE_SPECIFIED:
        target = np.asarray(settings.line_rgb, dtype=np.int16)
        distance = np.max(np.abs(rgb.astype(np.int16) - target[None, None, :]), axis=2)
        mask = (distance <= int(settings.line_rgb_tolerance)) & valid_alpha
        polarity = "specified_rgb"
        explored = [polarity]
    elif settings.line_color_mode == COLOR_MODE_AUTO_NONZERO:
        threshold = int(settings.nonzero_threshold)
        nonzero = (np.max(rgb, axis=2) > threshold) & valid_alpha
        candidates: list[tuple[str, np.ndarray]] = [("nonzero", nonzero)]
        if settings.auto_explore_polarity:
            candidates.append(("zero_inverse", (~nonzero) & valid_alpha))
            otsu_value, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            candidates.extend(
                [
                    ("otsu_dark", (gray <= otsu_value) & valid_alpha),
                    ("otsu_light", (gray > otsu_value) & valid_alpha),
                ]
            )
        polarity, mask = min(candidates, key=lambda item: _mask_score(item[1]))
        explored = [name for name, _ in candidates]
    else:
        raise ValueError(f"不支持的线稿颜色模式：{settings.line_color_mode}")

    return mask, {
        "color_mode": settings.line_color_mode,
        "selected_polarity": polarity,
        "explored_polarities": explored,
        "line_rgb": list(settings.line_rgb),
        "line_rgb_tolerance": int(settings.line_rgb_tolerance),
        "nonzero_threshold": int(settings.nonzero_threshold),
        "raw_line_pixels": int(mask.sum()),
    }


def remove_small_components(mask: np.ndarray, min_pixels: int) -> tuple[np.ndarray, int]:
    min_pixels = max(1, int(min_pixels))
    if not mask.any() or min_pixels <= 1:
        return mask.astype(bool), 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = np.zeros(count, dtype=bool)
    removed = 0
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area >= min_pixels:
            keep[label] = True
        else:
            removed += 1
    return keep[labels], removed


def zhang_suen_thinning(
    mask: np.ndarray,
    max_iterations: int = 512,
    progress_callback: ProgressCallback | None = None,
) -> tuple[np.ndarray, int]:
    del max_iterations  # OpenCV 的实现内部迭代到收敛，不暴露迭代上限。
    image = mask.astype(np.uint8)
    if image.shape[0] < 3 or image.shape[1] < 3:
        _progress(progress_callback, 100, "图像尺寸过小，跳过中心线细化")
        return image.astype(bool), 0
    _progress(progress_callback, 10, "正在使用 OpenCV Zhang-Suen 算法提取中心线")
    thinned = cv2.ximgproc.thinning(
        image * 255,
        thinningType=cv2.ximgproc.THINNING_ZHANGSUEN,
    )
    _progress(progress_callback, 100, "OpenCV 中心线细化完成")
    return thinned > 0, 1


def _component_count(mask: np.ndarray) -> int:
    if not mask.any():
        return 0
    count, _ = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
    return max(0, int(count) - 1)


def _junction_count(mask: np.ndarray) -> int:
    if not mask.any():
        return 0
    padded = np.pad(mask.astype(np.uint8), 1)
    neighbors = (
        padded[:-2, :-2] + padded[:-2, 1:-1] + padded[:-2, 2:]
        + padded[1:-1, :-2] + padded[1:-1, 2:]
        + padded[2:, :-2] + padded[2:, 1:-1] + padded[2:, 2:]
    )
    return int(np.count_nonzero(mask & (neighbors > 2)))


def clean_line_mask(mask: np.ndarray, settings: SketchGiaSettings) -> tuple[np.ndarray, dict[str, Any]]:
    """只做断线连接与小连通域清理，不执行骨架细化。"""
    work = mask.astype(np.uint8)
    close_kernel = max(1, int(settings.close_kernel))
    if close_kernel > 1:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_kernel, close_kernel))
        work = cv2.morphologyEx(work, cv2.MORPH_CLOSE, kernel)

    cleaned, removed_components = remove_small_components(work > 0, settings.remove_small_components)
    return cleaned, {
        "cleaned_pixels": int(cleaned.sum()),
        "removed_small_components": int(removed_components),
        "close_kernel": close_kernel,
    }


def skeletonize_cleaned_mask(
    cleaned: np.ndarray,
    settings: SketchGiaSettings,
    progress_callback: ProgressCallback | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """只把已清理线稿转换为中心骨架。"""
    selected_radius = 0
    explored: list[dict[str, Any]] = []
    if settings.auto_thin_wide_lines:
        max_radius = max(0, min(8, int(settings.wide_line_search_radius)))
        original_components = max(1, _component_count(cleaned))
        best: tuple[float, np.ndarray, int, int] | None = None
        candidate_count = max_radius + 1
        for candidate_index, radius in enumerate(range(candidate_count)):
            candidate_start = round(candidate_index * 92 / max(candidate_count, 1))
            candidate_end = round((candidate_index + 1) * 92 / max(candidate_count, 1))
            _progress(
                progress_callback,
                candidate_start,
                f"正在探索宽线中心：候选 {candidate_index + 1}/{candidate_count}，膨胀半径 {radius}",
            )
            if radius == 0:
                candidate_mask = cleaned
            else:
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
                candidate_mask = cv2.dilate(cleaned.astype(np.uint8), kernel) > 0
            candidate_skeleton, candidate_iterations = zhang_suen_thinning(
                candidate_mask,
                progress_callback=_subprogress(
                    progress_callback,
                    candidate_start,
                    candidate_end,
                    f"候选 {candidate_index + 1}/{candidate_count}：",
                ),
            )
            pixels = int(candidate_skeleton.sum())
            components = _component_count(candidate_skeleton)
            junctions = _junction_count(candidate_skeleton)
            lost_components = max(0, original_components - components)
            score = pixels + junctions * 0.35 + radius * max(1, pixels) * 0.02 + lost_components * 250.0
            explored.append(
                {
                    "dilation_radius": radius,
                    "skeleton_pixels": pixels,
                    "components": components,
                    "junction_pixels": junctions,
                    "score": float(score),
                }
            )
            if best is None or score < best[0]:
                best = (score, candidate_skeleton, candidate_iterations, radius)
        assert best is not None
        _, skeleton, thinning_iterations, selected_radius = best
    else:
        _progress(progress_callback, 40, "已关闭自动细化，直接使用清理后的线稿")
        skeleton = cleaned.astype(bool)
        thinning_iterations = 0

    _progress(progress_callback, 96, "正在统计骨架连通结构")
    summary = {
        "skeleton_pixels": int(skeleton.sum()),
        "thinning_enabled": bool(settings.auto_thin_wide_lines),
        "thinning_engine": (
            "opencv_ximgproc_zhang_suen"
            if settings.auto_thin_wide_lines
            else "disabled"
        ),
        "thinning_iterations": int(thinning_iterations),
        "wide_line_search_radius": int(settings.wide_line_search_radius),
        "selected_dilation_radius": int(selected_radius),
        "wide_line_candidates": explored,
    }
    _progress(progress_callback, 100, "中心骨架提取完成")
    return skeleton, summary


def clean_and_skeletonize(mask: np.ndarray, settings: SketchGiaSettings) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """兼容旧调用：按顺序完成清理与骨架细化。"""
    cleaned, clean_summary = clean_line_mask(mask, settings)
    skeleton, skeleton_summary = skeletonize_cleaned_mask(cleaned, settings)
    return cleaned, skeleton, {**clean_summary, **skeleton_summary}


Pixel = tuple[int, int]
Point2 = tuple[float, float]
Segment2 = tuple[Point2, Point2]
_NEIGHBOR_OFFSETS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1),           (0, 1),
    (1, -1),  (1, 0),  (1, 1),
)


def _edge_key(a: Pixel, b: Pixel) -> tuple[Pixel, Pixel]:
    return (a, b) if a <= b else (b, a)


def _build_skeleton_neighbors(skeleton: np.ndarray) -> dict[Pixel, list[Pixel]]:
    rows, cols = np.nonzero(skeleton)
    pixels: set[Pixel] = set(zip(rows.tolist(), cols.tolist()))
    neighbors: dict[Pixel, list[Pixel]] = {}
    for row, col in sorted(pixels):
        item: list[Pixel] = []
        for dr, dc in _NEIGHBOR_OFFSETS:
            candidate = (row + dr, col + dc)
            if candidate not in pixels:
                continue
            # 对角邻接若已有正交通路则跳过，避免 2x2 像素块形成大量伪三角支路。
            if dr != 0 and dc != 0:
                if (row + dr, col) in pixels or (row, col + dc) in pixels:
                    continue
            item.append(candidate)
        neighbors[(row, col)] = item
    return neighbors


def trace_skeleton_paths(
    skeleton: np.ndarray,
    progress_callback: ProgressCallback | None = None,
) -> list[list[Pixel]]:
    _progress(progress_callback, 0, "正在读取骨架像素")
    neighbors = _build_skeleton_neighbors(skeleton)
    if not neighbors:
        return []

    sorted_pixels = sorted(neighbors)
    total_pixels = len(sorted_pixels)
    update_stride = max(1, total_pixels // 100)
    for pixel_index, _ in enumerate(sorted_pixels):
        if pixel_index % update_stride == 0:
            _progress(
                progress_callback,
                round(45 * pixel_index / max(total_pixels, 1)),
                f"正在建立像素邻接图：{pixel_index:,}/{total_pixels:,}",
            )

    nodes = {pixel for pixel, items in neighbors.items() if len(items) != 2}
    visited_edges: set[tuple[Pixel, Pixel]] = set()
    paths: list[list[Pixel]] = []

    def trace(start: Pixel, nxt: Pixel) -> list[Pixel]:
        path = [start, nxt]
        visited_edges.add(_edge_key(start, nxt))
        previous, current = start, nxt
        while current not in nodes:
            candidates = [p for p in neighbors[current] if p != previous]
            if not candidates:
                break
            next_pixel = candidates[0]
            key = _edge_key(current, next_pixel)
            if key in visited_edges:
                break
            visited_edges.add(key)
            path.append(next_pixel)
            previous, current = current, next_pixel
        return path

    sorted_nodes = sorted(nodes)
    total_nodes = len(sorted_nodes)
    node_stride = max(1, total_nodes // 100) if total_nodes else 1
    for node_index, node in enumerate(sorted_nodes):
        if node_index % node_stride == 0:
            _progress(
                progress_callback,
                45 + round(30 * node_index / max(total_nodes, 1)),
                f"正在追踪分叉曲线：{node_index:,}/{total_nodes:,}",
            )
        if not neighbors[node]:
            continue
        for neighbor in neighbors[node]:
            key = _edge_key(node, neighbor)
            if key in visited_edges:
                continue
            path = trace(node, neighbor)
            if len(path) >= 2:
                paths.append(path)

    for start_index, start in enumerate(sorted_pixels):
        if start_index % update_stride == 0:
            _progress(
                progress_callback,
                75 + round(24 * start_index / max(total_pixels, 1)),
                f"正在追踪闭合曲线：{start_index:,}/{total_pixels:,}",
            )
        for neighbor in neighbors[start]:
            key = _edge_key(start, neighbor)
            if key in visited_edges:
                continue
            path = [start, neighbor]
            visited_edges.add(key)
            previous, current = start, neighbor
            while True:
                candidates = [p for p in neighbors[current] if p != previous]
                if not candidates:
                    break
                next_pixel = candidates[0]
                next_key = _edge_key(current, next_pixel)
                if next_key in visited_edges:
                    if next_pixel == start and path[-1] != start:
                        path.append(start)
                    break
                visited_edges.add(next_key)
                path.append(next_pixel)
                previous, current = current, next_pixel
            if len(path) >= 2:
                paths.append(path)
    _progress(progress_callback, 100, f"拓扑曲线追踪完成，共 {len(paths):,} 条")
    return paths


def _simplify_open_points(
    points: Sequence[Point2],
    epsilon: float,
) -> list[Point2]:
    if len(points) <= 2:
        return list(points)
    effective_epsilon = max(
        float(epsilon),
        float(np.finfo(np.float64).eps),
    )
    try:
        from simplification.cutil import simplify_coords
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "线稿曲线简化需要 simplification；请按 requirements.txt 安装项目依赖"
        ) from exc
    simplified = simplify_coords(
        np.asarray(points, dtype=np.float64),
        effective_epsilon,
    )
    return [
        (float(point[0]), float(point[1]))
        for point in np.asarray(simplified, dtype=np.float64)
    ]


def _cycle_slice(points: Sequence[Point2], start: int, end: int) -> list[Point2]:
    result = [points[start]]
    index = start
    while index != end:
        index = (index + 1) % len(points)
        result.append(points[index])
    return result


def simplify_path(path: Sequence[Pixel], epsilon: float) -> list[Point2]:
    points: list[Point2] = [(float(col), float(row)) for row, col in path]
    if len(points) <= 2:
        return points
    closed = points[0] == points[-1]
    if not closed:
        return _simplify_open_points(points, epsilon)

    cycle = points[:-1]
    if len(cycle) <= 3:
        return cycle + [cycle[0]]
    array = np.asarray(cycle, dtype=np.float64)
    first = int(np.argmax(np.sum((array - array[0]) ** 2, axis=1)))
    second = int(np.argmax(np.sum((array - array[first]) ** 2, axis=1)))
    arc_a = _cycle_slice(cycle, first, second)
    arc_b = _cycle_slice(cycle, second, first)
    simple_a = _simplify_open_points(arc_a, epsilon)
    simple_b = _simplify_open_points(arc_b, epsilon)
    merged = simple_a[:-1] + simple_b[:-1]
    if merged and merged[0] != merged[-1]:
        merged.append(merged[0])
    return merged


def _path_length_px(path: Sequence[Pixel]) -> float:
    return float(
        sum(
            math.hypot(float(b[1] - a[1]), float(b[0] - a[0]))
            for a, b in zip(path, path[1:])
        )
    )


def _connected_pixel_components(
    pixels: set[Pixel],
    neighbors: dict[Pixel, list[Pixel]],
) -> list[set[Pixel]]:
    if not pixels:
        return []
    ordered = sorted(pixels)
    index_by_pixel = {pixel: index for index, pixel in enumerate(ordered)}
    rows: list[int] = []
    columns: list[int] = []
    for pixel, row_index in index_by_pixel.items():
        for neighbor in neighbors.get(pixel, []):
            column_index = index_by_pixel.get(neighbor)
            if column_index is None:
                continue
            rows.append(row_index)
            columns.append(column_index)
    graph = coo_matrix(
        (
            np.ones(len(rows), dtype=np.uint8),
            (rows, columns),
        ),
        shape=(len(ordered), len(ordered)),
    ).tocsr()
    component_count, labels = connected_components(
        graph,
        directed=False,
        return_labels=True,
    )
    components = [set() for _ in range(int(component_count))]
    for pixel, label in zip(ordered, labels):
        components[int(label)].add(pixel)
    return components


def _ordered_component_path(
    component: set[Pixel],
    neighbors: dict[Pixel, list[Pixel]],
) -> list[Pixel]:
    local_neighbors = {
        pixel: [neighbor for neighbor in neighbors[pixel] if neighbor in component]
        for pixel in component
    }
    endpoints = sorted(pixel for pixel, items in local_neighbors.items() if len(items) <= 1)
    start = endpoints[0] if endpoints else min(component)
    path = [start]
    previous: Pixel | None = None
    current = start
    while True:
        candidates = [
            neighbor
            for neighbor in local_neighbors[current]
            if neighbor != previous
        ]
        if not candidates:
            break
        next_pixel = candidates[0]
        if next_pixel == start:
            path.append(start)
            break
        if next_pixel in path:
            break
        path.append(next_pixel)
        previous, current = current, next_pixel
    return path


def _sample_outgoing_direction(
    path: Sequence[Pixel],
    side: int,
    sample_distance_px: float = 8.0,
) -> np.ndarray:
    oriented = list(path if side == 0 else reversed(path))
    start = oriented[0]
    selected = oriented[-1]
    traveled = 0.0
    for a, b in zip(oriented, oriented[1:]):
        traveled += math.hypot(float(b[1] - a[1]), float(b[0] - a[0]))
        selected = b
        if traveled >= sample_distance_px:
            break
    vector = np.asarray(
        [float(selected[1] - start[1]), float(selected[0] - start[0])],
        dtype=np.float64,
    )
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        return np.asarray([1.0, 0.0], dtype=np.float64)
    return vector / norm


def trace_smooth_logical_curves(
    skeleton: np.ndarray,
) -> tuple[list[list[Pixel]], dict[str, int | str]]:
    """叉点只续接方向变化最平滑的一对分支，其余分支独立计为曲线。"""
    neighbors = _build_skeleton_neighbors(skeleton)
    if not neighbors:
        return [], {
            "junction_cluster_count": 0,
            "atomic_curve_count": 0,
            "smooth_pair_count": 0,
            "logical_curve_count": 0,
            "topology_component_engine": "scipy_sparse",
        }

    junction_pixels = {
        pixel for pixel, items in neighbors.items() if len(items) > 2
    }
    junction_components = _connected_pixel_components(junction_pixels, neighbors)
    junction_by_pixel: dict[Pixel, int] = {}
    junction_representatives: dict[int, Pixel] = {}
    for junction_id, component in enumerate(junction_components):
        for pixel in component:
            junction_by_pixel[pixel] = junction_id
        mean_row = sum(pixel[0] for pixel in component) / len(component)
        mean_col = sum(pixel[1] for pixel in component) / len(component)
        junction_representatives[junction_id] = min(
            component,
            key=lambda pixel: (
                (pixel[0] - mean_row) ** 2 + (pixel[1] - mean_col) ** 2,
                pixel,
            ),
        )

    residual_pixels = set(neighbors) - junction_pixels
    residual_components = _connected_pixel_components(residual_pixels, neighbors)
    atomic_paths: list[list[Pixel]] = []
    endpoint_nodes: list[tuple[int | None, int | None]] = []
    for component in residual_components:
        path = _ordered_component_path(component, neighbors)
        if not path:
            continue
        if path[0] == path[-1]:
            atomic_paths.append(path)
            endpoint_nodes.append((None, None))
            continue
        nodes: list[int | None] = []
        for endpoint in (path[0], path[-1]):
            adjacent = sorted(
                {
                    junction_by_pixel[neighbor]
                    for neighbor in neighbors[endpoint]
                    if neighbor in junction_by_pixel
                }
            )
            nodes.append(adjacent[0] if adjacent else None)
        atomic_paths.append(path)
        endpoint_nodes.append((nodes[0], nodes[1]))

    incident: dict[int, list[tuple[int, int]]] = {}
    for curve_index, nodes in enumerate(endpoint_nodes):
        for side, junction_id in enumerate(nodes):
            if junction_id is not None:
                incident.setdefault(junction_id, []).append((curve_index, side))

    pairings: dict[tuple[int, int], tuple[int, int]] = {}
    for tokens in incident.values():
        if len(tokens) < 2:
            continue
        best_pair: tuple[tuple[int, int], tuple[int, int]] | None = None
        best_dot = float("inf")
        for index, token_a in enumerate(tokens):
            direction_a = _sample_outgoing_direction(
                atomic_paths[token_a[0]],
                token_a[1],
            )
            for token_b in tokens[index + 1:]:
                if token_a[0] == token_b[0]:
                    continue
                direction_b = _sample_outgoing_direction(
                    atomic_paths[token_b[0]],
                    token_b[1],
                )
                dot = float(np.dot(direction_a, direction_b))
                if dot < best_dot:
                    best_dot = dot
                    best_pair = (token_a, token_b)
        if best_pair is not None:
            token_a, token_b = best_pair
            pairings[token_a] = token_b
            pairings[token_b] = token_a

    logical_curves: list[list[Pixel]] = []
    used_curves: set[int] = set()

    def walk(start_token: tuple[int, int]) -> list[Pixel]:
        result: list[Pixel] = []
        current_token = start_token
        while current_token[0] not in used_curves:
            curve_index, entry_side = current_token
            used_curves.add(curve_index)
            path = (
                atomic_paths[curve_index]
                if entry_side == 0
                else list(reversed(atomic_paths[curve_index]))
            )
            entry_node = endpoint_nodes[curve_index][entry_side]
            exit_side = 1 - entry_side
            exit_node = endpoint_nodes[curve_index][exit_side]
            if entry_node is not None:
                representative = junction_representatives[entry_node]
                if not result or result[-1] != representative:
                    result.append(representative)
            if result and path and result[-1] == path[0]:
                result.extend(path[1:])
            else:
                result.extend(path)
            if exit_node is not None:
                representative = junction_representatives[exit_node]
                if not result or result[-1] != representative:
                    result.append(representative)
            next_token = pairings.get((curve_index, exit_side))
            if next_token is None:
                break
            current_token = next_token
        return result

    unpaired_tokens = [
        (curve_index, side)
        for curve_index, nodes in enumerate(endpoint_nodes)
        for side, junction_id in enumerate(nodes)
        if junction_id is None or (curve_index, side) not in pairings
    ]
    for token in unpaired_tokens:
        if token[0] in used_curves:
            continue
        logical_curve = walk(token)
        if logical_curve:
            logical_curves.append(logical_curve)
    for curve_index in range(len(atomic_paths)):
        if curve_index in used_curves:
            continue
        logical_curve = walk((curve_index, 0))
        if logical_curve:
            logical_curves.append(logical_curve)

    return logical_curves, {
        "junction_cluster_count": len(junction_components),
        "atomic_curve_count": len(atomic_paths),
        "smooth_pair_count": len(pairings) // 2,
        "logical_curve_count": len(logical_curves),
        "topology_component_engine": "scipy_sparse",
    }


def exclude_short_logical_curves(
    skeleton: np.ndarray,
    min_length_px: float,
    protected_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """按平滑续接后的整条曲线长度删除短曲线。"""
    threshold = max(0.0, float(min_length_px))
    curves, topology_summary = trace_smooth_logical_curves(skeleton)
    protected = (
        protected_mask.astype(bool)
        if protected_mask is not None
        else np.zeros_like(skeleton, dtype=bool)
    )
    if protected.shape != skeleton.shape:
        raise ValueError(
            f"保护线掩码尺寸与骨架不一致：protected={protected.shape}, skeleton={skeleton.shape}"
        )
    kept_curves: list[list[Pixel]] = []
    removed_count = 0
    removed_length = 0.0
    protected_curve_count = 0
    for curve in curves:
        length = _path_length_px(curve)
        curve_is_protected = any(protected[row, col] for row, col in curve)
        if curve_is_protected:
            protected_curve_count += 1
            kept_curves.append(curve)
        elif length + 1e-9 < threshold:
            removed_count += 1
            removed_length += length
        else:
            kept_curves.append(curve)

    output = np.zeros_like(skeleton, dtype=np.uint8)
    for curve in kept_curves:
        for start, end in zip(curve, curve[1:]):
            cv2.line(
                output,
                (int(start[1]), int(start[0])),
                (int(end[1]), int(end[0])),
                1,
                1,
                lineType=cv2.LINE_8,
            )
        if len(curve) == 1:
            output[curve[0]] = 1
    return output.astype(bool), {
        **topology_summary,
        "exclude_curve_length_px": threshold,
        "excluded_curve_count": removed_count,
        "excluded_curve_total_length_px": float(removed_length),
        "remaining_curve_count": len(kept_curves),
        "protected_curve_count": protected_curve_count,
    }


def prefilter_short_paths(
    paths: Sequence[Sequence[Pixel]],
    min_length_px: float,
    protected_mask: np.ndarray | None = None,
) -> tuple[list[Sequence[Pixel]], dict[str, float | int]]:
    """在拟合预算搜索前，按完整路径长度排除过短线段。"""
    threshold = max(0.0, float(min_length_px))
    protected = protected_mask.astype(bool) if protected_mask is not None else None
    kept: list[Sequence[Pixel]] = []
    removed_count = 0
    removed_total_length = 0.0
    protected_path_count = 0
    for path in paths:
        path_length = _path_length_px(path)
        path_is_protected = bool(
            protected is not None
            and any(
                0 <= row < protected.shape[0]
                and 0 <= col < protected.shape[1]
                and protected[row, col]
                for row, col in path
            )
        )
        if path_is_protected:
            protected_path_count += 1
            kept.append(path)
        elif path_length + 1e-9 < threshold:
            removed_count += 1
            removed_total_length += path_length
        else:
            kept.append(path)
    return kept, {
        "exclude_segment_length_px": threshold,
        "input_path_count": len(paths),
        "budget_path_count": len(kept),
        "excluded_short_segment_path_count": removed_count,
        "excluded_short_segment_total_length_px": float(removed_total_length),
        "protected_segment_path_count": protected_path_count,
    }


def _segments_from_simplified_paths(
    paths: Sequence[Sequence[Pixel]],
    epsilon: float,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[Segment2], list[list[Point2]]]:
    segments: list[Segment2] = []
    simplified_paths: list[list[Point2]] = []
    total_paths = len(paths)
    update_stride = max(1, total_paths // 100) if total_paths else 1
    for path_index, path in enumerate(paths):
        if path_index % update_stride == 0:
            _progress(
                progress_callback,
                round(100 * path_index / max(total_paths, 1)),
                f"正在简化曲线：{path_index:,}/{total_paths:,}",
            )
        simplified = simplify_path(path, epsilon)
        if len(simplified) < 2:
            continue
        simplified_paths.append(simplified)
        for a, b in zip(simplified, simplified[1:]):
            segments.append((a, b))
    _progress(progress_callback, 100, f"曲线简化完成，得到 {len(segments):,} 条候选线段")
    return segments, simplified_paths


def fit_paths_to_budget(
    paths: Sequence[Sequence[Pixel]],
    base_epsilon: float,
    min_length_px: float,
    max_primitives: int,
    slack_ratio: float,
    image_diagonal: float,
    progress_callback: ProgressCallback | None = None,
    *,
    protected_mask: np.ndarray | None = None,
) -> tuple[list[Segment2], dict[str, Any]]:
    target = max(1, int(max_primitives))
    allowed_max = max(target, int(math.ceil(target * (1.0 + max(0.0, float(slack_ratio))))))
    base_epsilon = max(0.0, float(base_epsilon))
    budget_paths, segment_filter_summary = prefilter_short_paths(
        paths,
        min_length_px,
        protected_mask=protected_mask,
    )

    cache: dict[float, list[Segment2]] = {}

    evaluation_index = 0
    maximum_evaluations = 20

    def evaluate(epsilon: float) -> list[Segment2]:
        nonlocal evaluation_index
        key = round(float(epsilon), 6)
        if key not in cache:
            current_index = evaluation_index
            evaluation_index += 1
            start = round(current_index * 92 / maximum_evaluations)
            end = round((current_index + 1) * 92 / maximum_evaluations)
            cache[key] = _segments_from_simplified_paths(
                budget_paths,
                key,
                progress_callback=_subprogress(
                    progress_callback,
                    start,
                    end,
                    f"预算搜索 {current_index + 1}/{maximum_evaluations}，误差 {epsilon:.3f}px：",
                ),
            )[0]
            _progress(
                progress_callback,
                end,
                f"预算搜索 {current_index + 1}/{maximum_evaluations}：得到 {len(cache[key]):,} 条线段",
            )
        return cache[key]

    _progress(progress_callback, 0, f"开始基元预算搜索，目标约 {target:,} 个")
    candidates: list[tuple[float, list[Segment2]]] = [(base_epsilon, evaluate(base_epsilon))]
    if len(candidates[0][1]) > allowed_max:
        low = base_epsilon
        high = max(base_epsilon + 1.0, image_diagonal * 0.5)
        candidates.append((high, evaluate(high)))
        for _ in range(18):
            mid = (low + high) * 0.5
            result = evaluate(mid)
            candidates.append((mid, result))
            if len(result) > target:
                low = mid
            else:
                high = mid

    def objective(item: tuple[float, list[Segment2]]) -> tuple[float, float, float]:
        epsilon, segments = item
        count = len(segments)
        overflow = max(0, count - allowed_max)
        return (overflow * 1000.0 + abs(count - target), abs(count - target), epsilon)

    _progress(progress_callback, 94, "正在选择最接近目标数量的候选结果")
    best_epsilon, best_segments = min(candidates, key=objective)
    pruned = 0
    if len(best_segments) > allowed_max:
        best_segments = sorted(best_segments, key=lambda seg: math.dist(seg[0], seg[1]), reverse=True)
        pruned = len(best_segments) - allowed_max
        best_segments = best_segments[:allowed_max]

    _progress(progress_callback, 100, f"预算拟合完成，最终 {len(best_segments):,} 条线段")
    return best_segments, {
        "requested_max_primitives": target,
        "allowed_soft_max": allowed_max,
        "base_simplify_tolerance_px": base_epsilon,
        "effective_simplify_tolerance_px": float(best_epsilon),
        "primitive_count": len(best_segments),
        "budget_slack_ratio": float(slack_ratio),
        **segment_filter_summary,
        "budget_overflow_pruned_segments": int(pruned),
        "soft_pruned_short_segments": int(pruned),
        "budget_rule": "先按拟合线段路径长度过滤，再自适应提高曲线误差阈值以接近目标数量；仍超限时按长度执行预算兜底裁剪",
    }



def _canonical_direction(dx: float, dy: float) -> np.ndarray:
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        return np.asarray([1.0, 0.0], dtype=np.float64)
    direction = np.asarray([dx / length, dy / length], dtype=np.float64)
    if direction[0] < 0.0 or (abs(direction[0]) <= 1e-12 and direction[1] < 0.0):
        direction = -direction
    return direction


def _pixel_to_world(point: Point2, width_px: int, height_px: int, width_m: float, height_m: float) -> tuple[float, float]:
    col, row = point
    x = ((col + 0.5) / max(width_px, 1) - 0.5) * float(width_m)
    z = (0.5 - (row + 0.5) / max(height_px, 1)) * float(height_m)
    return x, z


def segments_to_objects(
    segments: Sequence[Segment2 | FittedStroke],
    image_size: tuple[int, int],
    settings: SketchGiaSettings,
    *,
    entity_id_offset: int = 0,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    width_px, height_px = image_size
    enable_collision, enable_climb = collision_mode_flags(settings.collision_mode)
    objects: list[dict[str, Any]] = []
    depth = max(1e-6, float(settings.depth_m))
    max_length = max(1e-6, float(settings.max_primitive_length_m))

    total_segments = len(segments)
    update_stride = max(1, total_segments // 100) if total_segments else 1
    for segment_index, segment in enumerate(segments):
        if isinstance(segment, FittedStroke):
            point_a, point_b = segment.start, segment.end
            width = max(1e-6, float(settings.line_width_m))
            source_segment_count = int(segment.source_segment_count)
        else:
            point_a, point_b = segment
            width = max(1e-6, float(settings.line_width_m))
            source_segment_count = 1
        if segment_index % update_stride == 0:
            _progress(
                progress_callback,
                round(100 * segment_index / max(total_segments, 1)),
                f"正在生成基元变换：{segment_index:,}/{total_segments:,}",
            )
        ax, az = _pixel_to_world(point_a, width_px, height_px, settings.target_width_m, settings.target_height_m)
        bx, bz = _pixel_to_world(point_b, width_px, height_px, settings.target_width_m, settings.target_height_m)
        dx, dz = bx - ax, bz - az
        total_length = math.hypot(dx, dz)
        if total_length <= 1e-9:
            continue

        piece_count = max(1, int(math.ceil(total_length / max_length)))
        for piece_index in range(piece_count):
            t0 = piece_index / piece_count
            t1 = (piece_index + 1) / piece_count
            pax, paz = ax + dx * t0, az + dz * t0
            pbx, pbz = ax + dx * t1, az + dz * t1
            piece_dx, piece_dz = pbx - pax, pbz - paz
            length = math.hypot(piece_dx, piece_dz)
            center_x = (pax + pbx) * 0.5
            center_z = (paz + pbz) * 0.5

            if settings.long_axis == LONG_AXIS_Z:
                rotation_y = math.degrees(math.atan2(piece_dx, piece_dz))
                scale = [width, depth, length]
            else:
                rotation_y = -math.degrees(math.atan2(piece_dz, piece_dx))
                scale = [length, depth, width]

            object_index = len(objects)
            objects.append(
                {
                    "entity_id": int(settings.entity_id_start) + int(entity_id_offset) + object_index,
                    "name": (
                        f"SketchWideLine_{segment_index:06d}_{piece_index:03d}"
                        if source_segment_count > 1
                        else f"SketchLine_{segment_index:06d}_{piece_index:03d}"
                    ),
                    "template_id": int(settings.template_id),
                    "position": [center_x, 0.0, center_z],
                    "rotation": [0.0, rotation_y, 0.0],
                    "scale": scale,
                    "color": {
                        "rgb": [int(v) for v in settings.output_rgb],
                        "opacity": float(settings.output_opacity),
                    },
                    "collision": enable_collision and not settings.decoration_packaging,
                    "climb": enable_climb and not settings.decoration_packaging,
                    "enable_out_of_range_run": bool(settings.enable_out_of_range_run),
                    "out_of_range_display_mode": int(settings.out_of_range_display_mode),
                }
            )
    _progress(progress_callback, 100, f"基元变换生成完成，共 {len(objects):,} 个")
    return objects


def build_white_backing_object(settings: SketchGiaSettings) -> dict[str, Any] | None:
    if not settings.add_white_backing:
        return None

    enable_collision, enable_climb = collision_mode_flags(settings.collision_mode)
    backing_thickness = max(1e-6, float(settings.backing_thickness_m))

    # 叠底是独立静态元件，底面必须贴在 Y=0 地面，否则会被地形遮挡。
    # 线条对象会在导出阶段再放置到叠底顶面。
    backing_center_y = backing_thickness / 2.0
    return {
        "entity_id": int(settings.entity_id_start),
        "name": "SketchWhiteBacking",
        "template_id": int(settings.backing_template_id),
        "position": [0.0, backing_center_y, 0.0],
        "rotation": [0.0, 0.0, 0.0],
        "scale": [
            max(1e-6, float(settings.target_width_m)),
            backing_thickness,
            max(1e-6, float(settings.target_height_m)),
        ],
        "color": {"rgb": [255, 255, 255], "opacity": 100.0},
        "collision": enable_collision,
        "climb": enable_climb,
        "enable_out_of_range_run": bool(settings.enable_out_of_range_run),
        "out_of_range_display_mode": int(settings.out_of_range_display_mode),
        "exclude_from_decoration": True,
        "standalone_kind": "sketch_white_backing",
    }


def mask_preview(mask: np.ndarray) -> Image.Image:
    array = np.where(mask, 0, 255).astype(np.uint8)
    return Image.fromarray(array, mode="L")


def normalize_pixel_path(
    path: Sequence[Pixel],
    image_size: tuple[int, int],
) -> list[Point2]:
    """把骨架像素路径转换为 0..1 坐标，便于跨分支缩放映射。"""
    width, height = image_size
    denominator_x = max(1, int(width) - 1)
    denominator_y = max(1, int(height) - 1)
    return [
        (
            max(0.0, min(1.0, float(col) / denominator_x)),
            max(0.0, min(1.0, float(row) / denominator_y)),
        )
        for row, col in path
    ]


def rasterize_normalized_paths(
    paths: Sequence[Sequence[Point2]],
    image_size: tuple[int, int],
) -> np.ndarray:
    """把分支中保存的归一化保护曲线映射回当前主画布。"""
    width, height = map(int, image_size)
    output = np.zeros((max(1, height), max(1, width)), dtype=np.uint8)
    scale_x = max(1, width - 1)
    scale_y = max(1, height - 1)
    for path in paths:
        points = [
            (
                int(round(max(0.0, min(1.0, float(x))) * scale_x)),
                int(round(max(0.0, min(1.0, float(y))) * scale_y)),
            )
            for x, y in path
        ]
        for start, end in zip(points, points[1:]):
            cv2.line(output, start, end, 1, 1, lineType=cv2.LINE_8)
        if len(points) == 1:
            output[points[0][1], points[0][0]] = 1
    return output.astype(bool)


def nearest_logical_curve(
    skeleton: np.ndarray,
    point_xy: Point2,
    *,
    max_distance_px: float = 12.0,
    curves: Sequence[Sequence[Pixel]] | None = None,
) -> tuple[list[Pixel] | None, float]:
    """返回点击点附近最近的平滑续接逻辑曲线。"""
    logical_curves = (
        [list(curve) for curve in curves]
        if curves is not None
        else trace_smooth_logical_curves(skeleton.astype(bool))[0]
    )
    if not logical_curves:
        return None, float("inf")
    point = np.asarray([float(point_xy[0]), float(point_xy[1])], dtype=np.float64)
    best_curve: list[Pixel] | None = None
    best_distance = float("inf")
    for curve in logical_curves:
        if not curve:
            continue
        points = np.asarray(
            [(float(col), float(row)) for row, col in curve],
            dtype=np.float64,
        )
        distance = float(np.sqrt(np.min(np.sum((points - point) ** 2, axis=1))))
        if distance < best_distance:
            best_curve = curve
            best_distance = distance
    if best_distance > max(0.0, float(max_distance_px)):
        return None, best_distance
    return best_curve, best_distance


def preview_image_to_mask(preview_image: Image.Image) -> np.ndarray:
    """把上一步实际展示的黑白预览图还原为拟合输入掩码。

    最终拟合必须以用户在“中心骨架”Tab 中看到的图片为准，不能绕过预览图
    再读取另一份内部中间数据。透明图片优先利用 Alpha 识别可见线稿，同时
    与“合成到白底后识别深色线条”的候选结果比较，避免透明背景底层的黑色
    RGB 被误判为整幅黑色前景。
    """
    rgba = np.asarray(preview_image.convert("RGBA"), dtype=np.uint8)
    rgb = rgba[..., :3]
    alpha = rgba[..., 3]

    if np.any(alpha < 255):
        alpha_foreground = alpha > 0
        alpha_float = alpha.astype(np.float32)[..., None] / 255.0
        white_composite = (
            rgb.astype(np.float32) * alpha_float
            + 255.0 * (1.0 - alpha_float)
        ).astype(np.uint8)
        composite_gray = cv2.cvtColor(white_composite, cv2.COLOR_RGB2GRAY)
        dark_foreground = composite_gray < 128
        return min(
            (alpha_foreground, dark_foreground),
            key=_mask_score,
        ).astype(bool)

    grayscale = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return grayscale < 128


def render_segments_preview(
    segments: Sequence[Segment2 | FittedStroke],
    source_size: tuple[int, int],
    settings: SketchGiaSettings,
    progress_callback: ProgressCallback | None = None,
) -> Image.Image:
    # 预览严格保持处理图像的实际像素尺寸，不再为了页面展示进行二次缩放。
    source_width, source_height = source_size
    width = max(1, int(source_width))
    height = max(1, int(source_height))
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)

    px_per_meter_x = width / max(float(settings.target_width_m), 1e-9)
    px_per_meter_z = height / max(float(settings.target_height_m), 1e-9)
    default_line_width_px = max(
        1,
        int(round(float(settings.line_width_m) * 0.5 * (px_per_meter_x + px_per_meter_z))),
    )
    line_color = tuple(max(0, min(255, int(value))) for value in settings.output_rgb)
    total_segments = len(segments)
    update_stride = max(1, total_segments // 100) if total_segments else 1
    for segment_index, segment in enumerate(segments):
        if isinstance(segment, FittedStroke):
            (ax, ay), (bx, by) = segment.start, segment.end
            line_width_px = (
                max(1, int(round(float(segment.width_px))))
                if segment.width_px is not None
                else default_line_width_px
            )
        else:
            (ax, ay), (bx, by) = segment
            line_width_px = default_line_width_px
        if segment_index % update_stride == 0:
            _progress(
                progress_callback,
                round(100 * segment_index / max(total_segments, 1)),
                f"正在绘制最终预览：{segment_index:,}/{total_segments:,}",
            )
        draw.line((ax, ay, bx, by), fill=line_color, width=line_width_px)
    _progress(progress_callback, 100, "最终预览绘制完成")
    return canvas


def _overlapped_original_curve_mask(
    original_mask: np.ndarray,
    protected_mask: np.ndarray,
) -> tuple[np.ndarray, int]:
    """找出与保护线实质重叠的原有逻辑曲线，并返回其完整掩码。"""
    original = original_mask.astype(bool)
    protected = protected_mask.astype(bool)
    output = np.zeros_like(original, dtype=np.uint8)
    if not original.any() or not protected.any():
        return output.astype(bool), 0

    nearby_protected = cv2.dilate(
        protected.astype(np.uint8),
        np.ones((3, 3), dtype=np.uint8),
        iterations=1,
    ).astype(bool)
    if not np.any(original & nearby_protected):
        return output.astype(bool), 0

    curves, _ = trace_smooth_logical_curves(original)
    removed_count = 0
    for curve in curves:
        if not curve:
            continue
        exact_hits = [bool(protected[row, col]) for row, col in curve]
        nearby_hits = [bool(nearby_protected[row, col]) for row, col in curve]
        longest_nearby_run = 0
        current_run = 0
        for is_nearby in nearby_hits:
            current_run = current_run + 1 if is_nearby else 0
            longest_nearby_run = max(longest_nearby_run, current_run)

        # 两个精确重合像素，或至少四个连续邻近像素才算“重叠”。
        # 这样不会因为两条曲线只在叉点/交点接触一次就误删整条原线。
        if sum(exact_hits) < 2 and longest_nearby_run < 4:
            continue
        removed_count += 1
        for start, end in zip(curve, curve[1:]):
            cv2.line(
                output,
                (int(start[1]), int(start[0])),
                (int(end[1]), int(end[0])),
                1,
                1,
                lineType=cv2.LINE_8,
            )
        if len(curve) == 1:
            output[curve[0]] = 1
    return output.astype(bool), removed_count


def merge_protected_mask_into_processed_stage(
    processed: SketchProcessedStage,
    protected_mask: np.ndarray | None,
    deleted_mask: np.ndarray | None = None,
) -> SketchProcessedStage:
    """不重复检测边缘：移除重叠原线、执行删除，再写入保护线。"""
    if processed.cleaned_mask is None:
        raise ValueError("直通处理结果没有内部清理掩码，不能直接合并保护曲线")
    protected = (
        protected_mask.astype(bool)
        if protected_mask is not None
        else np.zeros_like(processed.cleaned_mask, dtype=bool)
    )
    if protected.shape != processed.cleaned_mask.shape:
        raise ValueError(
            "保护线掩码尺寸与处理图不一致："
            f"protected={protected.shape}, cleaned={processed.cleaned_mask.shape}"
        )
    deleted = (
        deleted_mask.astype(bool)
        if deleted_mask is not None
        else np.zeros_like(processed.cleaned_mask, dtype=bool)
    )
    if deleted.shape != processed.cleaned_mask.shape:
        raise ValueError(
            "删除线掩码尺寸与处理图不一致："
            f"deleted={deleted.shape}, cleaned={processed.cleaned_mask.shape}"
        )
    original = processed.cleaned_mask.astype(bool)
    overlapped_original, replaced_curve_count = (
        _overlapped_original_curve_mask(original, protected)
    )
    effective_deleted = deleted | overlapped_original
    combined = (original & ~effective_deleted) | protected
    cleanup_summary = {
        **processed.cleanup_summary,
        "protected_pixels": int(protected.sum()),
        "protected_lines_merged": bool(protected.any()),
        "deleted_pixels": int(deleted.sum()),
        "deleted_lines_applied": bool(deleted.any()),
        "protected_overlap_replaced_curve_count": int(replaced_curve_count),
        "protected_overlap_removed_pixels": int(overlapped_original.sum()),
        "cleaned_pixels": int(combined.sum()),
    }
    return SketchProcessedStage(
        scaled_image=processed.scaled_image,
        cleaned_mask=combined,
        binary_image=mask_preview(combined),
        source_summary=dict(processed.source_summary),
        cleanup_summary=cleanup_summary,
        protected_mask=protected if protected.any() else None,
        pre_short_curve_mask=processed.pre_short_curve_mask,
        pre_short_curve_image=processed.pre_short_curve_image,
    )


def process_sketch_image(
    scaled_image: Image.Image,
    settings: SketchGiaSettings,
    progress_callback: ProgressCallback | None = None,
    *,
    protected_mask: np.ndarray | None = None,
    deleted_mask: np.ndarray | None = None,
) -> SketchProcessedStage:
    """阶段 2：执行线稿识别、清理及可选的短逻辑曲线排除。"""
    if (
        settings.source_mode == SOURCE_LINE_ART
        and settings.line_color_mode == COLOR_MODE_PASSTHROUGH
    ):
        _progress(progress_callback, 20, "已选择直通，跳过线稿识别与清理")
        _progress(progress_callback, 100, "上一步缩放图已原样传递")
        return SketchProcessedStage(
            scaled_image=scaled_image,
            cleaned_mask=None,
            binary_image=scaled_image,
            source_summary={
                "color_mode": COLOR_MODE_PASSTHROUGH,
                "passthrough": True,
            },
            cleanup_summary={
                "passthrough": True,
                "cleanup_applied": False,
            },
        )

    _progress(progress_callback, 5, "正在准备线稿识别")
    _progress(progress_callback, 12, "正在识别线稿边缘…")
    if settings.source_mode == SOURCE_EDGE:
        raw_mask, source_summary = detect_edges(scaled_image, settings)
    elif settings.source_mode == SOURCE_LINE_ART:
        raw_mask, source_summary = extract_line_art_mask(scaled_image, settings)
    else:
        raise ValueError(f"不支持的线稿来源模式：{settings.source_mode}")
    if not raw_mask.any():
        raise ValueError("没有识别到线稿像素，请调整算子阈值、RGB 或极性探索设置")

    _progress(progress_callback, 58, f"线稿识别完成，共 {int(raw_mask.sum()):,} 个候选像素")
    _progress(progress_callback, 68, "正在连接断线并过滤小连通区域…")
    cleaned, cleanup_summary = clean_line_mask(raw_mask, settings)
    if not cleaned.any():
        raise ValueError("线稿清理后为空，请降低小组件过滤值或调整识别参数")

    _progress(progress_callback, 92, "正在生成处理线稿预览图")
    _progress(progress_callback, 100, "处理线稿预览完成")
    base_stage = SketchProcessedStage(
        scaled_image=scaled_image,
        cleaned_mask=cleaned,
        binary_image=mask_preview(cleaned),
        source_summary=source_summary,
        cleanup_summary=cleanup_summary,
    )
    edited_stage = merge_protected_mask_into_processed_stage(
        base_stage,
        protected_mask,
        deleted_mask,
    )
    if float(settings.exclude_curve_length_px) <= 0.0:
        return edited_stage

    _progress(progress_callback, 93, "正在细化线稿并按逻辑曲线长度排除短线…")
    skeleton, skeleton_summary = skeletonize_cleaned_mask(
        edited_stage.cleaned_mask,
        settings,
        progress_callback=_subprogress(progress_callback, 93, 97),
    )
    cleaned_skeleton, curve_cleanup_summary = exclude_short_logical_curves(
        skeleton,
        settings.exclude_curve_length_px,
        protected_mask=edited_stage.protected_mask,
    )
    if not cleaned_skeleton.any():
        raise ValueError("曲线长度清理后为空，请降低“排除曲线长度”")
    if edited_stage.protected_mask is not None and edited_stage.protected_mask.any():
        cleaned_skeleton = cleaned_skeleton | edited_stage.protected_mask.astype(bool)

    _progress(progress_callback, 100, "线稿识别、清理和短曲线排除完成")
    return SketchProcessedStage(
        scaled_image=edited_stage.scaled_image,
        cleaned_mask=cleaned_skeleton,
        binary_image=mask_preview(cleaned_skeleton),
        source_summary=dict(edited_stage.source_summary),
        cleanup_summary={
            **edited_stage.cleanup_summary,
            **skeleton_summary,
            **curve_cleanup_summary,
            "short_curve_cleanup_applied": True,
            "skeleton_pixels_before_curve_cleanup": int(skeleton.sum()),
            "skeleton_pixels": int(cleaned_skeleton.sum()),
        },
        protected_mask=edited_stage.protected_mask,
        pre_short_curve_mask=edited_stage.cleaned_mask.copy(),
        pre_short_curve_image=edited_stage.binary_image.copy(),
    )


def skeletonize_sketch_stage(
    processed: SketchProcessedStage,
    settings: SketchGiaSettings,
    progress_callback: ProgressCallback | None = None,
    *,
    include_logical_curves: bool = False,
) -> SketchSkeletonStage:
    """阶段 3：提取中心骨架；短逻辑曲线已在阶段 2 清理。"""
    _progress(progress_callback, 3, "正在准备中心骨架提取")
    cleaned_mask = processed.cleaned_mask
    input_summary: dict[str, Any] = {}
    passthrough_without_thinning = False
    if cleaned_mask is None:
        _progress(progress_callback, 4, "正在读取直通图像作为骨架输入")
        cleaned_mask = preview_image_to_mask(processed.binary_image)
        input_summary = {
            "passthrough_mask_conversion": "alpha_aware_preview_mask",
        }
        if not cleaned_mask.any():
            raise ValueError("直通图像中没有可用于骨架提取的黑色线稿像素")
        passthrough_without_thinning = not settings.auto_thin_wide_lines

    short_curve_cleanup_applied = bool(
        processed.cleanup_summary.get("short_curve_cleanup_applied", False)
    )
    skeleton, skeleton_summary = skeletonize_cleaned_mask(
        cleaned_mask,
        settings,
        progress_callback=_subprogress(progress_callback, 5, 82),
    )
    if not skeleton.any():
        raise ValueError("中心骨架为空，请关闭自动细化或调整宽线探索半径")

    curve_cleanup_summary: dict[str, Any] = {
        "exclude_curve_length_px": float(settings.exclude_curve_length_px),
        "excluded_curve_count": 0,
        "excluded_curve_total_length_px": 0.0,
        "protected_curve_count": 0,
    }
    if short_curve_cleanup_applied:
        curve_cleanup_summary = {
            key: processed.cleanup_summary.get(key, value)
            for key, value in curve_cleanup_summary.items()
        }
    cleaned_skeleton = skeleton
    if float(settings.exclude_curve_length_px) > 0.0 and not short_curve_cleanup_applied:
        _progress(progress_callback, 84, "正在按叉点方向重组曲线并排除短曲线")
        cleaned_skeleton, curve_cleanup_summary = exclude_short_logical_curves(
            skeleton,
            settings.exclude_curve_length_px,
            protected_mask=processed.protected_mask,
        )
        if not cleaned_skeleton.any():
            raise ValueError("曲线长度清理后为空，请降低“排除曲线长度”")

    protected_skeleton = (
        processed.protected_mask.astype(bool)
        if processed.protected_mask is not None
        else None
    )
    if protected_skeleton is not None and protected_skeleton.any():
        cleaned_skeleton = cleaned_skeleton | protected_skeleton

    logical_curves: list[list[Pixel]] | None = None
    if include_logical_curves:
        _progress(progress_callback, 97, "正在生成可选择的保护曲线")
        logical_curves = trace_smooth_logical_curves(cleaned_skeleton)[0]

    preserve_passthrough_image = (
        passthrough_without_thinning
        and float(settings.exclude_curve_length_px) <= 0.0
    )
    skeleton_image = (
        processed.binary_image
        if preserve_passthrough_image
        else mask_preview(cleaned_skeleton)
    )
    _progress(progress_callback, 100, "中心骨架预览完成")
    return SketchSkeletonStage(
        skeleton_mask=cleaned_skeleton,
        skeleton_image=skeleton_image,
        cleanup_summary={
            **processed.cleanup_summary,
            **input_summary,
            **skeleton_summary,
            **curve_cleanup_summary,
            "skeleton_pixels_before_curve_cleanup": int(skeleton.sum()),
            "skeleton_pixels": int(cleaned_skeleton.sum()),
            "passthrough": bool(processed.cleanup_summary.get("passthrough", False)),
            "protected_pixels": (
                int(protected_skeleton.sum())
                if protected_skeleton is not None
                else 0
            ),
        },
        protected_mask=protected_skeleton,
        logical_curves=logical_curves,
    )


def _ribbon_rect_corners(rect: RibbonRect) -> np.ndarray:
    start = np.asarray(rect.start, dtype=np.float64)
    end = np.asarray(rect.end, dtype=np.float64)
    direction = end - start
    length = float(np.linalg.norm(direction))
    if length <= 1e-12:
        return np.repeat(start[None, :], 4, axis=0)
    unit = direction / length
    normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
    half_width = max(0.5, float(rect.width_px) * 0.5)
    return np.asarray(
        [
            start + normal * half_width,
            end + normal * half_width,
            end - normal * half_width,
            start - normal * half_width,
        ],
        dtype=np.float64,
    )


def _ribbon_rectangles_mask(
    rects: Sequence[RibbonRect],
    image_size: tuple[int, int],
) -> np.ndarray:
    width, height = map(int, image_size)
    canvas = np.zeros((max(1, height), max(1, width)), dtype=np.uint8)
    for rect in rects:
        corners = np.round(_ribbon_rect_corners(rect)).astype(np.int32)
        cv2.fillConvexPoly(canvas, corners, 1)
    return canvas.astype(bool)


def _ribbon_coverage_metrics(
    target_mask: np.ndarray,
    rects: Sequence[RibbonRect],
    image_size: tuple[int, int],
) -> dict[str, float | int]:
    fitted = _ribbon_rectangles_mask(rects, image_size)
    target = target_mask.astype(bool)
    intersection = int(np.logical_and(target, fitted).sum())
    union = int(np.logical_or(target, fitted).sum())
    missed = int(np.logical_and(target, ~fitted).sum())
    overflow = int(np.logical_and(~target, fitted).sum())
    target_count = max(1, int(target.sum()))
    return {
        "intersection_pixels": intersection,
        "union_pixels": union,
        "missed_pixels": missed,
        "overflow_pixels": overflow,
        "iou": float(intersection / union) if union else 1.0,
        "miss_ratio": float(missed / target_count),
        "overflow_ratio": float(overflow / target_count),
        "total_error_ratio": float((missed + overflow) / target_count),
    }


def _component_to_ribbon_rect(
    component_mask: np.ndarray,
    *,
    padding_px: float = 0.75,
    source_path_index: int = -1,
) -> RibbonRect | None:
    """使用 OpenCV 最小面积旋转矩形生成漏覆盖补片。"""
    rows, cols = np.nonzero(component_mask)
    if len(rows) == 0:
        return None
    points = np.column_stack((cols, rows)).astype(np.float32)
    rotated_rect = cv2.minAreaRect(points)
    center = np.asarray(rotated_rect[0], dtype=np.float64)
    box = cv2.boxPoints(rotated_rect).astype(np.float64)
    edge_vectors = np.roll(box, -1, axis=0) - box
    edge_lengths = np.linalg.norm(edge_vectors, axis=1)
    major_index = int(np.argmax(edge_lengths))
    major_length = float(edge_lengths[major_index]) + 1.0 + 2.0 * float(padding_px)
    minor_length = float(np.min(edge_lengths)) + 1.0 + 2.0 * float(padding_px)
    major = edge_vectors[major_index]
    major_norm = float(np.linalg.norm(major))
    if major_norm <= 1e-9:
        major = np.asarray([1.0, 0.0], dtype=np.float64)
    else:
        major = major / major_norm
    start = center - major * major_length * 0.5
    end = center + major * major_length * 0.5
    return RibbonRect(
        start=(float(start[0]), float(start[1])),
        end=(float(end[0]), float(end[1])),
        width_px=max(1.0, minor_length),
        source_path_index=int(source_path_index),
        source_point_count=int(len(points)),
    )


def _add_ribbon_residual_patches(
    target_mask: np.ndarray,
    rects: Sequence[RibbonRect],
    image_size: tuple[int, int],
    *,
    target_miss_ratio: float,
    minimum_component_area: int,
    maximum_total_rectangles: int,
) -> tuple[list[RibbonRect], int, dict[str, float | int]]:
    """在数量救援上限内，为漏覆盖区域添加旋转矩形补片。"""
    result = list(rects)
    target_ratio = max(0.0, float(target_miss_ratio))
    min_area = max(1, int(minimum_component_area))
    max_total = max(len(result), int(maximum_total_rectangles))
    added = 0

    for _ in range(3):
        metrics = _ribbon_coverage_metrics(target_mask, result, image_size)
        if float(metrics["miss_ratio"]) <= target_ratio or len(result) >= max_total:
            return result, added, metrics

        fitted = _ribbon_rectangles_mask(result, image_size)
        missed = np.logical_and(target_mask.astype(bool), ~fitted).astype(np.uint8)
        label_count, labels, stats, _ = cv2.connectedComponentsWithStats(missed, connectivity=8)
        components: list[tuple[int, int]] = []
        for label in range(1, label_count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area >= min_area:
                components.append((area, label))
        components.sort(reverse=True)
        if not components:
            return result, added, metrics

        added_this_round = 0
        for area, label in components:
            if len(result) >= max_total:
                break
            patch = _component_to_ribbon_rect(
                labels == label,
                padding_px=0.75,
                source_path_index=-(added + 1),
            )
            if patch is None:
                continue
            result.append(patch)
            added += 1
            added_this_round += 1
        if added_this_round == 0:
            break

    return result, added, _ribbon_coverage_metrics(target_mask, result, image_size)


def fit_ribbon_lines_opencv(
    skeleton: np.ndarray,
    distance_map: np.ndarray,
    *,
    straightness_tolerance_px: float,
    width_variation_tolerance: float,
    minimum_length_px: float,
    width_scale: float,
    minimum_width_px: float,
    joint_overlap_px: float,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[RibbonRect], dict[str, Any]]:
    """使用 OpenCV 概率霍夫变换把中心骨架检测为旋转矩形带。"""
    del width_variation_tolerance  # 线宽直接由 OpenCV 距离变换的稳健分位数确定。
    tolerance = max(0.0, float(straightness_tolerance_px))
    minimum_length = max(1.0, float(minimum_length_px))
    overlap = max(0.0, float(joint_overlap_px))
    hough_threshold = max(2, min(100, int(round(minimum_length * 0.5 + tolerance))))
    maximum_gap = max(0, min(64, int(round(tolerance))))
    _progress(progress_callback, 15, "正在使用 OpenCV HoughLinesP 检测中心线段")
    detected = cv2.HoughLinesP(
        skeleton.astype(np.uint8) * 255,
        rho=1.0,
        theta=np.pi / 720.0,
        threshold=hough_threshold,
        minLineLength=max(1.0, minimum_length),
        maxLineGap=maximum_gap,
    )
    if detected is None:
        return [], {
            "engine": "opencv_hough_lines_p",
            "detected_line_count": 0,
            "hough_threshold": hough_threshold,
            "max_line_gap_px": maximum_gap,
        }

    lines = detected.reshape(-1, 4)
    rects: list[RibbonRect] = []
    height, width = distance_map.shape
    for line_index, (x1, y1, x2, y2) in enumerate(lines):
        if line_index % max(1, len(lines) // 100) == 0:
            _progress(
                progress_callback,
                20 + round(75 * line_index / max(len(lines), 1)),
                f"正在计算 OpenCV 线段宽度：{line_index:,}/{len(lines):,}",
            )
        start = np.asarray([float(x1), float(y1)], dtype=np.float64)
        end = np.asarray([float(x2), float(y2)], dtype=np.float64)
        vector = end - start
        length = float(np.linalg.norm(vector))
        if length < minimum_length or length <= 1e-9:
            continue
        sample_count = max(2, int(math.ceil(length)) + 1)
        sample_x = np.clip(
            np.rint(np.linspace(start[0], end[0], sample_count)).astype(np.int32),
            0,
            width - 1,
        )
        sample_y = np.clip(
            np.rint(np.linspace(start[1], end[1], sample_count)).astype(np.int32),
            0,
            height - 1,
        )
        widths = 2.0 * distance_map[sample_y, sample_x] * float(width_scale)
        positive_widths = widths[widths > 0.0]
        width_px = max(
            float(minimum_width_px),
            float(np.percentile(positive_widths, 80.0))
            if positive_widths.size
            else float(minimum_width_px),
        )
        unit = vector / length
        extension = min(overlap * 0.5, length * 0.45)
        start = start - unit * extension
        end = end + unit * extension
        rects.append(
            RibbonRect(
                start=(float(start[0]), float(start[1])),
                end=(float(end[0]), float(end[1])),
                width_px=float(width_px),
                source_path_index=int(line_index),
                source_point_count=int(sample_count),
            )
        )

    _progress(progress_callback, 100, f"OpenCV 线段检测完成，共 {len(rects):,} 个旋转矩形")
    return rects, {
        "engine": "opencv_hough_lines_p",
        "detected_line_count": int(len(lines)),
        "accepted_line_count": int(len(rects)),
        "hough_threshold": int(hough_threshold),
        "max_line_gap_px": int(maximum_gap),
        "theta_resolution_degrees": 0.25,
    }


def fit_ribbon_rectangles_to_budget(
    mask: np.ndarray,
    image_size: tuple[int, int],
    settings: SketchGiaSettings,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[RibbonRect], dict[str, Any], np.ndarray]:
    """使用 OpenCV 检测线段、估计宽度并按覆盖率选择旋转矩形。"""
    target_mask = mask.astype(bool)
    if not target_mask.any():
        raise ValueError("处理线稿预览为空，无法构造矩形带网格")

    _progress(progress_callback, 2, "正在从线稿区域提取中心路径")
    skeleton, skeleton_summary = skeletonize_cleaned_mask(
        target_mask,
        settings,
        progress_callback=_subprogress(progress_callback, 3, 22),
    )
    if not skeleton.any():
        raise ValueError("线稿区域无法提取中心路径")
    _progress(progress_callback, 23, "正在准备 OpenCV 线段检测输入")
    distance_map = cv2.distanceTransform(target_mask.astype(np.uint8), cv2.DIST_L2, 5)

    target = max(1, int(settings.ribbon_max_primitives))
    slack_ratio = max(0.0, float(settings.ribbon_budget_slack_ratio))
    allowed_max = max(target, int(math.ceil(target * (1.0 + slack_ratio))))
    target_miss_ratio = max(0.0, min(1.0, float(settings.ribbon_target_miss_ratio)))
    rescue_ratio = max(slack_ratio, float(settings.ribbon_coverage_rescue_ratio))
    coverage_rescue_max = max(allowed_max, int(math.ceil(target * (1.0 + rescue_ratio))))
    base_tolerance = max(0.0, float(settings.ribbon_straightness_tolerance_px))
    diagonal = max(1.0, math.hypot(*image_size))

    candidates: list[dict[str, Any]] = []
    maximum_candidates = 14
    for candidate_index in range(maximum_candidates):
        fraction = candidate_index / max(maximum_candidates - 1, 1)
        tolerance = base_tolerance + (diagonal * 0.20 - base_tolerance) * (fraction ** 2)
        start_progress = 42 + round(44 * candidate_index / maximum_candidates)
        end_progress = 42 + round(44 * (candidate_index + 1) / maximum_candidates)
        rects, detector_summary = fit_ribbon_lines_opencv(
            skeleton,
            distance_map,
            straightness_tolerance_px=tolerance,
            width_variation_tolerance=settings.ribbon_width_variation_tolerance,
            minimum_length_px=settings.ribbon_minimum_length_px,
            width_scale=settings.ribbon_width_scale,
            minimum_width_px=settings.ribbon_minimum_width_px,
            joint_overlap_px=settings.ribbon_joint_overlap_px,
            progress_callback=_subprogress(
                progress_callback,
                start_progress,
                end_progress,
                f"候选 {candidate_index + 1}/{maximum_candidates}：",
            ),
        )
        if not rects:
            continue
        base_count = len(rects)
        rescued_rects, residual_patch_count, coverage = _add_ribbon_residual_patches(
            target_mask,
            rects,
            image_size,
            target_miss_ratio=target_miss_ratio,
            minimum_component_area=int(settings.ribbon_residual_min_area_px),
            maximum_total_rectangles=max(base_count, coverage_rescue_max),
        )
        candidates.append(
            {
                "rects": rescued_rects,
                "count": len(rescued_rects),
                "base_count": base_count,
                "residual_patch_count": int(residual_patch_count),
                "straightness_tolerance_px": float(tolerance),
                "coverage": coverage,
                "detector": detector_summary,
            }
        )

    if not candidates:
        raise ValueError("矩形带拟合没有生成任何有效矩形")

    def score(candidate: dict[str, Any]) -> tuple[float, ...]:
        count = int(candidate["count"])
        count_gap = abs(count - target) / max(target, 1)
        coverage = candidate["coverage"]
        miss_ratio = float(coverage["miss_ratio"])
        total_error = float(coverage["total_error_ratio"])
        meets_coverage = miss_ratio <= target_miss_ratio
        if meets_coverage:
            # 达到漏覆盖目标后，仍优先减少总覆盖误差，再考虑数量接近程度。
            return (0.0, total_error, count_gap)
        # 未达到覆盖目标时，覆盖完整度绝对优先于基元数量。
        return (1.0, miss_ratio, total_error, count_gap)

    bounded_candidates = [
        item for item in candidates if int(item["count"]) <= coverage_rescue_max
    ]
    selection_pool = bounded_candidates or candidates
    selected = min(selection_pool, key=score)
    _progress(progress_callback, 90, "正在统计矩形带覆盖误差")
    summary = {
        "center_path_count": int(selected["detector"].get("accepted_line_count", 0)),
        "skeleton_pixel_count": int(skeleton.sum()),
        "requested_max_primitives": target,
        "allowed_soft_max": allowed_max,
        "coverage_rescue_max": coverage_rescue_max,
        "budget_slack_ratio": slack_ratio,
        "target_miss_ratio": target_miss_ratio,
        "rectangle_count": int(selected["count"]),
        "base_rectangle_count": int(selected["base_count"]),
        "residual_patch_count": int(selected["residual_patch_count"]),
        "base_straightness_tolerance_px": base_tolerance,
        "effective_straightness_tolerance_px": float(selected["straightness_tolerance_px"]),
        "width_scale": float(settings.ribbon_width_scale),
        "minimum_width_px": float(settings.ribbon_minimum_width_px),
        "joint_overlap_px": float(settings.ribbon_joint_overlap_px),
        "candidate_count": len(candidates),
        "bounded_candidate_count": len(bounded_candidates),
        "candidate_counts": [
            {
                "rectangle_count": int(item["count"]),
                "base_rectangle_count": int(item["base_count"]),
                "residual_patch_count": int(item["residual_patch_count"]),
                "straightness_tolerance_px": float(item["straightness_tolerance_px"]),
                "iou": float(item["coverage"]["iou"]),
                "miss_ratio": float(item["coverage"]["miss_ratio"]),
            }
            for item in candidates
        ],
        "coverage": selected["coverage"],
        "geometry_engine": "opencv_hough_lines_p",
        "residual_patch_engine": "opencv_min_area_rect",
        "detector": selected["detector"],
        "mesh_rule": "OpenCV HoughLinesP 检测中心线段，distanceTransform 估计线宽，minAreaRect 补齐漏覆盖区域；不是像素网格",
        "budget_rule": "先满足漏覆盖目标，再接近软数量预算；未满足时覆盖完整度绝对优先，并用局部旋转矩形补齐残余区域",
        "skeleton_summary": skeleton_summary,
    }
    _progress(progress_callback, 100, f"矩形带预算完成，选择 {len(selected['rects']):,} 个旋转矩形")
    return list(selected["rects"]), summary, skeleton


def render_ribbon_preview(
    target_mask: np.ndarray,
    rects: Sequence[RibbonRect],
    source_size: tuple[int, int],
    progress_callback: ProgressCallback | None = None,
) -> Image.Image:
    width, height = map(int, source_size)
    target = target_mask.astype(bool)
    canvas_array = np.full((max(1, height), max(1, width), 3), 255, dtype=np.uint8)
    canvas_array[target] = np.asarray([205, 205, 205], dtype=np.uint8)
    canvas = Image.fromarray(canvas_array, mode="RGB")
    draw = ImageDraw.Draw(canvas)
    total = len(rects)
    for index, rect in enumerate(rects):
        if index % max(1, max(total, 1) // 100) == 0:
            _progress(progress_callback, round(100 * index / max(total, 1)), f"正在绘制矩形带预览：{index:,}/{total:,}")
        corners = [tuple(map(float, point)) for point in _ribbon_rect_corners(rect)]
        draw.polygon(corners, fill=(0, 0, 0), outline=(90, 90, 90))
    _progress(progress_callback, 100, "矩形带预览绘制完成")
    return canvas


def _pixel_width_to_world_width(
    width_px_value: float,
    start: Point2,
    end: Point2,
    image_size: tuple[int, int],
    target_size: tuple[float, float],
) -> float:
    width_px, height_px = image_size
    target_width_m, target_height_m = target_size
    dx = float(end[0] - start[0])
    dy = float(end[1] - start[1])
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        return max(1e-6, float(width_px_value) * target_width_m / max(width_px, 1))
    nx = -dy / length
    ny = dx / length
    meters_per_px_x = float(target_width_m) / max(width_px, 1)
    meters_per_px_z = float(target_height_m) / max(height_px, 1)
    return max(
        1e-6,
        float(width_px_value) * math.hypot(nx * meters_per_px_x, ny * meters_per_px_z),
    )


def ribbon_rectangles_to_objects(
    rects: Sequence[RibbonRect],
    image_size: tuple[int, int],
    settings: SketchGiaSettings,
    *,
    entity_id_offset: int = 0,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    width_px, height_px = image_size
    enable_collision, enable_climb = collision_mode_flags(settings.collision_mode)
    depth = max(1e-6, float(settings.depth_m))
    objects: list[dict[str, Any]] = []
    total = len(rects)
    for index, rect in enumerate(rects):
        if index % max(1, max(total, 1) // 100) == 0:
            _progress(progress_callback, round(100 * index / max(total, 1)), f"正在生成矩形带基元：{index:,}/{total:,}")
        ax, az = _pixel_to_world(rect.start, width_px, height_px, settings.target_width_m, settings.target_height_m)
        bx, bz = _pixel_to_world(rect.end, width_px, height_px, settings.target_width_m, settings.target_height_m)
        dx, dz = bx - ax, bz - az
        length = math.hypot(dx, dz)
        if length <= 1e-9:
            continue
        width_m = _pixel_width_to_world_width(
            rect.width_px,
            rect.start,
            rect.end,
            image_size,
            (settings.target_width_m, settings.target_height_m),
        )
        center_x = (ax + bx) * 0.5
        center_z = (az + bz) * 0.5
        if settings.long_axis == LONG_AXIS_Z:
            rotation_y = math.degrees(math.atan2(dx, dz))
            scale = [width_m, depth, length]
        else:
            rotation_y = -math.degrees(math.atan2(dz, dx))
            scale = [length, depth, width_m]
        objects.append(
            {
                "entity_id": int(settings.entity_id_start) + int(entity_id_offset) + len(objects),
                "name": f"SketchRibbon_{index:06d}",
                "template_id": int(settings.ribbon_template_id),
                "position": [center_x, 0.0, center_z],
                "rotation": [0.0, rotation_y, 0.0],
                "scale": scale,
                "color": {
                    "rgb": [int(value) for value in settings.output_rgb],
                    "opacity": float(settings.output_opacity),
                },
                "collision": enable_collision and not settings.decoration_packaging,
                "climb": enable_climb and not settings.decoration_packaging,
                "enable_out_of_range_run": bool(settings.enable_out_of_range_run),
                "out_of_range_display_mode": int(settings.out_of_range_display_mode),
            }
        )
    _progress(progress_callback, 100, f"矩形带基元生成完成，共 {len(objects):,} 个")
    return objects


def fit_ribbon_mesh_stage(
    processed: SketchProcessedStage,
    settings: SketchGiaSettings,
    progress_callback: ProgressCallback | None = None,
    *,
    source_preview_image: Image.Image | None = None,
    source_label: str = "previous_processed_preview_image",
) -> SketchAnalysisResult:
    """实验功能：把线稿区域构造成带状四边形网格，再拉升为旋转长方体。"""
    _progress(progress_callback, 2, "正在读取处理线稿预览图")
    fit_preview = source_preview_image if source_preview_image is not None else processed.binary_image
    fit_input_mask = preview_image_to_mask(fit_preview)
    if fit_preview.size != processed.scaled_image.size:
        raise ValueError(
            f"矩形带输入预览尺寸与缩放图不一致：preview={fit_preview.size}, scaled={processed.scaled_image.size}"
        )
    if not fit_input_mask.any():
        raise ValueError("处理线稿预览图为空，无法进行矩形带拉升拟合")

    rects, ribbon_summary, skeleton = fit_ribbon_rectangles_to_budget(
        fit_input_mask,
        processed.scaled_image.size,
        settings,
        progress_callback=_subprogress(progress_callback, 4, 72),
    )

    _progress(progress_callback, 73, "正在生成旋转矩形基元与预览")
    backing = build_white_backing_object(settings)
    ribbon_objects = ribbon_rectangles_to_objects(
        rects,
        processed.scaled_image.size,
        settings,
        entity_id_offset=1 if backing is not None else 0,
        progress_callback=_subprogress(progress_callback, 74, 88),
    )
    if backing is not None:
        ribbon_objects = place_objects_on_backing(ribbon_objects, backing)
    objects = ([backing] if backing is not None else []) + ribbon_objects
    final_preview = render_ribbon_preview(
        fit_input_mask,
        rects,
        processed.scaled_image.size,
        progress_callback=_subprogress(progress_callback, 89, 98),
    )
    summary = {
        "source": processed.source_summary,
        "cleanup": processed.cleanup_summary,
        "topology": {
            "fit_input": str(source_label),
            "fit_input_black_pixel_count": int(fit_input_mask.sum()),
            "center_path_count": int(ribbon_summary["center_path_count"]),
        },
        "ribbon_mesh": ribbon_summary,
        "output": {
            "object_count": len(objects),
            "line_object_count": len(ribbon_objects),
            "backing_object_count": 1 if backing is not None else 0,
            "fitted_segment_count": len(rects),
            "raw_fitted_segment_count": len(rects),
            "template_id": int(settings.ribbon_template_id),
            "depth_m": None,
            "depth_configured_at_export": True,
            "white_backing": {
                "enabled": bool(settings.add_white_backing),
                "template_id": int(settings.backing_template_id),
                "thickness_m": float(settings.backing_thickness_m),
                "rgb": [255, 255, 255],
            },
            "target_size_m": {
                "width": float(settings.target_width_m),
                "height": float(settings.target_height_m),
            },
            "position_step": 0.0,
            "rotation_step_deg": 0.0,
            "scale_step": 0.0,
            "transform_rule": "线稿带状四边形沿 Y 轴拉升为旋转长方体，不进行 0.01Z^3 量化",
        },
        "settings": asdict(settings),
    }
    _progress(progress_callback, 100, "矩形带拉升拟合预览完成")
    return SketchAnalysisResult(
        scaled_image=processed.scaled_image,
        binary_image=processed.binary_image,
        skeleton_image=mask_preview(skeleton),
        final_preview=final_preview,
        objects=objects,
        segments_px=[],
        strokes_px=[],
        ribbon_rects_px=list(rects),
        geometry_mode="ribbon_mesh",
        summary=summary,
    )


def fit_sketch_stage(
    processed: SketchProcessedStage,
    skeleton_stage: SketchSkeletonStage,
    settings: SketchGiaSettings,
    progress_callback: ProgressCallback | None = None,
) -> SketchAnalysisResult:
    """阶段 4：只执行曲线追踪、预算拟合、基元转换和最终预览。"""
    _progress(progress_callback, 2, "正在读取上一步中心骨架预览图")
    fit_input_mask = preview_image_to_mask(skeleton_stage.skeleton_image)
    expected_size = processed.scaled_image.size
    if skeleton_stage.skeleton_image.size != expected_size:
        raise ValueError(
            "中心骨架预览尺寸与处理图不一致："
            f"preview={skeleton_stage.skeleton_image.size}, processed={expected_size}"
        )
    if not fit_input_mask.any():
        raise ValueError("上一步中心骨架预览图为空，无法进行最终拟合")

    paths = trace_skeleton_paths(
        fit_input_mask,
        progress_callback=_subprogress(progress_callback, 4, 34),
    )
    if not paths:
        raise ValueError("未能从骨架提取有效曲线")

    _progress(progress_callback, 35, "正在合并曲线点并逼近基元预算…")
    width_px, height_px = processed.scaled_image.size
    segments, budget_summary = fit_paths_to_budget(
        paths,
        settings.simplify_tolerance_px,
        settings.min_segment_length_px,
        settings.max_primitives,
        settings.budget_slack_ratio,
        math.hypot(width_px, height_px),
        progress_callback=_subprogress(progress_callback, 36, 70),
        protected_mask=skeleton_stage.protected_mask,
    )
    if not segments:
        raise ValueError(
            "曲线简化后没有可生成的线段，请降低曲线误差阈值、"
            "排除曲线长度或排除线段长度"
        )

    # 原曲线拟合保持既有逻辑：每条简化线段直接生成一个固定厚度基元。
    # 网格拟合是独立实验功能，不在这里插入宽线相似合并。
    strokes = [FittedStroke(start=a, end=b) for a, b in segments]

    _progress(progress_callback, 72, "正在生成线段基元、纯白叠底与最终预览…")
    backing = build_white_backing_object(settings)
    line_objects = segments_to_objects(
        strokes,
        processed.scaled_image.size,
        settings,
        entity_id_offset=1 if backing is not None else 0,
        progress_callback=_subprogress(progress_callback, 73, 90),
    )
    if backing is not None:
        line_objects = place_objects_on_backing(line_objects, backing)
    objects = ([backing] if backing is not None else []) + line_objects
    final_preview = render_segments_preview(
        strokes,
        processed.scaled_image.size,
        settings,
        progress_callback=_subprogress(progress_callback, 91, 98),
    )

    summary = {
        "source": processed.source_summary,
        "cleanup": skeleton_stage.cleanup_summary,
        "topology": {
            "fit_input": "previous_skeleton_preview_image",
            "fit_input_black_pixel_count": int(fit_input_mask.sum()),
            "curve_path_count": len(paths),
            "raw_path_point_count": int(sum(len(path) for path in paths)),
        },
        "budget": budget_summary,
        "output": {
            "object_count": len(objects),
            "line_object_count": len(line_objects),
            "backing_object_count": 1 if backing is not None else 0,
            "fitted_segment_count": len(strokes),
            "raw_fitted_segment_count": len(segments),
            "length_split_extra_objects": max(0, len(line_objects) - len(segments)),
            "template_id": int(settings.template_id),
            "long_axis": settings.long_axis,
            "line_width_m": float(settings.line_width_m),
            "depth_m": float(settings.depth_m),
            "white_backing": {
                "enabled": bool(settings.add_white_backing),
                "template_id": int(settings.backing_template_id),
                "thickness_m": float(settings.backing_thickness_m),
                "rgb": [255, 255, 255],
            },
            "target_size_m": {
                "width": float(settings.target_width_m),
                "height": float(settings.target_height_m),
            },
            "position_step": 0.0,
            "rotation_step_deg": 0.0,
            "scale_step": 0.0,
            "transform_rule": "连续期望值直接写出，不进行 0.01Z^3 位置/旋转/缩放量化",
        },
        "settings": asdict(settings),
    }
    _progress(progress_callback, 100, "最终拟合预览完成")
    return SketchAnalysisResult(
        scaled_image=processed.scaled_image,
        binary_image=processed.binary_image,
        skeleton_image=skeleton_stage.skeleton_image,
        final_preview=final_preview,
        objects=objects,
        segments_px=[(stroke.start, stroke.end) for stroke in strokes],
        strokes_px=list(strokes),
        ribbon_rects_px=[],
        geometry_mode="strokes",
        summary=summary,
    )


def analyze_sketch(
    image: Image.Image,
    settings: SketchGiaSettings,
    progress_callback: ProgressCallback | None = None,
) -> SketchAnalysisResult:
    """完整兼容入口；分阶段界面使用上面的三个阶段函数。"""
    _progress(progress_callback, 2, "正在缩放上传图…")
    scaled = scale_image(image, settings.scale_x_percent, settings.scale_y_percent)
    _progress(progress_callback, 5, "上传图缩放完成")
    processed = process_sketch_image(
        scaled,
        settings,
        progress_callback=_subprogress(progress_callback, 6, 30),
    )
    skeleton_stage = skeletonize_sketch_stage(
        processed,
        settings,
        progress_callback=_subprogress(progress_callback, 31, 58),
    )
    return fit_sketch_stage(
        processed,
        skeleton_stage,
        settings,
        progress_callback=_subprogress(progress_callback, 59, 100),
    )


def build_sketch_gia_bytes(
    *,
    result: SketchAnalysisResult,
    settings: SketchGiaSettings,
    template_path: Path = DEFAULT_TEMPLATE_GIA,
    progress_callback: ProgressCallback | None = None,
) -> tuple[bytes, dict[str, Any], str]:
    if not template_path.exists():
        raise FileNotFoundError(f"模板 GIA 不存在：{template_path}")
    geometry_mode = getattr(result, "geometry_mode", "strokes")
    # 颜色、碰撞和纯白叠底属于导出选项。
    # 在导出时根据已缓存的几何结果重新物化对象，避免这些选项污染拟合缓存。
    _progress(progress_callback, 4, "正在应用导出颜色、碰撞与叠底设置…")
    backing = build_white_backing_object(settings)
    if geometry_mode == "ribbon_mesh":
        export_rects = list(getattr(result, "ribbon_rects_px", []) or [])
        if not export_rects:
            raise ValueError("没有可导出的矩形带拟合结果")
        line_objects = ribbon_rectangles_to_objects(
            export_rects,
            result.scaled_image.size,
            settings,
            entity_id_offset=1 if backing is not None else 0,
            progress_callback=_subprogress(progress_callback, 6, 30),
        )
        empty_message = "没有可导出的矩形带对象"
    else:
        export_strokes = result.strokes_px if getattr(result, "strokes_px", None) else [
            FittedStroke(start=a, end=b) for a, b in result.segments_px
        ]
        if not export_strokes:
            raise ValueError("没有可导出的拟合线段")
        line_objects = segments_to_objects(
            export_strokes,
            result.scaled_image.size,
            settings,
            entity_id_offset=1 if backing is not None else 0,
            progress_callback=_subprogress(progress_callback, 6, 30),
        )
        empty_message = "没有可导出的线段对象"
    if not line_objects:
        raise ValueError(empty_message)
    if backing is not None:
        # 最终导出会重新物化线条对象，必须在这里再次应用贴面布局；
        # 不能依赖拟合预览阶段缓存的 objects。
        line_objects = place_objects_on_backing(line_objects, backing)
    objects = ([backing] if backing is not None else []) + line_objects

    _progress(progress_callback, 34, "正在准备线段对象数据…")
    objects_json = json.dumps(objects, ensure_ascii=False, indent=2) + "\n"
    with tempfile.TemporaryDirectory(prefix="qx_sketch_gia_") as tmp:
        tmp_dir = Path(tmp)
        objects_path = tmp_dir / "sketch_objects.json"
        output_path = tmp_dir / "sketch_lines.gia"
        summary_path = tmp_dir / "sketch_lines.summary.json"
        objects_path.write_text(objects_json, encoding="utf-8")
        _progress(progress_callback, 40, "正在复制模板元件并写入连续变换…")
        # 图片导入的父空模型需要精确覆盖 AABB 来提供碰撞；线稿的父模型
        # 只承载装饰物关系且碰撞固定关闭，因此保持单位缩放，避免空模型
        # 被拉伸成异常色块。父原点仍使用线条几何的底面中心。
        line_bounds = geometry_bounds(line_objects)
        build_summary = build_gia(
            template_path=template_path,
            objects_path=objects_path,
            output_path=output_path,
            summary_path=summary_path,
            entity_id_start=settings.entity_id_start,
            progress_callback=_subprogress(progress_callback, 40, 88),
            decoration_packaging=bool(settings.decoration_packaging),
            max_decorations_per_parent=int(settings.max_decorations_per_parent),
            wrapper_template_id=int(settings.wrapper_template_id),
            wrapper_static=bool(settings.wrapper_static),
            # 线稿包装父空模型和子装饰物都固定关闭碰撞/攀爬；
            # 需要物理表面时只使用独立纯白叠底。
            wrapper_collision=False,
            wrapper_climb=False,
            wrapper_enable_out_of_range_run=bool(settings.wrapper_enable_out_of_range_run),
            wrapper_out_of_range_display_mode=int(settings.wrapper_out_of_range_display_mode),
            decoration_parent_position=list(line_bounds.bottom_center)
            if settings.decoration_packaging
            else None,
            decoration_parent_scale=[1.0, 1.0, 1.0]
            if settings.decoration_packaging
            else None,
        )
        _progress(progress_callback, 92, "正在封装 GIA 文件…")
        data = output_path.read_bytes()

    summary = dict(result.summary)
    summary["settings"] = asdict(settings)
    summary["output"] = dict(summary.get("output", {}))
    summary["output"].update(
        {
            "object_count": len(objects),
            "line_object_count": len(line_objects),
            "backing_object_count": 1 if backing is not None else 0,
            "output_rgb": [int(value) for value in settings.output_rgb],
            "collision_mode": settings.collision_mode,
            "enable_out_of_range_run": bool(settings.enable_out_of_range_run),
            "out_of_range_display_mode": int(settings.out_of_range_display_mode),
            "geometry_mode": geometry_mode,
            "depth_m": float(settings.depth_m),
            "decoration_packaging": bool(settings.decoration_packaging),
            "max_decorations_per_parent": int(settings.max_decorations_per_parent),
            "wrapper_static": bool(settings.wrapper_static),
            "wrapper_collision": False,
            "wrapper_climb": False,
            "wrapper_enable_out_of_range_run": bool(settings.wrapper_enable_out_of_range_run),
            "wrapper_out_of_range_display_mode": int(settings.wrapper_out_of_range_display_mode),
            "white_backing": {
                "enabled": bool(settings.add_white_backing),
                "template_id": int(settings.backing_template_id),
                "thickness_m": float(settings.backing_thickness_m),
                "position": list(backing["position"]) if backing is not None else None,
                "scale": list(backing["scale"]) if backing is not None else None,
                "rgb": [255, 255, 255],
            },
        }
    )
    summary["gia"] = {
        "template": str(template_path),
        "file_size": len(data),
        "asset_count": build_summary.get("asset_count"),
        "parent_count": build_summary.get("parent_count"),
        "decoration_count": build_summary.get("decoration_count"),
        "standalone_asset_count": build_summary.get("standalone_asset_count", 0),
        "version": build_summary.get("version"),
    }
    _progress(progress_callback, 100, "GIA 导出完成")
    return data, summary, objects_json
