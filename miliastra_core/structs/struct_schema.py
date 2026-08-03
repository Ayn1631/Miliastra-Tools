from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from .miliastra_container import MiliastraContainer
from miliastra_core.protobuf.wire import (
    WireError,
    WireField,
    first_bytes,
    first_field,
    first_varint,
    parse_fields,
)


TYPE_NAMES = {
    1: "object",
    2: "GUID",
    3: "int",
    4: "bool",
    5: "float",
    6: "str",
    7: "GUID_list",
    8: "int_list",
    9: "bool_list",
    10: "float_list",
    11: "str_list",
    12: "vector",
    13: "object_list",
    15: "vector_list",
    17: "camp",
    20: "configurationID",
    21: "componentID",
    22: "configurationID_list",
    23: "componentID_list",
    24: "camp_list",
    25: "struct",
    26: "struct_list",
    27: "dict",
}


def _all_fields(
    fields: list[WireField], number: int, wire_type: int | None = None
) -> list[WireField]:
    return [
        field
        for field in fields
        if field.number == number and (wire_type is None or field.wire_type == wire_type)
    ]


def _strict_utf8(payload: bytes, *, context: str) -> str:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WireError(f"{context}: invalid UTF-8") from exc
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise WireError(f"{context}: UTF-8 contains surrogate code points")
    return text


def _parse(data: bytes, context: str) -> list[WireField]:
    return parse_fields(data, context=context)


def _try_parse(data: bytes, context: str) -> list[WireField] | None:
    if not data:
        return None
    try:
        return _parse(data, context)
    except WireError:
        return None


def _nested_fields(fields: list[WireField], path: list[int], context: str) -> list[WireField] | None:
    current = fields
    for number in path:
        payload = first_bytes(current, number)
        if payload is None:
            return None
        current = _try_parse(payload, context)
        if current is None:
            return None
    return current


def _nested_varint(fields: list[WireField], message_path: list[int], number: int, context: str) -> int | None:
    nested = _nested_fields(fields, message_path, context)
    return None if nested is None else first_varint(nested, number)


def _text_field(fields: list[WireField], number: int, context: str) -> str | None:
    payload = first_bytes(fields, number)
    if payload is None:
        return None
    try:
        return _strict_utf8(payload, context=context)
    except WireError:
        return None


def _field_doc(schema_field: list[WireField], index: int, *, inline: bool) -> dict[str, Any] | None:
    name = _text_field(schema_field, 501, "struct field name")
    if not name and not inline:
        name = _text_field(schema_field, 5, "struct field fallback name")
    type_code = first_varint(schema_field, 1 if inline else 502)
    if not name or type_code is None:
        return None

    result: dict[str, Any] = {
        "index": index,
        "name": name,
        "type_code": int(type_code),
        "type": TYPE_NAMES.get(int(type_code), f"type_{type_code}"),
    }
    if not inline:
        schema_index = first_varint(schema_field, 503)
        if schema_index is None:
            return None
        result["schema_index"] = int(schema_index)

    nested_path = [2, 2] if inline else [1, 2]
    nested_id = _nested_varint(schema_field, nested_path, 2, "nested struct type")
    if nested_id is not None and type_code in (25, 26):
        result["element_struct_id" if type_code == 26 else "struct_id"] = int(nested_id)

    if type_code == 27:
        dict_payload = first_bytes(schema_field, 37)
        dict_fields = _try_parse(dict_payload, "dict metadata") if dict_payload is not None else None
        if dict_fields is None and not inline:
            outer = _nested_fields(schema_field, [3], "dict value wrapper")
            dict_payload = first_bytes(outer, 37) if outer else None
            dict_fields = _try_parse(dict_payload, "dict metadata") if dict_payload else None
        if dict_fields:
            key_code = first_varint(dict_fields, 503)
            value_code = first_varint(dict_fields, 504)
            value_struct_id = first_varint(dict_fields, 505)
            if key_code is not None:
                result["dict_key_type_code"] = int(key_code)
            if value_code is not None:
                result["dict_value_type_code"] = int(value_code)
            if value_struct_id is not None:
                result["dict_value_struct_id"] = int(value_struct_id)
    return result


def _parse_named_schema(
    fields: list[WireField],
    struct_id: int,
    *,
    source_record_index: int,
    source: str,
) -> dict[str, Any] | None:
    name = _text_field(fields, 501, "struct name")
    raw_fields = _all_fields(fields, 3, 2)
    if not name or not raw_fields:
        return None
    parsed_fields: list[dict[str, Any]] = []
    for index, raw_field in enumerate(raw_fields, start=1):
        nested = _try_parse(bytes(raw_field.value), "struct schema field")
        if nested is None:
            return None
        field_doc = _field_doc(nested, index, inline=False)
        if field_doc is None:
            return None
        parsed_fields.append(field_doc)
    return {
        "id": int(struct_id),
        "name": name,
        "field_count": len(parsed_fields),
        "fields": parsed_fields,
        "schema_offset": getattr(fields[0], "offset", None) if fields else None,
        "source_record_index": source_record_index,
        "schema_path": source,
        "layout_source": "schema_message_order",
        "confidence": "verified",
    }


