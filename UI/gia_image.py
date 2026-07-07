from __future__ import annotations

from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parents[0]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from UI.image_to_gia import (
    COLLISION_MODE_NATIVE,
    COLLISION_MODE_NATIVE_AND_CLIMB,
    COLLISION_MODE_OFF,
    DEFAULT_TEMPLATE_GIA,
    ImageGiaSettings,
    build_image_gia_bytes,
    grid_layout,
    grid_units_to_meters,
    load_rgba_image,
    meters_to_grid_units,
    resize_for_pixel_budget,
    scale_image_for_parsing_xy,
)

__all__ = [
    'COLLISION_MODE_NATIVE',
    'COLLISION_MODE_NATIVE_AND_CLIMB',
    'COLLISION_MODE_OFF',
    'DEFAULT_TEMPLATE_GIA',
    'ImageGiaSettings',
    'build_image_gia_bytes',
    'grid_layout',
    'grid_units_to_meters',
    'load_rgba_image',
    'meters_to_grid_units',
    'resize_for_pixel_budget',
    'scale_image_for_parsing_xy',
]
