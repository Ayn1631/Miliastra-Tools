from __future__ import annotations

from typing import Any

import streamlit as st

from miliastra_core.sketch_config import (
    COLOR_MODE_AUTO_NONZERO,
    COLOR_MODE_PASSTHROUGH,
    COLOR_MODE_SPECIFIED,
    EDGE_OPERATORS,
    SOURCE_EDGE,
    SOURCE_LINE_ART,
    EdgeProcessingConfig,
    LineCleanupConfig,
    SketchProcessingConfig,
    default_operator_params,
)


def _option_index(options: list[int] | tuple[str, ...], value: int | str) -> int:
    try:
        return list(options).index(value)
    except ValueError:
        return 0


def render_edge_operator_controls(
    operator: str,
    *,
    key_prefix: str,
    defaults: dict[str, int | float] | None = None,
) -> dict[str, int | float]:
    params = {**default_operator_params(operator), **(defaults or {})}
    if operator == "Canny":
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            low = st.number_input(
                "低阈值",
                0,
                255,
                int(params["low_threshold"]),
                1,
                key=f"{key_prefix}_canny_low",
                help="Canny 的弱边缘阈值；越低保留的细弱线条越多，也更容易引入噪点。",
            )
        with col_b:
            high = st.number_input(
                "高阈值",
                0,
                255,
                int(params["high_threshold"]),
                1,
                key=f"{key_prefix}_canny_high",
                help="Canny 的强边缘阈值；高于此值的像素会作为可靠边缘起点。",
            )
        with col_c:
            apertures = [3, 5, 7]
            aperture = st.selectbox(
                "Canny 内部 Sobel 核尺寸",
                apertures,
                index=_option_index(apertures, int(params["aperture_size"])),
                key=f"{key_prefix}_canny_aperture",
                help=(
                    "核从 3 增大到 5 或 7 时，梯度响应尺度通常也会变大；如果低、高阈值保持不变，"
                    "超过阈值的像素可能增多，因此显示的线条可能反而更多。\n\n"
                    "一般建议保持 3。选择 5 或 7 时，通常需要同步提高低阈值和高阈值；"
                    "如果目的只是减少噪点，请优先增大“高斯模糊核尺寸”。"
                ),
            )
        return {
            "low_threshold": int(low),
            "high_threshold": int(high),
            "aperture_size": int(aperture),
        }

    if operator == "Sobel":
        col_a, col_b = st.columns(2)
        with col_a:
            threshold = st.number_input(
                "响应阈值",
                0,
                255,
                int(params["threshold"]),
                1,
                key=f"{key_prefix}_sobel_threshold",
                help="Sobel 梯度响应的截断值；越高只保留越明显的边缘。",
            )
        with col_b:
            kernels = [1, 3, 5, 7]
            kernel = st.selectbox(
                "卷积核尺寸",
                kernels,
                index=_option_index(kernels, int(params["kernel_size"])),
                key=f"{key_prefix}_sobel_kernel",
                help="Sobel 卷积核尺寸；较大核抗噪更强，边缘位置也会更平滑。",
            )
        return {"threshold": int(threshold), "kernel_size": int(kernel)}

    if operator == "Scharr":
        threshold = st.number_input(
            "响应阈值",
            0,
            255,
            int(params["threshold"]),
            1,
            key=f"{key_prefix}_scharr_threshold",
            help="Scharr 梯度响应的截断值；越高则输出边缘越少。",
        )
        return {"threshold": int(threshold)}

    if operator == "Laplacian":
        col_a, col_b = st.columns(2)
        with col_a:
            threshold = st.number_input(
                "响应阈值",
                0,
                255,
                int(params["threshold"]),
                1,
                key=f"{key_prefix}_laplacian_threshold",
                help="Laplacian 二阶导数响应阈值；用于排除弱变化和噪点。",
            )
        with col_b:
            kernels = [1, 3, 5, 7]
            kernel = st.selectbox(
                "卷积核尺寸",
                kernels,
                index=_option_index(kernels, int(params["kernel_size"])),
                key=f"{key_prefix}_laplacian_kernel",
                help="Laplacian 卷积核尺寸；越大越强调大范围边缘。",
            )
        return {"threshold": int(threshold), "kernel_size": int(kernel)}

    threshold = st.number_input(
        "响应阈值",
        0,
        255,
        int(params["threshold"]),
        1,
        key=f"{key_prefix}_prewitt_threshold",
        help="Prewitt 梯度响应的截断值；越高越忽略细弱边缘。",
    )
    return {"threshold": int(threshold)}