def _parse_inline_schema(
    fields: list[WireField],
    *,
    source_record_index: int,
    source: str,
) -> dict[str, Any] | None:
    struct_id = first_varint(fields, 501)
    raw_fields = _all_fields(fields, 1, 2)
    if struct_id is None or not raw_fields:
        return None
    parsed_fields: list[dict[str, Any]] = []
    for index, raw_field in enumerate(raw_fields, start=1):
        nested = _try_parse(bytes(raw_field.value), "inline struct field")
        if nested is None:
            return None
        field_doc = _field_doc(nested, index, inline=True)
        if field_doc is None:
            return None
        parsed_fields.append(field_doc)
    return {
        "id": int(struct_id),
        "name": f"struct_{struct_id}",
        "field_count": len(parsed_fields),
        "fields": parsed_fields,
        "schema_offset": getattr(fields[0], "offset", None) if fields else None,
        "source_record_index": source_record_index,
        "schema_path": source,
        "layout_source": "gia_inline_layout",
        "confidence": "heuristic",
    }


def _walk_messages(
    fields: list[WireField],
    *,
    path: str,
    depth: int = 0,
    max_depth: int = 64,
) -> Iterator[tuple[str, list[WireField]]]:
    yield path, fields
    if depth >= max_depth:
        return
    for index, field in enumerate(fields):
        if field.wire_type != 2:
            continue
        nested = _try_parse(bytes(field.value), f"{path}.f{field.number}[{index}]")
        if nested is not None:
            yield from _walk_messages(
                nested,
                path=f"{path}.f{field.number}[{index}]",
                depth=depth + 1,
                max_depth=max_depth,
            )


def _finalize(
    path: Path,
    structs: list[dict[str, Any]],
    *,
    source_format: str,
    extraction_mode: str,
) -> dict[str, Any]:
    structs.sort(key=lambda item: int(item["id"]))
    return {
        "file": str(path),
        "size": path.stat().st_size,
        "source_format": source_format,
        "extraction_mode": extraction_mode,
        "struct_count": len(structs),
        "structs": structs,
        "structs_by_name": {
            item["name"]: item["id"]
            for item in structs
            if isinstance(item.get("name"), str) and item.get("name")
        },
        "structs_by_id": {str(item["id"]): item for item in structs},
    }


def extract_gil_structs(path: Path) -> dict[str, Any]:
    container = MiliastraContainer.load(path)
    root = container.top_fields()
    top10 = first_bytes(root, 10)
    top10_fields = _parse(top10, "GIL root.f10") if top10 is not None else []
    structs: list[dict[str, Any]] = []
    seen: set[int] = set()
    for record_index, record in enumerate(_all_fields(top10_fields, 6, 2), start=1):
        record_fields = _parse(bytes(record.value), f"GIL root.f10 record {record_index}")
        part_payload = first_bytes(record_fields, 1)
        if part_payload is None:
            continue
        part = _parse(part_payload, f"GIL struct {record_index}")
        struct_id = first_varint(part, 1)
        if struct_id is None or struct_id in seen:
            continue
        parsed = _parse_named_schema(
            part,
            struct_id,
            source_record_index=record_index,
            source=f"root.f10.record[{record_index}]",
        )
        if parsed is None:
            continue
        parsed["layout_source"] = "gil_root_f10"
        parsed["confidence"] = "verified"
        seen.add(struct_id)
        structs.append(parsed)
    return _finalize(path, structs, source_format="gil", extraction_mode="root_f10_schema_lossless")


def extract_gia_structs(path: Path) -> dict[str, Any]:
    container = MiliastraContainer.load(path)
    root = container.top_fields()
    named: dict[int, dict[str, Any]] = {}
    inline: dict[int, dict[str, Any]] = {}

    for record_index, record in enumerate(_all_fields(root, 1, 2), start=1):
        record_fields = _parse(bytes(record.value), f"GIA record {record_index}")
        struct_id = _nested_varint(record_fields, [1], 4, "GIA record metadata")
        for candidate_path, candidate in _walk_messages(
            record_fields,
            path=f"record[{record_index}]",
        ):
            inline_doc = _parse_inline_schema(
                candidate,
                source_record_index=record_index,
                source=candidate_path,
            )
            if inline_doc is not None:
                current = inline.get(inline_doc["id"])
                if current is None or inline_doc["field_count"] > current["field_count"]:
                    inline[inline_doc["id"]] = inline_doc
            if struct_id is not None:
                named_doc = _parse_named_schema(
                    candidate,
                    struct_id,
                    source_record_index=record_index,
                    source=candidate_path,
                )
                if named_doc is not None:
                    current = named.get(named_doc["id"])
                    if current is None or named_doc["field_count"] > current["field_count"]:
                        named[named_doc["id"]] = named_doc

    if not named:
        raise ValueError("no struct definition records were found in the GIA")

    structs: list[dict[str, Any]] = []
    for struct_id, struct_doc in named.items():
        inline_doc = inline.get(struct_id)
        if inline_doc is not None:
            struct_doc["fields"] = inline_doc["fields"]
            struct_doc["field_count"] = inline_doc["field_count"]
            struct_doc["layout_source"] = "gia_inline_layout"
            struct_doc["layout_offset"] = inline_doc.get("schema_offset")
            struct_doc["confidence"] = "cross_checked"
        else:
            struct_doc["layout_source"] = "gia_schema_message_order"
            struct_doc["confidence"] = "verified"
        structs.append(struct_doc)
    return _finalize(
        path,
        structs,
        source_format="gia",
        extraction_mode="gia_schema_lossless",
    )


def extract_structs_from_path(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".gil":
        return extract_gil_structs(source)
    if suffix == ".gia":
        return extract_gia_structs(source)
    raise ValueError(f"unsupported source format: {source.suffix}; expected .gia or .gil")
