from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from miliastra_core.protobuf.wire import (
    WireError,
    WireField,
    decode_packed_varints,
    encode_packed_varints,
    first_bytes,
    first_field,
    first_index,
    first_varint,
    fixed32_field,
    len_field,
    parse_fields,
    rebuild_message,
    set_bytes,
    set_fixed32,
    set_varint,
    unpack_fixed32,
    varint_field,
)

from .models import Decoration, DecorationSpec, SceneObject, Transform, Vec3


class GilError(ValueError):
    pass


@dataclass(frozen=True)
class GilHeader:
    left_size: int
    schema: int
    head_tag: int
    file_type: int
    proto_size: int
    tail_tag: int


def _parse(data: bytes, context: str) -> list[WireField]:
    try:
        return parse_fields(data, context=context)
    except WireError as exc:
        raise GilError(str(exc)) from exc


def _component(fields: list[WireField], container: int, component_type: int) -> tuple[int, list[WireField]] | None:
    for index, field in enumerate(fields):
        if field.number != container or field.wire_type != 2:
            continue
        nested = _parse(bytes(field.value), f"component {component_type}")
        if first_varint(nested, 1) == component_type:
            return index, nested
    return None


def _read_string(fields: list[WireField], container: int) -> str:
    found = _component(fields, container, 1)
    if found is None:
        return ""
    block = first_bytes(found[1], 11)
    if block is None:
        return ""
    value = first_bytes(_parse(block, "name block"), 1)
    return "" if value is None else value.decode("utf-8", errors="replace")


def _read_vec3(data: bytes | None) -> Vec3:
    if data is None:
        return Vec3(0.0, 0.0, 0.0)
    fields = _parse(data, "vec3")
    values = []
    for number in (1, 2, 3):
        value = unpack_fixed32(first_field(fields, number, 5))
        values.append(0.0 if value is None else float(value))
    return Vec3(*values)


def _read_transform(fields: list[WireField], container: int) -> Transform:
    found = _component(fields, container, 1)
    if found is None:
        zero = Vec3(0.0, 0.0, 0.0)
        return Transform(zero, zero, Vec3(1.0, 1.0, 1.0))
    block = first_bytes(found[1], 11)
    if block is None:
        zero = Vec3(0.0, 0.0, 0.0)
        return Transform(zero, zero, Vec3(1.0, 1.0, 1.0))
    nested = _parse(block, "transform block")
    return Transform(
        _read_vec3(first_bytes(nested, 1)),
        _read_vec3(first_bytes(nested, 2)),
        _read_vec3(first_bytes(nested, 3)),
    )


def _read_owner(fields: list[WireField]) -> int | None:
    found = _component(fields, 4, 40)
    if found is None:
        return None
    payload = first_bytes(found[1], 50)
    return None if payload is None else first_varint(_parse(payload, "owner payload"), 502)


def _read_refs(fields: list[WireField]) -> tuple[int, ...]:
    found = _component(fields, 5, 40)
    if found is None:
        return ()
    payload = first_bytes(found[1], 50)
    if payload is None:
        return ()
    packed = first_bytes(_parse(payload, "parent refs payload"), 501)
    return () if packed is None else tuple(decode_packed_varints(packed))


def _read_rgba(fields: list[WireField]) -> tuple[int, int, int, int] | None:
    found = _component(fields, 5, 22)
    if found is None:
        return None
    payload = first_bytes(found[1], 32)
    if payload is None:
        return None
    color = _parse(payload, "color payload")
    argb = first_varint(color, 3)
    if argb is None:
        return None
    return ((argb >> 16) & 255, (argb >> 8) & 255, argb & 255, (argb >> 24) & 255)


def _set_vec3(original: bytes | None, value: Vec3) -> bytes:
    fields = [] if original is None else _parse(original, "vec3 patch")
    set_fixed32(fields, 1, value.x)
    set_fixed32(fields, 2, value.y)
    set_fixed32(fields, 3, value.z)
    return rebuild_message(fields)


def patch_name(fields: list[WireField], container: int, name: str) -> None:
    found = _component(fields, container, 1)
    encoded = name.encode("utf-8")
    if found is None:
        fields.append(len_field(container, rebuild_message([
            varint_field(1, 1),
            len_field(11, rebuild_message([len_field(1, encoded)])),
        ])))
        return
    index, component = found
    block = first_bytes(component, 11)
    block_fields = [] if block is None else _parse(block, "name patch")
    set_bytes(block_fields, 1, encoded)
    set_bytes(component, 11, rebuild_message(block_fields))
    fields[index] = fields[index].with_value(rebuild_message(component))


