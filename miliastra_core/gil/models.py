from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Vec3:
    x: float
    y: float
    z: float

    def as_tuple(self) -> tuple[float, float, float]:
        return self.x, self.y, self.z

    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)


@dataclass(frozen=True)
class Transform:
    position: Vec3
    rotation: Vec3
    scale: Vec3


@dataclass(frozen=True)
class SceneObject:
    index: int
    object_id: int
    asset_id: int | None
    name: str
    transform: Transform
    decoration_ids: tuple[int, ...]


@dataclass(frozen=True)
class Decoration:
    index: int
    store_field: int
    decoration_id: int
    asset_id: int | None
    owner_id: int | None
    name: str
    transform: Transform
    rgba: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class DecorationSpec:
    name: str
    asset_id: int
    position: Vec3
    scale: Vec3
    rgba: tuple[int, int, int, int]
    rotation: Vec3 = Vec3(0.0, 0.0, 0.0)
