from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Literal

from miliastra_core.protobuf.wire import (
    WireField,
    first_bytes,
    first_varint,
    parse_fields,
    rebuild_message,
    set_bytes,
    set_varint,
)
from miliastra_core.structs.miliastra_container import MiliastraContainer

from .document import GilDocument, GilError


SOURCE_GIA = "gia"
SOURCE_ALGORITHM = "algorithm"

SOURCE_NOTE_GIA = "筛选来自 GIA 文件"
SOURCE_NOTE_ALGORITHM = "筛选来自于算法筛选，筛选出无额外组件/节点图的实体"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_GIL = _REPO_ROOT / "Template" / "自动转换模板.gil"
_BASELINE_ENTITY_NAME = "长方体"
_EXCLUDED_NAME_MARKERS = ("默认模板", "默认模版", "关卡实体")
_FALLBACK_BASELINE_FIELD7_TYPES = frozenset({1, 3, 6, 14, 18, 19})
_NODE_GRAPH_CONTAINER = 6
_NODE_GRAPH_TYPE = 3
_EXTRA_CONTAINER = 7


@dataclass(frozen=True)
class GiaComponent:
    entity_id: int
    name: str


@dataclass(frozen=True)
class EntityConvertRecord:
    object_id: int
    name: str
    asset_id: int | None
    converted: bool
    skip_reason: str | None = None


@dataclass(frozen=True)
class StaticConvertResult:
    gil_bytes: bytes
    source: Literal["gia", "algorithm"]
    source_note: str
    change_list: tuple[int, ...]
    gia_components: tuple[GiaComponent, ...]
    converted: tuple[EntityConvertRecord, ...]
    skipped: tuple[EntityConvertRecord, ...]
    unmatched_gia_ids: tuple[int, ...]


@dataclass(frozen=True)
class StaticConvertPreview:
    source: Literal["gia", "algorithm"]
    source_note: str
    change_list: tuple[int, ...]
    gia_components: tuple[GiaComponent, ...]
    candidates: tuple[EntityConvertRecord, ...]
    skipped: tuple[EntityConvertRecord, ...]
    unmatched_gia_ids: tuple[int, ...]


def extract_gia_components(gia_bytes: bytes) -> list[GiaComponent]:
    """Extract component IDs and names from a binary GIA archive."""
    container = MiliastraContainer.from_bytes(gia_bytes)
    components: list[GiaComponent] = []
    seen: set[int] = set()
    for field in container.top_fields():
        if field.number != 1 or field.wire_type != 2:
            continue
        asset = parse_fields(bytes(field.value), context="gia asset")
        meta_blob = first_bytes(asset, 1)
        asset_id = None
        if meta_blob is not None:
            asset_id = first_varint(parse_fields(meta_blob, context="gia meta"), 4)
        name_raw = first_bytes(asset, 3)
        name = name_raw.decode("utf-8", "replace") if name_raw else ""
        entity_id = None
        entity_blob = first_bytes(asset, 12)
        if entity_blob is not None:
            entity = parse_fields(entity_blob, context="gia entity")
            core_blob = first_bytes(entity, 1)
            if core_blob is not None:
                core = parse_fields(core_blob, context="gia core")
                entity_id = first_varint(core, 1)
                if not name:
                    name = _read_name(core)
        component_id = entity_id if entity_id is not None else asset_id
        if component_id is None or component_id in seen:
            continue
        seen.add(component_id)
        components.append(GiaComponent(entity_id=component_id, name=name))
    if not components:
        raise GilError("GIA 中没有可提取的元件 ID")
    return components


def entity_has_extra_attachments(entry: list[WireField]) -> bool:
    """True when a GIL entity carries extra components or a node graph.

    Presence of extra field-7 slots is compared against the 长方体 baseline in
    ``Template/自动转换模板.gil``. Built-in slot values are ignored. Node graphs
    are field-6 type 3 with a non-empty payload.
    """
    extra_types = _container_types(entry, _EXTRA_CONTAINER) - _baseline_field7_types()
    if extra_types:
        return True
    return _component_payload_size(entry, _NODE_GRAPH_CONTAINER, _NODE_GRAPH_TYPE) > 0