def patch_transform(fields: list[WireField], container: int, transform: Transform) -> None:
    for label, value in (("position", transform.position), ("rotation", transform.rotation), ("scale", transform.scale)):
        if not all(math.isfinite(v) for v in value.as_tuple()):
            raise GilError(f"{label} 包含非有限数")
    if any(v <= 0 for v in transform.scale.as_tuple()):
        raise GilError("scale 三轴必须 > 0")
    found = _component(fields, container, 1)
    if found is None:
        block = rebuild_message([
            len_field(1, _set_vec3(None, transform.position)),
            len_field(2, _set_vec3(None, transform.rotation)),
            len_field(3, _set_vec3(None, transform.scale)),
        ])
        fields.append(len_field(container, rebuild_message([varint_field(1, 1), len_field(11, block)])))
        return
    index, component = found
    block = first_bytes(component, 11)
    nested = [] if block is None else _parse(block, "transform patch")
    set_bytes(nested, 1, _set_vec3(first_bytes(nested, 1), transform.position))
    set_bytes(nested, 2, _set_vec3(first_bytes(nested, 2), transform.rotation))
    set_bytes(nested, 3, _set_vec3(first_bytes(nested, 3), transform.scale))
    set_bytes(component, 11, rebuild_message(nested))
    fields[index] = fields[index].with_value(rebuild_message(component))


def patch_owner(fields: list[WireField], owner_id: int) -> None:
    found = _component(fields, 4, 40)
    if found is None:
        payload = rebuild_message([varint_field(502, owner_id)])
        fields.append(len_field(4, rebuild_message([varint_field(1, 40), len_field(50, payload)])))
        return
    index, component = found
    payload = first_bytes(component, 50)
    nested = [] if payload is None else _parse(payload, "owner patch")
    set_varint(nested, 502, owner_id)
    set_bytes(component, 50, rebuild_message(nested))
    fields[index] = fields[index].with_value(rebuild_message(component))


def patch_color(fields: list[WireField], rgba: tuple[int, int, int, int]) -> None:
    if len(rgba) != 4 or any(not 0 <= int(v) <= 255 for v in rgba):
        raise GilError("RGBA 必须为四个 0..255 整数")
    r, g, b, a = (int(v) for v in rgba)
    argb = (a << 24) | (r << 16) | (g << 8) | b
    rgb = (r << 16) | (g << 8) | b
    found = _component(fields, 5, 22)
    if found is None:
        payload = rebuild_message([
            varint_field(1, 1), varint_field(3, argb), fixed32_field(4, a * 100.0 / 255.0),
            varint_field(5, rgb), varint_field(6, 6700),
        ])
        fields.append(len_field(5, rebuild_message([varint_field(1, 22), len_field(32, payload)])))
        return
    index, component = found
    payload = first_bytes(component, 32)
    color = [] if payload is None else _parse(payload, "color patch")
    set_varint(color, 1, 1)
    set_varint(color, 3, argb)
    set_fixed32(color, 4, a * 100.0 / 255.0)
    set_varint(color, 5, rgb)
    set_bytes(component, 32, rebuild_message(color))
    fields[index] = fields[index].with_value(rebuild_message(component))


def patch_decoration_entry(template: bytes, decoration_id: int, parent_id: int, spec: DecorationSpec) -> bytes:
    fields = _parse(template, "decoration patch")
    set_varint(fields, 1, decoration_id)
    set_varint(fields, 2, spec.asset_id)
    patch_name(fields, 4, spec.name)
    patch_owner(fields, parent_id)
    patch_transform(fields, 5, Transform(spec.position, spec.rotation, spec.scale))
    patch_color(fields, spec.rgba)
    return rebuild_message(fields)


def patch_parent_entry(
    template: bytes,
    *,
    object_id: int,
    name: str,
    transform: Transform,
    decoration_ids: Iterable[int],
) -> bytes:
    fields = _parse(template, "parent patch")
    set_varint(fields, 1, object_id)
    patch_name(fields, 5, name)
    patch_transform(fields, 6, transform)
    found = _component(fields, 5, 40)
    if found is None:
        raise GilError("父节点缺少 type-40 装饰引用组件")
    index, component = found
    payload = first_bytes(component, 50)
    nested = [] if payload is None else _parse(payload, "parent refs patch")
    set_bytes(nested, 501, encode_packed_varints(decoration_ids))
    set_bytes(component, 50, rebuild_message(nested))
    fields[index] = fields[index].with_value(rebuild_message(component))
    return rebuild_message(fields)


