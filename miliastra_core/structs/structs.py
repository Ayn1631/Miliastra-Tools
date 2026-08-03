from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .create_components import build_components as _build_components
from .struct_schema import extract_structs_from_path


def normalize_structs_doc(data: Any, *, source_name: str = "") -> dict[str, Any]:
    if isinstance(data, list):
        data = {"structs": data}
    if not isinstance(data, dict) or not isinstance(data.get("structs"), list) or not data["structs"]:
        raise ValueError("结构体数据必须包含非空 structs 数组。")
    structs = data["structs"]
    for index, item in enumerate(structs, 1):
        if not isinstance(item, dict) or not (item.get("name") or item.get("id")):
            raise ValueError(f"structs[{index}] 不是有效结构体。")
        if not isinstance(item.get("fields"), list):
            raise ValueError(f"structs[{index}] 缺少 fields 数组。")
    data["struct_count"] = len(structs)
    data["structs_by_name"] = {str(x["name"]): x.get("id") for x in structs if x.get("name")}
    data["structs_by_id"] = {str(x["id"]): x for x in structs if x.get("id") is not None}
    if source_name and not data.get("file"):
        data["file"] = source_name
    return data


def extract_structs_from_uploaded(path: str | Path) -> dict[str, Any]:
    return normalize_structs_doc(extract_structs_from_path(path), source_name=str(path))


def parse_structs_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    return normalize_structs_doc(json.loads(source.read_text(encoding="utf-8")), source_name=str(source))


def build_components_gil(
    input_gil: str | Path,
    components_json: str | Path,
    template_gil: str | Path,
    output_gil: str | Path,
) -> dict[str, Any]:
    """Build a GIL from a components document using the lossless core pipeline."""

    input_path = Path(input_gil).resolve()
    components_path = Path(components_json).resolve()
    template_path = Path(template_gil).resolve()
    output_path = Path(output_gil).resolve()
    components_doc = json.loads(components_path.read_text(encoding="utf-8"))
    if isinstance(components_doc, list):
        components_doc = {"components": components_doc}
    if not isinstance(components_doc, dict) or not isinstance(components_doc.get("components"), list):
        raise ValueError("components JSON 顶层必须是对象并包含 components 数组。")

    structs_doc = extract_structs_from_path(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    structs_path = output_path.with_suffix(".structs.json")
    structs_path.write_text(json.dumps(structs_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    spec_path = output_path.with_suffix(".workflow.json")
    spec = {
        "format": "gil_component_workflow_spec",
        "version": 1,
        "input_gil": str(input_path),
        "template_gil": str(template_path),
        "template_component": "Template",
        "structs_json": str(structs_path),
        "output_gil": str(output_path),
        "overwrite": True,
        "id_policy": components_doc.get(
            "id_policy",
            {
                "component_definition_start": 1077940000,
                "component_index_start": 1077950000,
                "value_ref_start": 1075000000,
            },
        ),
        "components": components_doc["components"],
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _build_components(spec_path)
