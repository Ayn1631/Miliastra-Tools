from __future__ import annotations

from PIL import Image

from .models import ResampleMode


_RESAMPLING = {
    ResampleMode.NEAREST: Image.Resampling.NEAREST,
    ResampleMode.BOX: Image.Resampling.BOX,
    ResampleMode.BILINEAR: Image.Resampling.BILINEAR,
    ResampleMode.BICUBIC: Image.Resampling.BICUBIC,
    ResampleMode.LANCZOS: Image.Resampling.LANCZOS,
}


def constrained_size(
    width: int,
    height: int,
    *,
    max_pixels: int,
    max_width: int | None = None,
    max_height: int | None = None,
) -> tuple[int, int]:
    """在不放大的前提下，用整数二分搜索尽量吃满像素预算。

    长边作为单调搜索变量，短边按原始宽高比四舍五入。相比直接计算浮点
    sqrt 比例后一次取整，这个实现会返回满足所有约束的最大整数尺寸。
    """

    width = int(width)
    height = int(height)
    max_pixels = max(1, int(max_pixels))
    if width < 1 or height < 1:
        raise ValueError("图像尺寸必须为正整数")
    max_width = width if max_width is None else min(width, max(1, int(max_width)))
    max_height = height if max_height is None else min(height, max(1, int(max_height)))

    def valid(w: int, h: int) -> bool:
        return w >= 1 and h >= 1 and w <= max_width and h <= max_height and w * h <= max_pixels

    if valid(width, height):
        return width, height

    horizontal = width >= height
    original_long = width if horizontal else height
    original_short = height if horizontal else width
    long_cap = max_width if horizontal else max_height

    def dims(long_side: int) -> tuple[int, int]:
        short_side = max(1, int(round(original_short * long_side / original_long)))
        return (long_side, short_side) if horizontal else (short_side, long_side)

    lo, hi = 1, max(1, long_cap)
    best = (1, 1)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = dims(mid)
        if valid(*candidate):
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1

    # 四舍五入台阶偶尔会让相邻几个值有相同短边。局部扫描确保面积最大。
    start = max(1, (best[0] if horizontal else best[1]) - 3)
    end = min(long_cap, (best[0] if horizontal else best[1]) + 4)
    candidates = [dims(v) for v in range(start, end + 1)]
    candidates = [item for item in candidates if valid(*item)]
    return max(candidates, key=lambda item: (item[0] * item[1], item[0], item[1]), default=best)


def resize_image(
    image: Image.Image,
    *,
    max_pixels: int,
    max_width: int | None = None,
    max_height: int | None = None,
    resample_mode: ResampleMode = ResampleMode.LANCZOS,
) -> Image.Image:
    target = constrained_size(
        *image.size,
        max_pixels=max_pixels,
        max_width=max_width,
        max_height=max_height,
    )
    if target == image.size:
        return image.copy()
    return image.resize(target, _RESAMPLING[ResampleMode(resample_mode)])