def preview_gil_to_static(
    gil_bytes: bytes,
    gia_bytes: bytes | None = None,
) -> StaticConvertPreview:
    """Read conversion candidates without patching or rebuilding the GIL."""
    document = GilDocument.from_bytes(gil_bytes)
    top5 = document.top_data(5)
    if top5 is None:
        raise GilError("GIL 缺少场景实体列表")

    gia_components: tuple[GiaComponent, ...] = ()
    if gia_bytes is not None:
        extracted = extract_gia_components(gia_bytes)
        gia_components = tuple(extracted)
        change_ids = {item.entity_id for item in extracted}
        source: Literal["gia", "algorithm"] = SOURCE_GIA
        source_note = SOURCE_NOTE_GIA
    else:
        change_ids = set()
        source = SOURCE_ALGORITHM
        source_note = SOURCE_NOTE_ALGORITHM

    fields = parse_fields(top5, context="gil top5")
    candidates: list[EntityConvertRecord] = []
    skipped: list[EntityConvertRecord] = []
    present_ids: set[int] = set()
    algorithm_ids: list[int] = []

    for field in fields:
        if field.number != 1 or field.wire_type != 2:
            continue
        entry = parse_fields(bytes(field.value), context="gil scene object")
        object_id = first_varint(entry, 1)
        if object_id is None:
            continue
        present_ids.add(object_id)
        name = _read_name(entry)
        asset_id = first_varint(entry, 8)
        skip_reason = _skip_reason(name, entry, source, change_ids, object_id)
        if skip_reason is not None:
            skipped.append(
                EntityConvertRecord(
                    object_id=object_id,
                    name=name,
                    asset_id=asset_id,
                    converted=False,
                    skip_reason=skip_reason,
                )
            )
            continue
        if source == SOURCE_ALGORITHM:
            algorithm_ids.append(object_id)
        elif object_id not in change_ids:
            skipped.append(EntityConvertRecord(
                object_id=object_id, name=name, asset_id=asset_id, converted=False,
                skip_reason="不在 GIA 筛选列表中",
            ))
            continue
        if not _has_static_name_property(entry):
            skipped.append(
                EntityConvertRecord(
                    object_id=object_id,
                    name=name,
                    asset_id=asset_id,
                    converted=False,
                    skip_reason="实体缺少可写入的名称属性",
                )
            )
            continue
        candidates.append(
            EntityConvertRecord(
                object_id=object_id,
                name=name,
                asset_id=asset_id,
                converted=False,
            )
        )

    if source == SOURCE_ALGORITHM:
        change_ids = set(algorithm_ids)

    unmatched = tuple(sorted(change_ids - present_ids)) if source == SOURCE_GIA else ()
    return StaticConvertPreview(
        source=source,
        source_note=source_note,
        change_list=tuple(sorted(change_ids)),
        gia_components=gia_components,
        candidates=tuple(candidates),
        skipped=tuple(skipped),
        unmatched_gia_ids=unmatched,
    )


def convert_gil_to_static(
    gil_bytes: bytes,
    gia_bytes: bytes | None = None,
    *,
    selected_ids: Iterable[int] | None = None,
) -> StaticConvertResult:
    """Write only the retained preview candidates; omitted IDs stay unchanged."""
    preview = preview_gil_to_static(gil_bytes, gia_bytes)
    candidate_ids = {item.object_id for item in preview.candidates}
    selected = set(candidate_ids)
    if selected_ids is not None:
        selected = set()
        for object_id in selected_ids:
            if isinstance(object_id, bool) or not isinstance(object_id, int):
                raise GilError("选择转换的实体 ID 必须为整数")
            selected.add(object_id)
        invalid = selected - candidate_ids
        if invalid:
            raise GilError(f"选择的实体不在当前预览候选列表中：{sorted(invalid)}")

    converted = tuple(replace(item, converted=True) for item in preview.candidates if item.object_id in selected)
    skipped = preview.skipped + tuple(
        replace(item, skip_reason="用户移出转换列表")
        for item in preview.candidates if item.object_id not in selected
    )
    result_bytes = gil_bytes
    if selected:
        document = GilDocument.from_bytes(gil_bytes)
        fields = parse_fields(document.top_data(5), context="gil top5 conversion")
        for index, field in enumerate(fields):
            if field.number != 1 or field.wire_type != 2:
                continue
            entry = parse_fields(bytes(field.value), context="gil scene conversion")
            if first_varint(entry, 1) not in selected:
                continue
            if not _set_entity_static(entry):
                raise GilError("待转换实体缺少可写入的名称属性")
            fields[index] = field.with_value(rebuild_message(entry))
        rebuilt = document.replace_top_data({5: rebuild_message(fields)})
        rebuilt.validate_roundtrip()
        result_bytes = rebuilt.build_bytes()
    return StaticConvertResult(
        gil_bytes=result_bytes,
        source=preview.source,
        source_note=preview.source_note,
        change_list=preview.change_list,
        gia_components=preview.gia_components,
        converted=converted,
        skipped=skipped,
        unmatched_gia_ids=preview.unmatched_gia_ids,
    )