def render_line_cleanup_controls(
    *,
    key_prefix: str,
    defaults: LineCleanupConfig | None = None,
    show_pixel_cleanup: bool = True,
) -> LineCleanupConfig:
    config = defaults or LineCleanupConfig()
    alpha_threshold = int(config.alpha_threshold)
    close_kernel = int(config.close_kernel)
    remove_small_components = int(config.remove_small_components)

    if show_pixel_cleanup:
        col_alpha, col_close, col_component = st.columns(3)
        with col_alpha:
            alpha_threshold = st.number_input(
                "Alpha 阈值",
                0,
                255,
                int(config.alpha_threshold),
                1,
                key=f"{key_prefix}_alpha_threshold",
                help="Alpha 低于该值的像素视为透明背景，不参与线稿识别。",
            )
        with col_close:
            close_kernels = [1, 3, 5, 7]
            close_kernel = st.selectbox(
                "断线连接核",
                close_kernels,
                index=_option_index(close_kernels, int(config.close_kernel)),
                key=f"{key_prefix}_close_kernel",
                help="用形态学闭运算连接小断线；1 表示不连接，较大值可连接更宽缝隙。",
            )
        with col_component:
            remove_small_components = st.number_input(
                "最小连通区域（像素）",
                1,
                10000,
                int(config.remove_small_components),
                1,
                key=f"{key_prefix}_min_component",
                help="小于该像素数的独立连通区会被删除，用于清理散点噪声。",
            )

    exclude_enabled = st.checkbox(
        "排除短曲线",
        value=bool(config.exclude_curve_length_enabled),
        key=f"{key_prefix}_exclude_curve_length_enabled",
        help=(
            "对检测边缘和上传线稿图都生效。中心骨架生成后，在叉点处续接方向变化"
            "最平滑的一对分支，其余树杈分别计算曲线长度。"
        ),
    )
    if exclude_enabled:
        exclude_length = st.number_input(
            "排除曲线长度（px）",
            0.5,
            1000.0,
            float(config.exclude_curve_length_px),
            0.5,
            key=f"{key_prefix}_exclude_curve_length_px",
            help=(
                "长度小于该值的整条逻辑曲线会在骨架清理阶段删除，不会进入曲线拟合"
                "或占用基元预算。"
            ),
        )
    else:
        exclude_length = 0.0
    st.caption(
        "曲线长度清理会在中心骨架生成后执行：叉点只连接最平滑的一对分支，"
        "其他树杈独立计长。"
    )
    return LineCleanupConfig(
        alpha_threshold=int(alpha_threshold),
        close_kernel=int(close_kernel),
        remove_small_components=int(remove_small_components),
        exclude_curve_length_enabled=bool(exclude_enabled),
        exclude_curve_length_px=float(exclude_length),
    )


def render_edge_processing_controls(
    *,
    key_prefix: str,
    defaults: EdgeProcessingConfig | None = None,
) -> EdgeProcessingConfig:
    config = defaults or EdgeProcessingConfig()
    operator = st.selectbox(
        "边缘检测算子",
        EDGE_OPERATORS,
        index=_option_index(EDGE_OPERATORS, config.operator),
        key=f"{key_prefix}_operator",
        help="选择从普通图片提取边缘的算法；Canny 通用性较好，其他算子适合特定纹理。",
    )
    operator_params = render_edge_operator_controls(
        str(operator),
        key_prefix=key_prefix,
        defaults=(
            config.operator_params
            if str(operator) == config.operator
            else default_operator_params(str(operator))
        ),
    )
    blur_kernels = [1, 3, 5, 7, 9]
    blur_kernel = st.selectbox(
        "检测前高斯模糊核",
        blur_kernels,
        index=_option_index(blur_kernels, int(config.blur_kernel)),
        key=f"{key_prefix}_blur_kernel",
        help="边缘检测前的高斯模糊核；1 表示不模糊，越大越能抑制噪点但会损失细节。",
    )
    st.info("只显示当前算子的参数，切换算子后面板同步切换。")
    cleanup = render_line_cleanup_controls(
        key_prefix=key_prefix,
        defaults=config.cleanup,
        show_pixel_cleanup=True,
    )
    return EdgeProcessingConfig(
        operator=str(operator),
        operator_params=operator_params,
        blur_kernel=int(blur_kernel),
        cleanup=cleanup,
    )


