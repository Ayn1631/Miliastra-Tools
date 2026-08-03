from __future__ import annotations

import copy
import struct
import sys
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
VARIABLES_SCRIPT_DIR = SCRIPTS_DIR / "gia_component_variables"
IMPORT_SCRIPT_DIR = SCRIPTS_DIR / "gia_component_import"
sys.path.insert(0, str(VARIABLES_SCRIPT_DIR))
sys.path.insert(0, str(IMPORT_SCRIPT_DIR))

from .import_component_variables import (  # noqa: E402
    FOOTER_SIZE,
    HEADER_SIZE,
    encode_varint,
    key,
    length_field,
    string_field,
    varint_field,
)
from .parse_component_variables import (  # noqa: E402
    children,
    first_field,
    parse_message,
    varint,
)


TYPE_INFO: dict[int, dict[str, Any]] = {
    1: {"name": "object", "value_field": 11, "aliases": ("object",)},
    2: {"name": "GUID", "value_field": 12, "aliases": ("guid", "GUID")},
    3: {"name": "int", "value_field": 13, "aliases": ("int", "integer")},
    4: {"name": "bool", "value_field": 14, "aliases": ("bool", "boolean")},
    5: {"name": "float", "value_field": 15, "aliases": ("float", "number")},
    6: {"name": "str", "value_field": 16, "aliases": ("str", "string")},
    7: {"name": "GUID_list", "value_field": 17, "aliases": ("guid_list", "GUID_list")},
    8: {"name": "int_list", "value_field": 18, "aliases": ("int_list",)},
    9: {"name": "bool_list", "value_field": 19, "aliases": ("bool_list",)},
    10: {"name": "float_list", "value_field": 20, "aliases": ("float_list",)},
    11: {"name": "str_list", "value_field": 21, "aliases": ("str_list", "string_list")},
    12: {"name": "vector", "value_field": 22, "aliases": ("vector", "vec3")},
    13: {"name": "object_list", "value_field": 23, "aliases": ("object_list", "object_lsit")},
    15: {"name": "vector_list", "value_field": 25, "aliases": ("vector_list", "vec3_list")},
    17: {"name": "camp", "value_field": 27, "aliases": ("camp",)},
    20: {"name": "configurationID", "value_field": 30, "aliases": ("configurationID", "config", "config_id")},
    21: {"name": "componentID", "value_field": 31, "aliases": ("componentID", "component_id")},
    22: {"name": "configurationID_list", "value_field": 32, "aliases": ("configurationID_list", "config_list")},
    23: {"name": "componentID_list", "value_field": 33, "aliases": ("componentID_list", "component_id_list")},
    24: {"name": "camp_list", "value_field": 34, "aliases": ("camp_list",)},
    25: {"name": "struct", "value_field": 35, "aliases": ("struct",)},
    26: {"name": "struct_list", "value_field": 36, "aliases": ("struct_list",)},
    27: {"name": "dict", "value_field": 37, "aliases": ("dict", "dictionary")},
}

TYPE_BY_NAME: dict[str, int] = {}
for code, info in TYPE_INFO.items():
    TYPE_BY_NAME[str(info["name"]).lower()] = code
    for alias in info["aliases"]:
        TYPE_BY_NAME[str(alias).lower()] = code


