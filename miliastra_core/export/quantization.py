from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum


class QuantizationMode(str, Enum):
    NONE = "none"
    GRID = "grid"
    FLOAT32 = "float32"


@dataclass(frozen=True)
class QuantizationPolicy:
    mode: QuantizationMode = QuantizationMode.NONE
    step: float = 0.01

    def __post_init__(self) -> None:
        mode = QuantizationMode(self.mode)
        object.__setattr__(self, "mode", mode)
        if mode is QuantizationMode.GRID and self.step <= 0:
            raise ValueError("网格量化 step 必须 > 0")

    def apply(self, value: float) -> float:
        value = float(value)
        if self.mode is QuantizationMode.NONE:
            return value
        if self.mode is QuantizationMode.FLOAT32:
            return struct.unpack("<f", struct.pack("<f", value))[0]
        return round(value / self.step) * self.step

    def positive(self, value: float) -> float:
        result = self.apply(value)
        if result <= 0:
            if self.mode is QuantizationMode.GRID:
                return self.step
            raise ValueError(f"量化后尺寸必须 > 0，得到 {result}")
        return result

    def vec3(self, values: tuple[float, float, float] | list[float]) -> list[float]:
        return [self.apply(float(v)) for v in values]

    def positive_vec3(self, values: tuple[float, float, float] | list[float]) -> list[float]:
        return [self.positive(float(v)) for v in values]
