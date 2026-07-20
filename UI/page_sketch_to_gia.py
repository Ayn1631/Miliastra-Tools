from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import math
from pathlib import Path
from dataclasses import replace
from typing import Any, Callable

import streamlit as st

from UI.build_gia_objects import TYPE_NAME_TO_TEMPLATE_ID
from UI.image_to_gia import (
    COLLISION_MODE_NATIVE,
    COLLISION_MODE_NATIVE_AND_CLIMB,
    COLLISION_MODE_OFF,
    DEFAULT_TEMPLATE_GIA,
)
from UI.sketch_edge_controls import (
    SketchProcessingConfig,
    render_sketch_processing_controls,
)
from UI.sketch_edge_protection import (
    build_curve_candidates,
    build_curve_items_mask,
    build_deleted_mask,
    build_protected_mask,
    new_protection_branch,
    protected_path_groups,
    protection_branch_signature,
    protection_branch_color,
    render_edge_protection_picker,
)
from UI.sketch_to_gia import (
    COLOR_MODE_PASSTHROUGH,
    LONG_AXIS_X,
    LONG_AXIS_Z,
    SOURCE_LINE_ART,
    SketchGiaSettings,
    SketchProcessedStage,
    SketchSkeletonStage,
    build_sketch_gia_bytes,
    fit_ribbon_mesh_stage,
    fit_sketch_stage,
    load_rgba_image,
    mask_preview,
    merge_protected_mask_into_processed_stage,
    process_sketch_image,
    preview_image_to_mask,
    scale_image,
    skeletonize_sketch_stage,
    trace_smooth_logical_curves,
)
from UI.task_ui import run_heavy_action


LINE_PRIMITIVE_NAMES = list(TYPE_NAME_TO_TEMPLATE_ID)
_SCALE_CACHE_KEY = "sketch_to_gia_scale_stage"
_PROCESS_CACHE_KEY = "sketch_to_gia_process_stage"
_SKELETON_CACHE_KEY = "sketch_to_gia_skeleton_stage"
_FIT_CACHE_KEY = "sketch_to_gia_fit_stage"
_RIBBON_CACHE_KEY = "sketch_to_gia_ribbon_stage"
_EXPORT_CACHE_KEY = "sketch_to_gia_export"
_PROTECTION_STATE_KEY = "sketch_to_gia_edge_protection"
_PROTECTION_PENDING_STAGE_KEY = "pending_processed_stage"
_PROCESS_NOTICE_KEY = "sketch_to_gia_process_notice"


def _png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _preview_100_percent_size(
    image,
    *,
    max_width: int = 960,
    max_height: int = 640,
) -> tuple[int, int]:
    """把原图适配到合理预览尺寸，并把该尺寸定义为显示 100%。"""
    width, height = image.size
    if width <= 0 or height <= 0:
        return 1, 1
    fit_scale = min(max_width / width, max_height / height)
    return max(1, round(width * fit_scale)), max(1, round(height * fit_scale))


def _preview_display_size(image, scale_x_percent: int, scale_y_percent: int) -> tuple[int, int]:
    base_width, base_height = _preview_100_percent_size(image)
    return (
        max(1, round(base_width * float(scale_x_percent) / 100.0)),
        max(1, round(base_height * float(scale_y_percent) / 100.0)),
    )


