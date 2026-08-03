from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path
from typing import Any

from .components_validator import validate_components_document
from .miliastra_container import MiliastraContainer
from .gil_common import (
    FOOTER_SIZE,
    HEADER_SIZE,
    TYPE_INFO,
    children,
    clone_field,
    collect_varints,
    field_strings,
    find_record_containing,
    find_variable_list_field,
    first_field,
    key,
    length_field,
    load_gil,
    mark_dirty,
    next_free_id,
    protobuf_type_ref,
    repeated_float_payload,
    repeated_string_payload,
    repeated_varint_payload,
    replace_exact_utf8,
    replace_varint_values,
    root_children,
    set_first_varint_child,
    set_ref_id,
    set_utf8,
    string_field,
    top_field,
    type_code_from_spec,
    validate_gil_bytes,
    variable_name,
    variable_type_code,
    varint,
    varint_field,
    write_gil,
)


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = SCRIPTS_DIR.parent
DEFAULT_TEMPLATE_GIL = PROJECT_DIR / "Template" / "Template.gil"


class BuildContext:
    def __init__(
        self,
        structs: dict[str, Any],
        used_ids: set[int],
        value_ref_start: int,
        dict_entry_type_refs: dict[tuple[int, int, int | None], tuple[bytes, int]] | None = None,
    ) -> None:
        self.structs = structs
        self.used_ids = used_ids
        self.value_ref_next = value_ref_start
        self.dict_entry_type_refs = dict_entry_type_refs or {}
        self.warnings: list[str] = []

    def alloc_value_ref(self) -> int:
        value = next_free_id(self.used_ids, self.value_ref_next)
        self.value_ref_next = value + 1
        return value

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_struct_ref(ctx: BuildContext, struct_ref: Any) -> int:
    if isinstance(struct_ref, int):
        return struct_ref
    text = str(struct_ref)
    if text.isdigit():
        return int(text)
    structs_by_name = ctx.structs.get("structs_by_name", {})
    if text in structs_by_name:
        return int(structs_by_name[text])
    raise ValueError(f"struct not found in extracted structs: {struct_ref}")


def resolve_struct_id(ctx: BuildContext, spec: dict[str, Any]) -> int:
    for key_name in (
        "structId",
        "struct_id",
        "elementStructId",
        "element_struct_id",
        "value_structId",
        "value_struct_id",
    ):
        if key_name in spec:
            return resolve_struct_ref(ctx, spec[key_name])
    if "struct" in spec:
        return resolve_struct_ref(ctx, spec["struct"])
    default_id = next(iter(ctx.structs.get("structs_by_name", {}).values()), None)
    if default_id is None:
        raise ValueError("struct variable requires struct/struct_id, and no extracted struct is available")
    ctx.warn(f"struct variable used first extracted struct id by default: {default_id}")
    return int(default_id)


def optional_struct_id(ctx: BuildContext, spec: dict[str, Any]) -> int | None:
    for key_name in (
        "structId",
        "struct_id",
        "elementStructId",
        "element_struct_id",
        "value_structId",
        "value_struct_id",
        "dict_value_struct_id",
    ):
        if key_name in spec and spec[key_name] not in (None, ""):
            return resolve_struct_ref(ctx, spec[key_name])
    struct_ref = spec.get("struct")
    if struct_ref in (None, ""):
        return None
    return resolve_struct_id(ctx, {"struct": struct_ref})


def normalize_dict_spec(spec: dict[str, Any], value: Any) -> dict[str, Any]:
    dict_spec = dict(spec)
    if isinstance(value, dict):
        for key_name in (
            "key_type",
            "key_type_code",
            "value_type",
            "value_type_code",
            "value_struct_id",
            "value_structId",
            "dict_value_struct_id",
            "struct",
            "struct_id",
            "entries",
        ):
            if key_name in value:
                dict_spec[key_name] = value[key_name]
    return dict_spec


def protobuf_dict_type_ref(key_code: int, value_code: int) -> bytes:
    return varint_field(1, 27) + length_field(
        2,
        varint_field(2, 66) + varint_field(502, key_code) + varint_field(503, value_code),
    )


