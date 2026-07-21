from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from .models import ColorMergeMode, RasterRect, ScanStrategy
from .prefix_sum import RgbaPrefixSum

RGBA = tuple[int, int, int, int]


def _channels(include_alpha: bool) -> range:
    return range(4 if include_alpha else 3)


class RegionColorState(Protocol):
    def accepts(self, candidates: Iterable[RGBA]) -> bool: ...
    def add(self, candidates: Iterable[RGBA]) -> None: ...


@dataclass
class ExactState:
    seed: RGBA
    include_alpha: bool

    def accepts(self, candidates: Iterable[RGBA]) -> bool:
        channels = _channels(self.include_alpha)
        return all(all(int(pixel[c]) == int(self.seed[c]) for c in channels) for pixel in candidates)

    def add(self, candidates: Iterable[RGBA]) -> None:
        return None


@dataclass
class SeedState:
    seed: RGBA
    tolerance: int
    include_alpha: bool

    def accepts(self, candidates: Iterable[RGBA]) -> bool:
        channels = _channels(self.include_alpha)
        return all(
            max(abs(int(pixel[c]) - int(self.seed[c])) for c in channels) <= self.tolerance
            for pixel in candidates
        )

    def add(self, candidates: Iterable[RGBA]) -> None:
        return None


@dataclass
class DynamicMeanState:
    sums: list[int]
    count: int
    tolerance: int
    include_alpha: bool

    @classmethod
    def from_seed(cls, seed: RGBA, tolerance: int, include_alpha: bool) -> "DynamicMeanState":
        return cls([int(v) for v in seed], 1, tolerance, include_alpha)

    def accepts(self, candidates: Iterable[RGBA]) -> bool:
        values = list(candidates)
        if not values:
            return False
        channels = _channels(self.include_alpha)
        means = [self.sums[c] / self.count for c in range(4)]
        # 整条新边统一与“扩展前区域均值”比较，避免边内遍历顺序改变结果。
        return all(
            max(abs(int(pixel[c]) - means[c]) for c in channels) <= self.tolerance
            for pixel in values
        )

    def add(self, candidates: Iterable[RGBA]) -> None:
        values = list(candidates)
        for pixel in values:
            for c in range(4):
                self.sums[c] += int(pixel[c])
        self.count += len(values)


@dataclass
class RangeState:
    minimums: list[int]
    maximums: list[int]
    tolerance: int
    include_alpha: bool

    @classmethod
    def from_seed(cls, seed: RGBA, tolerance: int, include_alpha: bool) -> "RangeState":
        values = [int(v) for v in seed]
        return cls(values.copy(), values.copy(), tolerance, include_alpha)

    def _prospective(self, candidates: Iterable[RGBA]) -> tuple[list[int], list[int]]:
        minimums = self.minimums.copy()
        maximums = self.maximums.copy()
        for pixel in candidates:
            for c in range(4):
                value = int(pixel[c])
                minimums[c] = min(minimums[c], value)
                maximums[c] = max(maximums[c], value)
        return minimums, maximums

    def accepts(self, candidates: Iterable[RGBA]) -> bool:
        values = list(candidates)
        if not values:
            return False
        minimums, maximums = self._prospective(values)
        return all(maximums[c] - minimums[c] <= self.tolerance for c in _channels(self.include_alpha))

    def add(self, candidates: Iterable[RGBA]) -> None:
        self.minimums, self.maximums = self._prospective(candidates)


def make_color_state(mode: ColorMergeMode, seed: RGBA, tolerance: int, include_alpha: bool) -> RegionColorState:
    mode = ColorMergeMode(mode)
    if mode is ColorMergeMode.EXACT:
        return ExactState(seed, include_alpha)
    if mode is ColorMergeMode.SEED:
        return SeedState(seed, tolerance, include_alpha)
    if mode is ColorMergeMode.DYNAMIC_MEAN:
        return DynamicMeanState.from_seed(seed, tolerance, include_alpha)
    if mode is ColorMergeMode.RANGE:
        return RangeState.from_seed(seed, tolerance, include_alpha)
    raise ValueError(f"未知颜色合并模式: {mode}")


def _is_visible(pixel: RGBA, threshold: int, background: bool) -> bool:
    # 用户定义：阈值范围 1..256，并且采用包含判定。
    return int(pixel[3]) >= threshold and not background


def _edge_pixels_right(pixels: Any, x: int, y: int, width: int, height: int) -> list[RGBA]:
    edge_x = x + width
    return [pixels[edge_x, row] for row in range(y, y + height)]


def _edge_pixels_down(pixels: Any, x: int, y: int, width: int, height: int) -> list[RGBA]:
    edge_y = y + height
    return [pixels[col, edge_y] for col in range(x, x + width)]


def can_extend_right(
    pixels: Any,
    used: list[bytearray],
    background_mask: list[bytearray],
    x: int,
    y: int,
    width: int,
    height: int,
    image_width: int,
    alpha_threshold: int,
    state: RegionColorState,
) -> tuple[bool, list[RGBA]]:
    edge_x = x + width
    if edge_x >= image_width:
        return False, []
    candidates = _edge_pixels_right(pixels, x, y, width, height)
    for offset, pixel in enumerate(candidates):
        row = y + offset
        if used[row][edge_x] or not _is_visible(pixel, alpha_threshold, bool(background_mask[row][edge_x])):
            return False, []
    return state.accepts(candidates), candidates


