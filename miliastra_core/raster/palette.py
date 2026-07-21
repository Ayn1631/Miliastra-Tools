from __future__ import annotations

from PIL import Image


def quantize_rgba_median_cut(image: Image.Image, colors: int) -> Image.Image:
    """对 RGB 做 Median Cut，无抖动，并原样挂回 alpha 通道。"""

    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    rgb = rgba.convert("RGB")
    quantized = rgb.quantize(
        colors=max(2, min(256, int(colors))),
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    ).convert("RGB")
    result = quantized.convert("RGBA")
    result.putalpha(alpha)
    return result