def fallback_dict_entry_type_ref(key_code: int, value_code: int, value_struct_id: int | None) -> tuple[bytes, int]:
    marker = 66
    body = varint_field(2, marker) + varint_field(502, key_code) + varint_field(503, value_code)
    if value_struct_id is not None:
        body += varint_field(504, value_struct_id)
    return varint_field(1, 25) + length_field(2, body), marker


def dict_entry_type_ref(
    ctx: BuildContext,
    key_code: int,
    value_code: int,
    value_struct_id: int | None,
) -> tuple[bytes, int]:
    for key_ref in ((key_code, value_code, value_struct_id), (key_code, value_code, None)):
        matched = ctx.dict_entry_type_refs.get(key_ref)
        if matched is not None:
            return matched
    ctx.warn(
        f"dict entry type_ref sample not found for key={key_code}, value={value_code}; used fallback marker"
    )
    return fallback_dict_entry_type_ref(key_code, value_code, value_struct_id)


def struct_def_by_id(ctx: BuildContext, struct_id: int) -> dict[str, Any] | None:
    return ctx.structs.get("structs_by_id", {}).get(str(struct_id))


def vector_components(value: Any) -> tuple[float, float, float]:
    if isinstance(value, dict):
        return (
            float(value.get("x") or value.get("X") or 0.0),
            float(value.get("y") or value.get("Y") or 0.0),
            float(value.get("z") or value.get("Z") or 0.0),
        )
    if isinstance(value, (list, tuple)):
        values = list(value)[:3]
        values += [0.0] * (3 - len(values))
        return (float(values[0] or 0.0), float(values[1] or 0.0), float(values[2] or 0.0))
    return (0.0, 0.0, 0.0)


def vector_payload(value: Any) -> bytes:
    x, y, z = vector_components(value)
    if x == 0.0 and y == 0.0 and z == 0.0:
        return b""
    return (
        key(1, 5)
        + struct.pack("<f", x)
        + key(2, 5)
        + struct.pack("<f", y)
        + key(3, 5)
        + struct.pack("<f", z)
    )


def encode_scalar_slot(type_code: int, value: Any, ctx: BuildContext) -> bytes:
    if type_code == 6:
        return repeated_string_payload(["" if value is None else value])
    if type_code == 11:
        return repeated_string_payload(list(value or []))
    if type_code == 3:
        return b"" if int(value or 0) == 0 else varint_field(1, int(value))
    if type_code == 8:
        return repeated_varint_payload(list(value or []))
    if type_code == 5:
        number = float(value or 0.0)
        return b"" if number == 0.0 else key(1, 5) + struct.pack("<f", number)
    if type_code == 10:
        return repeated_float_payload(list(value or []))
    if type_code == 4:
        return varint_field(1, 1) if bool(value) else b""
    if type_code == 9:
        return repeated_varint_payload([1 if item else 0 for item in list(value or [])])
    if type_code == 17:
        return b"" if int(value or 0) == 0 else varint_field(1, int(value))
    if type_code == 24:
        return repeated_varint_payload(list(value or []))
    if type_code == 12:
        return length_field(1, vector_payload(value))
    if type_code == 15:
        items = value if isinstance(value, list) else []
        return b"".join(length_field(1, vector_payload(item)) for item in items)
    if type_code in (2, 7, 12, 13, 15, 20, 21, 22, 23, 1):
        if value not in (None, "", [], {}):
            ctx.warn(
                f"type {TYPE_INFO[type_code]['name']} non-default value encoding is not fully confirmed; template default was used"
            )
        if type_code in (20, 21, 22, 23):
            return length_field(1, b"")
        if type_code in (7, 13):
            return repeated_varint_payload([0])
        return b""
    raise ValueError(f"unsupported scalar/list type_code: {type_code}")