def can_extend_down(
    pixels: Any,
    used: list[bytearray],
    background_mask: list[bytearray],
    x: int,
    y: int,
    width: int,
    height: int,
    image_height: int,
    alpha_threshold: int,
    state: RegionColorState,
) -> tuple[bool, list[RGBA]]:
    edge_y = y + height
    if edge_y >= image_height:
        return False, []
    candidates = _edge_pixels_down(pixels, x, y, width, height)
    for offset, pixel in enumerate(candidates):
        col = x + offset
        if used[edge_y][col] or not _is_visible(pixel, alpha_threshold, bool(background_mask[edge_y][col])):
            return False, []
    return state.accepts(candidates), candidates


def _find_rectangle(
    pixels: Any,
    used: list[bytearray],
    background_mask: list[bytearray],
    x: int,
    y: int,
    image_width: int,
    image_height: int,
    *,
    alpha_threshold: int,
    mode: ColorMergeMode,
    tolerance: int,
    include_alpha: bool,
    horizontal_first: bool,
) -> tuple[int, int]:
    seed = pixels[x, y]
    state = make_color_state(mode, seed, tolerance, include_alpha)
    width = height = 1

    def grow_right() -> None:
        nonlocal width
        while True:
            accepted, candidates = can_extend_right(
                pixels, used, background_mask, x, y, width, height,
                image_width, alpha_threshold, state,
            )
            if not accepted:
                break
            state.add(candidates)
            width += 1

    def grow_down() -> None:
        nonlocal height
        while True:
            accepted, candidates = can_extend_down(
                pixels, used, background_mask, x, y, width, height,
                image_height, alpha_threshold, state,
            )
            if not accepted:
                break
            state.add(candidates)
            height += 1

    if horizontal_first:
        grow_right()
        grow_down()
    else:
        grow_down()
        grow_right()
    return width, height


def _merge_once(
    pixels: Any,
    width: int,
    height: int,
    background_mask: list[bytearray],
    prefix: RgbaPrefixSum,
    *,
    alpha_threshold: int,
    mode: ColorMergeMode,
    tolerance: int,
    include_alpha: bool,
    horizontal_first: bool,
) -> list[RasterRect]:
    used = [bytearray(width) for _ in range(height)]
    rectangles: list[RasterRect] = []
    for y in range(height):
        for x in range(width):
            if used[y][x]:
                continue
            pixel = pixels[x, y]
            if not _is_visible(pixel, alpha_threshold, bool(background_mask[y][x])):
                used[y][x] = 1
                continue
            rect_width, rect_height = _find_rectangle(
                pixels, used, background_mask, x, y, width, height,
                alpha_threshold=alpha_threshold,
                mode=mode,
                tolerance=tolerance,
                include_alpha=include_alpha,
                horizontal_first=horizontal_first,
            )
            for row in range(y, y + rect_height):
                used[row][x:x + rect_width] = b"\x01" * rect_width
            rectangles.append(
                RasterRect(
                    x=x,
                    y=y,
                    width=rect_width,
                    height=rect_height,
                    rgba=prefix.mean_rgba(x, y, rect_width, rect_height),
                )
            )
    return rectangles


def merge_rectangles(
    pixels: Any,
    width: int,
    height: int,
    background_mask: list[bytearray],
    *,
    alpha_threshold: int,
    mode: ColorMergeMode,
    tolerance: int,
    include_alpha: bool,
    scan_strategy: ScanStrategy,
) -> list[RasterRect]:
    prefix = RgbaPrefixSum(pixels, width, height)
    strategy = ScanStrategy(scan_strategy)
    if strategy is ScanStrategy.HORIZONTAL_FIRST:
        return _merge_once(
            pixels, width, height, background_mask, prefix,
            alpha_threshold=alpha_threshold, mode=mode, tolerance=tolerance,
            include_alpha=include_alpha, horizontal_first=True,
        )
    if strategy is ScanStrategy.VERTICAL_FIRST:
        return _merge_once(
            pixels, width, height, background_mask, prefix,
            alpha_threshold=alpha_threshold, mode=mode, tolerance=tolerance,
            include_alpha=include_alpha, horizontal_first=False,
        )
    horizontal = _merge_once(
        pixels, width, height, background_mask, prefix,
        alpha_threshold=alpha_threshold, mode=mode, tolerance=tolerance,
        include_alpha=include_alpha, horizontal_first=True,
    )
    vertical = _merge_once(
        pixels, width, height, background_mask, prefix,
        alpha_threshold=alpha_threshold, mode=mode, tolerance=tolerance,
        include_alpha=include_alpha, horizontal_first=False,
    )
    # 矩形数优先，其次偏好覆盖面积更大的前部矩形，保证选择确定性。
    def score(items: list[RasterRect]) -> tuple[int, tuple[int, ...]]:
        return len(items), tuple(-item.area for item in items[:32])
    return horizontal if score(horizontal) <= score(vertical) else vertical


def pixels_as_rectangles(
    pixels: Any,
    width: int,
    height: int,
    background_mask: list[bytearray],
    *,
    alpha_threshold: int,
) -> list[RasterRect]:
    result: list[RasterRect] = []
    for y in range(height):
        for x in range(width):
            pixel = pixels[x, y]
            if _is_visible(pixel, alpha_threshold, bool(background_mask[y][x])):
                result.append(RasterRect(x, y, 1, 1, tuple(int(v) for v in pixel)))
    return result
