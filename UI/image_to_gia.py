from __future__ import annotations

import io
import json
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR
ROOT_DIR = SCRIPTS_DIR.parent
DEFAULT_TEMPLATE_GIA = ROOT_DIR / "UI" / "白模解析.gia"
GRID_STEP_M = 0.01

COLLISION_MODE_NATIVE = "native"
COLLISION_MODE_NATIVE_AND_CLIMB = "native_and_climb"
COLLISION_MODE_OFF = "off"
_COLLISION_MODE_FLAGS = {
    COLLISION_MODE_NATIVE: (True, False),
    COLLISION_MODE_NATIVE_AND_CLIMB: (True, True),
    COLLISION_MODE_OFF: (False, False),
}

GIA_OBJECT_PLACEMENT_DIR = SCRIPTS_DIR / "gia_object_placement"
if str(GIA_OBJECT_PLACEMENT_DIR) not in sys.path:
    sys.path.insert(0, str(GIA_OBJECT_PLACEMENT_DIR))

from UI.build_gia_objects import DEFAULT_ENTITY_ID_START, build_gia  # noqa: E402


@dataclass(frozen=True)
class ImageGiaSettings:
    target_width_m: float
    target_height_m: float
    max_pixels: int
    alpha_threshold: int
    template_id: int
    block_height_m: float | None = None
    entity_id_start: int = DEFAULT_ENTITY_ID_START + 100_000
    merge_rectangles: bool = False
    color_tolerance: int = 0
    background_rgb: tuple[int, int, int] | None = None
    background_tolerance: int = 0
    collision_mode: str = COLLISION_MODE_OFF


def collision_mode_flags(mode: str) -> tuple[bool, bool]:
    normalized = str(mode).strip().lower()
    try:
        return _COLLISION_MODE_FLAGS[normalized]
    except KeyError as exc:
        valid = ", ".join(sorted(_COLLISION_MODE_FLAGS))
        raise ValueError(f"unsupported collision_mode: {mode!r}; expected one of: {valid}") from exc


def clamp_scale(value: float) -> float:
    return max(0.01, min(50.0, float(value)))


def meters_to_grid_units(value: float) -> int:
    return max(1, int(round(float(value) / GRID_STEP_M)))


def grid_units_to_meters(value: int | float) -> float:
    return round(float(value) * GRID_STEP_M, 2)


def quantize_scale(value: float) -> float:
    return grid_units_to_meters(meters_to_grid_units(clamp_scale(value)))


def load_rgba_image(raw: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(raw))
    return image.convert("RGBA")


def scale_image_for_parsing(image: Image.Image, scale_percent: int) -> Image.Image:
    return scale_image_for_parsing_xy(image, scale_percent, scale_percent)


def scale_image_for_parsing_xy(
    image: Image.Image,
    scale_x_percent: int,
    scale_y_percent: int,
) -> Image.Image:
    scale_x = max(1, int(scale_x_percent)) / 100.0
    scale_y = max(1, int(scale_y_percent)) / 100.0
    width, height = image.size
    scaled_width = max(1, int(round(width * scale_x)))
    scaled_height = max(1, int(round(height * scale_y)))
    if (scaled_width, scaled_height) == image.size:
        return image
    return image.resize((scaled_width, scaled_height), Image.Resampling.NEAREST)


def resize_for_pixel_budget(
    image: Image.Image,
    max_pixels: int,
    target_width_units: int,
    target_height_units: int,
) -> Image.Image:
    max_pixels = max(1, int(max_pixels))
    width, height = image.size
    if width * height <= max_pixels and width <= target_width_units and height <= target_height_units:
        return image

    ratio = min(
        (max_pixels / (width * height)) ** 0.5,
        target_width_units / width,
        target_height_units / height,
    )
    new_width = max(1, round(width * ratio))
    new_height = max(1, round(height * ratio))
    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


