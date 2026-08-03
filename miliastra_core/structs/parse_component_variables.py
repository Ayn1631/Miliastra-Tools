from __future__ import annotations

import argparse
import csv
import json
import struct
from pathlib import Path
from typing import Any


HEADER_SIZE = 20
FOOTER_SIZE = 4


def read_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(data):
            raise ValueError(f"truncated varint at offset {start}")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError(f"varint too long at offset {start}")


def try_read_varint_bytes(data: bytes) -> int | None:
    try:
        value, pos = read_varint(data, 0)
    except ValueError:
        return None
    return value if pos == len(data) else None


def parse_message(data: bytes, base_offset: int = 0, depth: int = 0) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    pos = 0
    while pos < len(data):
        field_offset = base_offset + pos
        key, pos = read_varint(data, pos)
        field_no = key >> 3
        wire_type = key & 7
        field: dict[str, Any] = {
            "offset": field_offset,
            "field": field_no,
            "wire_type": wire_type,
        }

        if wire_type == 0:
            value, pos = read_varint(data, pos)
            field["value"] = value
        elif wire_type == 1:
            raw = data[pos : pos + 8]
            if len(raw) != 8:
                raise ValueError(f"truncated fixed64 at offset {field_offset}")
            pos += 8
            field["raw"] = raw
            field["double_le"] = struct.unpack("<d", raw)[0]
        elif wire_type == 2:
            length, pos = read_varint(data, pos)
            raw = data[pos : pos + length]
            if len(raw) != length:
                raise ValueError(f"truncated length-delimited field at offset {field_offset}")
            pos += length
            field["length"] = length
            field["raw"] = raw
            try:
                field["utf8"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
            if raw and depth < 64:
                try:
                    field["children"] = parse_message(raw, base_offset + pos - length, depth + 1)
                except ValueError:
                    pass
        elif wire_type == 5:
            raw = data[pos : pos + 4]
            if len(raw) != 4:
                raise ValueError(f"truncated fixed32 at offset {field_offset}")
            pos += 4
            field["raw"] = raw
            field["uint32_le"] = struct.unpack("<I", raw)[0]
            field["float_le"] = struct.unpack("<f", raw)[0]
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type} at offset {field_offset}")

        fields.append(field)
    return fields