def encode_value_message(
    type_code: int,
    value: Any,
    ctx: BuildContext,
    *,
    field_name: str | None = None,
    struct_id: int | None = None,
    variable_spec: dict[str, Any] | None = None,
) -> bytes:
    spec = variable_spec or {}
    effective_struct_id = struct_id
    if type_code in (25, 26):
        effective_struct_id = resolve_struct_id(ctx, spec) if effective_struct_id is None else effective_struct_id

    if type_code == 27:
        dict_spec = normalize_dict_spec(spec, value)
        key_code = type_code_from_name_or_code(dict_spec, "key_type", "key_type_code", default=6)
        value_code = type_code_from_name_or_code(dict_spec, "value_type", "value_type_code", default=6)
        type_ref = protobuf_dict_type_ref(key_code, value_code)
    else:
        type_ref = protobuf_type_ref(type_code, effective_struct_id)
    result = varint_field(1, type_code) + length_field(2, type_ref)

    if type_code == 25:
        if not isinstance(value, dict):
            value = {}
        result += length_field(35, encode_struct_payload(ctx, effective_struct_id, value))
    elif type_code == 26:
        if isinstance(value, list):
            items = value
        elif isinstance(value, dict):
            items = [value]
        else:
            items = []
        body = b"".join(
            length_field(
                1,
                encode_value_message(25, item, ctx, struct_id=effective_struct_id, variable_spec=spec),
            )
            for item in items
            if isinstance(item, dict)
        )
        body += varint_field(501, effective_struct_id)
        result += length_field(36, body)
    elif type_code == 27:
        result += length_field(37, encode_dict_payload(spec, value, ctx))
    else:
        value_field = TYPE_INFO[type_code]["value_field"]
        result += length_field(value_field, encode_scalar_slot(type_code, value, ctx))

    if field_name:
        result += string_field(501, field_name)
    return result


def encode_struct_payload(ctx: BuildContext, struct_id: int, values: dict[str, Any]) -> bytes:
    struct_def = struct_def_by_id(ctx, struct_id)
    if not struct_def:
        ctx.warn(f"struct schema not found for id {struct_id}; encoded empty struct payload")
        return varint_field(501, struct_id) + length_field(
            502,
            varint_field(2, 28) + varint_field(4, ctx.alloc_value_ref()),
        )

    payload = bytearray()
    for field in struct_def.get("fields", []):
        field_name = field.get("name")
        if not field_name:
            continue
        type_code = int(field["type_code"])
        field_value = values.get(field_name)
        field_spec: dict[str, Any] = dict(field)
        if "element_struct_id" in field:
            field_spec["struct_id"] = field["element_struct_id"]
        elif "struct_id" in field:
            field_spec["struct_id"] = field["struct_id"]
        if "dict_key_type_code" in field:
            field_spec["key_type_code"] = field["dict_key_type_code"]
        if "dict_value_type_code" in field:
            field_spec["value_type_code"] = field["dict_value_type_code"]
        if "dict_value_struct_id" in field:
            field_spec["value_struct_id"] = field["dict_value_struct_id"]
        field_structs = values.get("__struct_types__", {})
        if isinstance(field_structs, dict) and field_name in field_structs:
            field_spec["struct"] = field_structs[field_name]
        payload += length_field(
            1,
            encode_value_message(type_code, field_value, ctx, field_name=field_name, variable_spec=field_spec),
        )

    payload += varint_field(501, struct_id)
    payload += length_field(502, varint_field(2, 28) + varint_field(4, ctx.alloc_value_ref()))
    return bytes(payload)


