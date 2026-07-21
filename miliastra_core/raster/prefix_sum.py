from __future__ import annotations

from array import array
from typing import Any


class RgbaPrefixSum:
    """RGBA 二维积分图，任意矩形均值 O(1)。"""

    __slots__ = ("width", "height", "stride", "channels")

    def __init__(self, pixels: Any, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.stride = width + 1
        size = (width + 1) * (height + 1)
        self.channels = [array("Q", [0]) * size for _ in range(4)]
        for y in range(height):
            row_acc = [0, 0, 0, 0]
            upper = y * self.stride
            current = (y + 1) * self.stride
            for x in range(width):
                rgba = pixels[x, y]
                index = current + x + 1
                for c in range(4):
                    row_acc[c] += int(rgba[c])
                    self.channels[c][index] = self.channels[c][upper + x + 1] + row_acc[c]

    def sum_channel(self, channel: int, x: int, y: int, width: int, height: int) -> int:
        x2, y2 = x + width, y + height
        p = self.channels[channel]
        s = self.stride
        return int(p[y2 * s + x2] - p[y * s + x2] - p[y2 * s + x] + p[y * s + x])

    def mean_rgba(self, x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
        count = width * height
        if count <= 0:
            return 0, 0, 0, 0
        return tuple(int(round(self.sum_channel(c, x, y, width, height) / count)) for c in range(4))
