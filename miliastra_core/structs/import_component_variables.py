from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
VARIABLES_SCRIPT_DIR = SCRIPT_DIR.parent / "gia_component_variables"
sys.path.insert(0, str(VARIABLES_SCRIPT_DIR))

from .parse_component_variables import (
    FOOTER_SIZE,
    HEADER_SIZE,
    all_fields,
    children,
    extract_variable,
    find_variable_messages,
    first_field,
    parse_component_variables,
    parse_message,
    utf8,
)


STORY_LIST_STRUCT_ID = 1077936130
STORY_NODE_STRUCT_ID = 1077936129


def encode_varint(value: int) -> bytes:
    if value < 0:
        value = (1 << 64) + value
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result.append(byte | 0x80)
        else:
            result.append(byte)
            return bytes(result)


def key(field_no: int, wire_type: int) -> bytes:
    return encode_varint((field_no << 3) | wire_type)


def varint_field(field_no: int, value: int) -> bytes:
    return key(field_no, 0) + encode_varint(value)


def length_field(field_no: int, value: bytes) -> bytes:
    return key(field_no, 2) + encode_varint(len(value)) + value


def string_field(field_no: int, value: str) -> bytes:
    return length_field(field_no, value.encode("utf-8"))


def type_ref(type_code: int, struct_id: int) -> bytes:
    return (
        varint_field(1, type_code)
        + length_field(2, varint_field(1, 1) + varint_field(2, struct_id))
    )


def string_list_payload(values: list[Any]) -> bytes:
    return b"".join(string_field(1, str(value)) for value in values)


def int32_list_payload(values: list[Any]) -> bytes:
    return b"".join(length_field(1, encode_varint(int(value))) for value in values)


def encode_param_slot(param: dict[str, Any]) -> bytes:
    param_type = param["param_type"]
    value = param["value"]
    if param_type == "String":
        return (
            varint_field(1, 6)
            + length_field(2, varint_field(1, 6) + length_field(2, b""))
            + length_field(16, string_list_payload([value]))
        )
    if param_type == "StringList":
        return (
            varint_field(1, 11)
            + length_field(2, varint_field(1, 11) + length_field(2, b""))
            + length_field(21, string_list_payload(value))
        )
    if param_type == "Int32List":
        return (
            varint_field(1, 8)
            + length_field(2, varint_field(1, 8) + length_field(2, b""))
            + length_field(18, int32_list_payload(value))
        )
    raise ValueError(f"unsupported param_type: {param_type}")


def encode_story_node(item: dict[str, Any], node_object_id: int) -> bytes:
    value = item["value"]
    if value.get("structId") != str(STORY_NODE_STRUCT_ID):
        raise ValueError(f"unexpected StoryNode structId: {value.get('structId')}")
    node_params = value["value"]
    payload = (
        b"".join(length_field(1, encode_param_slot(param)) for param in node_params)
        + varint_field(501, STORY_NODE_STRUCT_ID)
        + length_field(502, varint_field(2, 28) + varint_field(4, node_object_id))
    )
    return (
        varint_field(1, 25)
        + length_field(2, type_ref(25, STORY_NODE_STRUCT_ID))
        + length_field(35, payload)
    )


def report_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    if report.get("structId") != str(STORY_LIST_STRUCT_ID):
        raise ValueError(f"unexpected Story_List structId: {report.get('structId')}")
    values = report.get("value", [])
    if len(values) != 1 or values[0].get("param_type") != "StructList":
        raise ValueError("report must contain exactly one StructList value")
    struct_list = values[0]["value"]
    if struct_list.get("structId") != str(STORY_NODE_STRUCT_ID):
        raise ValueError(f"unexpected StructList structId: {struct_list.get('structId')}")
    return struct_list["value"]


def encode_story_value(report: dict[str, Any], value_ref_id: int, node_object_ids: list[int]) -> bytes:
    items = report_items(report)
    if len(items) != len(node_object_ids):
        raise ValueError("node_object_ids count does not match report item count")

    story_blob = (
        b"".join(
            length_field(1, encode_story_node(item, node_id))
            for item, node_id in zip(items, node_object_ids)
        )
        + varint_field(501, STORY_NODE_STRUCT_ID)
    )
    custom_value = (
        varint_field(1, 26)
        + length_field(2, type_ref(26, STORY_NODE_STRUCT_ID))
        + length_field(36, story_blob)
        + string_field(501, "Story_List")
    )
    custom_group = (
        length_field(1, custom_value)
        + varint_field(501, STORY_LIST_STRUCT_ID)
        + length_field(502, varint_field(2, 28) + varint_field(4, value_ref_id))
    )
    return (
        varint_field(1, 25)
        + length_field(2, type_ref(25, STORY_LIST_STRUCT_ID))
        + length_field(35, custom_group)
    )


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
        elif field_is_dirty(field):
            raw = serialize_message(field["children"])
        else:
            raw = field["raw"]
        return prefix + encode_varint(len(raw)) + raw
    if wire_type == 5:
        return prefix + field["raw"]
    raise ValueError(f"unsupported wire type: {wire_type}")