def encode_dict_payload(spec: dict[str, Any], value: Any, ctx: BuildContext) -> bytes:
    dict_spec = normalize_dict_spec(spec, value)
    key_code = type_code_from_name_or_code(dict_spec, "key_type", "key_type_code", default=6)
    value_code = type_code_from_name_or_code(dict_spec, "value_type", "value_type_code", default=6)
    value_struct_id = optional_struct_id(ctx, dict_spec) if value_code in (25, 26) else None
    entries = value.get("entries", []) if isinstance(value, dict) else dict_spec.get("entries", [])
    if entries:
        ctx.warn("dict custom entries are experimental; verify generated file in editor before using it as a base")
    payload = bytearray()
    key_messages: list[bytes] = []
    value_messages: list[bytes] = []
    entry_type_ref, entry_marker = dict_entry_type_ref(ctx, key_code, value_code, value_struct_id)
    for entry in entries if isinstance(entries, list) else []:
        key_value = entry.get("key") if isinstance(entry, dict) else None
        item_value = entry.get("value") if isinstance(entry, dict) else None
        key_message = encode_value_message(key_code, key_value, ctx)
        value_message = encode_value_message(
            value_code,
            item_value,
            ctx,
            struct_id=value_struct_id,
            variable_spec=dict_spec,
        )
        key_messages.append(key_message)
        value_messages.append(value_message)
        entry_struct = (
            length_field(1, key_message)
            + length_field(1, value_message)
            + varint_field(501, entry_marker)
            + length_field(502, varint_field(2, 28) + varint_field(4, ctx.alloc_value_ref()))
        )
        entry_message = (
            varint_field(1, 25)
            + length_field(2, entry_type_ref)
            + length_field(35, entry_struct)
        )
        payload += length_field(1, entry_message)
    payload += b"".join(length_field(501, item) for item in key_messages)
    payload += b"".join(length_field(502, item) for item in value_messages)
    payload += varint_field(503, key_code)
    payload += varint_field(504, value_code)
    if value_struct_id is not None:
        payload += varint_field(505, value_struct_id)
    return bytes(payload)


def type_code_from_name_or_code(spec: dict[str, Any], name_key: str, code_key: str, default: int) -> int:
    if code_key in spec:
        return int(spec[code_key])
    if name_key in spec:
        return type_code_from_spec({"type": spec[name_key]})
    return default


def collect_dict_entry_type_refs(root: list[dict[str, Any]]) -> dict[tuple[int, int, int | None], tuple[bytes, int]]:
    refs: dict[tuple[int, int, int | None], tuple[bytes, int]] = {}

    def visit(field: dict[str, Any]) -> None:
        if field.get("field") == 37:
            field_children = children(field)
            key_code = varint(field_children, 503)
            value_code = varint(field_children, 504)
            value_struct_id = varint(field_children, 505)
            first_entry = first_field(field_children, 1)
            type_ref_field = first_field(children(first_entry), 2) if first_entry else None
            if isinstance(key_code, int) and isinstance(value_code, int) and type_ref_field is not None:
                marker = None
                inner_type_ref = first_field(children(type_ref_field), 2)
                if inner_type_ref is not None:
                    marker = varint(children(inner_type_ref), 2)
                if isinstance(marker, int):
                    key_ref = (key_code, value_code, value_struct_id if isinstance(value_struct_id, int) else None)
                    refs.setdefault(key_ref, (type_ref_field.get("raw", b""), marker))
                    refs.setdefault((key_code, value_code, None), (type_ref_field.get("raw", b""), marker))
        for child in children(field):
            visit(child)

    for item in root:
        visit(item)
    return refs


def collect_component_templates(template_gil: Path, template_name: str) -> dict[str, Any]:
    _, root = load_gil(template_gil)
    definition = find_record_containing(root_children(root, 4), template_name)
    index = find_record_containing(root_children(root, 8), template_name)
    definition_id = first_field(children(definition), 1)
    index_id = first_field(children(index), 1)
    if definition_id is None or index_id is None:
        raise ValueError("template component ids not found")

    variable_templates: dict[int, dict[str, Any]] = {}
    dict_templates: dict[tuple[int, int], dict[str, Any]] = {}
    for variable in children(find_variable_list_field(definition)):
        if variable.get("field") != 1:
            continue
        type_code = variable_type_code(variable)
        if not isinstance(type_code, int):
            continue
        variable_templates.setdefault(type_code, variable)
        if type_code == 27:
            key_value = inspect_dict_key_value_codes(variable)
            if key_value:
                dict_templates[key_value] = variable

    return {
        "definition": definition,
        "index": index,
        "name": template_name,
        "definition_id": int(definition_id["value"]),
        "index_id": int(index_id["value"]),
        "variable_templates": variable_templates,
        "dict_templates": dict_templates,
        "dict_entry_type_refs": collect_dict_entry_type_refs(root),
    }


