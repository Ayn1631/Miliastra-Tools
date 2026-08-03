from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import streamlit as st

from miliastra_core.structs import build_components_gil, extract_structs_from_uploaded


UI_DIR = Path(__file__).resolve().parent
PROJECT_DIR = UI_DIR.parent
TEMPLATE_DIR = PROJECT_DIR / "Template"
DEFAULT_TEMPLATE_GIL = TEMPLATE_DIR / "Template.gil"
COMPONENTS_EXAMPLE_JSON = TEMPLATE_DIR / "components.example.json"
COMPONENTS_JSON_GUIDE = TEMPLATE_DIR / "COMPONENTS_JSON_GUIDE.md"


def _write_upload(uploaded_file: Any, destination: Path) -> Path:
    destination.write_bytes(uploaded_file.getvalue())
    return destination


def _parse_structs_upload(uploaded_file: Any) -> dict[str, Any]:
    suffix = Path(uploaded_file.name).suffix.lower()
    if suffix not in {".gil", ".gia"}:
        raise ValueError("只支持上传 .gil 或 .gia 文件。")
    with tempfile.TemporaryDirectory(prefix="miliastra_structs_") as temp_dir:
        source = _write_upload(uploaded_file, Path(temp_dir) / f"source{suffix}")
        return extract_structs_from_uploaded(source)


def _structs_doc_summary(structs_doc: dict[str, Any], fallback_suffix: str = "") -> str:
    count = int(structs_doc.get("struct_count") or len(structs_doc.get("structs", [])))
    source_format = str(structs_doc.get("source_format") or fallback_suffix.lstrip(".") or "json").upper()
    return f"{count} 个结构体（{source_format}）"


def _default_components_json_text() -> str:
    if not COMPONENTS_EXAMPLE_JSON.exists():
        return '{\n  "components": []\n}\n'
    return COMPONENTS_EXAMPLE_JSON.read_text(encoding="utf-8")


def _show_build_error(title: str, exc: Exception) -> None:
    st.error(f"{title}：{exc}")


def _show_summary_warnings(summary: dict[str, Any]) -> None:
    warnings = summary.get("warnings") or []
    for warning in warnings:
        st.warning(str(warning))

def page_extract_structs() -> None:
    st.header("导出结构体 JSON")
    uploaded = st.file_uploader("上传 .gil 或 .gia", type=["gil", "gia"], key="extract_structs_source")
    if st.button("导出结构体", type="primary", disabled=uploaded is None):
        suffix = Path(uploaded.name).suffix.lower()
        try:
            with st.spinner("正在解析结构体，首次解析后会缓存结果..."):
                result = _parse_structs_upload(uploaded)
        except Exception as exc:
            _show_build_error("结构体解析失败", exc)
            return
        data = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        st.success(
            f"已识别为 {result.get('source_format', suffix.lstrip('.')).upper()}，"
            f"提取到 {_structs_doc_summary(result, suffix)}"
        )
        st.json(result, expanded=False)
        st.download_button(
            "下载 structs.json",
            data.encode("utf-8"),
            file_name=f"{Path(uploaded.name).stem}.structs.json",
            mime="application/json",
        )




def page_components_builder() -> None:
    st.header("按 components JSON 构建 GIL")
    col_example, col_guide = st.columns(2)
    with col_example:
        if COMPONENTS_EXAMPLE_JSON.exists():
            st.download_button(
                "下载 components JSON 示例",
                COMPONENTS_EXAMPLE_JSON.read_bytes(),
                file_name=COMPONENTS_EXAMPLE_JSON.name,
                mime="application/json",
            )
        else:
            st.warning(f"找不到示例文件：{COMPONENTS_EXAMPLE_JSON}")
    with col_guide:
        if COMPONENTS_JSON_GUIDE.exists():
            st.download_button(
                "下载填写帮助文档",
                COMPONENTS_JSON_GUIDE.read_bytes(),
                file_name=COMPONENTS_JSON_GUIDE.name,
                mime="text/markdown",
            )
        else:
            st.warning(f"找不到帮助文档：{COMPONENTS_JSON_GUIDE}")

    components_input_mode = st.radio(
        "components JSON 输入方式",
        ["上传文件", "直接输入"],
        horizontal=True,
        key="components_input_mode",
    )
    with st.form("components_to_gil"):
        input_gil_upload = st.file_uploader("输入地图 .gil", type=["gil"], key="components_gil")
        components_upload = None
        components_text_input = ""
        if components_input_mode == "上传文件":
            components_upload = st.file_uploader("components.example.json 同格式 JSON", type=["json"], key="components_json")
        else:
            components_text_input = st.text_area(
                "components.example.json 同格式 JSON 内容",
                value=_default_components_json_text(),
                height=360,
                key="components_json_text_input",
            )
        template_upload = st.file_uploader("Template.gil（可选，默认使用内置模板）", type=["gil"], key="components_template")
        output_name = st.text_input("输出文件名", value="components_output.gil")
        submitted = st.form_submit_button("生成 GIL", type="primary")

    if submitted:
        if not input_gil_upload:
            st.error("需要上传地图 .gil。")
            return
        if components_input_mode == "上传文件" and not components_upload:
            st.error("需要上传 components JSON。")
            return
        if components_input_mode == "直接输入" and not components_text_input.strip():
            st.error("需要输入 components JSON 内容。")
            return
        if not template_upload and not DEFAULT_TEMPLATE_GIL.is_file():
            st.error(f"默认 Template.gil 不存在：{DEFAULT_TEMPLATE_GIL}")
            return
        with tempfile.TemporaryDirectory(prefix="qx_components_") as tmp:
            tmp_dir = Path(tmp)
            input_gil = _write_upload(input_gil_upload, tmp_dir / "input.gil")
            components_json = tmp_dir / "components.json"
            if components_input_mode == "上传文件":
                components_json = _write_upload(components_upload, components_json)
            else:
                components_json.write_text(components_text_input, encoding="utf-8")
            template_gil = (
                _write_upload(template_upload, tmp_dir / "Template.gil")
                if template_upload
                else DEFAULT_TEMPLATE_GIL
            )
            safe_output_name = Path(output_name).name.strip() or "components_output.gil"
            if not safe_output_name.lower().endswith(".gil"):
                safe_output_name += ".gil"
            output_gil = tmp_dir / safe_output_name
            try:
                summary = build_components_gil(input_gil, components_json, template_gil, output_gil)
            except Exception as exc:
                _show_build_error("GIL 编译失败", exc)
                return
            st.success("GIL 已生成")
            _show_summary_warnings(summary)
            st.json(summary, expanded=False)
            st.download_button("下载处理后的 GIL", output_gil.read_bytes(), file_name=output_gil.name)



def page_export_component() -> None:
    # st.caption("工程类导出能力集中在这里，避免可视化剧情页加载过多逻辑。")
    tab_structs, tab_components = st.tabs(
        ["导出结构体 JSON", "components JSON 构建 GIL"]
    )
    with tab_structs:
        page_extract_structs()
    with tab_components:
        page_components_builder()