def children(field: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not field:
        return []
    return field.get("children", [])


def all_fields(fields: list[dict[str, Any]], field_no: int) -> list[dict[str, Any]]:
    return [field for field in fields if field["field"] == field_no]


def first_field(fields: list[dict[str, Any]], field_no: int) -> dict[str, Any] | None:
    return next((field for field in fields if field["field"] == field_no), None)


def varint(fields: list[dict[str, Any]], field_no: int) -> int | None:
    field = first_field(fields, field_no)
    return field.get("value") if field else None


def utf8(fields: list[dict[str, Any]], field_no: int) -> str | None:
    field = first_field(fields, field_no)
    return field.get("utf8") if field else None


def strings_from_repeated_field(message: dict[str, Any] | None, field_no: int = 1) -> list[str]:
    return [
        field["utf8"]
        for field in all_fields(children(message), field_no)
        if isinstance(field.get("utf8"), str)
    ]


def nested_varint(message: dict[str, Any] | None, path: list[int]) -> int | None:
    fields = children(message)
    current: dict[str, Any] | None = None
    for field_no in path:
        current = first_field(fields, field_no)
        if current is None:
            return None
        fields = children(current)
    return current.get("value") if current else None


def extract_slot(slot: dict[str, Any]) -> dict[str, Any]:
    fields = children(slot)
    type_code = varint(fields, 1)
    result: dict[str, Any] = {
        "type_code": type_code,
        "offset": slot["offset"],
    }

    if type_code == 6:
        result["name"] = "status"
        result["values"] = strings_from_repeated_field(first_field(fields, 16))
    elif type_code == 11:
        result["name"] = "str"
        result["values"] = strings_from_repeated_field(first_field(fields, 21))
    elif type_code == 8:
        result["name"] = "next"
        raw_values: list[dict[str, Any]] = []
        for item in all_fields(children(first_field(fields, 18)), 1):
            raw = item.get("raw", b"")
            raw_values.append(
                {
                    "raw_hex": raw.hex(" "),
                    "varint": try_read_varint_bytes(raw),
                    "utf8": item.get("utf8"),
                }
            )
        result["values"] = raw_values
    else:
        result["name"] = f"field_{type_code}" if type_code is not None else "unknown"
        values: list[str] = []
        for candidate_no in (16, 18, 21):
            values.extend(strings_from_repeated_field(first_field(fields, candidate_no)))
        if values:
            result["values"] = values

    return result


def extract_story_nodes(story_blob: dict[str, Any] | None) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for node in all_fields(children(story_blob), 1):
        node_fields = children(node)
        payload = first_field(node_fields, 35)
        slots = [extract_slot(slot) for slot in all_fields(children(payload), 1)]
        text_values = [
            value
            for slot in slots
            if slot.get("name") == "str"
            for value in slot.get("values", [])
        ]
        status_values = [
            value
            for slot in slots
            if slot.get("name") == "status"
            for value in slot.get("values", [])
        ]
        nodes.append(
            {
                "offset": node["offset"],
                "object_id": nested_varint(payload, [502, 4]),
                "status": status_values[0] if status_values else None,
                "text": text_values[0] if text_values else None,
                "slots": slots,
            }
        )
    return nodes


def extract_variable(variable: dict[str, Any], index: int) -> dict[str, Any]:
    fields = children(variable)
    type_code = varint(fields, 3)
    value = first_field(fields, 4)
    value_fields = children(value)
    result: dict[str, Any] = {
        "index": index,
        "offset": variable["offset"],
        "name": utf8(fields, 2),
        "type_code": type_code,
        "enabled_or_locked": varint(fields, 5),
    }

    if type_code == 11:
        list_field = first_field(value_fields, 21)
        values = strings_from_repeated_field(list_field)
        result.update(
            {
                "kind": "string_list",
                "values": values,
                "value_count": len(values),
            }
        )
        return result

    if type_code == 25:
        custom_group = first_field(value_fields, 35)
        custom_value = first_field(children(custom_group), 1)
        story_blob = first_field(children(custom_value), 36)
        story_nodes = extract_story_nodes(story_blob)
        result.update(
            {
                "kind": "story",
                "value_kind": utf8(children(custom_value), 501),
                "owner_id": varint(children(custom_group), 501),
                "value_ref_id": nested_varint(custom_group, [502, 4]),
                "story_node_count": len(story_nodes),
                "story_nodes": story_nodes,
            }
        )
        return result

    result["kind"] = "unknown"
    return result


def find_variable_messages(component_record: dict[str, Any]) -> list[dict[str, Any]]:
    record_fields = children(component_record)
    data = first_field(children(first_field(record_fields, 11)), 1)
    for block in all_fields(children(data), 8):
        block_fields = children(block)
        if varint(block_fields, 1) == 1 and varint(block_fields, 2) == 1:
            variable_list = first_field(block_fields, 11)
            if variable_list:
                return all_fields(children(variable_list), 1)
    return []


def parse_component_variables(path: Path, component_name: str | None = None) -> dict[str, Any]:
    data = path.read_bytes()
    if len(data) < HEADER_SIZE + FOOTER_SIZE:
        raise ValueError("file is too small to be a GIA container")

    header_words = [
        int.from_bytes(data[offset : offset + 4], "big")
        for offset in range(0, HEADER_SIZE, 4)
    ]
    payload_len = header_words[4]
    payload_end = HEADER_SIZE + payload_len
    if payload_end + FOOTER_SIZE != len(data):
        raise ValueError(
            f"payload length mismatch: header says {payload_len}, file size is {len(data)}"
        )

    payload = data[HEADER_SIZE:payload_end]
    root = parse_message(payload, HEADER_SIZE)
    records = all_fields(root, 1)
    if component_name:
        component_record = next(
            (
                record
                for record in records
                if utf8(children(record), 3) == component_name
            ),
            None,
        )
        if component_record is None:
            raise ValueError(f"component not found: {component_name}")
    else:
        component_record = records[0] if records else None
    if component_record is None:
        raise ValueError("no component record found")

    record_fields = children(component_record)
    variables = [
        extract_variable(variable, index)
        for index, variable in enumerate(find_variable_messages(component_record), start=1)
    ]

    return {
        "file": str(path),
        "size": len(data),
        "container": {
            "header_u32_be": header_words,
            "payload_offset": HEADER_SIZE,
            "payload_size": payload_len,
            "footer_u32_be": int.from_bytes(data[payload_end:], "big"),
            "footer_hex": data[payload_end:].hex(" "),
        },
        "component": {
            "name": utf8(record_fields, 3),
            "id": nested_varint(component_record, [1, 4]),
            "variable_count": len(variables),
        },
        "variables": variables,
        "source_path": utf8(root, 3),
        "version": utf8(root, 5),
    }


def write_csv(parsed: dict[str, Any], path: Path) -> None:
    rows: list[dict[str, Any]] = []
    for variable in parsed["variables"]:
        first_text = None
        if variable.get("story_nodes"):
            first_text = next(
                (node.get("text") for node in variable["story_nodes"] if node.get("text")),
                None,
            )
        rows.append(
            {
                "index": variable["index"],
                "name": variable["name"],
                "kind": variable["kind"],
                "type_code": variable["type_code"],
                "value_ref_id": variable.get("value_ref_id"),
                "value_count": variable.get("value_count", ""),
                "story_node_count": variable.get("story_node_count", ""),
                "first_text": first_text or "",
            }
        )

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "index",
                "name",
                "kind",
                "type_code",
                "value_ref_id",
                "value_count",
                "story_node_count",
                "first_text",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract component variables from observed GIA files.")
    parser.add_argument("path", type=Path, help="input .gia file")
    parser.add_argument("--component", help="component name; defaults to the first root record")
    parser.add_argument("--json", type=Path, help="write full JSON result to this path")
    parser.add_argument("--csv", type=Path, help="write a compact CSV summary to this path")
    args = parser.parse_args()

    parsed = parse_component_variables(args.path, args.component)
    output = json.dumps(parsed, ensure_ascii=False, indent=2)
    if args.json:
        args.json.write_text(output + "\n", encoding="utf-8")
    if args.csv:
        write_csv(parsed, args.csv)
    if not args.json:
        print(output)


if __name__ == "__main__":
    main()
