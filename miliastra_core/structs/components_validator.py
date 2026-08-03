from __future__ import annotations

import math
from typing import Any

from .gil_common import TYPE_INFO, type_code_from_spec


class ComponentsValidationError(ValueError):
    pass


DEFAULT_ONLY_TYPES = {1, 2, 7, 13, 20, 21, 22, 23}


def _fail(path: str, message: str) -> None:
    raise ComponentsValidationError(f"{path}: {message}")


def _validate_text(value: Any, path: str) -> None:
    if not isinstance(value, str):
        _fail(path, f"expected string, got {type(value).__name__}")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _fail(path, "string contains an invalid Unicode surrogate and cannot be encoded as UTF-8")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ComponentsValidationError(f"{path}: string cannot be encoded as UTF-8") from exc


def _validate_int(value: Any, path: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(path, f"expected integer, got {type(value).__name__}")


def _validate_float(value: Any, path: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail(path, f"expected number, got {type(value).__name__}")
    if not math.isfinite(float(value)):
        _fail(path, "number must be finite")


def _validate_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, f"expected array, got {type(value).__name__}")
    return value


def _struct_maps(structs: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    raw_structs = structs.get("structs", [])
    if not raw_structs and isinstance(structs.get("structs_by_id"), dict):
        raw_structs = [
            item
            for item in structs["structs_by_id"].values()
            if isinstance(item, dict)
        ]
    by_id = {
        str(item["id"]): item
        for item in raw_structs
        if isinstance(item, dict) and item.get("id") is not None
    }
    by_name = {
        str(item["name"]): item
        for item in raw_structs
        if isinstance(item, dict) and item.get("name")
    }
    return by_id, by_name


def _resolve_struct(
    spec: dict[str, Any],
    structs: dict[str, Any],
    path: str,
    *,
    fallback_ref: Any = None,
) -> dict[str, Any]:
    ref = fallback_ref
    for key in (
        "struct",
        "struct_id",
        "structId",
        "element_struct_id",
        "elementStructId",
        "value_struct_id",
        "value_structId",
        "dict_value_struct_id",
    ):
        if spec.get(key) not in (None, ""):
            ref = spec[key]
            break
    if ref in (None, ""):
        _fail(path, "missing nested struct reference")
    by_id, by_name = _struct_maps(structs)
    result = by_name.get(str(ref)) or by_id.get(str(ref))
    if result is None:
        _fail(path, f"struct {ref!r} was not found in the uploaded schema")
    return result


def _type_code(spec: dict[str, Any], path: str) -> int:
    try:
        code = type_code_from_spec(spec)
    except (TypeError, ValueError) as exc:
        raise ComponentsValidationError(f"{path}: {exc}") from exc
    if code not in TYPE_INFO:
        _fail(path, f"unsupported type_code {code}")
    return code


def _dict_spec(spec: dict[str, Any], value: Any) -> dict[str, Any]:
    result = dict(spec)
    if isinstance(value, dict):
        for key in (
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
            if key in value:
                result[key] = value[key]
    return result


def _dict_code(spec: dict[str, Any], name_key: str, code_key: str, path: str) -> int:
    if code_key in spec:
        return _type_code({"type_code": spec[code_key]}, path)
    if name_key in spec:
        return _type_code({"type": spec[name_key]}, path)
    return 6


def _validate_struct_value(
    value: Any,
    struct_doc: dict[str, Any],
    structs: dict[str, Any],
    path: str,
) -> None:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        _fail(path, f"expected object for struct {struct_doc.get('name')}, got {type(value).__name__}")
    field_map = {
        str(field.get("name")): field
        for field in struct_doc.get("fields", [])
        if isinstance(field, dict) and field.get("name")
    }
    metadata = {"__struct_types__", "__struct__", "__struct_id__"}
    unknown = sorted(str(key) for key in value if key not in field_map and key not in metadata)
    if unknown:
        _fail(path, f"fields are not defined by struct {struct_doc.get('name')}: {unknown}")
    overrides = value.get("__struct_types__", {})
    if overrides is not None and not isinstance(overrides, dict):
        _fail(f"{path}.__struct_types__", "expected object")

    for field_name, field in field_map.items():
        if field_name not in value:
            continue
        field_spec = dict(field)
        if isinstance(overrides, dict) and field_name in overrides:
            field_spec["struct"] = overrides[field_name]
        _validate_value(
            int(field["type_code"]),
            value[field_name],
            field_spec,
            structs,
            f"{path}.{field_name}",
        )


def _validate_dict(value: Any, spec: dict[str, Any], structs: dict[str, Any], path: str) -> None:
    dict_spec = _dict_spec(spec, value)
    key_code = _dict_code(dict_spec, "key_type", "key_type_code", f"{path}.key_type")
    value_code = _dict_code(dict_spec, "value_type", "value_type_code", f"{path}.value_type")
    if value_code in (25, 26):
        _resolve_struct(dict_spec, structs, f"{path}.value_type")
    entries = value.get("entries", []) if isinstance(value, dict) else dict_spec.get("entries", [])
    if entries is None:
        entries = []
    entries = _validate_list(entries, f"{path}.entries")
    for index, entry in enumerate(entries):
        entry_path = f"{path}.entries[{index}]"
        if not isinstance(entry, dict):
            _fail(entry_path, "expected object")
        if "key" not in entry or "value" not in entry:
            _fail(entry_path, "dictionary entry requires key and value")
        _validate_value(key_code, entry["key"], {}, structs, f"{entry_path}.key")
        _validate_value(value_code, entry["value"], dict_spec, structs, f"{entry_path}.value")


def _validate_value(
    type_code: int,
    value: Any,
    spec: dict[str, Any],
    structs: dict[str, Any],
    path: str,
) -> None:
    if value is None:
        return
    if type_code == 6:
        _validate_text(value, path)
    elif type_code == 11:
        for index, item in enumerate(_validate_list(value, path)):
            _validate_text(item, f"{path}[{index}]")
    elif type_code in (3, 17):
        _validate_int(value, path)
    elif type_code in (8, 24):
        for index, item in enumerate(_validate_list(value, path)):
            _validate_int(item, f"{path}[{index}]")
    elif type_code == 5:
        _validate_float(value, path)
    elif type_code == 10:
        for index, item in enumerate(_validate_list(value, path)):
            _validate_float(item, f"{path}[{index}]")
    elif type_code == 4:
        if not isinstance(value, bool):
            _fail(path, f"expected boolean, got {type(value).__name__}")
    elif type_code == 9:
        for index, item in enumerate(_validate_list(value, path)):
            if not isinstance(item, bool):
                _fail(f"{path}[{index}]", f"expected boolean, got {type(item).__name__}")
    elif type_code == 12:
        items = list(value.values()) if isinstance(value, dict) else value
        if not isinstance(items, (list, tuple)) or len(items) > 3:
            _fail(path, "vector must be {x,y,z} or an array with at most three numbers")
        for index, item in enumerate(items):
            _validate_float(item, f"{path}[{index}]")
    elif type_code == 15:
        for index, item in enumerate(_validate_list(value, path)):
            _validate_value(12, item, {}, structs, f"{path}[{index}]")
    elif type_code == 25:
        struct_doc = _resolve_struct(spec, structs, path)
        _validate_struct_value(value, struct_doc, structs, path)
    elif type_code == 26:
        struct_doc = _resolve_struct(spec, structs, path)
        for index, item in enumerate(_validate_list(value, path)):
            _validate_struct_value(item, struct_doc, structs, f"{path}[{index}]")
    elif type_code == 27:
        _validate_dict(value, spec, structs, path)
    elif type_code in DEFAULT_ONLY_TYPES:
        if value not in (None, "", [], {}):
            _fail(path, f"{TYPE_INFO[type_code]['name']} only supports a default value safely")
    else:
        _fail(path, f"no validator is registered for type_code {type_code}")


def validate_components_document(document: dict[str, Any], structs: dict[str, Any]) -> None:
    components = document.get("components")
    if not isinstance(components, list):
        _fail("components", "expected array")
    for component_index, component in enumerate(components):
        component_path = f"components[{component_index}]"
        if not isinstance(component, dict):
            _fail(component_path, "expected object")
        _validate_text(component.get("name"), f"{component_path}.name")
        variables = component.get("variables", [])
        if not isinstance(variables, list):
            _fail(f"{component_path}.variables", "expected array")
        for variable_index, variable in enumerate(variables):
            variable_path = f"{component_path}.variables[{variable_index}]"
            if not isinstance(variable, dict):
                _fail(variable_path, "expected object")
            _validate_text(variable.get("name"), f"{variable_path}.name")
            code = _type_code(variable, f"{variable_path}.type")
            value = variable.get("value")
            if code == 27 and "value" not in variable:
                value = variable
            _validate_value(code, value, variable, structs, f"{variable_path}.value")
