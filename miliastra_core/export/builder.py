from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .decoration import (
    DEFAULT_WRAPPER_TEMPLATE_ID,
    MAX_DECORATIONS_PER_PARENT,
    build_decorated_gia,
)


HEADER_SIZE = 20
FOOTER_SIZE = 4
DEFAULT_ENTITY_ID_START = 1_078_400_000
OUT_OF_RANGE_DISPLAY_DEFAULT = 0
OUT_OF_RANGE_DISPLAY_PERMANENT = 1
OUT_OF_RANGE_DISPLAY_PERMANENT_HIGHEST_PRECISION = 2
OUT_OF_RANGE_DISPLAY_MODES = {
    OUT_OF_RANGE_DISPLAY_DEFAULT,
    OUT_OF_RANGE_DISPLAY_PERMANENT,
    OUT_OF_RANGE_DISPLAY_PERMANENT_HIGHEST_PRECISION,
}

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
DEFAULT_DECORATION_TEMPLATE_GIA = ROOT_DIR / "Template" /"装饰物测试.gia"


TYPE_NAME_TO_TEMPLATE_ID = {
    "长方体": 10009001,
    "球体": 10009002,
    "平面": 10009003,
    "三棱柱": 10009004,
    "五棱柱": 10009005,
    "三棱锥": 10009006,
    "五棱锥": 10009007,
    "圆柱": 10009008,
    "圆锥": 10009009,
    "线框长方体": 10009010,
    "线框圆柱": 10009011,
}

TEMPLATE_ID_TO_TYPE_NAME = {value: key for key, value in TYPE_NAME_TO_TEMPLATE_ID.items()}


def _add_message(file_proto, name: str):
    message = file_proto.message_type.add()
    message.name = name
    return message


def _add_field(
    message,
    name: str,
    number: int,
    field_type: int,
    *,
    type_name: str | None = None,
    repeated: bool = False,
) -> None:
    from google.protobuf import descriptor_pb2

    field = message.field.add()
    field.name = name
    field.number = number
    field.type = field_type
    field.label = (
        descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
        if repeated
        else descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    )
    if type_name is not None:
        field.type_name = type_name


