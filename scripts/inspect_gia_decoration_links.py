from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miliastra_core.protobuf.wire import (  # noqa: E402
    decode_packed_varints,
    first_bytes,
    first_varint,
    parse_fields,
)


def _component(fields, container: int, component_type: int):
    for field in fields:
        if field.number != container or field.wire_type != 2:
            continue
        nested = parse_fields(bytes(field.value), context=f"component {container}/{component_type}")
        if first_varint(nested, 1) == component_type:
            return nested
    return None


def _id(value: int | None) -> dict[str, int | str] | None:
    return None if value is None else {"decimal": value, "hex": f"0x{value:08X}"}


def _asset_meta(asset) -> dict[str, object] | None:
    blob = first_bytes(asset, 1)
    if blob is None:
        return None
    meta = parse_fields(blob, context="asset meta")
    return {
        "field_2": first_varint(meta, 2),
        "meta_type": first_varint(meta, 3),
        "asset_id": _id(first_varint(meta, 4)),
        "wire_signature": [[field.number, field.wire_type] for field in meta],
    }


def _parent_summary(blob: bytes) -> dict[str, object]:
    asset = parse_fields(blob, context="parent asset")
    dependencies = []
    for field in asset:
        if field.number == 2 and field.wire_type == 2:
            meta = parse_fields(bytes(field.value), context="dependency meta")
            dependencies.append(
                {
                    "field_2": first_varint(meta, 2),
                    "meta_type": first_varint(meta, 3),
                    "asset_id": _id(first_varint(meta, 4)),
                    "wire_signature": [[item.number, item.wire_type] for item in meta],
                }
            )
    entity = parse_fields(first_bytes(asset, 12), context="parent entity")
    core = parse_fields(first_bytes(entity, 1), context="parent core")
    refs_component = _component(core, 5, 40)
    refs: list[int] = []
    if refs_component is not None:
        payload = first_bytes(refs_component, 50)
        if payload is not None:
            packed = first_bytes(parse_fields(payload, context="parent refs"), 501)
            if packed is not None:
                refs = decode_packed_varints(packed)
    name_component = _component(core, 5, 1)
    name_payload_signature = []
    if name_component is not None:
        name_payload = first_bytes(name_component, 11)
        if name_payload is not None:
            name_payload_signature = [
                [field.number, field.wire_type, field.value if field.wire_type == 0 else None]
                for field in parse_fields(name_payload, context="parent name")
            ]
    return {
        "asset_meta": _asset_meta(asset),
        "asset_type": first_varint(asset, 5),
        "asset_wire_signature": [[field.number, field.wire_type] for field in asset],
        "dependency_count": len(dependencies),
        "dependencies_head": dependencies[:3],
        "dependencies_tail": dependencies[-3:],
        "entity_wrapper_signature": [[field.number, field.wire_type] for field in entity],
        "entity_template_id": first_varint(entity, 4),
        "entity_id": _id(first_varint(core, 1)),
        "core_template_id_ref": first_varint(core, 8),
        "name_payload_signature": name_payload_signature,
        "ref_count": len(refs),
        "refs_head": [_id(value) for value in refs[:3]],
        "refs_tail": [_id(value) for value in refs[-3:]],
        "refs_match_dependencies": refs == [
            first_varint(parse_fields(bytes(field.value)), 4)
            for field in asset
            if field.number == 2 and field.wire_type == 2
        ],
    }


def _resource_summary(blob: bytes) -> dict[str, object]:
    asset = parse_fields(blob, context="resource asset")
    wrapper = parse_fields(first_bytes(asset, 21), context="decoration wrapper")
    decoration = parse_fields(first_bytes(wrapper, 1), context="decoration")
    owner_component = _component(decoration, 4, 40)
    owner = None
    if owner_component is not None:
        payload = first_bytes(owner_component, 50)
        if payload is not None:
            owner = first_varint(parse_fields(payload, context="owner"), 502)
    return {
        "asset_meta": _asset_meta(asset),
        "asset_type": first_varint(asset, 5),
        "decoration_id": _id(first_varint(decoration, 1)),
        "model_asset_id": first_varint(decoration, 2),
        "owner_id": _id(owner),
        "decoration_wire_signature": [[field.number, field.wire_type] for field in decoration],
    }


def inspect(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    payload_size = int.from_bytes(data[16:20], "big")
    root = parse_fields(data[20 : 20 + payload_size], context="GIA root")
    parents = [bytes(field.value) for field in root if field.number == 1 and field.wire_type == 2]
    resources = [bytes(field.value) for field in root if field.number == 2 and field.wire_type == 2]
    return {
        "file": str(path),
        "parent_count": len(parents),
        "resource_count": len(resources),
        "parents_head": [_parent_summary(blob) for blob in parents[:2]],
        "parents_tail": [_parent_summary(blob) for blob in parents[-2:]],
        "resources_head": [_resource_summary(blob) for blob in resources[:2]],
        "resources_tail": [_resource_summary(blob) for blob in resources[-2:]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 GIA 空模型与装饰资源的关联字段。")
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([inspect(path) for path in args.inputs], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
