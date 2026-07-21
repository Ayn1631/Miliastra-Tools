from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from .background import edge_connected_background_mask
from .merge import merge_rectangles, pixels_as_rectangles
from .models import RasterAlgorithmSettings, RasterPlan
from .palette import quantize_rgba_median_cut
from .resize import resize_image


def image_sha256(image: Image.Image) -> str:
    rgba = image.convert("RGBA")
    h = hashlib.sha256()
    h.update(f"{rgba.width}x{rgba.height}:RGBA\0".encode("ascii"))
    h.update(rgba.tobytes())
    return h.hexdigest()


def load_rgba_image(raw: bytes | bytearray | memoryview | str | Path) -> Image.Image:
    if isinstance(raw, (str, Path)):
        return Image.open(raw).convert("RGBA")
    return Image.open(io.BytesIO(bytes(raw))).convert("RGBA")


def build_raster_plan(image: Image.Image, settings: RasterAlgorithmSettings) -> RasterPlan:
    source = image.convert("RGBA")
    source_hash = image_sha256(source)
    sampled = resize_image(
        source,
        max_pixels=settings.max_pixels,
        max_width=settings.max_width_px,
        max_height=settings.max_height_px,
        resample_mode=settings.resample_mode,
    )
    if settings.palette_enabled:
        sampled = quantize_rgba_median_cut(sampled, settings.palette_colors)
    pixels = sampled.load()
    width, height = sampled.size
    background_mask = edge_connected_background_mask(
        pixels,
        width,
        height,
        settings.background_rgb,
        settings.background_tolerance,
    )
    if settings.merge_rectangles:
        rectangles = merge_rectangles(
            pixels,
            width,
            height,
            background_mask,
            alpha_threshold=settings.alpha_threshold,
            mode=settings.merge_color_mode,
            tolerance=settings.color_tolerance,
            include_alpha=settings.include_alpha_in_color_distance,
            scan_strategy=settings.scan_strategy,
        )
    else:
        rectangles = pixels_as_rectangles(
            pixels,
            width,
            height,
            background_mask,
            alpha_threshold=settings.alpha_threshold,
        )
    return RasterPlan(
        source_size_px=source.size,
        sampled_size_px=sampled.size,
        rectangles=tuple(rectangles),
        algorithm_settings=settings,
        source_sha256=source_hash,
        sampled_rgba_sha256=image_sha256(sampled),
    )