def grid_layout(
    target_width_m: float,
    target_height_m: float,
    width_px: int,
    height_px: int,
) -> dict[str, Any]:
    target_width_units = meters_to_grid_units(clamp_scale(target_width_m))
    target_height_units = meters_to_grid_units(clamp_scale(target_height_m))
    cell_width_units = max(1, int(round(target_width_units / max(width_px, 1))))
    cell_height_units = max(1, int(round(target_height_units / max(height_px, 1))))
    actual_width_units = cell_width_units * width_px
    actual_height_units = cell_height_units * height_px

    first_x_units = int(round((-actual_width_units + cell_width_units) / 2.0))
    first_z_units = int(round((actual_height_units - cell_height_units) / 2.0))
    center_offset_x_units = first_x_units + (width_px - 1) * cell_width_units / 2.0
    center_offset_z_units = first_z_units - (height_px - 1) * cell_height_units / 2.0

    return {
        "target_width_units": target_width_units,
        "target_height_units": target_height_units,
        "cell_width_units": cell_width_units,
        "cell_height_units": cell_height_units,
        "actual_width_units": actual_width_units,
        "actual_height_units": actual_height_units,
        "first_x_units": first_x_units,
        "first_z_units": first_z_units,
        "center_offset_m": {
            "x": round(center_offset_x_units * GRID_STEP_M, 4),
            "z": round(center_offset_z_units * GRID_STEP_M, 4),
        },
    }


def color_distance_ok(
    pixel: tuple[int, int, int, int],
    seed: tuple[int, int, int, int],
    tolerance: int,
    alpha_threshold: int,
) -> bool:
    if pixel[3] < alpha_threshold:
        return False
    tolerance = max(0, int(tolerance))
    return (
        abs(int(pixel[0]) - int(seed[0])) <= tolerance
        and abs(int(pixel[1]) - int(seed[1])) <= tolerance
        and abs(int(pixel[2]) - int(seed[2])) <= tolerance
        and abs(int(pixel[3]) - int(seed[3])) <= tolerance
    )


def is_background_pixel(
    pixel: tuple[int, int, int, int],
    background_rgb: tuple[int, int, int] | None,
    background_tolerance: int,
) -> bool:
    if background_rgb is None:
        return False
    tolerance = max(0, int(background_tolerance))
    return (
        abs(int(pixel[0]) - int(background_rgb[0])) <= tolerance
        and abs(int(pixel[1]) - int(background_rgb[1])) <= tolerance
        and abs(int(pixel[2]) - int(background_rgb[2])) <= tolerance
    )


def is_visible_pixel(
    pixel: tuple[int, int, int, int],
    alpha_threshold: int,
    is_background: bool,
) -> bool:
    return pixel[3] >= alpha_threshold and not is_background


def build_edge_connected_background_mask(
    pixels: Any,
    width_px: int,
    height_px: int,
    background_rgb: tuple[int, int, int] | None,
    background_tolerance: int,
) -> list[list[bool]]:
    mask = [[False for _ in range(width_px)] for _ in range(height_px)]
    if background_rgb is None or width_px <= 0 or height_px <= 0:
        return mask

    queue: deque[tuple[int, int]] = deque()

    def enqueue_if_background(col: int, row: int) -> None:
        if mask[row][col]:
            return
        if not is_background_pixel(pixels[col, row], background_rgb, background_tolerance):
            return
        mask[row][col] = True
        queue.append((col, row))

    for col in range(width_px):
        enqueue_if_background(col, 0)
        enqueue_if_background(col, height_px - 1)
    for row in range(height_px):
        enqueue_if_background(0, row)
        enqueue_if_background(width_px - 1, row)

    while queue:
        col, row = queue.popleft()
        for next_col, next_row in (
            (col - 1, row),
            (col + 1, row),
            (col, row - 1),
            (col, row + 1),
        ):
            if 0 <= next_col < width_px and 0 <= next_row < height_px:
                enqueue_if_background(next_col, next_row)

    return mask


def average_color(
    pixels: Any,
    x0: int,
    y0: int,
    width: int,
    height: int,
) -> tuple[list[int], float]:
    total_r = total_g = total_b = total_a = count = 0
    for row in range(y0, y0 + height):
        for col in range(x0, x0 + width):
            r, g, b, a = pixels[col, row]
            total_r += int(r)
            total_g += int(g)
            total_b += int(b)
            total_a += int(a)
            count += 1
    if count == 0:
        return [0, 0, 0], 0.0
    return (
        [
            round(total_r / count),
            round(total_g / count),
            round(total_b / count),
        ],
        total_a / count / 255.0 * 100.0,
    )


def rectangle_fits(
    pixels: Any,
    used: list[list[bool]],
    x0: int,
    y0: int,
    width: int,
    height: int,
    seed: tuple[int, int, int, int],
    tolerance: int,
    alpha_threshold: int,
    background_mask: list[list[bool]],
) -> bool:
    for row in range(y0, y0 + height):
        for col in range(x0, x0 + width):
            if used[row][col]:
                return False
            pixel = pixels[col, row]
            if not is_visible_pixel(pixel, alpha_threshold, background_mask[row][col]):
                return False
            if not color_distance_ok(pixel, seed, tolerance, alpha_threshold):
                return False
    return True