def inspect_dict_key_value_codes(variable: dict[str, Any]) -> tuple[int, int] | None:
    value = first_field(children(variable), 4)
    dict_value = first_field(children(value), 37) if value else None
    key_code = first_field(children(dict_value), 503) if dict_value else None
    value_code = first_field(children(dict_value), 504) if dict_value else None
    if key_code and value_code and isinstance(key_code.get("value"), int) and isinstance(value_code.get("value"), int):
        return int(key_code["value"]), int(value_code["value"])
    return None


def choose_variable_template(templates: dict[str, Any], variable_spec: dict[str, Any]) -> dict[str, Any]:
    type_code = type_code_from_spec(variable_spec)
    if type_code == 27:
        key_code = type_code_from_name_or_code(variable_spec, "key_type", "key_type_code", default=6)
        value_code = type_code_from_name_or_code(variable_spec, "value_type", "value_type_code", default=6)
        matched = templates["dict_templates"].get((key_code, value_code))
        if matched is not None:
            return matched
    if type_code not in templates["variable_templates"]:
        raise ValueError(f"template variable for type_code {type_code} was not found in Template.gil")
    return templates["variable_templates"][type_code]


def build_variable(variable_spec: dict[str, Any], templates: dict[str, Any], ctx: BuildContext) -> dict[str, Any]:
    type_code = type_code_from_spec(variable_spec)
    variable = clone_field(choose_variable_template(templates, variable_spec))
    name_field = first_field(children(variable), 2)
    type_field = first_field(children(variable), 3)
    value_field = first_field(children(variable), 4)
    type_ref_field = first_field(children(variable), 6)
    if name_field is None or type_field is None or value_field is None:
        raise ValueError(f"invalid variable template for spec: {variable_spec}")

    set_utf8(name_field, str(variable_spec["name"]))
    type_field["value"] = type_code
    mark_dirty(type_field)
    value = variable_spec.get("value")
    value_field["_raw_override"] = encode_value_message(type_code, value, ctx, variable_spec=variable_spec)
    if type_ref_field is not None and type_code in (25, 26):
        type_ref_field["_raw_override"] = protobuf_type_ref(type_code, resolve_struct_id(ctx, variable_spec))
        mark_dirty(type_ref_field)
    elif type_ref_field is not None and type_code == 27:
        dict_spec = normalize_dict_spec(variable_spec, value)
        key_code = type_code_from_name_or_code(dict_spec, "key_type", "key_type_code", default=6)
        value_code = type_code_from_name_or_code(dict_spec, "value_type", "value_type_code", default=6)
        type_ref_field["_raw_override"] = protobuf_dict_type_ref(key_code, value_code)
        mark_dirty(type_ref_field)
    elif type_ref_field is not None and type_code != 27:
        type_ref_field["_raw_override"] = protobuf_type_ref(type_code)
        mark_dirty(type_ref_field)
    mark_dirty(variable)
    return variable


def replace_variables(record: dict[str, Any], variables: list[dict[str, Any]]) -> None:
    variable_list = find_variable_list_field(record)
    variable_list["children"] = variables
    variable_list.pop("_raw_override", None)
    mark_dirty(variable_list)
    mark_dirty(record)