def update_top6_mappings(top6: bytes, *, remove_ids: set[int], add_ids: Iterable[int]) -> bytes:
    fields = _parse(top6, "top6 patch")
    for outer_index, field in enumerate(fields):
        if field.number != 1 or field.wire_type != 2:
            continue
        entry = _parse(bytes(field.value), "top6 category")
        if first_varint(entry, 1) != 3:
            continue
        for child_index, child in enumerate(entry):
            if child.number != 3 or child.wire_type != 2:
                continue
            child_fields = _parse(bytes(child.value), "top6 category child")
            retained: list[WireField] = []
            existing: set[int] = set()
            for mapping in child_fields:
                if mapping.number != 5 or mapping.wire_type != 2:
                    retained.append(mapping)
                    continue
                record = _parse(bytes(mapping.value), "top6 mapping")
                target = first_varint(record, 2) if first_varint(record, 1) == 200 else None
                if target is not None and target in remove_ids:
                    continue
                if target is not None:
                    existing.add(target)
                retained.append(mapping)
            for object_id in add_ids:
                if object_id not in existing:
                    retained.append(len_field(5, rebuild_message([varint_field(1, 200), varint_field(2, object_id)])))
                    existing.add(object_id)
            entry[child_index] = child.with_value(rebuild_message(retained))
            fields[outer_index] = field.with_value(rebuild_message(entry))
            return rebuild_message(fields)
    raise GilError("top6 中未找到 category=3 / child field=3")