def find_merge_rectangle(
    pixels: Any,
    used: list[list[bool]],
    x0: int,
    y0: int,
    width_px: int,
    height_px: int,
    tolerance: int,
    alpha_threshold: int,
    background_mask: list[list[bool]],
) -> tuple[int, int]:
    seed = pixels[x0, y0]
    rect_width = 1
    while (
        x0 + rect_width < width_px
        and rectangle_fits(
            pixels,
            used,
            x0,
            y0,
            rect_width + 1,
            1,
            seed,
            tolerance,
            alpha_threshold,
            background_mask,
        )
    ):
        rect_width += 1

    rect_height = 1
    while (
        y0 + rect_height < height_px
        and rectangle_fits(
            pixels,
            used,
            x0,
            y0,
            rect_width,
            rect_height + 1,
            seed,
            tolerance,
            alpha_threshold,
            background_mask,
        )
    ):
        rect_height += 1
    return rect_width, rect_height


def image_to_objects(image: Image.Image, settings: ImageGiaSettings) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested_width_m = clamp_scale(settings.target_width_m)
    requested_height_m = clamp_scale(settings.target_height_m)
    target_width_units = meters_to_grid_units(requested_width_m)
    target_height_units = meters_to_grid_units(requested_height_m)
    sampled = resize_for_pixel_budget(
        image,
        settings.max_pixels,
        target_width_units,
        target_height_units,
    )
    width_px, height_px = sampled.size
    layout = grid_layout(requested_width_m, requested_height_m, width_px, height_px)
    cell_width_m = grid_units_to_meters(layout["cell_width_units"])
    cell_height_m = grid_units_to_meters(layout["cell_height_units"])
    actual_width_m = grid_units_to_meters(layout["actual_width_units"])
    actual_height_m = grid_units_to_meters(layout["actual_height_units"])
    if settings.block_height_m is None:
        block_height_m = min(cell_width_m, cell_height_m)
        block_height_mode = "auto_min_cell_size"
    else:
        block_height_m = quantize_scale(settings.block_height_m)
        block_height_mode = "manual"

    pixels = sampled.load()
    objects: list[dict[str, Any]] = []
    next_entity_id = int(settings.entity_id_start)
    merge_rectangles = bool(settings.merge_rectangles)
    color_tolerance = max(0, min(255, int(settings.color_tolerance)))
    background_rgb = settings.background_rgb
    background_tolerance = max(0, min(255, int(settings.background_tolerance)))
    enable_collision, enable_climb = collision_mode_flags(settings.collision_mode)
    background_mask = build_edge_connected_background_mask(
        pixels,
        width_px,
        height_px,
        background_rgb,
        background_tolerance,
    )

    if merge_rectangles:
        used = [[False for _ in range(width_px)] for _ in range(height_px)]
        for row in range(height_px):
            for col in range(width_px):
                if used[row][col]:
                    continue
                if not is_visible_pixel(pixels[col, row], int(settings.alpha_threshold), background_mask[row][col]):
                    used[row][col] = True
                    continue

                rect_width_px, rect_height_px = find_merge_rectangle(
                    pixels,
                    used,
                    col,
                    row,
                    width_px,
                    height_px,
                    color_tolerance,
                    int(settings.alpha_threshold),
                    background_mask,
                )
                for used_row in range(row, row + rect_height_px):
                    for used_col in range(col, col + rect_width_px):
                        used[used_row][used_col] = True

                rgb, opacity = average_color(pixels, col, row, rect_width_px, rect_height_px)
                x_units = (
                    layout["first_x_units"]
                    + col * layout["cell_width_units"]
                    + (rect_width_px - 1) * layout["cell_width_units"] / 2.0
                )
                z_units = (
                    layout["first_z_units"]
                    - row * layout["cell_height_units"]
                    - (rect_height_px - 1) * layout["cell_height_units"] / 2.0
                )
                rect_width_m = grid_units_to_meters(layout["cell_width_units"] * rect_width_px)
                rect_height_m = grid_units_to_meters(layout["cell_height_units"] * rect_height_px)
                objects.append(
                    {
                        "entity_id": next_entity_id,
                        "name": f"ImageRect_{row:04d}_{col:04d}_{rect_width_px}x{rect_height_px}",
                        "template_id": settings.template_id,
                        "position": [grid_units_to_meters(x_units), 0.0, grid_units_to_meters(z_units)],
                        "rotation": [0.0, 0.0, 0.0],
                        "scale": [rect_width_m, block_height_m, rect_height_m],
                        "color": {
                            "rgb": rgb,
                            "opacity": opacity,
                        },
                        "collision": enable_collision,
                        "climb": enable_climb,
                    }
                )
                next_entity_id += 1
    else:
        for row in range(height_px):
            for col in range(width_px):
                r, g, b, alpha = pixels[col, row]
                if not is_visible_pixel(pixels[col, row], int(settings.alpha_threshold), background_mask[row][col]):
                    continue

                x = grid_units_to_meters(layout["first_x_units"] + col * layout["cell_width_units"])
                z = grid_units_to_meters(layout["first_z_units"] - row * layout["cell_height_units"])
                opacity = alpha / 255.0 * 100.0
                objects.append(
                    {
                        "entity_id": next_entity_id,
                        "name": f"ImagePixel_{row:04d}_{col:04d}",
                        "template_id": settings.template_id,
                        "position": [x, 0.0, z],
                        "rotation": [0.0, 0.0, 0.0],
                        "scale": [cell_width_m, block_height_m, cell_height_m],
                        "color": {
                            "rgb": [int(r), int(g), int(b)],
                            "opacity": opacity,
                        },
                        "collision": enable_collision,
                        "climb": enable_climb,
                    }
                )
                next_entity_id += 1

    summary = {
        "source_size_px": list(image.size),
        "sampled_size_px": [width_px, height_px],
        "requested_size_m": {
            "width": requested_width_m,
            "height": requested_height_m,
        },
        "actual_size_m": {
            "width": actual_width_m,
            "height": actual_height_m,
        },
        "cell_size_m": {
            "width": cell_width_m,
            "height": cell_height_m,
        },
        "grid_step_m": GRID_STEP_M,
        "center_offset_m": layout["center_offset_m"],
        "block_height_m": block_height_m,
        "block_height_mode": block_height_mode,
        "alpha_threshold": int(settings.alpha_threshold),
        "template_id": int(settings.template_id),
        "entity_id_start": int(settings.entity_id_start),
        "object_count": len(objects),
        "merge_rectangles": merge_rectangles,
        "color_tolerance": color_tolerance,
        "background_rgb": list(background_rgb) if background_rgb is not None else None,
        "background_tolerance": background_tolerance,
        "collision_mode": settings.collision_mode,
        "enable_native_collision": enable_collision,
        "enable_climb": enable_climb,
        "coordinate_rule": "position is the center of each pixel block bounding box on a 0.01 meter grid",
        "scale_rule": "scale x/y/z equals grid-aligned bounding-box size in meters; adjacent blocks share edges without gaps",
        "size_priority": "seamless grid first; actual width/height may differ from requested size",
    }
    return objects, summary


