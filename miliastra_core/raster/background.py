from __future__ import annotations

from collections import deque
from typing import Any


def is_background_pixel(pixel: tuple[int, int, int, int], rgb: tuple[int, int, int], tolerance: int) -> bool:
    return all(abs(int(pixel[i]) - int(rgb[i])) <= tolerance for i in range(3))


def edge_connected_background_mask(
    pixels: Any,
    width: int,
    height: int,
    background_rgb: tuple[int, int, int] | None,
    tolerance: int,
) -> list[bytearray]:
    mask = [bytearray(width) for _ in range(height)]
    if background_rgb is None or width <= 0 or height <= 0:
        return mask
    queue: deque[tuple[int, int]] = deque()

    def enqueue(x: int, y: int) -> None:
        if mask[y][x]:
            return
        if not is_background_pixel(pixels[x, y], background_rgb, tolerance):
            return
        mask[y][x] = 1
        queue.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while queue:
        x, y = queue.popleft()
        if x > 0:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y > 0:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)
    return mask