class GilDocument:
    """20 字节大端头 + lossless Protobuf payload + 4 字节尾。"""

    def __init__(self, header: GilHeader, payload: bytes, *, source_path: Path | None = None) -> None:
        self.header = header
        self.payload = bytes(payload)
        self.source_path = source_path

    @classmethod
    def from_bytes(cls, data: bytes, *, source_path: Path | None = None, strict_sizes: bool = True) -> "GilDocument":
        if len(data) < 24:
            raise GilError("GIL 文件小于 24 字节")
        left_size, schema, head_tag, file_type, proto_size = struct.unpack(">IIIII", data[:20])
        payload = data[20:-4]
        tail_tag = struct.unpack(">I", data[-4:])[0]
        if strict_sizes:
            if proto_size != len(payload):
                raise GilError(f"GIL proto_size={proto_size}，实际 payload={len(payload)}")
            if left_size != len(data) - 4:
                raise GilError(f"GIL left_size={left_size}，实际 file_size-4={len(data)-4}")
        return cls(GilHeader(left_size, schema, head_tag, file_type, proto_size, tail_tag), payload, source_path=source_path)

    @classmethod
    def load(cls, path: str | Path, *, strict_sizes: bool = True) -> "GilDocument":
        path = Path(path)
        return cls.from_bytes(path.read_bytes(), source_path=path, strict_sizes=strict_sizes)

    def build_bytes(self) -> bytes:
        prefix = struct.pack(">IIIII", len(self.payload) + 20, self.header.schema, self.header.head_tag, self.header.file_type, len(self.payload))
        return prefix + self.payload + struct.pack(">I", self.header.tail_tag)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.build_bytes()).hexdigest()

    def top_fields(self) -> list[WireField]:
        return _parse(self.payload, "GIL top-level")

    def top_data(self, number: int) -> bytes | None:
        return first_bytes(self.top_fields(), number)

    def replace_top_data(self, replacements: dict[int, bytes]) -> "GilDocument":
        fields = self.top_fields()
        for number, value in replacements.items():
            index = first_index(fields, number, 2)
            if index is None:
                raise GilError(f"缺少顶层字段 {number}")
            fields[index] = fields[index].with_value(value)
        payload = rebuild_message(fields)
        return GilDocument(
            GilHeader(len(payload) + 20, self.header.schema, self.header.head_tag, self.header.file_type, len(payload), self.header.tail_tag),
            payload,
            source_path=self.source_path,
        )

    def scene_objects(self) -> list[SceneObject]:
        top5 = self.top_data(5)
        if top5 is None:
            return []
        result: list[SceneObject] = []
        for index, field in enumerate(_parse(top5, "top5")):
            if field.number != 1 or field.wire_type != 2:
                continue
            entry = _parse(bytes(field.value), "scene object")
            object_id = first_varint(entry, 1)
            if object_id is None:
                continue
            result.append(SceneObject(index, object_id, first_varint(entry, 8), _read_string(entry, 5), _read_transform(entry, 6), _read_refs(entry)))
        return result

    def decorations(self) -> list[Decoration]:
        top27 = self.top_data(27)
        if top27 is None:
            return []
        result: list[Decoration] = []
        for index, field in enumerate(_parse(top27, "top27")):
            if field.number not in (1, 2) or field.wire_type != 2:
                continue
            entry = _parse(bytes(field.value), "decoration")
            decoration_id = first_varint(entry, 1)
            if decoration_id is None:
                continue
            result.append(Decoration(index, field.number, decoration_id, first_varint(entry, 2), _read_owner(entry), _read_string(entry, 4), _read_transform(entry, 5), _read_rgba(entry)))
        return result

    def choose_parent(self, parent_id: int | None = None) -> SceneObject:
        objects = self.scene_objects()
        if parent_id is not None:
            matches = [item for item in objects if item.object_id == parent_id]
        else:
            matches = [item for item in objects if item.asset_id == 10005018 and item.decoration_ids]
            if not matches:
                matches = [item for item in objects if item.decoration_ids]
        if len(matches) != 1:
            raise GilError(f"无法唯一选择装饰父节点，候选 ID: {[item.object_id for item in matches]}")
        return matches[0]

    def linked_decorations(self, parent: SceneObject) -> list[Decoration]:
        by_id = {item.decoration_id: item for item in self.decorations()}
        result: list[Decoration] = []
        for decoration_id in parent.decoration_ids:
            item = by_id.get(decoration_id)
            if item is None:
                raise GilError(f"父节点引用了不存在的装饰 ID {decoration_id}")
            if item.owner_id != parent.object_id:
                raise GilError(f"装饰 {decoration_id} owner={item.owner_id}，应为 {parent.object_id}")
            if item.store_field != 2:
                raise GilError("当前解析器只接受 top27 field 2 的场景装饰")
            result.append(item)
        return result

    def object_ids_across_spaces(self) -> set[int]:
        result: set[int] = set()
        for top_number in (4, 5, 8):
            data = self.top_data(top_number)
            if data is None:
                continue
            for field in _parse(data, f"top{top_number} id scan"):
                if field.number == 1 and field.wire_type == 2:
                    value = first_varint(_parse(bytes(field.value), "object id"), 1)
                    if value is not None:
                        result.add(value)
        return result

    def mapping_targets(self) -> set[int]:
        top6 = self.top_data(6)
        if top6 is None:
            return set()
        targets: set[int] = set()
        for field in _parse(top6, "top6 scan"):
            if field.number != 1 or field.wire_type != 2:
                continue
            entry = _parse(bytes(field.value), "category")
            if first_varint(entry, 1) != 3:
                continue
            for child in entry:
                if child.number != 3 or child.wire_type != 2:
                    continue
                for mapping in _parse(bytes(child.value), "category child"):
                    if mapping.number == 5 and mapping.wire_type == 2:
                        record = _parse(bytes(mapping.value), "mapping")
                        if first_varint(record, 1) == 200:
                            target = first_varint(record, 2)
                            if target is not None:
                                targets.add(target)
        return targets


    def inspect_summary(self) -> dict:
        """返回适合 JSON 输出的 GIL 结构摘要，不修改文件。"""
        top = self.top_fields()
        return {
            "header": {
                "left_size": self.header.left_size,
                "schema": self.header.schema,
                "head_tag": self.header.head_tag,
                "file_type": self.header.file_type,
                "proto_size": self.header.proto_size,
                "tail_tag": self.header.tail_tag,
            },
            "sha256": self.sha256,
            "top_fields": [
                {
                    "index": index,
                    "number": field.number,
                    "wire_type": field.wire_type,
                    "encoded_size": len(field.raw),
                    "value_size": len(field.value) if isinstance(field.value, bytes) else None,
                }
                for index, field in enumerate(top)
            ],
            "scene_objects": [
                {
                    "object_id": item.object_id,
                    "asset_id": item.asset_id,
                    "name": item.name,
                    "position": list(item.transform.position.as_tuple()),
                    "rotation": list(item.transform.rotation.as_tuple()),
                    "scale": list(item.transform.scale.as_tuple()),
                    "decoration_ids": list(item.decoration_ids),
                }
                for item in self.scene_objects()
            ],
            "decorations": [
                {
                    "store_field": item.store_field,
                    "decoration_id": item.decoration_id,
                    "asset_id": item.asset_id,
                    "owner_id": item.owner_id,
                    "name": item.name,
                    "position": list(item.transform.position.as_tuple()),
                    "rotation": list(item.transform.rotation.as_tuple()),
                    "scale": list(item.transform.scale.as_tuple()),
                    "rgba": list(item.rgba) if item.rgba is not None else None,
                }
                for item in self.decorations()
            ],
            "mapping_targets": sorted(self.mapping_targets()),
        }

    def validate_roundtrip(self) -> None:
        rebuilt = GilDocument.from_bytes(self.build_bytes())
        if rebuilt.payload != self.payload:
            raise GilError("GIL build/read roundtrip payload 不一致")