def _skip_reason(
    name: str,
    entry: list[WireField],
    source: Literal["gia", "algorithm"],
    change_ids: set[int],
    object_id: int,
) -> str | None:
    if source == SOURCE_GIA and object_id not in change_ids:
        return None
    if _is_excluded_name(name):
        return "名称包含默认模板/关卡实体"
    if source == SOURCE_ALGORITHM:
        has_extra = bool(_container_types(entry, _EXTRA_CONTAINER) - _baseline_field7_types())
        has_node_graph = _component_payload_size(entry, _NODE_GRAPH_CONTAINER, _NODE_GRAPH_TYPE) > 0
        if has_extra and has_node_graph:
            return "因额外组件和节点图被跳过"
        if has_extra:
            return "因额外组件被跳过"
        if has_node_graph:
            return "因节点图被跳过"
    return None


def _is_excluded_name(name: str) -> bool:
    return any(marker in name for marker in _EXCLUDED_NAME_MARKERS)


@lru_cache(maxsize=1)
def _baseline_field7_types() -> frozenset[int]:
    if not _TEMPLATE_GIL.is_file():
        return _FALLBACK_BASELINE_FIELD7_TYPES
    document = GilDocument.load(_TEMPLATE_GIL)
    top5 = document.top_data(5)
    if top5 is None:
        return _FALLBACK_BASELINE_FIELD7_TYPES
    for field in parse_fields(top5, context="template top5"):
        if field.number != 1 or field.wire_type != 2:
            continue
        entry = parse_fields(bytes(field.value), context="template entity")
        if _read_name(entry) == _BASELINE_ENTITY_NAME:
            return frozenset(_container_types(entry, _EXTRA_CONTAINER))
    return _FALLBACK_BASELINE_FIELD7_TYPES


def _container_types(entry: list[WireField], container: int) -> set[int]:
    types: set[int] = set()
    for field in entry:
        if field.number != container or field.wire_type != 2:
            continue
        nested = parse_fields(bytes(field.value), context="component slot")
        component_type = first_varint(nested, 1)
        if component_type is not None:
            types.add(component_type)
    return types


def _component_payload_size(entry: list[WireField], container: int, component_type: int) -> int:
    for field in entry:
        if field.number != container or field.wire_type != 2:
            continue
        nested = parse_fields(bytes(field.value), context="component payload")
        if first_varint(nested, 1) != component_type:
            continue
        return len(_attachment_payload(nested))
    return 0


def _read_name(fields: list[WireField]) -> str:
    for field in fields:
        if field.number != 5 or field.wire_type != 2:
            continue
        nested = parse_fields(bytes(field.value), context="name property")
        if first_varint(nested, 1) != 1:
            continue
        payload = first_bytes(nested, 11)
        if payload is None:
            return ""
        name_fields = parse_fields(payload, context="name payload")
        raw = first_bytes(name_fields, 1)
        return raw.decode("utf-8", "replace") if raw else ""
    return ""


def _attachment_payload(component: list[WireField]) -> bytes:
    chunks: list[bytes] = []
    for field in component:
        if field.number in (1, 2) or field.wire_type != 2:
            continue
        chunks.append(bytes(field.value))
    return b"".join(chunks)


def _has_static_name_property(entry: list[WireField]) -> bool:
    return any(
        first_varint(parse_fields(bytes(field.value), context="preview name property"), 1) == 1
        for field in entry if field.number == 5 and field.wire_type == 2
    )


def _set_entity_static(entry: list[WireField]) -> bool:
    for index, field in enumerate(entry):
        if field.number != 5 or field.wire_type != 2:
            continue
        component = parse_fields(bytes(field.value), context="static name property")
        if first_varint(component, 1) != 1:
            continue
        payload = first_bytes(component, 11)
        name_fields = [] if payload is None else parse_fields(payload, context="static name payload")
        set_varint(name_fields, 2, 1)
        set_bytes(component, 11, rebuild_message(name_fields))
        entry[index] = field.with_value(rebuild_message(component))
        return True
    return False