def build_image_gia_bytes(
    *,
    image: Image.Image,
    settings: ImageGiaSettings,
    template_path: Path = DEFAULT_TEMPLATE_GIA,
) -> tuple[bytes, dict[str, Any], str]:
    if not template_path.exists():
        raise FileNotFoundError(f"template GIA not found: {template_path}")

    objects, image_summary = image_to_objects(image, settings)
    if not objects:
        raise ValueError("no visible pixels found; lower the alpha threshold or use another image")

    with tempfile.TemporaryDirectory(prefix="qx_image_gia_") as tmp:
        tmp_dir = Path(tmp)
        objects_path = tmp_dir / "image_objects.json"
        output_path = tmp_dir / "image_pixels.gia"
        summary_path = tmp_dir / "image_pixels.summary.json"
        objects_path.write_text(json.dumps(objects, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        build_summary = build_gia(
            template_path=template_path,
            objects_path=objects_path,
            output_path=output_path,
            summary_path=summary_path,
            entity_id_start=settings.entity_id_start,
        )
        data = output_path.read_bytes()

    summary = {
        "image": image_summary,
        "gia": {
            "template": str(template_path),
            "file_size": len(data),
            "asset_count": build_summary.get("asset_count"),
            "version": build_summary.get("version"),
        },
    }
    objects_json = json.dumps(objects, ensure_ascii=False, indent=2) + "\n"
    return data, summary, objects_json
