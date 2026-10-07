from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import streamlit as st

from miliastra_core.gil import convert_gil_to_static, preview_gil_to_static


_STATE_KEY = "static_convert_preview_state"
_REMOVE_KEY = "static_convert_remove_ids"
_RESTORE_KEY = "static_convert_restore_ids"


def _move_selected(to_skipped: bool) -> None:
    state = st.session_state[_STATE_KEY]
    key = _REMOVE_KEY if to_skipped else _RESTORE_KEY
    selected = set(st.session_state.get(key, []))
    if to_skipped:
        state["excluded_ids"].update(selected)
    else:
        state["excluded_ids"].difference_update(selected)
    state["result"] = None
    st.session_state[key] = []


def _record_rows(records, *, with_reason: bool = False):
    rows = []
    for item in records:
        row = {"id": item.object_id, "名称": item.name, "asset_id": item.asset_id}
        if with_reason:
            row["原因"] = item.skip_reason
        rows.append(row)
    return rows


def render_auto_convert_to_static() -> None:
    st.caption("GIL 必填，GIA 可选。先预览，再将不需要转换的元件移入不转换列表，最后确认转换并下载 GIL。")

    gil_upload = st.file_uploader("上传地图 .gil（必填）", type=["gil"], key="static_convert_gil")
    gia_upload = st.file_uploader("上传元件 .gia（可选）", type=["gia"], key="static_convert_gia")

    gil_bytes = gil_upload.getvalue() if gil_upload is not None else None
    gia_bytes = gia_upload.getvalue() if gia_upload is not None else None
    input_key = None if gil_bytes is None else (
        hashlib.sha256(gil_bytes).hexdigest(),
        hashlib.sha256(gia_bytes).hexdigest() if gia_bytes is not None else None,
    )
    state = st.session_state.get(_STATE_KEY)
    if state is None or state["input_key"] != input_key:
        state = {"input_key": input_key, "preview": None, "excluded_ids": set(), "result": None}
        st.session_state[_STATE_KEY] = state
        st.session_state.pop(_REMOVE_KEY, None)
        st.session_state.pop(_RESTORE_KEY, None)

    if st.button("预览转换结果", type="primary", disabled=gil_bytes is None, key="static_convert_preview"):
        state.update(preview=None, excluded_ids=set(), result=None)
        st.session_state.pop(_REMOVE_KEY, None)
        st.session_state.pop(_RESTORE_KEY, None)
        try:
            with st.spinner("正在解析和筛选元件..."):
                state["preview"] = preview_gil_to_static(gil_bytes, gia_bytes)
        except Exception as exc:
            st.error(f"预览失败：{exc}")
            return

    preview = state["preview"]
    if preview is None:
        return
    excluded_ids = state["excluded_ids"]
    remaining = [item for item in preview.candidates if item.object_id not in excluded_ids]
    removed = [item for item in preview.candidates if item.object_id in excluded_ids]
    skipped = list(preview.skipped) + [replace(item, skip_reason="用户移出转换列表") for item in removed]
    by_id = {item.object_id: item for item in preview.candidates}

    st.info(preview.source_note)
    st.caption("预览只读取文件；移出转换列表不会删除地图中的元件。确认转换后才写入静态标记并生成下载文件。")
    st.write({"待转换": len(remaining), "不转换": len(skipped),
              "GIA 中未出现在 GIL 的 ID": len(preview.unmatched_gia_ids)})
    st.subheader("待转换列表")
    if remaining:
        st.dataframe(
            _record_rows(remaining),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("当前没有待转换元件，确认后将保留原 GIL 内容。")
    to_remove = st.multiselect(
        "选择要移出转换列表的元件", [item.object_id for item in remaining],
        format_func=lambda object_id: f"{by_id[object_id].name or '未命名'}（ID: {object_id}）",
        key=_REMOVE_KEY,
    )
    st.button("移入不转换列表", disabled=not to_remove, on_click=_move_selected, args=(True,),
              key="static_convert_remove")

    st.subheader("不转换列表")
    if skipped:
        st.dataframe(
            _record_rows(skipped, with_reason=True),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.caption("没有不转换的元件。")
    if removed:
        with st.expander("恢复移除的元件"):
            to_restore = st.multiselect(
                "选择要恢复的元件", [item.object_id for item in removed],
                format_func=lambda object_id: f"{by_id[object_id].name or '未命名'}（ID: {object_id}）",
                key=_RESTORE_KEY,
            )
            st.button("恢复到转换列表", disabled=not to_restore, on_click=_move_selected, args=(False,),
                      key="static_convert_restore")
    if preview.unmatched_gia_ids:
        st.warning(f"GIA 中有 {len(preview.unmatched_gia_ids)} 个元件 ID 未在 GIL 中找到，已忽略。")

    if st.button("确认转换并生成 GIL", type="primary", key="static_convert_confirm"):
        state["result"] = None
        try:
            with st.spinner("正在写入保留元件的静态标记..."):
                state["result"] = convert_gil_to_static(
                    gil_bytes, gia_bytes, selected_ids=[item.object_id for item in remaining],
                )
        except Exception as exc:
            st.error(f"转换失败：{exc}")
            return
    result = state["result"]
    if result is not None:
        st.success(f"已将 {len(result.converted)} 个实体设为静态元件，其余实体保持原状。")
        st.download_button(
            "下载修改后的 GIL", result.gil_bytes,
            file_name=f"{Path(gil_upload.name).stem}_static.gil", mime="application/octet-stream",
            key="static_convert_download",
        )