def load_gil(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    data = path.read_bytes()
    if len(data) < HEADER_SIZE + FOOTER_SIZE:
        raise ValueError(f"file is too small: {path}")
    payload_len = int.from_bytes(data[16:20], "big")
    payload_end = HEADER_SIZE + payload_len
    if payload_end + FOOTER_SIZE != len(data):
        raise ValueError(
            f"payload length mismatch: header says {payload_len}, file size is {len(data)}"
        )
    return data, parse_message(data[HEADER_SIZE:payload_end], HEADER_SIZE)


def write_gil(original: bytes, root: list[dict[str, Any]], output_path: Path) -> dict[str, Any]:
    payload = serialize_message(root)
    new_size = HEADER_SIZE + len(payload) + FOOTER_SIZE
    header = bytearray(original[:HEADER_SIZE])
    header[0:4] = (new_size - 4).to_bytes(4, "big")
    header[16:20] = len(payload).to_bytes(4, "big")
    output = bytes(header) + payload + original[-FOOTER_SIZE:]
    validate_gil_bytes(output)
    output_path.write_bytes(output)
    return {
        "output": str(output_path),
        "output_size": len(output),
        "payload_size": len(payload),
        "footer_hex": output[-FOOTER_SIZE:].hex(" "),
    }


def field_is_dirty(field: dict[str, Any]) -> bool:
    return "_dirty" in field or "_raw_override" in field or any(
        field_is_dirty(child) for child in field.get("children", [])
    )


def serialize_message(fields: list[dict[str, Any]]) -> bytes:
    return b"".join(serialize_field(field) for field in fields)


def serialize_field(field: dict[str, Any]) -> bytes:
    field_no = field["field"]
    wire_type = field["wire_type"]
    prefix = key(field_no, wire_type)
    if wire_type == 0:
        return prefix + encode_varint(field["value"])
    if wire_type == 1:
        return prefix + field["raw"]
    if wire_type == 2:
        if "_raw_override" in field:
            raw = field["_raw_override"]
        elif field_is_dirty(field) and "children" in field:
            raw = serialize_message(field["children"])
        else:
            raw = field.get("raw", b"")
        return prefix + encode_varint(len(raw)) + raw
    if wire_type == 5:
        return prefix + field["raw"]
    raise ValueError(f"unsupported protobuf wire type: {wire_type}")


def validate_gil_bytes(data: bytes) -> None:
    payload_len = int.from_bytes(data[16:20], "big")
    if HEADER_SIZE + payload_len + FOOTER_SIZE != len(data):
        raise ValueError("GIL header payload size mismatch")
    parse_message(data[HEADER_SIZE : HEADER_SIZE + payload_len], HEADER_SIZE)


def mark_dirty(field: dict[str, Any]) -> None:
    field["_dirty"] = True


def set_utf8(field: dict[str, Any], value: str) -> None:
    raw = value.encode("utf-8")
    field["utf8"] = value
    field["raw"] = raw
    field["length"] = len(raw)
    field.pop("children", None)
    field["_raw_override"] = raw


def set_varint(field: dict[str, Any], value: int) -> None:
    field["value"] = int(value)
    mark_dirty(field)


def walk_field(field: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield field
    for child in field.get("children", []):
        yield from walk_field(child)


def walk_fields(fields: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for field in fields:
        yield from walk_field(field)


def collect_varints(fields: list[dict[str, Any]]) -> set[int]:
    return {
        item["value"]
        for item in walk_fields(fields)
        if isinstance(item.get("value"), int)
    }


def next_free_id(used: set[int], start: int) -> int:
    value = start
    while value in used:
        value += 1
    used.add(value)
    return value


def top_field(root: list[dict[str, Any]], field_no: int) -> dict[str, Any]:
    field = first_field(root, field_no)
    if field is None:
        raise ValueError(f"root field not found: f{field_no}")
    return field


def root_children(root: list[dict[str, Any]], field_no: int) -> list[dict[str, Any]]:
    field = top_field(root, field_no)
    if "children" not in field:
        field["children"] = []
    return field["children"]


def replace_exact_utf8(field: dict[str, Any], old: str, new: str) -> None:
    for item in walk_field(field):
        if item.get("utf8") == old:
            set_utf8(item, new)


def replace_varint_values(field: dict[str, Any], replacements: dict[int, int]) -> None:
    for item in walk_field(field):
        value = item.get("value")
        if isinstance(value, int) and value in replacements:
            set_varint(item, replacements[value])


def set_first_varint_child(field: dict[str, Any], field_no: int, value: int) -> None:
    child = first_field(children(field), field_no)
    if child is None or child.get("wire_type") != 0:
        raise ValueError(f"varint child not found: f{field_no} at offset {field.get('offset')}")
    set_varint(child, value)


def set_ref_id(record: dict[str, Any], value: int) -> None:
    ref = first_field(children(record), 2)
    if ref is None:
        return
    ref_id = first_field(children(ref), 1)
    if ref_id is not None and ref_id.get("wire_type") == 0:
        set_varint(ref_id, value)


def field_strings(field: dict[str, Any]) -> list[str]:
    return [
        item["utf8"]
        for item in walk_field(field)
        if isinstance(item.get("utf8"), str) and item.get("utf8")
    ]


def find_record_containing(records: list[dict[str, Any]], text: str) -> dict[str, Any]:
    for record in records:
        if text in field_strings(record):
            return record
    raise ValueError(f"record containing {text!r} not found")


def find_variable_list_field(record: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for item in walk_field(record):
        if item.get("field") != 11 or item.get("wire_type") != 2:
            continue
        variable_fields = [child for child in children(item) if child.get("field") == 1]
        if not variable_fields:
            continue
        for variable in variable_fields:
            name = first_field(children(variable), 2)
            type_code = first_field(children(variable), 3)
            if isinstance(name, dict) and isinstance(name.get("utf8"), str):
                if isinstance(type_code, dict) and isinstance(type_code.get("value"), int):
                    candidates.append(item)
                    break
    if not candidates:
        raise ValueError(f"variable list field not found in record at offset {record.get('offset')}")
    return candidates[-1]


def variable_name(variable: dict[str, Any]) -> str | None:
    name = first_field(children(variable), 2)
    return name.get("utf8") if name else None


def variable_type_code(variable: dict[str, Any]) -> int | None:
    return varint(children(variable), 3)


def type_code_from_spec(spec: dict[str, Any]) -> int:
    if "type_code" in spec:
        return int(spec["type_code"])
    type_name = str(spec.get("type", "")).strip().lower()
    if not type_name:
        raise ValueError(f"variable has no type/type_code: {spec}")
    if type_name not in TYPE_BY_NAME:
        raise ValueError(f"unsupported variable type: {spec.get('type')}")
    return TYPE_BY_NAME[type_name]


def protobuf_type_ref(type_code: int, struct_id: int | None = None) -> bytes:
    if type_code in (25, 26) and struct_id is not None:
        return varint_field(1, type_code) + length_field(
            2,
            varint_field(1, 1) + varint_field(2, struct_id),
        )
    return varint_field(1, type_code) + length_field(2, b"")


def repeated_varint_payload(values: list[Any]) -> bytes:
    return b"".join(length_field(1, encode_varint(int(value))) for value in values)


def repeated_float_payload(values: list[Any]) -> bytes:
    return b"".join(length_field(1, struct.pack("<f", float(value))) for value in values)


def repeated_string_payload(values: list[Any]) -> bytes:
    return b"".join(string_field(1, str(value)) for value in values)


def clone_field(field: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(field)