def find_component_record(root: list[dict[str, Any]], component_name: str) -> dict[str, Any]:
    for record in all_fields(root, 1):
        if utf8(children(record), 3) == component_name:
            return record
    raise ValueError(f"component not found: {component_name}")


def collect_used_object_ids(story_variables: list[dict[str, Any]]) -> set[int]:
    used: set[int] = set()
    for index, variable in enumerate(story_variables, start=1):
        summary = extract_variable(variable, index)
        ref_id = summary.get("value_ref_id")
        if isinstance(ref_id, int):
            used.add(ref_id)
        for node in summary.get("story_nodes", []):
            object_id = node.get("object_id")
            if isinstance(object_id, int):
                used.add(object_id)
    return used


def allocate_node_ids(
    variable: dict[str, Any],
    item_count: int,
    used_object_ids: set[int],
    next_object_id: int,
) -> tuple[list[int], int]:
    summary = extract_variable(variable, 0)
    existing_ids = [
        node["object_id"]
        for node in summary.get("story_nodes", [])
        if isinstance(node.get("object_id"), int)
    ]
    node_ids = existing_ids[:item_count]
    while len(node_ids) < item_count:
        while next_object_id in used_object_ids:
            next_object_id += 1
        node_ids.append(next_object_id)
        used_object_ids.add(next_object_id)
        next_object_id += 1
    return node_ids, next_object_id


def load_reports(report_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    files = sorted(report_dir.glob("report_*.json"))
    files = [path for path in files if path.name != "report_manifest.json"]
    reports: list[tuple[Path, dict[str, Any]]] = []
    for path in files:
        reports.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return reports


def import_reports(input_path: Path, report_dir: Path, output_path: Path, component_name: str) -> dict[str, Any]:
    original = input_path.read_bytes()
    header_words = [
        int.from_bytes(original[offset : offset + 4], "big")
        for offset in range(0, HEADER_SIZE, 4)
    ]
    payload_len = header_words[4]
    payload_start = HEADER_SIZE
    payload_end = payload_start + payload_len
    if payload_end + FOOTER_SIZE != len(original):
        raise ValueError(
            f"payload length mismatch: header says {payload_len}, file size is {len(original)}"
        )

    root = parse_message(original[payload_start:payload_end], payload_start)
    component_record = find_component_record(root, component_name)
    variables = find_variable_messages(component_record)
    story_variables = [
        variable for variable in variables if extract_variable(variable, 0).get("kind") == "story"
    ]
    reports = load_reports(report_dir)
    if len(reports) != len(story_variables):
        raise ValueError(
            f"report count ({len(reports)}) does not match story variable count ({len(story_variables)})"
        )

    used_object_ids = collect_used_object_ids(story_variables)
    next_object_id = max(used_object_ids) + 1 if used_object_ids else 1
    imported: list[dict[str, Any]] = []

    for variable, (report_path, report) in zip(story_variables, reports):
        summary = extract_variable(variable, 0)
        value_ref_id = summary.get("value_ref_id")
        if not isinstance(value_ref_id, int):
            raise ValueError(f"story variable has no value_ref_id: {summary.get('name')}")

        items = report_items(report)
        node_ids, next_object_id = allocate_node_ids(
            variable, len(items), used_object_ids, next_object_id
        )
        value_field = first_field(children(variable), 4)
        if value_field is None:
            raise ValueError(f"story variable has no value field: {summary.get('name')}")
        value_field["_raw_override"] = encode_story_value(report, value_ref_id, node_ids)
        imported.append(
            {
                "variable": summary.get("name"),
                "report": str(report_path),
                "value_ref_id": value_ref_id,
                "node_count": len(items),
                "first_node_id": node_ids[0] if node_ids else None,
                "last_node_id": node_ids[-1] if node_ids else None,
            }
        )

    new_payload = serialize_message(root)
    new_header = bytearray(original[:HEADER_SIZE])
    new_size = HEADER_SIZE + len(new_payload) + FOOTER_SIZE
    new_header[0:4] = (new_size - 4).to_bytes(4, "big")
    new_header[16:20] = len(new_payload).to_bytes(4, "big")
    output_path.write_bytes(bytes(new_header) + new_payload + original[payload_end:])

    parsed_output = parse_component_variables(output_path, component_name)
    return {
        "input": str(input_path),
        "output": str(output_path),
        "report_dir": str(report_dir),
        "component": parsed_output["component"],
        "imported_count": len(imported),
        "imported": imported,
        "new_size": new_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import report_*.json Story_List values into a GIA component.")
    parser.add_argument("--input", type=Path, required=True, help="input .gia file")
    parser.add_argument("--reports", type=Path, required=True, help="directory containing report_001.json...")
    parser.add_argument("--output", type=Path, required=True, help="output .gia file")
    parser.add_argument("--component", default="第一章", help="component name")
    parser.add_argument("--summary", type=Path, help="write import summary JSON")
    args = parser.parse_args()

    if args.output.exists():
        raise FileExistsError(f"output already exists: {args.output}")
    summary = import_reports(args.input, args.reports, args.output, args.component)
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.summary:
        args.summary.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