def render_sketch_processing_controls(
    *,
    key_prefix: str,
    defaults: SketchProcessingConfig | None = None,
    source_label: str = "线稿来源",
) -> SketchProcessingConfig:
    """为主线稿与保护分支渲染同一套完整来源参数。"""
    config = defaults or SketchProcessingConfig()
    source_options = ["检测边缘", "上传线稿图"]
    source_mode_label = st.radio(
        source_label,
        source_options,
        index=0 if config.source_mode == SOURCE_EDGE else 1,
        horizontal=True,
        key=f"{key_prefix}_source_mode",
        help="“检测边缘”会从普通图片提取轮廓；“上传线稿图”直接按颜色解析现有线稿。",
    )
    source_mode = (
        SOURCE_EDGE if source_mode_label == "检测边缘" else SOURCE_LINE_ART
    )

    if source_mode == SOURCE_EDGE:
        edge = render_edge_processing_controls(
            key_prefix=f"{key_prefix}_edge",
            defaults=config.edge,
        )
        return SketchProcessingConfig(
            source_mode=source_mode,
            edge=edge,
            line_color_mode=config.line_color_mode,
            line_rgb=config.line_rgb,
            line_rgb_tolerance=config.line_rgb_tolerance,
            nonzero_threshold=config.nonzero_threshold,
            auto_explore_polarity=config.auto_explore_polarity,
            line_cleanup=config.line_cleanup,
        )

    color_labels = ["指定 RGB", "自动：非 0 值视为线稿", "直通"]
    color_mode_to_index = {
        COLOR_MODE_SPECIFIED: 0,
        COLOR_MODE_AUTO_NONZERO: 1,
        COLOR_MODE_PASSTHROUGH: 2,
    }
    color_mode_label = st.radio(
        "线稿颜色识别",
        color_labels,
        index=color_mode_to_index.get(config.line_color_mode, 1),
        horizontal=True,
        key=f"{key_prefix}_color_mode",
        help="指定 RGB 按目标颜色识别；自动模式按非零值判定；直通模式不做颜色清理。",
    )
    line_color_mode = {
        "指定 RGB": COLOR_MODE_SPECIFIED,
        "自动：非 0 值视为线稿": COLOR_MODE_AUTO_NONZERO,
        "直通": COLOR_MODE_PASSTHROUGH,
    }[color_mode_label]

    line_rgb = config.line_rgb
    line_rgb_tolerance = int(config.line_rgb_tolerance)
    nonzero_threshold = int(config.nonzero_threshold)
    auto_explore_polarity = bool(config.auto_explore_polarity)
    if line_color_mode == COLOR_MODE_SPECIFIED:
        col_r, col_g, col_b = st.columns(3)
        with col_r:
            line_r = st.number_input(
                "线稿 R", 0, 255, int(line_rgb[0]), 1,
                key=f"{key_prefix}_line_r",
                help="目标线稿颜色的红色通道，取值 0–255。",
            )
        with col_g:
            line_g = st.number_input(
                "线稿 G", 0, 255, int(line_rgb[1]), 1,
                key=f"{key_prefix}_line_g",
                help="目标线稿颜色的绿色通道，取值 0–255。",
            )
        with col_b:
            line_b = st.number_input(
                "线稿 B", 0, 255, int(line_rgb[2]), 1,
                key=f"{key_prefix}_line_b",
                help="目标线稿颜色的蓝色通道，取值 0–255。",
            )
        line_rgb_tolerance = int(
            st.number_input(
                "RGB 容差", 0, 255, int(line_rgb_tolerance), 1,
                key=f"{key_prefix}_rgb_tolerance",
                help="像素颜色与目标 RGB 的允许差异；越大识别范围越宽。",
            )
        )
        line_rgb = (int(line_r), int(line_g), int(line_b))
        auto_explore_polarity = False
    elif line_color_mode == COLOR_MODE_AUTO_NONZERO:
        nonzero_threshold = int(
            st.number_input(
                "非 0 判定阈值", 0, 254, int(nonzero_threshold), 1,
                key=f"{key_prefix}_nonzero_threshold",
                help="自动模式下，通道亮度高于该值才视为非零线稿候选。",
            )
        )
        auto_explore_polarity = st.checkbox(
            "自动探索黑线/白线极性",
            value=bool(auto_explore_polarity),
            key=f"{key_prefix}_auto_polarity",
            help="同时尝试黑线和白线解释，自动选择更像线稿的结果。",
        )
        st.info("背景被误判为线稿时，会自动尝试反相与 Otsu 极性。")
    else:
        st.info(
            "直通模式不会执行颜色识别、Alpha 过滤、断线连接或小区域过滤；"
            "缩放结果会原样传给下一步。"
        )

    line_cleanup = render_line_cleanup_controls(
        key_prefix=f"{key_prefix}_line_cleanup",
        defaults=config.line_cleanup,
        show_pixel_cleanup=line_color_mode != COLOR_MODE_PASSTHROUGH,
    )
    return SketchProcessingConfig(
        source_mode=source_mode,
        edge=config.edge,
        line_color_mode=line_color_mode,
        line_rgb=line_rgb,
        line_rgb_tolerance=int(line_rgb_tolerance),
        nonzero_threshold=int(nonzero_threshold),
        auto_explore_polarity=bool(auto_explore_polarity),
        line_cleanup=line_cleanup,
    )