def _build_embedded_gia_pb2():
    """Build a minimal in-process protobuf schema used by this generator.

    The schema is intentionally partial. Fields that this project does not touch are
    preserved by protobuf as unknown fields when a template GIA is parsed and written
    back. This removes the runtime dependency on external .proto files and protoc.
    """
    from types import SimpleNamespace

    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "gia_embedded.proto"
    file_proto.package = "game.gia.embedded"
    file_proto.syntax = "proto3"

    TYPE = descriptor_pb2.FieldDescriptorProto

    vec3 = _add_message(file_proto, "Vec3")
    _add_field(vec3, "x", 1, TYPE.TYPE_FLOAT)
    _add_field(vec3, "y", 2, TYPE.TYPE_FLOAT)
    _add_field(vec3, "z", 3, TYPE.TYPE_FLOAT)

    asset_meta = _add_message(file_proto, "AssetMeta")
    _add_field(asset_meta, "asset_id", 4, TYPE.TYPE_UINT64)

    template_ref = _add_message(file_proto, "TemplateRef")
    _add_field(template_ref, "template_id", 1, TYPE.TYPE_UINT64)
    _add_field(template_ref, "field_2", 2, TYPE.TYPE_UINT32)

    name_property = _add_message(file_proto, "NameProperty")
    _add_field(name_property, "name", 1, TYPE.TYPE_STRING)
    # The project writes static_block=1. Field 2 is the observed companion slot for
    # this property family in the reverse-engineered entity schema.
    _add_field(name_property, "static_block", 2, TYPE.TYPE_UINT32)

    prop = _add_message(file_proto, "Property")
    _add_field(prop, "property_type", 1, TYPE.TYPE_UINT32)
    _add_field(
        prop,
        "name",
        11,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.NameProperty",
    )

    transform = _add_message(file_proto, "Transform")
    _add_field(
        transform,
        "position",
        1,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Vec3",
    )
    _add_field(
        transform,
        "rotation",
        2,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Vec3",
    )
    _add_field(
        transform,
        "scale",
        3,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Vec3",
    )
    _add_field(transform, "field_501", 501, TYPE.TYPE_UINT32)

    static_collider = _add_message(file_proto, "StaticCollider")
    _add_field(static_collider, "enable_native_collision", 1, TYPE.TYPE_BOOL)
    _add_field(static_collider, "enable_climb", 2, TYPE.TYPE_BOOL)

    field_22 = _add_message(file_proto, "Field22")
    _add_field(field_22, "enable_out_of_range_run", 1, TYPE.TYPE_BOOL)
    _add_field(field_22, "field_501", 501, TYPE.TYPE_UINT32)

    field_30 = _add_message(file_proto, "Field30")
    _add_field(field_30, "display_mode", 2, TYPE.TYPE_UINT32)

    model_display = _add_message(file_proto, "ModelDisplay")
    _add_field(model_display, "field_1", 1, TYPE.TYPE_UINT32)
    _add_field(model_display, "argb_color", 3, TYPE.TYPE_UINT32)
    _add_field(model_display, "opacity_percent", 4, TYPE.TYPE_FLOAT)
    _add_field(model_display, "rgb_color", 5, TYPE.TYPE_UINT32)
    _add_field(model_display, "material_or_shader_id", 6, TYPE.TYPE_UINT32)
    _add_field(model_display, "field_9", 9, TYPE.TYPE_UINT32)

    component = _add_message(file_proto, "Component")
    _add_field(component, "component_type", 1, TYPE.TYPE_UINT32)
    _add_field(
        component,
        "transform",
        11,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Transform",
    )
    _add_field(
        component,
        "static_collider",
        15,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.StaticCollider",
    )
    _add_field(
        component,
        "field_22",
        22,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Field22",
    )
    _add_field(
        component,
        "field_30",
        30,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Field30",
    )
    _add_field(
        component,
        "model_display",
        32,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.ModelDisplay",
    )

    entity_core = _add_message(file_proto, "EntityCore")
    _add_field(entity_core, "entity_id", 1, TYPE.TYPE_UINT64)
    _add_field(
        entity_core,
        "template",
        2,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.TemplateRef",
    )
    _add_field(
        entity_core,
        "properties",
        5,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Property",
        repeated=True,
    )
    _add_field(
        entity_core,
        "components",
        6,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Component",
        repeated=True,
    )
    _add_field(entity_core, "template_id_ref", 8, TYPE.TYPE_UINT64)

    entity_data = _add_message(file_proto, "EntityData")
    _add_field(
        entity_data,
        "data",
        1,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.EntityCore",
    )
    _add_field(entity_data, "template_id", 4, TYPE.TYPE_UINT64)

    asset = _add_message(file_proto, "Asset")
    _add_field(
        asset,
        "meta",
        1,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.AssetMeta",
    )
    _add_field(asset, "name", 3, TYPE.TYPE_STRING)
    _add_field(
        asset,
        "entity_data",
        12,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.EntityData",
    )

    collection = _add_message(file_proto, "GIACollection")
    _add_field(
        collection,
        "assets",
        1,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Asset",
        repeated=True,
    )
    _add_field(
        collection,
        "resource_assets",
        2,
        TYPE.TYPE_MESSAGE,
        type_name=".game.gia.embedded.Asset",
        repeated=True,
    )
    _add_field(collection, "source_path", 3, TYPE.TYPE_STRING)
    _add_field(collection, "version", 5, TYPE.TYPE_STRING)

    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    descriptor = pool.FindMessageTypeByName("game.gia.embedded.GIACollection")
    try:
        message_cls = message_factory.GetMessageClass(descriptor)
    except AttributeError:
        message_cls = message_factory.MessageFactory(pool).GetPrototype(descriptor)

    return SimpleNamespace(GIACollection=message_cls)


_GIA_PB2 = None


def get_gia_pb2():
    global _GIA_PB2
    if _GIA_PB2 is None:
        _GIA_PB2 = _build_embedded_gia_pb2()
    return _GIA_PB2


def load_collection(gia_pb2, path: Path):
    data = path.read_bytes()
    if len(data) < HEADER_SIZE + FOOTER_SIZE:
        raise ValueError(f"file is too small to be a GIA container: {path}")

    header = bytearray(data[:HEADER_SIZE])
    payload_size = int.from_bytes(header[16:20], "big")
    payload_end = HEADER_SIZE + payload_size
    if payload_end + FOOTER_SIZE != len(data):
        raise ValueError(
            f"payload length mismatch: header says {payload_size}, file size is {len(data)}"
        )

    collection = gia_pb2.GIACollection()
    collection.ParseFromString(data[HEADER_SIZE:payload_end])
    return collection, header, data[payload_end:]