def clone_component_records(
    component_spec: dict[str, Any],
    templates: dict[str, Any],
    ctx: BuildContext,
    definition_id: int,
    index_id: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    component_name = str(component_spec["name"])
    variables = [
        build_variable(variable_spec, templates, ctx)
        for variable_spec in component_spec.get("variables", [])
    ]

    definition = clone_field(templates["definition"])
    index = clone_field(templates["index"])

    set_first_varint_child(definition, 1, definition_id)
    set_first_varint_child(index, 1, index_id)
    set_ref_id(index, definition_id)

    replace_exact_utf8(definition, templates["name"], component_name)
    replace_exact_utf8(index, templates["name"], component_name)
    replace_varint_values(
        definition,
        {templates["definition_id"]: definition_id, templates["index_id"]: index_id},
    )
    replace_varint_values(
        index,
        {templates["definition_id"]: definition_id, templates["index_id"]: index_id},
    )

    replace_variables(definition, [clone_field(variable) for variable in variables])
    replace_variables(index, [clone_field(variable) for variable in variables])
    return definition, index, variables


def build_components(spec_path: Path) -> dict[str, Any]:
    spec = load_json(spec_path)
    base_dir = spec_path.parent
    input_gil = (base_dir / spec["input_gil"]).resolve()
    output_gil = (base_dir / spec["output_gil"]).resolve()
    template_gil = (base_dir / spec.get("template_gil", str(DEFAULT_TEMPLATE_GIL))).resolve()
    structs_json = spec.get("structs_json")
    structs_path = (base_dir / structs_json).resolve() if structs_json else None
    structs = load_json(structs_path)
    validate_components_document({"components": spec.get("components", [])}, structs)

    # The legacy semantic editor below still operates on its established
    # field dictionaries. Guard both sides with the shared lossless container
    # so malformed sizes, unknown wire fields and noncanonical encodings are
    # checked before and after the targeted f4/f8 mutation.
    MiliastraContainer.load(input_gil).validate_roundtrip()
    original, root = load_gil(input_gil)
    templates = collect_component_templates(template_gil, spec.get("template_component", "Template"))
    used_ids = collect_varints(root)
    id_policy = spec.get("id_policy", {})
    component_start = int(id_policy.get("component_definition_start", 1077936200))
    index_start = int(id_policy.get("component_index_start", 1077936300))
    value_ref_start = int(id_policy.get("value_ref_start", 1073742200))
    ctx = BuildContext(structs, used_ids, value_ref_start, templates.get("dict_entry_type_refs", {}))

    f4_records = root_children(root, 4)
    f8_records = root_children(root, 8)
    generated: list[dict[str, Any]] = []

    for component_spec in spec.get("components", []):
        definition_id = next_free_id(used_ids, component_start)
        component_start = definition_id + 1
        index_id = next_free_id(used_ids, index_start)
        index_start = index_id + 1

        definition, index, variables = clone_component_records(
            component_spec,
            templates,
            ctx,
            definition_id,
            index_id,
        )
        f4_records.append(definition)
        f8_records.append(index)

        generated.append(
            {
                "name": component_spec["name"],
                "definition_id": definition_id,
                "index_id": index_id,
                "variable_count": len(variables),
                "variables": [
                    {
                        "name": variable_name(variable),
                        "type_code": variable_type_code(variable),
                        "type": TYPE_INFO.get(variable_type_code(variable) or -1, {}).get("name"),
                    }
                    for variable in variables
                ],
            }
        )

    for field_no in (4, 8):
        field = top_field(root, field_no)
        field.pop("_raw_override", None)
        mark_dirty(field)

    if output_gil.exists() and not spec.get("overwrite", False):
        raise FileExistsError(f"output already exists: {output_gil}")
    output_gil.parent.mkdir(parents=True, exist_ok=True)
    written = write_gil(original, root, output_gil)
    validate_gil_bytes(output_gil.read_bytes())
    MiliastraContainer.load(output_gil).validate_roundtrip()

    summary = {
        "input": str(input_gil),
        "template_gil": str(template_gil),
        "structs_json": str(structs_path) if structs_path else None,
        **written,
        "generated_count": len(generated),
        "generated": generated,
        "warnings": ctx.warnings,
        "note": "Only root.f4 and root.f8 are appended by default; root.f5 scene instances and root.f10 struct schemas are preserved.",
    }
    summary_path = output_gil.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Create components and variables in a Qianxing .gil project.")
    parser.add_argument("spec", type=Path, help="workflow/component generation JSON")
    args = parser.parse_args()
    print(json.dumps(build_components(args.spec), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