def _render_image_at_preview_scale(
    image,
    caption: str,
    *,
    display_width: int,
    display_height: int,
) -> None:
    encoded = base64.b64encode(_png_bytes(image)).decode("ascii")
    safe_caption = html.escape(caption)
    width = max(1, int(display_width))
    height = max(1, int(display_height))
    st.markdown(
        f"""
        <div style="overflow:auto; max-width:100%; padding:8px; min-height:120px;
                    border:1px solid rgba(128,128,128,0.25); border-radius:10px;">
          <img src="data:image/png;base64,{encoded}"
               width="{width}" height="{height}"
               style="display:block; width:{width}px; height:{height}px; max-width:none; margin:0 auto;" />
        </div>
        <div style="font-size:0.88rem; opacity:0.75; margin-top:0.35rem; margin-bottom:0.5rem;">
          {safe_caption}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_preview_placeholder(message: str, *, min_height: int = 220) -> None:
    safe_message = html.escape(message)
    st.markdown(
        f"""
        <div style="min-height:{int(min_height)}px; display:flex; align-items:center; justify-content:center;
                    border:2px dashed rgba(128,128,128,0.35); border-radius:12px;
                    padding:24px; text-align:center; opacity:0.78;">
          <div>🖼️<br><br>{safe_message}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _stage_signature(raw: bytes, stage_name: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(raw)
    digest.update(stage_name.encode("utf-8"))
    digest.update(encoded)
    return digest.hexdigest()


def _normalize_preview_step(value: Any, step_count: int) -> int:
    """兼容旧预览导航状态，确保索引始终落在有效范围。"""
    if int(step_count) <= 0:
        return 0
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(int(step_count) - 1, normalized))


def _get_valid_stage(key: str, signature: str):
    cached = st.session_state.get(key)
    if not isinstance(cached, dict) or cached.get("signature") != signature:
        return None
    return cached.get("value")


def _put_stage(key: str, signature: str, value: Any) -> None:
    st.session_state[key] = {"signature": signature, "value": value}
    st.session_state.pop(_EXPORT_CACHE_KEY, None)


def _show_stage_update(message: str) -> None:
    """显示阶段完成提示，但不强制重跑页面，避免当前 Tab 与滚动焦点跳走。"""
    st.success(message)


def _run_stage(
    prefix: str,
    action: Callable[[Callable[[int, str], None]], Any],
    *,
    use_queue: bool = True,
):
    return run_heavy_action(prefix, action, use_queue=use_queue)


def _protection_state(raw_digest: str) -> dict[str, Any]:
    state = st.session_state.get(_PROTECTION_STATE_KEY)
    if not isinstance(state, dict) or state.get("raw_digest") != raw_digest:
        state = {
            "raw_digest": raw_digest,
            "branches": [],
            "active_branch_id": None,
            "selector_version": 0,
            "main_deleted_curves": [],
        }
        st.session_state[_PROTECTION_STATE_KEY] = state
    return state


def _clear_protection_branches(state: dict[str, Any]) -> bool:
    """清空仅属于保护机制的状态，保留独立的主删除选择。"""
    had_branches = bool(state.get("branches"))
    state["branches"] = []
    state["active_branch_id"] = None
    state["selector_version"] = int(state.get("selector_version", 0)) + 1
    state.pop(_PROTECTION_PENDING_STAGE_KEY, None)
    return had_branches


def _sync_main_source_mode(state: dict[str, Any], source_mode: str) -> bool:
    """记录主来源；来源发生切换时清空共用保护分支。"""
    previous_source_mode = state.get("main_source_mode")
    cleared = False
    if previous_source_mode is not None and previous_source_mode != source_mode:
        cleared = _clear_protection_branches(state)
    state["main_source_mode"] = source_mode
    return cleared


def _main_deleted_curves(state: dict[str, Any]) -> list[dict[str, Any]]:
    curves = _normalize_curve_items(state.get("main_deleted_curves"))
    state["main_deleted_curves"] = curves
    return curves


def _main_deletion_signature(curves: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        curves,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _edit_signature(
    branches: list[dict[str, Any]],
    main_deleted_curves: list[dict[str, Any]],
) -> str:
    payload = {
        "branches": protection_branch_signature(branches),
        "main_deleted": _main_deletion_signature(main_deleted_curves),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _editable_processed_stage(stage: SketchProcessedStage) -> SketchProcessedStage:
    """让直通图也能进入基于曲线的删除编辑，保存结果为黑白线稿。"""
    if stage.cleaned_mask is not None:
        return stage
    mask = preview_image_to_mask(stage.binary_image)
    if not mask.any():
        raise ValueError("直通图片中没有可删除的黑色线稿")
    return SketchProcessedStage(
        scaled_image=stage.scaled_image,
        cleaned_mask=mask,
        binary_image=mask_preview(mask),
        source_summary=dict(stage.source_summary),
        cleanup_summary={
            **stage.cleanup_summary,
            "main_deletion_passthrough_mask_conversion": True,
        },
    )


def _process_stage_with_masks(
    scaled_image,
    settings: SketchGiaSettings,
    progress_callback: Callable[[int, str], None] | None,
    protected_mask,
    deleted_mask,
) -> SketchProcessedStage:
    """先确定下一阶段输入，再在其上统一应用保护新增与删除编辑。"""
    stage = process_sketch_image(
        scaled_image,
        settings,
        progress_callback,
    )
    if not protected_mask.any() and not deleted_mask.any():
        return stage
    editable_stage = (
        _editable_processed_stage(stage)
        if stage.cleaned_mask is None
        else stage
    )
    return merge_protected_mask_into_processed_stage(
        editable_stage,
        protected_mask,
        deleted_mask,
    )


def _branch_by_id(
    branches: list[dict[str, Any]],
    branch_id: str | None,
) -> dict[str, Any] | None:
    return next(
        (branch for branch in branches if str(branch.get("id")) == str(branch_id)),
        None,
    )


def _branch_preview_signature(
    raw_digest: str,
    branch: dict[str, Any],
) -> str:
    payload = {
        "raw_digest": raw_digest,
        "scale_percent": int(branch.get("scale_percent", 100)),
        "config": branch.get("config", {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _settings_from_processing_config(
    config: SketchProcessingConfig,
) -> SketchGiaSettings:
    cleanup = config.active_cleanup
    return SketchGiaSettings(
        source_mode=config.source_mode,
        edge_operator=config.edge.operator,
        edge_params=config.edge.operator_params,
        blur_kernel=config.edge.blur_kernel,
        line_color_mode=config.line_color_mode,
        line_rgb=config.line_rgb,
        line_rgb_tolerance=config.line_rgb_tolerance,
        nonzero_threshold=config.nonzero_threshold,
        alpha_threshold=cleanup.alpha_threshold,
        auto_explore_polarity=config.auto_explore_polarity,
        close_kernel=cleanup.close_kernel,
        remove_small_components=cleanup.remove_small_components,
        auto_thin_wide_lines=True,
        wide_line_search_radius=4,
        exclude_curve_length_px=(
            cleanup.exclude_curve_length_px
            if cleanup.exclude_curve_length_enabled
            else 0.0
        ),
    )


def _other_branch_groups(
    branches: list[dict[str, Any]],
    active_branch_id: str,
) -> list[dict[str, Any]]:
    return protected_path_groups(
        [
            branch
            for branch in branches
            if str(branch.get("id")) != str(active_branch_id)
        ]
    )


def _normalize_curve_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        curve_id = str(item.get("id", ""))
        points = item.get("points")
        if not curve_id or curve_id in seen or not isinstance(points, list):
            continue
        normalized_points = [
            [float(point[0]), float(point[1])]
            for point in points
            if isinstance(point, (list, tuple)) and len(point) == 2
        ]
        if not normalized_points:
            continue
        normalized.append({"id": curve_id, "points": normalized_points})
        seen.add(curve_id)
    return normalized


@st.dialog("删除原有线条", width="large")
def _render_main_line_deletion_dialog(
    *,
    raw_digest: str,
    main_base_stage: SketchProcessedStage,
    active_protection_branches: list[dict[str, Any]],
) -> None:
    """编辑主处理线稿：左图选中删除，右图实时展示最终输入。"""
    state = _protection_state(raw_digest)
    branches = active_protection_branches
    current_deleted = _main_deleted_curves(state)
    editable_base = _editable_processed_stage(main_base_stage)
    image_size = editable_base.binary_image.size
    candidate_curves = build_curve_candidates(
        trace_smooth_logical_curves(editable_base.cleaned_mask.astype(bool))[0],
        image_size,
    )
    main_deleted_mask = build_curve_items_mask(current_deleted, image_size)
    result_stage = merge_protected_mask_into_processed_stage(
        editable_base,
        None,
        main_deleted_mask,
    )
    selected_ids = [
        str(item.get("id"))
        for item in current_deleted
        if item.get("id")
    ]
    submission = render_edge_protection_picker(
        editable_base.binary_image,
        result_stage.binary_image,
        candidate_curves=candidate_curves,
        result_candidate_curves=[],
        selected_curve_ids=[],
        deleted_curve_ids=selected_ids,
        active_existing_paths=[],
        active_existing_deleted_paths=current_deleted,
        existing_path_groups=[],
        active_branch_id="main-delete",
        active_branch_name="主删除编辑",
        active_branch_color="#00B86B",
        editor_mode="delete",
        key=(
            f"sketch_main_delete_picker_{raw_digest}_"
            f"{_main_deletion_signature(current_deleted)}"
        ),
    )
    if submission is None or submission.get("action") != "confirm":
        st.caption(
            f"当前已保存删除 {len(current_deleted)} 条曲线。未点击确定前，"
            "左图选择只会在右图实时演示。"
        )
        return

    state["main_deleted_curves"] = _normalize_curve_items(
        submission.get("deleted_curves")
    )
    saved_deleted = _main_deleted_curves(state)
    saved_deleted_mask = build_curve_items_mask(saved_deleted, image_size)
    confirmed_stage = merge_protected_mask_into_processed_stage(
        editable_base,
        None,
        saved_deleted_mask,
    )
    state[_PROTECTION_PENDING_STAGE_KEY] = {
        "edit_signature": _edit_signature(branches, saved_deleted),
        "stage": confirmed_stage,
    }
    st.session_state[_PROTECTION_STATE_KEY] = state
    st.rerun(scope="app")


@st.dialog("线稿保护分支", width="large")
def _render_edge_protection_dialog(
    *,
    raw_digest: str,
    base_scaled_image,
    default_config: SketchProcessingConfig,
    main_base_stage: SketchProcessedStage,
) -> None:
    state = _protection_state(raw_digest)
    branches: list[dict[str, Any]] = state["branches"]
    main_deleted_curves = _main_deleted_curves(state)
    for index, item in enumerate(branches):
        item["color"] = str(
            item.get("color") or protection_branch_color(index)
        )
    if not branches:
        st.info("当前没有保护分支。")
        if st.button("新建保护分支", type="primary", use_container_width=True):
            branch = new_protection_branch(index=1, defaults=default_config)
            branches.append(branch)
            state["active_branch_id"] = branch["id"]
            state["branches"] = branches
            st.session_state[_PROTECTION_STATE_KEY] = state
            st.rerun(scope="fragment")
        return

    branch_ids = [str(branch["id"]) for branch in branches]
    active_id = str(state.get("active_branch_id") or branch_ids[0])
    if active_id not in branch_ids:
        active_id = branch_ids[0]
    selected_id = st.selectbox(
        "当前保护分支",
        branch_ids,
        index=branch_ids.index(active_id),
        format_func=lambda item: str(
            (_branch_by_id(branches, item) or {}).get("name", item)
        ),
        key=(
            f"sketch_protection_branch_selector_{raw_digest}_"
            f"{int(state.get('selector_version', 0))}"
        ),
    )
    state["active_branch_id"] = selected_id
    st.session_state[_PROTECTION_STATE_KEY] = state
    branch = _branch_by_id(branches, selected_id)
    if branch is None:
        st.error("保护分支状态已失效，请关闭后重新打开。")
        return
    branch_index = branches.index(branch)
    branch["color"] = str(
        branch.get("color") or protection_branch_color(branch_index)
    )

    col_name, col_add, col_delete = st.columns([3, 1, 1])
    with col_name:
        branch["name"] = st.text_input(
            "分支名称",
            value=str(branch.get("name", "保护分支")),
            key=f"sketch_protection_name_{branch['id']}",
        )
    with col_add:
        st.write("")
        st.write("")
        if st.button("新建", key=f"sketch_protection_add_{branch['id']}"):
            new_branch = new_protection_branch(
                index=len(branches) + 1,
                defaults=SketchProcessingConfig.from_dict(branch.get("config")),
            )
            branches.append(new_branch)
            state["active_branch_id"] = new_branch["id"]
            state["selector_version"] = int(state.get("selector_version", 0)) + 1
            state["branches"] = branches
            st.session_state[_PROTECTION_STATE_KEY] = state
            st.rerun(scope="fragment")
    with col_delete:
        st.write("")
        st.write("")
        if st.button(
            "删除",
            key=f"sketch_protection_delete_{branch['id']}",
            type="secondary",
        ):
            branches.remove(branch)
            state["active_branch_id"] = branches[0]["id"] if branches else None
            state["selector_version"] = int(state.get("selector_version", 0)) + 1
            state["branches"] = branches
            st.session_state[_PROTECTION_STATE_KEY] = state
            st.rerun(scope="fragment")

    has_generated_preview = branch.get("preview_stage") is not None
    with st.expander(
        "分支检测参数（修改后需重新生成）",
        expanded=not has_generated_preview,
    ):
        branch["scale_percent"] = int(
            st.number_input(
                "分支等比缩放（%）",
                10,
                400,
                int(branch.get("scale_percent", 100)),
                1,
                key=f"sketch_protection_scale_{branch['id']}",
                help="X/Y 使用同一倍率；保护曲线会按归一化坐标映射回主画布。",
            )
        )
        st.markdown("#### 线稿来源、识别与清理参数")
        config = render_sketch_processing_controls(
            key_prefix=f"sketch_protection_{branch['id']}",
            defaults=SketchProcessingConfig.from_dict(branch.get("config")),
            source_label="分支线稿来源",
        )
    branch["config"] = config.to_dict()
    current_signature = _branch_preview_signature(raw_digest, branch)
    other_branches = [
        item
        for item in branches
        if str(item.get("id")) != str(branch["id"])
    ]
    result_edit_base_stage = merge_protected_mask_into_processed_stage(
        main_base_stage,
        build_protected_mask(other_branches, base_scaled_image.size),
        build_deleted_mask(other_branches, base_scaled_image.size)
        | build_curve_items_mask(main_deleted_curves, base_scaled_image.size),
    )

    if st.button(
        "生成 / 刷新分支线稿",
        type="primary",
        use_container_width=True,
        key=f"sketch_protection_generate_{branch['id']}",
    ):
        try:
            branch_scaled = scale_image(
                base_scaled_image,
                branch["scale_percent"],
                branch["scale_percent"],
            )
            branch_settings = _settings_from_processing_config(config)
            processed = _run_stage(
                "保护分支线稿处理",
                lambda callback: process_sketch_image(
                    branch_scaled,
                    branch_settings,
                    callback,
                ),
            )
            skeleton = _run_stage(
                "保护分支逻辑曲线",
                lambda callback: skeletonize_sketch_stage(
                    processed,
                    branch_settings,
                    callback,
                    include_logical_curves=True,
                ),
            )
        except Exception as exc:
            st.error(f"保护分支生成失败：{exc}")
        else:
            branch["preview_signature"] = current_signature
            branch["preview_stage"] = skeleton
            branch["preview_image"] = skeleton.skeleton_image
            branch["preview_curves"] = skeleton.logical_curves or []
            state["branches"] = branches
            st.session_state[_PROTECTION_STATE_KEY] = state
            st.success(
                f"分支线稿已生成：{skeleton.skeleton_image.width} × "
                f"{skeleton.skeleton_image.height} px。"
            )

    preview_is_current = branch.get("preview_signature") == current_signature
    preview_stage = branch.get("preview_stage") if preview_is_current else None
    preview_image = branch.get("preview_image") if preview_is_current else None
    if preview_stage is None or preview_image is None:
        st.info("来源参数或缩放尚未生成当前预览。点击上方按钮后，才能在图中选择保护曲线。")
    else:
        candidate_curves = build_curve_candidates(
            branch.get("preview_curves") or [],
            preview_image.size,
        )
        active_existing = [
            item
            for item in branch.get("protected_curves", [])
            if isinstance(item, dict)
        ]
        selected_curve_ids = [
            str(item.get("id"))
            for item in active_existing
            if item.get("id")
        ]
        active_existing_deleted = [
            item
            for item in branch.get("deleted_curves", [])
            if isinstance(item, dict)
        ]
        deleted_curve_ids = [
            str(item.get("id"))
            for item in active_existing_deleted
            if item.get("id")
        ]
        result_candidate_curves = build_curve_candidates(
            trace_smooth_logical_curves(
                result_edit_base_stage.cleaned_mask.astype(bool)
            )[0],
            result_edit_base_stage.binary_image.size,
        )
        submission = render_edge_protection_picker(
            preview_image,
            result_edit_base_stage.binary_image,
            candidate_curves=candidate_curves,
            result_candidate_curves=result_candidate_curves,
            selected_curve_ids=selected_curve_ids,
            deleted_curve_ids=deleted_curve_ids,
            active_existing_paths=active_existing,
            active_existing_deleted_paths=active_existing_deleted,
            existing_path_groups=_other_branch_groups(
                branches,
                str(branch["id"]),
            ),
            active_branch_id=str(branch["id"]),
            active_branch_name=str(branch.get("name", "保护分支")),
            active_branch_color=str(branch["color"]),
            key=(
                f"sketch_protection_picker_{branch['id']}_{current_signature}_"
                f"{int(branch.get('picker_version', 0))}"
            ),
        )
        if (
            submission is not None
            and submission.get("action") == "confirm"
            and str(submission.get("active_branch_id")) == str(branch["id"])
        ):
            branch["protected_curves"] = _normalize_curve_items(
                submission.get("selected_curves")
            )
            branch["deleted_curves"] = _normalize_curve_items(
                submission.get("deleted_curves")
            )
            state["branches"] = branches

            protected_mask = build_protected_mask(
                branches,
                base_scaled_image.size,
            )
            deleted_mask = build_deleted_mask(
                branches,
                base_scaled_image.size,
            ) | build_curve_items_mask(
                main_deleted_curves,
                base_scaled_image.size,
            )
            confirmed_stage = merge_protected_mask_into_processed_stage(
                main_base_stage,
                protected_mask,
                deleted_mask,
            )
            state[_PROTECTION_PENDING_STAGE_KEY] = {
                "edit_signature": _edit_signature(branches, main_deleted_curves),
                "stage": confirmed_stage,
            }
            st.session_state[_PROTECTION_STATE_KEY] = state
            st.rerun(scope="app")

        protected_count = len(branch.get("protected_curves", []))
        deleted_count = len(branch.get("deleted_curves", []))
        st.caption(
            f"本分支已确认保护 {protected_count} 条、删除 {deleted_count} 条曲线。"
            "未点击确定前，两侧点击只在结果图中实时演示，不会修改主页面结果。"
        )


def render_sketch_to_gia_page() -> None:
    st.markdown("## 线稿转 GIA")
    st.caption("不同功能改为标签页切换；每个标签页内只生成本阶段预览，导出页会优先复用已有有效预览。")

    uploaded = st.file_uploader(
        "选择图片文件",
        type=["png", "jpg", "jpeg", "webp", "bmp"],
        key="sketch_to_gia_upload",
    )
    if uploaded is None:
        st.info("先上传图片。可从普通图片检测边缘，也可把上传图直接作为线稿图。")
        return

    raw = uploaded.getvalue()
    raw_digest = hashlib.sha256(raw).hexdigest()
    try:
        image = load_rgba_image(raw)
    except Exception as exc:
        st.error(f"图片读取失败：{exc}")
        return

    tab_scale, tab_process, tab_skeleton, tab_fit, tab_ribbon, tab_export = st.tabs(["1. 图片缩放", "2. 线稿识别与清理", "3. 宽线中心骨架", "4. 曲线拟合", "5. 矩形带拉升（实验）", "6. 导出 GIA"])

    # ───────────────────────────── 阶段 1：缩放 ─────────────────────────────
    with tab_scale:
        lock_scale = st.checkbox("等比缩放", value=True, key="sketch_to_gia_scale_lock")
        col_scale_x, col_scale_y = st.columns(2)
        with col_scale_x:
            scale_x_percent = st.number_input(
                "X 缩放（%）", 1, 400, 100, 1, key="sketch_to_gia_scale_x"
            )
        if lock_scale:
            scale_y_percent = int(scale_x_percent)
            with col_scale_y:
                st.number_input(
                    "Y 缩放（%）", 1, 400, int(scale_y_percent), 1,
                    disabled=True, key="sketch_to_gia_scale_y_locked"
                )
        else:
            with col_scale_y:
                scale_y_percent = st.number_input(
                    "Y 缩放（%）", 1, 400, 100, 1, key="sketch_to_gia_scale_y"
                )
        st.caption("原图会先适配到约 960×640 的合理位置并定义为显示 100%；这里的百分比基于该显示基准改变预览大小。")

        scale_payload = {
            "scale_x_percent": int(scale_x_percent),
            "scale_y_percent": int(scale_y_percent),
        }
        scale_signature = _stage_signature(raw, "scale", scale_payload)

        if st.button("生成 / 刷新缩放预览", type="primary", key="sketch_preview_scale", use_container_width=True):
            try:
                def run_scale(callback):
                    callback(10, "正在读取图片尺寸")
                    callback(35, "正在计算缩放后的画布")
                    value = scale_image(image, int(scale_x_percent), int(scale_y_percent))
                    callback(90, "正在写入缩放预览缓存")
                    callback(100, "缩放预览完成")
                    return value

                scaled_stage = _run_stage("图片缩放", run_scale, use_queue=False)
            except Exception as exc:
                st.error(f"图片缩放失败：{exc}")
            else:
                _put_stage(_SCALE_CACHE_KEY, scale_signature, scaled_stage)
                st.success(f"缩放预览已生成：{scaled_stage.width} × {scaled_stage.height} px。")

        scaled_stage = _get_valid_stage(_SCALE_CACHE_KEY, scale_signature)
        display_width, display_height = _preview_display_size(image, int(scale_x_percent), int(scale_y_percent))
        if scaled_stage is None:
            _render_preview_placeholder("点击上方按钮，只生成缩放后的输入图。")
        else:
            _render_image_at_preview_scale(
                scaled_stage,
                f"缩放后实际画布 {scaled_stage.width} × {scaled_stage.height} px；当前显示 {display_width} × {display_height} px。",
                display_width=display_width,
                display_height=display_height,
            )

    scaled_width = max(1, round(image.width * int(scale_x_percent) / 100.0))
    scaled_height = max(1, round(image.height * int(scale_y_percent) / 100.0))

    # 先收集所有设置值；主线稿与保护分支复用同一个完整来源配置。
    processing_config = SketchProcessingConfig()
    source_mode = processing_config.source_mode
    edge_operator = processing_config.edge.operator
    edge_params: dict[str, int | float] = processing_config.edge.operator_params
    blur_kernel = processing_config.edge.blur_kernel
    cleanup_config = processing_config.active_cleanup
    line_color_mode = processing_config.line_color_mode
    line_rgb = processing_config.line_rgb
    line_rgb_tolerance = processing_config.line_rgb_tolerance
    nonzero_threshold = processing_config.nonzero_threshold
    alpha_threshold = cleanup_config.alpha_threshold
    auto_explore_polarity = processing_config.auto_explore_polarity
    close_kernel = cleanup_config.close_kernel
    min_component = cleanup_config.remove_small_components
    exclude_curve_length_enabled = cleanup_config.exclude_curve_length_enabled
    exclude_curve_length_px = cleanup_config.exclude_curve_length_px

    # ───────────────────────── 阶段 2：识别与清理 ─────────────────────────
    with tab_process:
        process_notice = st.session_state.pop(_PROCESS_NOTICE_KEY, None)
        if process_notice:
            st.success(str(process_notice))
        processing_config = render_sketch_processing_controls(
            key_prefix="sketch_to_gia_main",
            defaults=processing_config,
        )
        source_mode = processing_config.source_mode
        edge_operator = processing_config.edge.operator
        edge_params = processing_config.edge.operator_params
        blur_kernel = processing_config.edge.blur_kernel
        cleanup_config = processing_config.active_cleanup
        line_color_mode = processing_config.line_color_mode
        line_rgb = processing_config.line_rgb
        line_rgb_tolerance = processing_config.line_rgb_tolerance
        nonzero_threshold = processing_config.nonzero_threshold
        auto_explore_polarity = processing_config.auto_explore_polarity
        open_main_deletion_dialog = False

        passthrough_enabled = (
            source_mode == SOURCE_LINE_ART
            and line_color_mode == COLOR_MODE_PASSTHROUGH
        )

        alpha_threshold = cleanup_config.alpha_threshold
        close_kernel = cleanup_config.close_kernel
        min_component = cleanup_config.remove_small_components
        exclude_curve_length_enabled = (
            cleanup_config.exclude_curve_length_enabled
        )
        exclude_curve_length_px = (
            cleanup_config.exclude_curve_length_px
            if exclude_curve_length_enabled
            else 0.0
        )

        process_settings = replace(
            _settings_from_processing_config(processing_config),
            scale_x_percent=int(scale_x_percent),
            scale_y_percent=int(scale_y_percent),
        )

        edit_state = _protection_state(raw_digest)
        if _sync_main_source_mode(edit_state, source_mode):
            st.info("主线稿来源已切换，原保护分支已全部清空。")
        st.session_state[_PROTECTION_STATE_KEY] = edit_state

        protection_branches: list[dict[str, Any]] = edit_state["branches"]
        st.markdown("#### 线稿保护分支")
        st.caption(
            "每个分支可独立选择来源并调整全部参数；"
            "左侧分支图选择要添加的线，右侧始终显示当前下一阶段输入。"
        )
        col_new_branch, col_manage_branch = st.columns(2)
        open_protection_dialog = False
        with col_new_branch:
            if st.button(
                "＋ 新建保护分支",
                key="sketch_protection_new_branch",
                use_container_width=True,
            ):
                branch = new_protection_branch(
                    index=len(protection_branches) + 1,
                    defaults=processing_config,
                )
                protection_branches.append(branch)
                edit_state["active_branch_id"] = branch["id"]
                edit_state["branches"] = protection_branches
                st.session_state[_PROTECTION_STATE_KEY] = edit_state
                open_protection_dialog = True
        with col_manage_branch:
            if st.button(
                "管理 / 切换保护分支",
                key="sketch_protection_manage_branch",
                use_container_width=True,
                disabled=not protection_branches,
            ):
                open_protection_dialog = True

        protected_curve_count = sum(
            len(branch.get("protected_curves", []))
            for branch in protection_branches
        )
        deleted_curve_count = sum(
            len(branch.get("deleted_curves", []))
            for branch in protection_branches
        )
        st.info(
            f"当前共有 {len(protection_branches)} 个保护分支，"
            f"已保护 {protected_curve_count} 条、删除 {deleted_curve_count} 条逻辑曲线。"
        )
        if open_protection_dialog:
            dialog_scaled = _get_valid_stage(_SCALE_CACHE_KEY, scale_signature)
            if dialog_scaled is None:
                dialog_scaled = scale_image(
                    image,
                    int(scale_x_percent),
                    int(scale_y_percent),
                )
                _put_stage(_SCALE_CACHE_KEY, scale_signature, dialog_scaled)
            try:
                # 不带保护/删除掩码生成主线稿，得到短曲线开关决定的原始下一阶段输入。
                dialog_base_stage = process_sketch_image(
                    dialog_scaled,
                    process_settings,
                )
            except Exception as exc:
                st.error(f"准备保护分支结果图失败：{exc}")
                dialog_base_stage = None
            if dialog_base_stage is None:
                return
            _render_edge_protection_dialog(
                raw_digest=raw_digest,
                base_scaled_image=dialog_scaled,
                default_config=processing_config,
                main_base_stage=dialog_base_stage,
            )

        main_deleted_curves = _main_deleted_curves(edit_state)
        st.markdown("#### 删除原有线条")
        col_delete_lines, col_delete_status = st.columns([2, 3])
        with col_delete_lines:
            open_main_deletion_dialog = st.button(
                "删除原有线条 / 管理删除",
                key="sketch_main_delete_manage",
                use_container_width=True,
            )
        with col_delete_status:
            st.caption(
                f"已保存删除 {len(main_deleted_curves)} 条逻辑曲线；"
                "打开后左图点击选择，右图实时预览，确定才会替换后续输入。"
            )

        active_protection_branches = edit_state["branches"]
        protection_signature = protection_branch_signature(
            active_protection_branches
        )
        protected_mask = build_protected_mask(
            active_protection_branches,
            (scaled_width, scaled_height),
        )
        deleted_mask = build_deleted_mask(
            active_protection_branches,
            (scaled_width, scaled_height),
        ) | build_curve_items_mask(
            main_deleted_curves,
            (scaled_width, scaled_height),
        )
        edit_signature = _edit_signature(
            active_protection_branches,
            main_deleted_curves,
        )

        process_payload = {
            "upstream": scale_signature,
            "source_mode": source_mode,
            "edge_operator": edge_operator,
            "edge_params": edge_params,
            "blur_kernel": int(blur_kernel),
            "line_color_mode": line_color_mode,
            "line_rgb": line_rgb,
            "line_rgb_tolerance": int(line_rgb_tolerance),
            "nonzero_threshold": int(nonzero_threshold),
            "alpha_threshold": int(alpha_threshold),
            "auto_explore_polarity": bool(auto_explore_polarity),
            "close_kernel": int(close_kernel),
            "remove_small_components": int(min_component),
            "exclude_curve_length_px": float(exclude_curve_length_px),
            "protection_signature": protection_signature,
            "main_deletion_signature": _main_deletion_signature(
                main_deleted_curves
            ),
            "protected_pixels": int(protected_mask.sum()),
            "deleted_pixels": int(deleted_mask.sum()),
        }
        process_signature = _stage_signature(raw, "process", process_payload)
        pending_stage = edit_state.get(_PROTECTION_PENDING_STAGE_KEY)
        if (
            isinstance(pending_stage, dict)
            and pending_stage.get("edit_signature") == edit_signature
            and isinstance(pending_stage.get("stage"), SketchProcessedStage)
        ):
            _put_stage(
                _PROCESS_CACHE_KEY,
                process_signature,
                pending_stage["stage"],
            )
            edit_state.pop(_PROTECTION_PENDING_STAGE_KEY, None)
            st.session_state[_PROTECTION_STATE_KEY] = edit_state
            st.success("保护/删除结果已确认，并已替换当前准备传给下一步的处理线稿。")

        process_clicked = st.button(
            "生成 / 刷新处理线稿预览", type="primary",
            key="sketch_preview_process", use_container_width=True
        )

        if process_clicked:
            had_branches = _clear_protection_branches(edit_state)
            edit_state["main_source_mode"] = source_mode
            st.session_state[_PROTECTION_STATE_KEY] = edit_state
            active_protection_branches = []
            protection_signature = protection_branch_signature([])
            protected_mask = build_protected_mask(
                [],
                (scaled_width, scaled_height),
            )
            deleted_mask = build_curve_items_mask(
                main_deleted_curves,
                (scaled_width, scaled_height),
            )
            edit_signature = _edit_signature([], main_deleted_curves)
            process_payload.update(
                {
                    "protection_signature": protection_signature,
                    "protected_pixels": 0,
                    "deleted_pixels": int(deleted_mask.sum()),
                }
            )
            process_signature = _stage_signature(raw, "process", process_payload)
            if had_branches:
                st.info("正在重新生成主线稿，原保护分支已全部清空。")

        if open_main_deletion_dialog:
            try:
                dialog_scaled = _get_valid_stage(_SCALE_CACHE_KEY, scale_signature)
                if dialog_scaled is None:
                    dialog_scaled = scale_image(
                        image,
                        int(scale_x_percent),
                        int(scale_y_percent),
                    )
                    _put_stage(_SCALE_CACHE_KEY, scale_signature, dialog_scaled)
                dialog_base_stage = _get_valid_stage(_PROCESS_CACHE_KEY, process_signature)
                if dialog_base_stage is None:
                    dialog_base_stage = _process_stage_with_masks(
                        dialog_scaled,
                        process_settings,
                        None,
                        protected_mask,
                        deleted_mask,
                    )
            except Exception as exc:
                st.error(f"准备原有线条删除图失败：{exc}")
            else:
                _render_main_line_deletion_dialog(
                    raw_digest=raw_digest,
                    main_base_stage=dialog_base_stage,
                    active_protection_branches=active_protection_branches,
                )

        if process_clicked:
            try:
                current_scaled = _get_valid_stage(_SCALE_CACHE_KEY, scale_signature)
                if current_scaled is None:
                    current_scaled = scale_image(image, int(scale_x_percent), int(scale_y_percent))
                    _put_stage(_SCALE_CACHE_KEY, scale_signature, current_scaled)
                processed_stage = _run_stage(
                    "处理线稿",
                    lambda callback: _process_stage_with_masks(
                        current_scaled,
                        process_settings,
                        callback,
                        protected_mask,
                        deleted_mask,
                    ),
                )
            except Exception as exc:
                st.error(f"处理线稿失败：{exc}")
            else:
                _put_stage(_PROCESS_CACHE_KEY, process_signature, processed_stage)
                st.session_state[_PROCESS_NOTICE_KEY] = (
                    "处理线稿预览已生成；保护分支已按重新生成规则清空，"
                    "若缩放阶段由本次自动补算，其缓存也已同步更新。"
                )
                st.rerun(scope="app")

        processed_stage: SketchProcessedStage | None = _get_valid_stage(_PROCESS_CACHE_KEY, process_signature)
        if processed_stage is None:
            if passthrough_enabled:
                _render_preview_placeholder("点击上方按钮，将缩放结果原样作为当前结果并传给下一步。")
            else:
                _render_preview_placeholder("点击上方按钮，只执行线稿识别、断线连接和小区域过滤。")
        else:
            primary_preview_image = (
                processed_stage.pre_short_curve_image
                if exclude_curve_length_enabled
                and processed_stage.pre_short_curve_image is not None
                else processed_stage.binary_image
            )
            preview_caption = (
                "直通结果与上一步缩放图一致，未执行颜色识别或清理。"
                if passthrough_enabled
                else (
                    "该图是排除短曲线前的线稿识别与基础清理结果。"
                    if exclude_curve_length_enabled
                    else "未执行骨架和拟合。"
                )
            )
            _render_image_at_preview_scale(
                primary_preview_image,
                f"处理线稿画布 {primary_preview_image.width} × {primary_preview_image.height} px；{preview_caption}",
                display_width=display_width,
                display_height=display_height,
            )
            if exclude_curve_length_enabled:
                cleanup = processed_stage.cleanup_summary
                st.markdown("#### 排除短曲线后")
                _render_image_at_preview_scale(
                    processed_stage.binary_image,
                    (
                        f"排除长度小于 {float(exclude_curve_length_px):.1f}px 的逻辑曲线后："
                        f"删除 {int(cleanup.get('excluded_curve_count', 0))} 条，"
                        f"剩余 {int(cleanup.get('remaining_curve_count', 0))} 条；"
                        "该结果已设为删除编辑和下一阶段的默认输入。"
                    ),
                    display_width=display_width,
                    display_height=display_height,
                )

    # ───────────────────────── 阶段 3：中心骨架 ─────────────────────────
    with tab_skeleton:
        auto_thin = st.checkbox(
            "自动把宽线细化为单像素中心骨架", value=True,
            key="sketch_to_gia_auto_thin"
        )
        if auto_thin:
            wide_line_search_radius = st.selectbox(
                "宽线探索半径", [0, 2, 4, 6, 8], index=2,
                key="sketch_wide_line_radius"
            )
        else:
            wide_line_search_radius = 0
        if passthrough_enabled and not auto_thin:
            st.caption("直通且关闭自动细化：本阶段不会修改图像，处理线稿会原样传给曲线拟合。")
        else:
            st.caption("这一阶段会读取线稿识别与清理后的最终输入并展示中心骨架")

        skeleton_payload = {
            "upstream": process_signature,
            "auto_thin_wide_lines": bool(auto_thin),
            "wide_line_search_radius": int(wide_line_search_radius),
        }
        skeleton_signature = _stage_signature(raw, "skeleton", skeleton_payload)
        skeleton_clicked = st.button(
            "生成 / 刷新中心骨架预览", type="primary",
            key="sketch_preview_skeleton", use_container_width=True
        )

        skeleton_settings = SketchGiaSettings(
            auto_thin_wide_lines=bool(auto_thin),
            wide_line_search_radius=int(wide_line_search_radius),
            exclude_curve_length_px=0.0,
        )

        if skeleton_clicked:
            try:
                current_processed = _get_valid_stage(_PROCESS_CACHE_KEY, process_signature)
                if current_processed is None:
                    current_scaled = _get_valid_stage(_SCALE_CACHE_KEY, scale_signature)
                    if current_scaled is None:
                        current_scaled = scale_image(image, int(scale_x_percent), int(scale_y_percent))
                        _put_stage(_SCALE_CACHE_KEY, scale_signature, current_scaled)
                    current_processed = _run_stage(
                        "补算处理线稿",
                        lambda callback: _process_stage_with_masks(
                            current_scaled,
                            process_settings,
                            callback,
                            protected_mask,
                            deleted_mask,
                        ),
                    )
                    _put_stage(_PROCESS_CACHE_KEY, process_signature, current_processed)
                skeleton_stage = _run_stage(
                    "中心骨架",
                    lambda callback: skeletonize_sketch_stage(current_processed, skeleton_settings, callback),
                )
            except Exception as exc:
                st.error(f"中心骨架生成失败：{exc}")
            else:
                _put_stage(_SKELETON_CACHE_KEY, skeleton_signature, skeleton_stage)
                _show_stage_update(
                    "中心骨架预览已生成；本次补算的缩放图和处理线稿缓存也已同步更新。"
                )

        skeleton_stage: SketchSkeletonStage | None = _get_valid_stage(_SKELETON_CACHE_KEY, skeleton_signature)
        if skeleton_stage is None:
            _render_preview_placeholder("点击上方按钮生成中心骨架。若前面的预览缺失或参数已变，只会先补算必要的前置阶段。")
        else:
            _render_image_at_preview_scale(
                skeleton_stage.skeleton_image,
                f"中心骨架画布 {skeleton_stage.skeleton_image.width} × {skeleton_stage.skeleton_image.height} px；未执行最终拟合。",
                display_width=display_width,
                display_height=display_height,
            )
            cleanup = skeleton_stage.cleanup_summary
            metric_a, metric_b, metric_c, metric_d = st.columns(4)
            with metric_a:
                st.metric("逻辑曲线", int(cleanup.get("logical_curve_count", 0)))
            with metric_b:
                st.metric("平滑叉点配对", int(cleanup.get("smooth_pair_count", 0)))
            with metric_c:
                st.metric("清理阶段排除短曲线", int(cleanup.get("excluded_curve_count", 0)))
            with metric_d:
                st.metric("剩余曲线", int(cleanup.get("remaining_curve_count", 0)))

    # ───────────────────────── 阶段 4：最终拟合 ─────────────────────────
    with tab_fit:
        simplify_tolerance = st.number_input(
            "曲线合并误差阈值（px）", 0.0, 100.0, 1.5, 0.1,
            key="sketch_simplify_tolerance"
        )
        min_segment_length = st.number_input(
            "排除线段长度（px，预算前过滤）",
            0.0,
            1000.0,
            2.0,
            0.5,
            key="sketch_min_segment_length",
            help="这是拟合阶段的独立过滤：先排除长度小于该值的骨架路径，再进行曲线简化和基元预算；不会替代清理 Tab 的逻辑曲线长度排除。",
        )
        max_primitives = st.number_input(
            "期望最大基元数", 1, 200000, 500, 50,
            key="sketch_max_primitives"
        )
        st.info("最终拟合严格读取上一个 Tab 当前有效的中心骨架预览图。该预览缺失或参数过期时，才自动补算并写回预览缓存。算法随后探索误差阈值，使数量尽量靠近期望值，并允许约 12% 浮动。")

        st.markdown("#### 最终米制大小")
        lock_output_aspect = st.checkbox(
            "锁定输出宽高比", value=True, key="sketch_lock_output_aspect"
        )
        target_width_m = st.number_input(
            "总宽度 X（米）", 0.01, 500.0, 10.0, 0.01,
            key="sketch_target_width"
        )
        target_aspect_height = float(target_width_m) * scaled_height / max(scaled_width, 1)
        if lock_output_aspect:
            target_height_m = max(0.01, target_aspect_height)
            st.number_input(
                "总高度 Z（米）", 0.01, 500.0, float(target_height_m), 0.01,
                disabled=True, key="sketch_target_height_locked"
            )
        else:
            target_height_m = st.number_input(
                "总高度 Z（米）", 0.01, 500.0, 10.0, 0.01,
                key="sketch_target_height"
            )

        selected_type = st.selectbox(
            "线段基元", LINE_PRIMITIVE_NAMES,
            index=LINE_PRIMITIVE_NAMES.index("长方体"),
            key="sketch_to_gia_primitive"
        )
        axis_label = st.selectbox(
            "长边与线边厚度轴",
            ["X 为长边，Z 为线边厚度", "Z 为长边，X 为线边厚度"],
            index=0, key="sketch_to_gia_long_axis"
        )
        long_axis = LONG_AXIS_X if axis_label.startswith("X") else LONG_AXIS_Z
        line_thickness_m = st.number_input(
            "线边厚度（米）", 0.0001, 50.0, 0.01, 0.001,
            format="%.4f", key="sketch_to_gia_line_thickness"
        )
        st.caption("线边厚度同时控制平面内短轴和 Y 向厚度；位置、旋转、缩放均为连续期望值。")


        settings = SketchGiaSettings(
            source_mode=source_mode,
            scale_x_percent=int(scale_x_percent),
            scale_y_percent=int(scale_y_percent),
            target_width_m=float(target_width_m),
            target_height_m=float(target_height_m),
            edge_operator=edge_operator,
            edge_params=edge_params,
            blur_kernel=int(blur_kernel),
            line_color_mode=line_color_mode,
            line_rgb=line_rgb,
            line_rgb_tolerance=int(line_rgb_tolerance),
            nonzero_threshold=int(nonzero_threshold),
            alpha_threshold=int(alpha_threshold),
            auto_explore_polarity=bool(auto_explore_polarity),
            auto_thin_wide_lines=bool(auto_thin),
            wide_line_search_radius=int(wide_line_search_radius),
            close_kernel=int(close_kernel),
            remove_small_components=int(min_component),
            exclude_curve_length_px=float(exclude_curve_length_px),
            simplify_tolerance_px=float(simplify_tolerance),
            min_segment_length_px=float(min_segment_length),
            max_primitives=int(max_primitives),
            budget_slack_ratio=0.12,
            template_id=TYPE_NAME_TO_TEMPLATE_ID[selected_type],
            long_axis=long_axis,
            line_width_m=float(line_thickness_m),
            depth_m=float(line_thickness_m),
            max_primitive_length_m=50.0,
            add_white_backing=False,
            backing_thickness_m=0.01,
            output_rgb=(0, 0, 0),
            collision_mode=COLLISION_MODE_OFF,
        )

        fit_payload = {
            "upstream": skeleton_signature,
            "simplify_tolerance_px": float(simplify_tolerance),
            "min_segment_length_px": float(min_segment_length),
            "max_primitives": int(max_primitives),
            "budget_slack_ratio": 0.12,
            "target_width_m": float(target_width_m),
            "target_height_m": float(target_height_m),
            "template_id": int(settings.template_id),
            "long_axis": long_axis,
            "line_width_m": float(line_thickness_m),
            "depth_m": float(line_thickness_m),
        }
        fit_signature = _stage_signature(raw, "fit", fit_payload)

        def ensure_processed_stage() -> SketchProcessedStage:
            current = _get_valid_stage(_PROCESS_CACHE_KEY, process_signature)
            if current is not None:
                return current
            current_scaled = _get_valid_stage(_SCALE_CACHE_KEY, scale_signature)
            if current_scaled is None:
                current_scaled = scale_image(image, int(scale_x_percent), int(scale_y_percent))
                _put_stage(_SCALE_CACHE_KEY, scale_signature, current_scaled)
            current = _run_stage(
                "补算处理线稿",
                lambda callback: _process_stage_with_masks(
                    current_scaled,
                    process_settings,
                    callback,
                    protected_mask,
                    deleted_mask,
                ),
            )
            _put_stage(_PROCESS_CACHE_KEY, process_signature, current)
            return current

        def ensure_skeleton_stage() -> tuple[SketchProcessedStage, SketchSkeletonStage]:
            current_processed = ensure_processed_stage()
            current_skeleton = _get_valid_stage(_SKELETON_CACHE_KEY, skeleton_signature)
            if current_skeleton is None:
                current_skeleton = _run_stage(
                    "补算中心骨架",
                    lambda callback: skeletonize_sketch_stage(current_processed, settings, callback),
                )
                _put_stage(_SKELETON_CACHE_KEY, skeleton_signature, current_skeleton)
            return current_processed, current_skeleton

        def ensure_fit_stage(*, force: bool) -> Any:
            current_fit = None if force else _get_valid_stage(_FIT_CACHE_KEY, fit_signature)
            if current_fit is not None:
                return current_fit
            current_processed, current_skeleton = ensure_skeleton_stage()
            current_fit = _run_stage(
                "最终拟合",
                lambda callback: fit_sketch_stage(current_processed, current_skeleton, settings, callback),
            )
            _put_stage(_FIT_CACHE_KEY, fit_signature, current_fit)
            return current_fit

        fit_clicked = st.button(
            "生成 / 刷新最终拟合预览", type="primary",
            key="sketch_preview_fit", use_container_width=True
        )
        if fit_clicked:
            try:
                result = ensure_fit_stage(force=True)
            except Exception as exc:
                st.error(f"最终拟合失败：{exc}")
            else:
                _show_stage_update(
                    f"最终拟合预览已生成：{result.summary['output']['line_object_count']} 个线段基元；"
                    "拟合严格使用上一步中心骨架预览图；本次自动补算的前置结果已写回对应缓存。"
                )

        result = _get_valid_stage(_FIT_CACHE_KEY, fit_signature)
        if result is None:
            _render_preview_placeholder("点击上方按钮执行最终曲线拟合。拟合输入就是上一个 Tab 的中心骨架预览图；若该预览缺失或已过期，会先自动生成并写回缓存。")
        else:
            _render_image_at_preview_scale(
                result.final_preview,
                f"最终拟合画布 {result.final_preview.width} × {result.final_preview.height} px；位置、旋转和缩放 step=0。",
                display_width=display_width,
                display_height=display_height,
            )
            budget = result.summary["budget"]
            metric_a, metric_b, metric_c, metric_d, metric_e = st.columns(5)
            with metric_a:
                st.metric("线段基元", result.summary["output"]["line_object_count"])
            with metric_b:
                st.metric("拟合线段", result.summary["output"]["fitted_segment_count"])
            with metric_c:
                st.metric("曲线链", result.summary["topology"]["curve_path_count"])
            with metric_d:
                st.metric("拟合排除短线", int(budget.get("excluded_short_segment_path_count", 0)))
            with metric_e:
                st.metric("实际误差阈值", f"{budget['effective_simplify_tolerance_px']:.3f}px")

    # ─────────────────────────────── 矩形带拉升（实验） ───────────────────────────────
    with tab_ribbon:
        ribbon_uses_excluded_input = bool(exclude_curve_length_enabled)
        ribbon_input_source = (
            "excluded_skeleton_preview_image"
            if ribbon_uses_excluded_input
            else "previous_processed_preview_image"
        )
        st.info(
            "这是与原曲线拟合完全分离的可选实验功能。它读取当前有效的上一步图片，"
            "使用 OpenCV 检测中心线段、估计局部线宽并生成旋转矩形，"
            "并沿 Y 轴拉升成长方体。它不是像素方块拟合。"
        )
        if ribbon_uses_excluded_input:
            st.caption(
                "已开启排除短曲线：本阶段严格读取“排除短曲线后”的图片作为输入，"
                "不会再读取排除前的处理线稿。"
            )
        else:
            st.caption(
                "未开启排除短曲线：本阶段读取处理线稿预览，以保留原线稿宽度。"
            )

        ribbon_lock_output_aspect = st.checkbox(
            "锁定矩形带输出宽高比", value=True, key="sketch_ribbon_lock_output_aspect"
        )
        ribbon_target_width_m = st.number_input(
            "矩形带总宽度 X（米）", 0.01, 500.0, 10.0, 0.01, key="sketch_ribbon_target_width"
        )
        ribbon_target_aspect_height = float(ribbon_target_width_m) * scaled_height / max(scaled_width, 1)
        if ribbon_lock_output_aspect:
            ribbon_target_height_m = max(0.01, ribbon_target_aspect_height)
            st.number_input(
                "矩形带总高度 Z（米）", 0.01, 500.0, float(ribbon_target_height_m), 0.01,
                disabled=True, key="sketch_ribbon_target_height_locked"
            )
        else:
            ribbon_target_height_m = st.number_input(
                "矩形带总高度 Z（米）", 0.01, 500.0, 10.0, 0.01, key="sketch_ribbon_target_height"
            )

        col_ra, col_rc, col_rd = st.columns(3)
        with col_ra:
            ribbon_straightness_tolerance_px = st.number_input(
                "OpenCV 最大断点连接距离（px）", 0.0, 64.0, 1.5, 0.1,
                key="sketch_ribbon_straightness_tolerance"
            )
        with col_rc:
            ribbon_minimum_length_px = st.number_input(
                "最短矩形长度（px）", 0.0, 1000.0, 2.0, 0.5,
                key="sketch_ribbon_minimum_length"
            )
        with col_rd:
            ribbon_max_primitives = st.number_input(
                "期望最大矩形基元数", 1, 200000, 500, 50,
                key="sketch_ribbon_max_primitives"
            )

        col_re, col_rf, col_rg, col_rh = st.columns(4)
        with col_re:
            ribbon_width_scale = st.number_input(
                "识别线宽倍率", 0.05, 10.0, 1.0, 0.05,
                key="sketch_ribbon_width_scale"
            )
        with col_rf:
            ribbon_minimum_width_px = st.number_input(
                "最小识别线宽（px）", 0.1, 500.0, 1.0, 0.1,
                key="sketch_ribbon_minimum_width"
            )
        with col_rg:
            ribbon_joint_overlap_px = st.number_input(
                "矩形接头重叠（px）", 0.0, 100.0, 1.0, 0.25,
                key="sketch_ribbon_joint_overlap"
            )
        with col_rh:
            ribbon_target_miss_percent = st.number_input(
                "最大漏覆盖率（%）", 0.0, 100.0, 1.0, 0.1,
                key="sketch_ribbon_target_miss_percent",
                help="覆盖完整度优先。若未达到该目标，算法允许在救援范围内增加局部矩形补片。",
            )

        ribbon_center_search_radius = st.selectbox(
            "中心路径探索半径",
            [0, 2, 4, 6, 8],
            index=2,
            key="sketch_ribbon_center_search_radius",
            help="只用于从宽线区域提取中心路径，不会把图像离散成像素网格。",
        )

        ribbon_axis_label = st.selectbox(
            "长方体长边轴",
            ["X 为长边，Z 为识别线宽", "Z 为长边，X 为识别线宽"],
            index=0,
            key="sketch_ribbon_long_axis",
        )
        ribbon_long_axis = LONG_AXIS_X if ribbon_axis_label.startswith("X") else LONG_AXIS_Z
        st.caption(
            "OpenCV HoughLinesP 负责中心线段检测，distanceTransform 估计真实线宽，"
            "minAreaRect 负责漏覆盖补片。覆盖完整度优先于数量预算；必要时覆盖补片最多允许约 35% 的救援浮动。"
            "Y 轴拉升高度在导出 Tab 设置。"
        )

        ribbon_settings = replace(
            settings,
            target_width_m=float(ribbon_target_width_m),
            target_height_m=float(ribbon_target_height_m),
            long_axis=ribbon_long_axis,
            # 预览只保存二维矩形几何；Y 轴拉升高度在导出阶段应用。
            depth_m=0.01,
            line_width_m=0.01,
            auto_thin_wide_lines=True,
            wide_line_search_radius=int(ribbon_center_search_radius),
            ribbon_straightness_tolerance_px=float(ribbon_straightness_tolerance_px),
            ribbon_minimum_length_px=float(ribbon_minimum_length_px),
            ribbon_max_primitives=int(ribbon_max_primitives),
            ribbon_budget_slack_ratio=0.12,
            ribbon_width_scale=float(ribbon_width_scale),
            ribbon_minimum_width_px=float(ribbon_minimum_width_px),
            ribbon_joint_overlap_px=float(ribbon_joint_overlap_px),
            ribbon_target_miss_ratio=float(ribbon_target_miss_percent) / 100.0,
            ribbon_residual_min_area_px=2,
            ribbon_coverage_rescue_ratio=0.35,
            ribbon_template_id=TYPE_NAME_TO_TEMPLATE_ID["长方体"],
            template_id=TYPE_NAME_TO_TEMPLATE_ID["长方体"],
            add_white_backing=False,
            output_rgb=(0, 0, 0),
            collision_mode=COLLISION_MODE_OFF,
        )

        ribbon_payload = {
            "upstream": (
                skeleton_signature
                if ribbon_uses_excluded_input
                else process_signature
            ),
            "input_source": ribbon_input_source,
            "target_width_m": float(ribbon_target_width_m),
            "target_height_m": float(ribbon_target_height_m),
            "straightness_tolerance_px": float(ribbon_straightness_tolerance_px),
            "minimum_length_px": float(ribbon_minimum_length_px),
            "max_primitives": int(ribbon_max_primitives),
            "budget_slack_ratio": 0.12,
            "width_scale": float(ribbon_width_scale),
            "minimum_width_px": float(ribbon_minimum_width_px),
            "joint_overlap_px": float(ribbon_joint_overlap_px),
            "target_miss_ratio": float(ribbon_target_miss_percent) / 100.0,
            "center_search_radius": int(ribbon_center_search_radius),
            "long_axis": ribbon_long_axis,
            "template_id": int(ribbon_settings.ribbon_template_id),
        }
        ribbon_signature = _stage_signature(raw, "ribbon_mesh", ribbon_payload)

        def ensure_ribbon_stage(*, force: bool) -> Any:
            current_ribbon = None if force else _get_valid_stage(_RIBBON_CACHE_KEY, ribbon_signature)
            if current_ribbon is not None:
                return current_ribbon
            if ribbon_uses_excluded_input:
                current_processed, current_skeleton = ensure_skeleton_stage()
                ribbon_source_preview = current_skeleton.skeleton_image
            else:
                current_processed = ensure_processed_stage()
                ribbon_source_preview = current_processed.binary_image
            current_ribbon = _run_stage(
                "矩形带拉升（实验）",
                lambda callback: fit_ribbon_mesh_stage(
                    current_processed,
                    ribbon_settings,
                    callback,
                    source_preview_image=ribbon_source_preview,
                    source_label=ribbon_input_source,
                ),
            )
            _put_stage(_RIBBON_CACHE_KEY, ribbon_signature, current_ribbon)
            return current_ribbon

        ribbon_clicked = st.button(
            "生成 / 刷新矩形带拉升预览",
            type="primary",
            key="sketch_preview_ribbon",
            use_container_width=True,
        )
        if ribbon_clicked:
            try:
                ribbon_result = ensure_ribbon_stage(force=True)
            except Exception as exc:
                st.error(f"矩形带拉升拟合失败：{exc}")
            else:
                _show_stage_update(
                    f"矩形带拉升预览已生成：{ribbon_result.summary['output']['line_object_count']} 个旋转矩形基元。"
                )

        ribbon_result = _get_valid_stage(_RIBBON_CACHE_KEY, ribbon_signature)
        if ribbon_result is None:
            _render_preview_placeholder(
                "点击上方按钮，使用 OpenCV 从处理线稿中检测中心线段并生成旋转矩形。不会运行像素网格算法。"
            )
        else:
            _render_image_at_preview_scale(
                ribbon_result.final_preview,
                f"矩形带拟合画布 {ribbon_result.final_preview.width} × {ribbon_result.final_preview.height} px；灰色为原线稿区域，黑色为旋转矩形覆盖。",
                display_width=display_width,
                display_height=display_height,
            )
            ribbon_summary = ribbon_result.summary["ribbon_mesh"]
            metric_a, metric_b, metric_c, metric_d, metric_e = st.columns(5)
            with metric_a:
                st.metric("旋转矩形基元", ribbon_result.summary["output"]["line_object_count"])
            with metric_b:
                st.metric("中心路径", ribbon_summary["center_path_count"])
            with metric_c:
                st.metric("覆盖 IoU", f"{float(ribbon_summary['coverage']['iou']) * 100.0:.1f}%")
            with metric_d:
                st.metric("漏覆盖", f"{float(ribbon_summary['coverage']['miss_ratio']) * 100.0:.1f}%")
            with metric_e:
                st.metric("覆盖补片", int(ribbon_summary.get("residual_patch_count", 0)))


    # ─────────────────────────────── 导出 ───────────────────────────────
    with tab_export:
        st.markdown("## 导出 GIA")
        st.info("导出时可选择使用“曲线拟合”或“矩形带拉升（实验）”结果。系统会优先复用对应有效预览；没有或已过期时，只补算该来源所需阶段。")

        export_source_label = st.radio("导出来源", ["曲线拟合结果", "矩形带拉升实验结果"], horizontal=True, key="sketch_export_source")

        ribbon_export_depth_m = None
        if export_source_label == "矩形带拉升实验结果":
            ribbon_export_depth_m = st.number_input(
                "Y 轴拉升高度（米）",
                0.0001,
                50.0,
                0.01,
                0.001,
                format="%.4f",
                key="sketch_ribbon_export_depth_m",
                help="该参数只影响导出的长方体高度，不影响二维矩形拟合与预览缓存。",
            )

        st.markdown("### 导出外观与碰撞")
        add_white_backing = st.checkbox(
            "加入纯白长方体叠底", value=True,
            key="sketch_to_gia_add_backing"
        )
        backing_thickness_m = st.number_input(
            "叠底厚度（米）", 0.0001, 50.0, 0.01, 0.001,
            format="%.4f", disabled=not add_white_backing,
            key="sketch_to_gia_backing_thickness"
        )

        st.markdown("#### 线稿颜色")
        col_out_r, col_out_g, col_out_b = st.columns(3)
        with col_out_r:
            output_r = st.number_input("输出 R", 0, 255, 0, 1, key="sketch_output_r")
        with col_out_g:
            output_g = st.number_input("输出 G", 0, 255, 0, 1, key="sketch_output_g")
        with col_out_b:
            output_b = st.number_input("输出 B", 0, 255, 0, 1, key="sketch_output_b")

        collision_label = st.radio(
            "碰撞模式", ["关闭碰撞", "开启原生碰撞", "开启碰撞和攀爬"],
            horizontal=True, key="sketch_to_gia_collision"
        )
        collision_mode = {
            "关闭碰撞": COLLISION_MODE_OFF,
            "开启原生碰撞": COLLISION_MODE_NATIVE,
            "开启碰撞和攀爬": COLLISION_MODE_NATIVE_AND_CLIMB,
        }[collision_label]
        enable_out_of_range_run = st.checkbox(
            "超出范围仍运行", value=False, key="sketch_to_gia_enable_out_of_range_run",
            help="开启后，元件超出加载范围时仍保持运行。",
        )
        display_mode_label = st.selectbox(
            "超出范围显示", ["默认", "永久显示", "永久以最高精度显示"],
            key="sketch_to_gia_out_of_range_display_mode",
            help="控制元件超出加载范围后的显示策略。",
        )
        out_of_range_display_mode = {"默认": 0, "永久显示": 1, "永久以最高精度显示": 2}[display_mode_label]

        export_base_settings = ribbon_settings if export_source_label == "矩形带拉升实验结果" else settings
        export_result_signature = ribbon_signature if export_source_label == "矩形带拉升实验结果" else fit_signature
        export_settings = replace(
            export_base_settings,
            depth_m=(
                float(ribbon_export_depth_m)
                if export_source_label == "矩形带拉升实验结果" and ribbon_export_depth_m is not None
                else float(export_base_settings.depth_m)
            ),
            add_white_backing=bool(add_white_backing),
            backing_thickness_m=float(backing_thickness_m),
            output_rgb=(int(output_r), int(output_g), int(output_b)),
            collision_mode=collision_mode,
            enable_out_of_range_run=bool(enable_out_of_range_run),
            out_of_range_display_mode=int(out_of_range_display_mode),
        )

        template_path_text = st.text_input(
            "模板 GIA", value=str(DEFAULT_TEMPLATE_GIA), key="sketch_template_path"
        )
        output_name = st.text_input(
            "输出文件名", value=f"{Path(uploaded.name).stem}_sketch.gia",
            key="sketch_output_name"
        )

        export_payload = {
            "result_signature": export_result_signature,
            "export_source": export_source_label,
            "template_path": str(Path(template_path_text)),
            "depth_m": float(export_settings.depth_m),
            "add_white_backing": bool(add_white_backing),
            "backing_thickness_m": float(backing_thickness_m),
            "output_rgb": export_settings.output_rgb,
            "collision_mode": collision_mode,
            "enable_out_of_range_run": bool(enable_out_of_range_run),
            "out_of_range_display_mode": int(out_of_range_display_mode),
        }
        export_signature = _stage_signature(raw, "export", export_payload)

        export_clicked = st.button("生成线稿 GIA", type="primary", use_container_width=True)
        if export_clicked:
            try:
                result = ensure_ribbon_stage(force=False) if export_source_label == "矩形带拉升实验结果" else ensure_fit_stage(force=False)
                gia_data, summary, objects_json = _run_stage(
                    "GIA 导出",
                    lambda callback: build_sketch_gia_bytes(
                        result=result,
                        settings=export_settings,
                        template_path=Path(template_path_text),
                        progress_callback=callback,
                    ),
                )
            except Exception as exc:
                st.error(f"线稿 GIA 生成失败：{exc}")
            else:
                st.session_state[_EXPORT_CACHE_KEY] = {
                    "signature": export_signature,
                    "gia_data": gia_data,
                    "summary": summary,
                    "objects_json": objects_json,
                }
                _show_stage_update(
                    f"GIA 已生成：{summary['gia']['asset_count']} 个元件，{len(gia_data):,} bytes；"
                    f"当前导出来源：{export_source_label}。下载按钮已在下方生成，页面不会强制跳转或改变焦点。"
                )
        export = st.session_state.get(_EXPORT_CACHE_KEY)
        if not isinstance(export, dict) or export.get("signature") != export_signature:
            st.caption("当前导出参数还没有对应的 GIA 文件。点击“生成线稿 GIA”后，下载按钮会显示在这里。")
            export = None

        download_name = output_name.strip() or "sketch.gia"
        if not download_name.lower().endswith(".gia"):
            download_name += ".gia"

        if export is not None:
            result = (
                _get_valid_stage(_RIBBON_CACHE_KEY, ribbon_signature)
                if export_source_label == "矩形带拉升实验结果"
                else _get_valid_stage(_FIT_CACHE_KEY, fit_signature)
            )
            if result is not None:
                with st.expander("分析摘要", expanded=False):
                    st.json(export["summary"], expanded=False)

            gia_col, object_col, summary_col = st.columns(3)
            with gia_col:
                st.download_button(
                    "下载线稿 GIA", export["gia_data"], file_name=download_name,
                    mime="application/octet-stream", use_container_width=True
                )
            with object_col:
                st.download_button(
                    "下载线段对象 JSON", export["objects_json"].encode("utf-8"),
                    file_name=f"{Path(download_name).stem}.objects.json",
                    mime="application/json", use_container_width=True
                )
            with summary_col:
                st.download_button(
                    "下载生成摘要 JSON",
                    (json.dumps(export["summary"], ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                    file_name=f"{Path(download_name).stem}.summary.json",
                    mime="application/json", use_container_width=True
                )