def write_collection(collection, header: bytearray, footer: bytes, path: Path) -> None:
    payload = collection.SerializeToString()
    total_size = HEADER_SIZE + len(payload) + len(footer)
    header[0:4] = (total_size - 4).to_bytes(4, "big")
    header[16:20] = len(payload).to_bytes(4, "big")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + payload + footer)


def component_by_type(entity, component_type: int):
    for component in entity.data.components:
        if component.component_type == component_type:
            return component
    raise ValueError(f"component type not found: {component_type}")


def ensure_component_by_type(entity, component_type: int):
    for component in entity.data.components:
        if component.component_type == component_type:
            return component
    component = entity.data.components.add()
    component.component_type = component_type
    return component


def set_vec3(vec, values: list[float] | tuple[float, float, float]) -> None:
    if len(values) != 3:
        raise ValueError(f"vec3 requires exactly 3 values: {values}")
    vec.x = float(values[0])
    vec.y = float(values[1])
    vec.z = float(values[2])


def set_name_property(entity, name: str) -> None:
    for prop in entity.data.properties:
        if prop.property_type == 1 and prop.HasField("name"):
            prop.name.name = name
            prop.name.static_block = 1
            return


def rgb_to_int(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (r << 16) | (g << 8) | b


def clamp_byte(value: Any, field_name: str) -> int:
    result = int(value)
    if not 0 <= result <= 255:
        raise ValueError(f"{field_name} must be in 0..255, got {value}")
    return result


def parse_color(value: Any) -> tuple[tuple[int, int, int], float]:
    if value is None:
        return (255, 255, 255), 100.0

    if isinstance(value, list):
        if len(value) not in (3, 4):
            raise ValueError("color list must be [r,g,b] or [r,g,b,opacity]")
        rgb = (
            clamp_byte(value[0], "r"),
            clamp_byte(value[1], "g"),
            clamp_byte(value[2], "b"),
        )
        opacity = float(value[3]) if len(value) == 4 else 100.0
        return rgb, opacity

    if isinstance(value, dict):
        if "rgb" in value:
            rgb_raw = value["rgb"]
            if not isinstance(rgb_raw, list) or len(rgb_raw) != 3:
                raise ValueError("color.rgb must be [r,g,b]")
            rgb = (
                clamp_byte(rgb_raw[0], "r"),
                clamp_byte(rgb_raw[1], "g"),
                clamp_byte(rgb_raw[2], "b"),
            )
        else:
            rgb = (
                clamp_byte(value.get("r", 255), "r"),
                clamp_byte(value.get("g", 255), "g"),
                clamp_byte(value.get("b", 255), "b"),
            )
        opacity = float(value.get("opacity", value.get("alpha_percent", 100.0)))
        if "alpha" in value:
            opacity = clamp_byte(value["alpha"], "alpha") / 255.0 * 100.0
        return rgb, opacity

    raise ValueError(f"unsupported color format: {value!r}")


def set_model_display(entity, rgb: tuple[int, int, int], opacity_percent: float) -> dict[str, Any]:
    display = component_by_type(entity, 22).model_display
    alpha = round(max(0.0, min(100.0, opacity_percent)) / 100.0 * 255.0)
    rgb_value = rgb_to_int(rgb)

    display.field_1 = 1
    display.argb_color = (alpha << 24) | rgb_value
    display.opacity_percent = alpha / 255.0 * 100.0
    display.rgb_color = rgb_value
    display.material_or_shader_id = 6700
    display.field_9 = 0
    return {
        "rgb": list(rgb),
        "argb_hex": f"0x{display.argb_color:08X}",
        "opacity_percent": display.opacity_percent,
    }


def set_transform(entity, position: list[float], rotation: list[float], scale: list[float]) -> None:
    transform = component_by_type(entity, 1).transform
    set_vec3(transform.position, position)
    set_vec3(transform.rotation, rotation)
    set_vec3(transform.scale, scale)
    transform.field_501 = 4_294_967_295


def set_static_collider(entity, enable_collision: bool, enable_climb: bool) -> None:
    collider = component_by_type(entity, 5).static_collider
    collider.enable_native_collision = bool(enable_collision)
    collider.enable_climb = bool(enable_climb)


def set_load_optimization(
    entity,
    enable_out_of_range_run: bool,
    out_of_range_display_mode: int,
) -> None:
    display_mode = int(out_of_range_display_mode)
    if display_mode not in OUT_OF_RANGE_DISPLAY_MODES:
        raise ValueError(
            "out_of_range_display_mode must be 0 (default), 1 (permanent), or 2 "
            f"(permanent highest precision), got {out_of_range_display_mode}"
        )

    run_component = ensure_component_by_type(entity, 12).field_22
    run_component.Clear()
    if enable_out_of_range_run:
        run_component.enable_out_of_range_run = True
    else:
        # The verified disabled representation is field_501 = 1.
        run_component.field_501 = 1

    display_component = ensure_component_by_type(entity, 20).field_30
    display_component.Clear()
    if display_mode:
        display_component.display_mode = display_mode


def template_assets_by_id(collection) -> dict[int, object]:
    result = {}
    for asset in collection.assets:
        if asset.HasField("entity_data"):
            result.setdefault(asset.entity_data.template_id, asset)
    return result


def resolve_template_id(item: dict[str, Any]) -> int:
    raw = item.get("template_id", item.get("type_id", item.get("type")))
    if raw is None:
        raise ValueError(f"object requires template_id/type_id/type: {item}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        if raw.isdigit():
            return int(raw)
        if raw in TYPE_NAME_TO_TEMPLATE_ID:
            return TYPE_NAME_TO_TEMPLATE_ID[raw]
    raise ValueError(f"unknown object type/template_id: {raw!r}")


def resolve_entity_id(item: dict[str, Any], fallback: int) -> int:
    raw = item.get("entity_id", item.get("object_id", item.get("id", fallback)))
    return int(raw)


def vec_from_item(item: dict[str, Any], names: tuple[str, ...], default: list[float]) -> list[float]:
    for name in names:
        if name in item:
            value = item[name]
            if not isinstance(value, list) or len(value) != 3:
                raise ValueError(f"{name} must be [x,y,z]")
            return [float(value[0]), float(value[1]), float(value[2])]
    return list(default)


def update_entity_asset(
    asset,
    item: dict[str, Any],
    entity_id: int,
    template_id: int,
) -> dict[str, Any]:
    type_name = TEMPLATE_ID_TO_TYPE_NAME.get(template_id, str(template_id))
    name = str(item.get("name") or f"Object_{entity_id}_{type_name}")
    position = vec_from_item(item, ("position", "pos"), [0.0, 0.0, 0.0])
    rotation = vec_from_item(item, ("rotation", "rot"), [0.0, 0.0, 0.0])
    scale = vec_from_item(item, ("scale",), [1.0, 1.0, 1.0])
    rgb, opacity_percent = parse_color(item.get("color", item.get("rgb")))
    enable_collision = bool(item.get("enable_collision", item.get("collision", True)))
    enable_climb = bool(item.get("enable_climb", item.get("climb", True)))
    enable_out_of_range_run = bool(item.get("enable_out_of_range_run", False))
    out_of_range_display_mode = int(item.get("out_of_range_display_mode", 0))

    asset.meta.asset_id = entity_id
    asset.name = name

    entity = asset.entity_data
    entity.template_id = template_id
    entity.data.entity_id = entity_id
    entity.data.template.template_id = template_id
    entity.data.template.field_2 = 1
    entity.data.template_id_ref = template_id

    set_name_property(entity, name)
    set_transform(entity, position, rotation, scale)
    display = set_model_display(entity, rgb, opacity_percent)
    set_static_collider(entity, enable_collision, enable_climb)
    set_load_optimization(entity, enable_out_of_range_run, out_of_range_display_mode)

    return {
        "entity_id": entity_id,
        "template_id": template_id,
        "type": type_name,
        "name": name,
        "position": position,
        "rotation": rotation,
        "scale": scale,
        "display": display,
        "enable_native_collision": enable_collision,
        "enable_climb": enable_climb,
        "enable_out_of_range_run": enable_out_of_range_run,
        "out_of_range_display_mode": out_of_range_display_mode,
    }


def load_objects(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("objects JSON root must be a list")
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"object #{index} must be a JSON object")
    return data


def _build_standalone_entity_assets(
    *,
    objects: list[dict[str, Any]],
    template_path: Path,
    entity_id_start: int,
) -> tuple[list[bytes], list[dict[str, Any]]]:
    if not objects:
        return [], []
    gia_pb2 = get_gia_pb2()
    collection, _, _ = load_collection(gia_pb2, template_path)
    templates = template_assets_by_id(collection)
    assets: list[bytes] = []
    records: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for index, item in enumerate(objects):
        template_id = resolve_template_id(item)
        if template_id not in templates:
            raise ValueError(f"独立静态元件 template_id {template_id} 不在普通 GIA 模板中")
        entity_id = resolve_entity_id(item, entity_id_start + index)
        if entity_id in used_ids:
            raise ValueError(f"独立静态元件 entity_id 重复：{entity_id}")
        used_ids.add(entity_id)
        asset = copy.deepcopy(templates[template_id])
        record = update_entity_asset(asset, item, entity_id, template_id)
        record["kind"] = str(item.get("standalone_kind") or "standalone_entity")
        records.append(record)
        assets.append(asset.SerializeToString())
    return assets, records


def build_gia(
    *,
    template_path: Path,
    objects_path: Path,
    output_path: Path,
    summary_path: Path | None,
    entity_id_start: int,
    progress_callback: Callable[[int, str], None] | None = None,
    decoration_packaging: bool = False,
    max_decorations_per_parent: int = MAX_DECORATIONS_PER_PARENT,
    wrapper_template_id: int = DEFAULT_WRAPPER_TEMPLATE_ID,
    decoration_template_path: Path | None = None,
    wrapper_static: bool = False,
    wrapper_collision: bool = False,
    wrapper_climb: bool = False,
    wrapper_enable_out_of_range_run: bool = False,
    wrapper_out_of_range_display_mode: int = 0,
    decoration_parent_position: tuple[float, float, float] | list[float] | None = None,
    decoration_parent_scale: tuple[float, float, float] | list[float] | None = None,
) -> dict[str, Any]:
    def report(percent: int, message: str) -> None:
        if progress_callback is not None:
            progress_callback(max(0, min(100, int(percent))), message)

    report(2, "正在读取对象列表")
    objects = load_objects(objects_path)
    if decoration_packaging:
        wrapper_path = Path(decoration_template_path or DEFAULT_DECORATION_TEMPLATE_GIA)
        if not wrapper_path.exists():
            raise FileNotFoundError(f"装饰物包装模板不存在：{wrapper_path}")
        decoration_objects: list[dict[str, Any]] = []
        standalone_objects: list[dict[str, Any]] = []
        for item in objects:
            if not bool(item.get("exclude_from_decoration", False)):
                # 编辑器不会为装饰物资源建立有效碰撞体。显示对象只保留显示数据，
                # 碰撞由独立静态叠底承担。
                decoration_item = dict(item)
                decoration_item["collision"] = False
                decoration_item["climb"] = False
                decoration_objects.append(decoration_item)
                continue
            standalone_objects.append(item)
        if not decoration_objects:
            raise ValueError("装饰物包装模式下没有可包装的显示对象")

        report(5, "正在生成独立静态叠底")
        standalone_assets, standalone_records = _build_standalone_entity_assets(
            objects=standalone_objects,
            template_path=Path(template_path),
            entity_id_start=entity_id_start,
        )
        report(6, "正在载入装饰物包装模板")
        summary = build_decorated_gia(
            objects=decoration_objects,
            decoration_template_path=wrapper_path,
            output_path=output_path,
            max_per_parent=max_decorations_per_parent,
            wrapper_template_id=wrapper_template_id,
            entity_id_start=entity_id_start,
            wrapper_static=wrapper_static,
            wrapper_collision=wrapper_collision,
            wrapper_climb=wrapper_climb,
            wrapper_enable_out_of_range_run=wrapper_enable_out_of_range_run,
            wrapper_out_of_range_display_mode=wrapper_out_of_range_display_mode,
            standalone_entity_assets=standalone_assets,
            standalone_entity_records=standalone_records,
            parent_position=decoration_parent_position,
            parent_scale=decoration_parent_scale,
            progress_callback=progress_callback,
        )
        summary["objects"] = str(objects_path)
        summary["source_object_template"] = str(template_path)
        summary["version"] = "6.7.0"
        if summary_path:
            report(97, "正在写入生成摘要")
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report(100, "GIA 装饰物包装完成")
        return summary

    report(4, "正在载入 GIA 模板")
    gia_pb2 = get_gia_pb2()
    collection, header, footer = load_collection(gia_pb2, template_path)
    templates = template_assets_by_id(collection)
    report(8, "正在读取对象列表")

    new_assets = []
    records = []
    used_ids: set[int] = set()
    total_objects = len(objects)
    update_stride = max(1, total_objects // 100) if total_objects else 1
    for index, item in enumerate(objects):
        if index % update_stride == 0:
            report(
                10 + round(75 * index / max(total_objects, 1)),
                f"正在复制模板并写入对象：{index:,}/{total_objects:,}",
            )
        template_id = resolve_template_id(item)
        if template_id not in templates:
            raise ValueError(f"template_id {template_id} not found in template GIA")
        entity_id = resolve_entity_id(item, entity_id_start + index)
        if entity_id in used_ids:
            raise ValueError(f"duplicate entity_id/object_id: {entity_id}")
        used_ids.add(entity_id)

        asset = copy.deepcopy(templates[template_id])
        records.append(update_entity_asset(asset, item, entity_id, template_id))
        new_assets.append(asset)

    report(87, f"对象写入完成，共 {len(new_assets):,} 个")
    del collection.assets[:]
    collection.assets.extend(new_assets)
    collection.source_path = f"generated\\{output_path.name}"
    if not collection.version:
        collection.version = "6.7.0"

    report(92, "正在序列化并写入 GIA 文件")
    write_collection(collection, header, footer, output_path)
    summary = {
        "template": str(template_path),
        "objects": str(objects_path),
        "output": str(output_path),
        "asset_count": len(new_assets),
        "entity_id_start": entity_id_start,
        "version": collection.version,
        "file_size": output_path.stat().st_size,
        "records": records,
    }
    if summary_path:
        report(97, "正在写入生成摘要")
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report(100, "GIA 对象构建完成")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a GIA file from a JSON object placement list.")
    parser.add_argument("--template", type=Path, required=True, help="template .gia containing white-model entity assets")
    parser.add_argument("--objects", type=Path, required=True, help="JSON list of objects to place")
    parser.add_argument("--output", type=Path, required=True, help="output .gia path")
    parser.add_argument("--summary", type=Path, help="write build summary JSON")
    parser.add_argument("--entity-id-start", type=int, default=DEFAULT_ENTITY_ID_START)
    parser.add_argument("--decoration-packaging", action="store_true", help="用空模型包装装饰物")
    parser.add_argument(
        "--max-decorations-per-parent",
        type=int,
        default=MAX_DECORATIONS_PER_PARENT,
        help=f"每个空模型最多装饰物数量（1..{MAX_DECORATIONS_PER_PARENT}）",
    )
    parser.add_argument("--decoration-template", type=Path, default=DEFAULT_DECORATION_TEMPLATE_GIA)
    parser.add_argument("--wrapper-static", action="store_true", help="把空模型主元件设为静态")
    parser.add_argument("--wrapper-collision", action="store_true", help="开启空模型原生碰撞")
    parser.add_argument("--wrapper-climb", action="store_true", help="开启空模型攀爬（同时要求碰撞）")
    parser.add_argument("--wrapper-enable-out-of-range-run", action="store_true")
    parser.add_argument("--wrapper-out-of-range-display-mode", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--overwrite", action="store_true", help="allow replacing output/summary")
    args = parser.parse_args()

    if args.output.exists() and not args.overwrite:
        raise FileExistsError(f"output already exists: {args.output}")
    if args.summary and args.summary.exists() and not args.overwrite:
        raise FileExistsError(f"summary already exists: {args.summary}")

    summary = build_gia(
        template_path=args.template,
        objects_path=args.objects,
        output_path=args.output,
        summary_path=args.summary,
        entity_id_start=args.entity_id_start,
        decoration_packaging=args.decoration_packaging,
        max_decorations_per_parent=args.max_decorations_per_parent,
        decoration_template_path=args.decoration_template,
        wrapper_static=args.wrapper_static,
        wrapper_collision=args.wrapper_collision,
        wrapper_climb=args.wrapper_climb,
        wrapper_enable_out_of_range_run=args.wrapper_enable_out_of_range_run,
        wrapper_out_of_range_display_mode=args.wrapper_out_of_range_display_mode,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
