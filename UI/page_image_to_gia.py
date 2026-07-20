from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import asdict
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from UI.gia_image import (
    COLLISION_MODE_NATIVE,
    COLLISION_MODE_NATIVE_AND_CLIMB,
    COLLISION_MODE_OFF,
    DEFAULT_TEMPLATE_GIA,
    ImageGiaSettings,
    build_image_final_preview,
    build_image_gia_bytes,
    grid_layout,
    grid_units_to_meters,
    load_rgba_image,
    meters_to_grid_units,
    resize_for_pixel_budget,
    scale_image_for_parsing_xy,
)
from UI.build_gia_objects import TYPE_NAME_TO_TEMPLATE_ID
from UI.task_ui import run_heavy_action


RESIZE_BOX_COMPONENT = components.declare_component(
    "resize_box",
    path=str(Path(__file__).resolve().parent / "components" / "resize_box"),
)
_FINAL_PREVIEW_CACHE_KEY = "image_to_gia_final_preview"


def image_data_url(image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def sync_dragged_scale(component_value: dict | None) -> None:
    if not isinstance(component_value, dict):
        return
    scale_x = int(component_value.get("scale_x_percent") or 100)
    scale_y = int(component_value.get("scale_y_percent") or scale_x)
    scale_x = max(1, min(400, scale_x))
    scale_y = max(1, min(400, scale_y))
    if (
        st.session_state.get("image_to_gia_scale_x_percent", 100) != scale_x
        or st.session_state.get("image_to_gia_scale_y_percent", 100) != scale_y
    ):
        st.session_state["image_to_gia_scale_x_percent"] = scale_x
        st.session_state["image_to_gia_scale_y_percent"] = scale_y
        st.rerun()


def final_preview_signature(image, settings: ImageGiaSettings) -> str:
    digest = hashlib.sha256()
    digest.update(image.mode.encode("utf-8"))
    digest.update(str(image.size).encode("ascii"))
    digest.update(image.tobytes())
    digest.update(
        json.dumps(asdict(settings), ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    return digest.hexdigest()


def render_image_to_gia_page() -> None:
    st.markdown("## 1. 图片输入")
    st.caption("上传图片后按像素解析并生成白模 GIA。透明区域可过滤，可选择逐像素生成或颜色矩形合并。")

    uploaded = st.file_uploader(
        "选择图片文件",
        type=["png", "jpg", "jpeg", "webp", "bmp"],
        key="image_to_gia_upload",
    )
    if uploaded is None:
        st.info("先上传图片。透明像素会按 alpha 阈值过滤。")
        return

    try:
        image = load_rgba_image(uploaded.getvalue())
    except Exception as exc:
        st.error(f"图片读取失败：{exc}")
        return

    width_px, height_px = image.size
    preview_col, info_col = st.columns([3, 1])
    with preview_col:
        st.image(image, caption=f"原始尺寸：{width_px} × {height_px} px", use_column_width=True)
    with info_col:
        st.markdown("### 图片信息")
        st.metric("宽度", f"{width_px} px")
        st.metric("高度", f"{height_px} px")
        st.metric("总像素数", f"{width_px * height_px:,}")

    st.markdown("---")
    st.markdown("## 2. 图片解析")
    st.markdown("### 2.1 解析缩放")
    st.caption("拖动缩放框或直接输入 X / Y 百分比，决定进入后续像素解析的图片尺寸。")
    if "image_to_gia_scale_x_percent" not in st.session_state:
        st.session_state["image_to_gia_scale_x_percent"] = 100
    if "image_to_gia_scale_y_percent" not in st.session_state:
        st.session_state["image_to_gia_scale_y_percent"] = 100

    scale_lock_aspect = st.checkbox("等比缩放图片", value=True, key="image_to_gia_scale_lock_aspect")
    dragged = RESIZE_BOX_COMPONENT(
        image_data_url=image_data_url(image),
        image_width=width_px,
        image_height=height_px,
        scale_x_percent=int(st.session_state["image_to_gia_scale_x_percent"]),
        scale_y_percent=int(st.session_state["image_to_gia_scale_y_percent"]),
        lock_aspect=scale_lock_aspect,
        key="image_to_gia_resize_box",
        default=None,
    )
    sync_dragged_scale(dragged)

    # col_scale_x, col_scale_y = st.columns(2)
    # with col_scale_x:
    #     scale_x_percent = st.number_input(
    #         "图片 X 缩放（%）",
    #         min_value=1,
    #         max_value=400,
    #         value=int(st.session_state["image_to_gia_scale_x_percent"]),
    #         step=1,
    #         key="image_to_gia_scale_x_percent",
    #     )
    # if scale_lock_aspect:
    #     scale_y_percent = int(scale_x_percent)
    #     st.session_state["image_to_gia_scale_y_percent"] = st.session_state["image_to_gia_scale_x_percent"]
    #     with col_scale_y:
    #         st.number_input(
    #             "图片 Y 缩放（%）",
    #             min_value=1,
    #             max_value=400,
    #             value=st.session_state["image_to_gia_scale_y_percent"],
    #             step=1,
    #             disabled=True,
    #             key="image_to_gia_scale_y_percent_locked",
    #         )
    # else:
    #     with col_scale_y:
    #         scale_y_percent = st.number_input(
    #             "图片 Y 缩放（%）",
    #             min_value=1,
    #             max_value=400,
    #             value=int(st.session_state["image_to_gia_scale_y_percent"]),
    #             step=1,
    #             key="image_to_gia_scale_y_percent",
    #         )

    scaled_image = scale_image_for_parsing_xy(image, int(st.session_state["image_to_gia_scale_x_percent"]), int(st.session_state["image_to_gia_scale_y_percent"]))
    scaled_width_px, scaled_height_px = scaled_image.size
    col_scaled_a, col_scaled_b = st.columns(2)
    with col_scaled_a:
        st.metric("解析用总像素数", f"{scaled_width_px * scaled_height_px:,}")
    with col_scaled_b:
        st.metric("图片缩放比例", f"X {int(st.session_state['image_to_gia_scale_x_percent'])}% / Y {int(st.session_state['image_to_gia_scale_y_percent'])}%")

    st.markdown("---")
    st.markdown("## 3. 生成尺寸与采样")
    st.markdown("### 3.1 尺寸控制方式")
    size_mode = st.radio(
        "尺寸模式",
        ["调高宽自动计算单个大小", "调单个大小自动计算高宽"],
        horizontal=True,
        key="image_to_gia_size_mode",
    )

    if size_mode == "调高宽自动计算单个大小":
        col_width, col_height, col_lock = st.columns([1, 1, 1])
        with col_lock:
            keep_aspect = st.checkbox("锁定图片宽高比", value=True, key="image_to_gia_keep_aspect")
        with col_width:
            target_width_m = st.number_input(
                "期望总宽度（米）",
                min_value=0.01,
                max_value=50.0,
                value=min(10.0, 50.0),
                step=0.01,
                key="image_to_gia_width_m",
            )
        aspect_height = target_width_m * scaled_height_px / max(scaled_width_px, 1)
        with col_height:
            if keep_aspect:
                target_height_m = max(0.01, min(50.0, aspect_height))
                st.number_input(
                    "期望总高度（米）",
                    min_value=0.01,
                    max_value=50.0,
                    value=target_height_m,
                    step=0.01,
                    disabled=True,
                    key="image_to_gia_height_m_locked",
                )
            else:
                target_height_m = st.number_input(
                    "期望总高度（米）",
                    min_value=0.01,
                    max_value=50.0,
                    value=max(0.01, min(10.0 * scaled_height_px / max(scaled_width_px, 1), 50.0)),
                    step=0.01,
                    key="image_to_gia_height_m",
                )
    else:
        col_cell_x, col_cell_z = st.columns(2)
        with col_cell_x:
            cell_width_input_m = st.number_input(
                "单个像素块宽度 X（米）",
                min_value=0.01,
                max_value=50.0,
                value=0.10,
                step=0.01,
                key="image_to_gia_cell_width_m",
            )
        with col_cell_z:
            cell_height_input_m = st.number_input(
                "单个像素块高度 Z（米）",
                min_value=0.01,
                max_value=50.0,
                value=0.10,
                step=0.01,
                key="image_to_gia_cell_height_m",
            )
        target_width_m = min(50.0, max(0.01, float(cell_width_input_m) * scaled_width_px))
        target_height_m = min(50.0, max(0.01, float(cell_height_input_m) * scaled_height_px))
        st.caption(
            "会先按最大解析像素数得到最终采样网格，再由单个像素块大小反推总高宽。"
            "若超过 50m 会被限制到 50m。"
        )

    st.markdown("### 3.2 采样与透明过滤")
    st.caption("限制解析像素数量可以显著控制生成对象数量；透明度阈值越高，被忽略的半透明像素越多。")

    col_a, col_b = st.columns(2)
    with col_a:
        max_pixels = st.number_input(
            "目标解析像素数",
            min_value=1,
            max_value=40000,
            value=2000,
            step=100,
            key="image_to_gia_max_pixels",
        )
    with col_b:
        alpha_threshold = st.number_input(
            "透明度过滤阈值",
            min_value=1,
            max_value=256,
            value=1,
            step=1,
            key="image_to_gia_alpha_threshold",
        )
        alpha_threshold_mode = st.radio(
            "最终透明度模式",
            ["使用原有透明度", "设置全局统一透明度"],
            horizontal=True,
            key="alpha_threshold_mode",
            index=1,
            )
        st.caption("注意: 在矩形合并时：\n- 参考透明度模式：会区分透明度差异，保留更多细节，基元数量更多\n- 无视透明度模式：忽略透明度差异做合并，减少整体基元数量")
        if alpha_threshold_mode == "设置全局统一透明度":
            alpha_threshold_num = st.number_input(
                "全局统一透明度",
                min_value=0,
                max_value=255,
                value=255,
                step=1,
                key="image_to_gia_global_alpha",
            )
        elif alpha_threshold_mode == "使用原有透明度":
            alpha_threshold_num = -1

    st.markdown("---")
    st.markdown("## 4. 生成规则")
    st.markdown("### 4.1 像素生成算法")
    merge_mode = st.radio(
        "像素生成算法",
        ["逐像素生成", "相近颜色矩形合并"],
        horizontal=True,
        key="image_to_gia_merge_mode",
        index=1,
    )
    if merge_mode == "相近颜色矩形合并":
        color_tolerance = st.number_input(
            "颜色合并容差",
            min_value=0,
            max_value=255,
            value=8,
            step=1,
            key="image_to_gia_color_tolerance",
        )
        st.caption("容差越大，越多相近颜色会合并为同一个矩形；矩形颜色取内部像素平均值。")
    else:
        color_tolerance = 0

    st.markdown("### 4.2 背景处理")
    remove_background = st.checkbox("按 RGB 去掉背景", value=False, key="image_to_gia_remove_background")
    if remove_background:
        col_bg_r, col_bg_g, col_bg_b, col_bg_tol = st.columns(4)
        with col_bg_r:
            background_r = st.number_input("背景 R", min_value=0, max_value=255, value=255, step=1)
        with col_bg_g:
            background_g = st.number_input("背景 G", min_value=0, max_value=255, value=255, step=1)
        with col_bg_b:
            background_b = st.number_input("背景 B", min_value=0, max_value=255, value=255, step=1)
        with col_bg_tol:
            background_tolerance = st.number_input("背景容差", min_value=0, max_value=255, value=0, step=1)
        background_rgb = (int(background_r), int(background_g), int(background_b))
        st.caption("只会从图片四周出发扣掉与该 RGB 接近且连通的背景；不会越过主体轮廓删除内部同色区域。")
    else:
        background_rgb = None
        background_tolerance = 0

    st.markdown("---")
    st.markdown("## 5. 碰撞与元件")
    st.markdown("### 5.1 碰撞模式")
    collision_mode_label = st.radio(
        "碰撞模式",
        ["开启原生碰撞", "开启碰撞和攀爬", "关闭碰撞"],
        index=2,
        horizontal=True,
        key="image_to_gia_collision_mode",
    )
    collision_mode = {
        "开启原生碰撞": COLLISION_MODE_NATIVE,
        "开启碰撞和攀爬": COLLISION_MODE_NATIVE_AND_CLIMB,
        "关闭碰撞": COLLISION_MODE_OFF,
    }[collision_mode_label]
    st.caption(
        "原生碰撞：可阻挡角色但不可攀爬；碰撞和攀爬：同时启用阻挡与攀爬；"
        "关闭碰撞：角色可直接穿过生成元件。"
    )

    st.markdown("### 5.2 生成元件")
    type_names = list(TYPE_NAME_TO_TEMPLATE_ID)
    selected_type = st.selectbox(
        "生成元件类型",
        type_names,
        index=type_names.index("长方体"),
        key="image_to_gia_template_type",
    )
    template_path_text = st.text_input(
        "模板 GIA",
        value=str(DEFAULT_TEMPLATE_GIA),
        key="image_to_gia_template_path",
    )
    st.markdown("### 5.3 输出设置")
    enable_out_of_range_run = st.checkbox(
        "超出范围仍运行", value=False, key="image_to_gia_enable_out_of_range_run",
        help="开启后，元件超出加载范围时仍保持运行。",
    )
    display_mode_label = st.selectbox(
        "超出范围显示", ["默认", "永久显示", "永久以最高精度显示"],
        key="image_to_gia_out_of_range_display_mode",
        help="控制元件超出加载范围后的显示策略。",
    )
    out_of_range_display_mode = {"默认": 0, "永久显示": 1, "永久以最高精度显示": 2}[display_mode_label]
    output_name = st.text_input("输出文件名", value=f"{Path(uploaded.name).stem}_image_pixels.gia")

    st.markdown("---")
    st.markdown("## 6. 生成预览")
    preview_sampled = resize_for_pixel_budget(
        scaled_image,
        int(max_pixels),
        meters_to_grid_units(target_width_m),
        meters_to_grid_units(target_height_m),
    )
    sampled_width_px, sampled_height_px = preview_sampled.size
    if size_mode == "调单个大小自动计算高宽":
        target_width_m = min(50.0, max(0.01, float(cell_width_input_m) * sampled_width_px))
        target_height_m = min(50.0, max(0.01, float(cell_height_input_m) * sampled_height_px))
        preview_sampled = resize_for_pixel_budget(
            scaled_image,
            int(max_pixels),
            meters_to_grid_units(target_width_m),
            meters_to_grid_units(target_height_m),
        )
        sampled_width_px, sampled_height_px = preview_sampled.size
    preview_layout = grid_layout(target_width_m, target_height_m, sampled_width_px, sampled_height_px)
    preview_cell_width_m = grid_units_to_meters(preview_layout["cell_width_units"])
    preview_cell_height_m = grid_units_to_meters(preview_layout["cell_height_units"])
    preview_block_height_m = min(preview_cell_width_m, preview_cell_height_m)
    expected_ratio = (target_width_m * target_height_m) ** 0.5
    metric_size, metric_grid, metric_thickness = st.columns(3)
    with metric_size:
        st.metric("期望总大小", f"高 {target_height_m:.3f} m × 宽 {target_width_m:.3f} m")
    with metric_grid:
        st.metric("采样像素数", f"{sampled_width_px * sampled_height_px:,}")
    with metric_thickness:
        st.metric("自动像素块厚度", f"{preview_block_height_m:.2f} m")
    st.caption(f"采样网格：{sampled_width_px} x {sampled_height_px}。透明像素会在生成时按阈值过滤。")
    st.caption(
        "坐标规则：每个像素块的 position 是包围盒中心；scale.x / scale.z 是该块实际宽高，"
        "scale.y 是厚度。生成时优先保证相邻单位严丝合缝，实际总高宽会落在 0.01 米网格上并尽量接近期望值。"
    )
    if expected_ratio <= 0:
        st.warning("目标尺寸无效。")
        return

    settings = ImageGiaSettings(
        target_width_m=float(target_width_m),
        target_height_m=float(target_height_m),
        max_pixels=int(max_pixels),
        alpha_threshold=int(alpha_threshold),
        alpha_threshold_num=int(alpha_threshold_num),
        template_id=TYPE_NAME_TO_TEMPLATE_ID[selected_type],
        merge_rectangles=merge_mode == "相近颜色矩形合并",
        color_tolerance=int(color_tolerance),
        background_rgb=background_rgb,
        background_tolerance=int(background_tolerance),
        collision_mode=collision_mode,
        enable_out_of_range_run=bool(enable_out_of_range_run),
        out_of_range_display_mode=int(out_of_range_display_mode),
    )
    preview_signature = final_preview_signature(scaled_image, settings)
    st.markdown("### 6.1 最终图片预览")
    st.caption("按当前导出对象反绘，包含采样、透明过滤、背景移除、矩形合并、颜色和最终透明度效果。")
    preview_clicked = st.button(
        "生成 / 刷新最终图片预览",
        key="image_to_gia_build_final_preview",
        use_container_width=True,
    )
    if preview_clicked:
        try:
            def build_preview(callback):
                callback(5, "正在准备最终图片预览")
                value = build_image_final_preview(
                    scaled_image,
                    settings,
                )
                callback(100, "最终图片预览生成完成")
                return value

            final_preview, final_preview_summary = run_heavy_action(
                "最终图片预览",
                build_preview,
            )
        except Exception as exc:
            st.error(f"最终图片预览生成失败：{exc}")
        else:
            st.session_state[_FINAL_PREVIEW_CACHE_KEY] = {
                "signature": preview_signature,
                "image": final_preview,
                "summary": final_preview_summary,
            }

    cached_preview = st.session_state.get(_FINAL_PREVIEW_CACHE_KEY)
    if isinstance(cached_preview, dict) and cached_preview.get("signature") == preview_signature:
        final_preview = cached_preview["image"]
        final_preview_summary = cached_preview["summary"]
        st.image(
            final_preview,
            caption=(
                f"最终图片：{final_preview.width} × {final_preview.height} px；"
                f"对应 {int(final_preview_summary['object_count']):,} 个 GIA 生成单位。"
            ),
            use_container_width=True,
        )
    else:
        st.info("当前参数尚未生成最终图片预览。点击上方按钮后显示；参数变化后需要重新生成。")

    st.markdown("---")
    st.markdown("## 7. 生成与下载")
    st.caption("确认上方参数后开始生成。生成完成后可下载 GIA、对象 JSON 和摘要 JSON。")
    submitted = st.button("生成图片 GIA", type="primary", key="image_to_gia_build", use_container_width=True)
    if not submitted:
        return

    try:
        def build_gia(callback):
            callback(5, "正在解析像素并准备 GIA 对象")
            value = build_image_gia_bytes(
                image=scaled_image,
                settings=settings,
                template_path=Path(template_path_text),
            )
            callback(100, "图片 GIA 生成完成")
            return value

        gia_data, summary, objects_json = run_heavy_action(
            "图片 GIA 导出",
            build_gia,
        )
    except Exception as exc:
        st.error(f"图片 GIA 生成失败：{exc}")
        return

    actual_size = summary["image"]["actual_size_m"]
    st.success(
        f"GIA 已生成：{summary['image']['object_count']} 个生成单位，{len(gia_data)} bytes；"
        f"解析尺寸：{scaled_width_px} x {scaled_height_px} px；"
        f"实际总大小：高 {actual_size['height']:.2f} m x 宽 {actual_size['width']:.2f} m"
    )
    with st.expander("生成摘要", expanded=False):
        st.json(summary, expanded=False)
    download_name = output_name.strip() or "image_pixels.gia"
    if not download_name.lower().endswith(".gia"):
        download_name += ".gia"
    st.download_button("下载图片 GIA", gia_data, file_name=download_name, mime="application/octet-stream")
    st.download_button(
        "下载像素对象 JSON",
        objects_json.encode("utf-8"),
        file_name=f"{Path(download_name).stem}.objects.json",
        mime="application/json",
    )
    st.download_button(
        "下载生成摘要 JSON",
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        file_name=f"{Path(download_name).stem}.summary.json",
        mime="application/json",
    )
