from __future__ import annotations

import hashlib
import io
import re
import time
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw

from miliastra_core.export.gia import GiaExportSettings, build_gia_from_plan
from miliastra_core.export.decoration import MAX_DECORATIONS_PER_PARENT
from miliastra_core.export.quantization import QuantizationMode
from miliastra_core.raster import (
    ColorMergeMode,
    RasterAlgorithmSettings,
    ResampleMode,
    ScanStrategy,
    build_raster_plan,
)
from miliastra_core.export.builder import DEFAULT_ENTITY_ID_START, build_gia
from miliastra_core.image import DEFAULT_TEMPLATE_GIA


_ALGORITHM_DEBOUNCE_KEY = "miliastra_image_algorithm_debounce"
_ALGORITHM_DEBOUNCE_SECONDS = 1.0


def _default_gia_filename(source_name: str) -> str:
    stem = Path(str(source_name)).stem.strip() or "image"
    return f"{stem}_image.gia"


def _normalize_gia_filename(value: str, source_name: str) -> str:
    # 浏览器下载只接受文件名；去掉用户误填的目录和 Windows 非法字符。
    name = re.split(r"[/\\]", str(value).strip())[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip(" .")
    if not name:
        name = _default_gia_filename(source_name)
    if not name.lower().endswith(".gia"):
        name += ".gia"
    return name


def _proportional_height(width: float, source_size: tuple[int, int]) -> float:
    source_width, source_height = source_size
    return max(0.000001, float(width) * source_height / max(source_width, 1))


def _debounce_algorithm_settings(
    source_digest: str,
    settings: RasterAlgorithmSettings,
) -> RasterAlgorithmSettings:
    """Apply algorithm settings only after the signature stays unchanged for one second."""
    signature = f"{source_digest}:{settings.digest()}"
    now = time.monotonic()
    state = st.session_state.get(_ALGORITHM_DEBOUNCE_KEY)
    if not isinstance(state, dict) or state.get("pending_signature") != signature:
        state = {
            "pending_signature": signature,
            "changed_at": now,
            "applied_signature": None if not isinstance(state, dict) else state.get("applied_signature"),
        }
        st.session_state[_ALGORITHM_DEBOUNCE_KEY] = state

    remaining = _ALGORITHM_DEBOUNCE_SECONDS - (time.monotonic() - float(state["changed_at"]))
    if remaining > 0:
        with st.spinner("参数防抖中：等待 1 秒无新改动后应用…"):
            time.sleep(remaining)

    latest = st.session_state.get(_ALGORITHM_DEBOUNCE_KEY)
    if isinstance(latest, dict) and latest.get("pending_signature") == signature:
        latest["applied_signature"] = signature
        latest["applied_at"] = time.monotonic()
        st.session_state[_ALGORITHM_DEBOUNCE_KEY] = latest
    return settings


def _plan_preview(plan) -> Image.Image:
    width, height = plan.sampled_size_px
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for rect in plan.rectangles:
        draw.rectangle(
            (rect.x, rect.y, rect.x + rect.width - 1, rect.y + rect.height - 1),
            fill=rect.rgba,
        )
    return canvas


def _algorithm_key(image_bytes: bytes, settings: RasterAlgorithmSettings) -> str:
    digest = hashlib.sha256()
    digest.update(image_bytes)
    digest.update(b"\0")
    digest.update(settings.digest().encode("ascii"))
    return digest.hexdigest()


def _load_or_build_plan(image_bytes: bytes, settings: RasterAlgorithmSettings):
    key = _algorithm_key(image_bytes, settings)
    cache = st.session_state.setdefault("miliastra_raster_plans", {})
    plan = cache.get(key)
    if plan is None:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        plan = build_raster_plan(image, settings)
        cache[key] = plan
        # 防止长期会话无限积累，只保留最近四份算法结果。
        while len(cache) > 4:
            cache.pop(next(iter(cache)))
    return plan, key


def _algorithm_controls(image: Image.Image) -> RasterAlgorithmSettings:
    st.subheader("图像算法参数")
    st.caption("修改这里会重新生成 RasterPlan。修改下方 GIA 导出参数不会重跑图像算法。")

    c1, c2, c3 = st.columns(3)
    max_pixels = c1.number_input(
        "最大像素预算", min_value=1, value=4096, step=256, key="image_max_pixels",
        help="限制参与转换的总像素数；较小会更快、元件更少，较大会保留更多细节。",
    )
    max_width = c2.number_input(
        "最大宽度 px，0 表示不限", min_value=0, value=0, step=1, key="image_max_width",
        help="采样图片允许的最大宽度；0 表示只受总像素预算和最大高度约束。",
    )
    max_height = c3.number_input(
        "最大高度 px，0 表示不限", min_value=0, value=0, step=1, key="image_max_height",
        help="采样图片允许的最大高度；0 表示只受总像素预算和最大宽度约束。",
    )

    c1, c2, c3 = st.columns(3)
    alpha_threshold = c1.slider(
        "透明度阈值，包含判定",
        min_value=1,
        max_value=256,
        value=1,
        key="image_alpha_threshold",
        help="像素满足 alpha >= 阈值才可见。256 会隐藏所有像素。",
    )
    resample = c2.selectbox(
        "重采样",
        [mode.value for mode in ResampleMode],
        index=[mode.value for mode in ResampleMode].index(ResampleMode.LANCZOS.value),
        key="image_resample_mode",
        help=(
            "仅在图片受总像素、最大宽度或最大高度限制而需要缩放时生效。"
            "算法会影响边缘清晰度、生成的新颜色以及后续同色矩形的合并率。\n\n"
            "- `nearest`：直接取最近像素，不混合颜色；最适合像素画、图标和硬边图形，"
            "但普通图片可能出现明显锯齿。\n"
            "- `box`：按覆盖区域求平均；大幅缩小时较稳定、伪影较少，但画面会偏柔和。\n"
            "- `bilinear`：邻近像素线性插值；速度快、过渡平滑，但细节容易变模糊。\n"
            "- `bicubic`：使用更大邻域插值；通常比 bilinear 更清晰，速度稍慢，"
            "并可能生成额外的过渡色。\n"
            "- `lanczos`（默认）：缩小时通常保留最多细节；计算较慢，硬边附近可能出现轻微振铃，"
            "新增过渡色也可能让最终元件数增加。\n\n"
            "建议：像素画选 `nearest`；普通照片或插画先用默认 `lanczos`；"
            "若更重视颜色块合并，可尝试 `box`。"
        ),
    )
    scan = c3.selectbox(
        "扫描策略",
        [mode.value for mode in ScanStrategy],
        index=[mode.value for mode in ScanStrategy].index(ScanStrategy.BEST_OF_BOTH.value),
        key="image_scan_strategy",
        help=(
            "仅在开启“贪心矩形合并”时生效。它决定连续同色或近似色像素先沿哪个方向扩展，"
            "因此会影响矩形形状和最终元件数量，不改变采样分辨率。\n\n"
            "- `horizontal_first`：先横向、再纵向扩展；适合横条纹或横向连续色块，只计算一次。\n"
            "- `vertical_first`：先纵向、再横向扩展；适合竖条纹或纵向连续色块，只计算一次。\n"
            "- `best_of_both`（默认）：分别执行上述两种贪心合并，优先选择矩形数量更少的结果；"
            "数量相同时再按较大矩形优先确定结果。通常元件更少，但合并阶段计算量接近两倍。\n\n"
            "建议：通常保留 `best_of_both`；只有在已知图案方向，或希望减少计算时间时，"
            "才固定选择横向或纵向。"
        ),
    )

    c1, c2 = st.columns(2)
    palette_enabled = c1.checkbox(
        "开启 Median Cut 调色板量化", value=False, key="image_palette_enabled",
        help="把相近颜色压缩到有限调色板，可提高合并率并减少元件数量。",
    )
    palette_colors = c2.slider(
        "调色板颜色数", 2, 256, 64, disabled=not palette_enabled, key="image_palette_colors",
        help="量化后允许保留的颜色数量；越低合并率越高，但色彩损失越明显。",
    )

    c1, c2, c3 = st.columns(3)
    merge_rectangles = c1.checkbox(
        "开启贪心矩形合并", value=True, key="image_merge_rectangles",
        help="把连续同色或近似色像素合并成长方体，显著减少导出元件数量。",
    )
    merge_mode = c2.selectbox(
        "颜色模式",
        [mode.value for mode in ColorMergeMode],
        index=[mode.value for mode in ColorMergeMode].index(ColorMergeMode.RANGE.value),
        disabled=not merge_rectangles,
        key="image_merge_color_mode",
        help="exact=精确色；seed=与初始色比较；dynamic_mean=与动态均值比较；range=限制区域全局色差范围。",
    )
    tolerance = c3.slider(
        "颜色容差", 0, 255, 0, disabled=not merge_rectangles or merge_mode == "exact",
        key="image_color_tolerance",
        help="允许合并的颜色差异；数值越大元件越少，但颜色边界越可能被合并。",
    )
    include_alpha = st.checkbox(
        "颜色距离包含 Alpha", value=True, disabled=not merge_rectangles,
        key="image_include_alpha",
        help="开启后透明度差异也会阻止像素合并；关闭后只比较 RGB。",
    )

    with st.expander("边缘连通背景移除"):
        enable_background = st.checkbox(
            "启用背景移除", value=False, key="image_background_enabled",
            help="从图片边缘开始移除与指定颜色连通的背景区域。",
        )
        sampled_corner = image.getpixel((0, 0))[:3]
        background_rgb = st.color_picker(
            "背景颜色",
            "#%02X%02X%02X" % sampled_corner,
            disabled=not enable_background,
            key="image_background_color",
            help="作为背景判定基准的颜色，默认取图片左上角像素。",
        )
        background_tolerance = st.slider(
            "背景容差", 0, 255, 0, disabled=not enable_background,
            key="image_background_tolerance",
            help="与背景基准色允许的最大差异；越大，移除范围越宽。",
        )

    rgb = None
    if enable_background:
        rgb = tuple(int(background_rgb[index:index + 2], 16) for index in (1, 3, 5))

    return RasterAlgorithmSettings(
        max_pixels=int(max_pixels),
        max_width_px=int(max_width) or None,
        max_height_px=int(max_height) or None,
        alpha_threshold=int(alpha_threshold),
        palette_enabled=palette_enabled,
        palette_colors=int(palette_colors),
        merge_rectangles=merge_rectangles,
        merge_color_mode=ColorMergeMode(merge_mode),
        color_tolerance=int(tolerance),
        include_alpha_in_color_distance=include_alpha,
        background_rgb=rgb,
        background_tolerance=int(background_tolerance),
        resample_mode=ResampleMode(resample),
        scan_strategy=ScanStrategy(scan),
    )


def _quantization_controls(prefix: str):
    modes = [mode.value for mode in QuantizationMode]
    mode = st.selectbox(
        "坐标/缩放量化", modes, index=modes.index(QuantizationMode.NONE.value), key=f"{prefix}_qmode",
        help="none 保留连续浮点坐标；grid 会把位置和缩放吸附到指定网格。",
    )
    step = st.number_input(
        "网格步长 m",
        min_value=0.000001,
        value=0.01,
        format="%.6f",
        disabled=mode != QuantizationMode.GRID.value,
        key=f"{prefix}_qstep",
        help="启用 grid 量化时使用的吸附间隔，单位为米。",
    )
    return QuantizationMode(mode), float(step)


def _render_gia_export(
    plan,
    *,
    source_name: str,
    source_size: tuple[int, int],
    source_digest: str,
) -> None:
    st.subheader("GIA 导出参数")
    lock_aspect = st.checkbox(
        "保持原图宽高比",
        value=True,
        key="gia_lock_source_aspect",
        help="默认开启。修改目标宽度时自动按原图比例计算目标高度，避免图片被拉伸。",
    )
    c1, c2, c3 = st.columns(3)
    target_width = c1.number_input(
        "目标宽度 m", min_value=0.000001, value=5.0, format="%.6f", key="gia_width",
        help="导出图片在世界坐标 X 方向的总宽度。",
    )
    proportional_height = _proportional_height(float(target_width), source_size)
    if lock_aspect:
        locked_height_key = f"gia_height_locked_{source_digest[:16]}"
        st.session_state[locked_height_key] = f"{proportional_height:.6f}"
        locked_height_text = c2.text_input(
            "目标高度 m（原比例）",
            disabled=True,
            key=locked_height_key,
            help="由目标宽度和原图宽高比自动计算；关闭“保持原图宽高比”后可手动修改。",
        )
        target_height = float(locked_height_text)
    else:
        target_height = c2.number_input(
            "目标高度 m", min_value=0.000001, value=proportional_height, format="%.6f",
            key="gia_height_manual",
            help="导出图片在世界坐标 Z 方向的总高度。",
        )
    block_height = c3.number_input(
        "厚度 m，0 表示自动", min_value=0.0, value=0.0, format="%.6f", key="gia_depth",
        help="每个像素块的 Y 方向厚度；0 会自动使用单元格宽度和高度中的较小值。",
    )

    c1, c2, c3 = st.columns(3)
    template_id = c1.number_input(
        "GIA 模板 ID", min_value=1, value=10009001, step=1, key="gia_template_id",
        help="装饰物使用的模型资源 ID；10009001 为长方体。",
    )
    entity_start = c2.number_input(
        "Entity ID 起点", min_value=1, value=DEFAULT_ENTITY_ID_START + 100_000, step=1,
        key="gia_entity_id_start",
        help="空模型主元件的 ID 起点；同一场景多次导入时应避免与已有元件重复。",
    )
    collision_label = c3.selectbox(
        "碰撞", ["无", "有", "攀爬"], key="gia_collision_mode",
        help=(
            "无：父空模型无碰撞；有：父空模型开启碰撞；攀爬：父空模型同时开启碰撞和攀爬。"
            "三种模式下父空模型都会精确覆盖最终图片块的包围盒，子装饰物始终关闭碰撞。"
        ),
    )
    collision_mode = {"无": "off", "有": "native", "攀爬": "native_and_climb"}[
        collision_label
    ]
    qmode, qstep = _quantization_controls("gia")

    wrap_decorations = st.checkbox(
        "使用空模型包装装饰物",
        value=True,
        key="gia_decoration_packaging",
        help="按 999 上限分组，但所有空模型都放在整张图片的统一几何中心，内部元件作为装饰物保存。",
    )
    max_decorations = st.number_input(
        "每个空模型最多装饰物",
        min_value=1,
        max_value=MAX_DECORATIONS_PER_PARENT,
        value=MAX_DECORATIONS_PER_PARENT,
        step=1,
        disabled=not wrap_decorations,
        key="gia_max_decorations_per_parent",
        help="单个空模型可编辑装饰物的最大数量；编辑器上限为 999。超出后自动创建同中心的新空模型。",
    )

    with st.expander("空模型属性", expanded=False):
        wrapper_static = st.checkbox(
            "空模型转为静态元件",
            value=False,
            disabled=not wrap_decorations,
            key="gia_wrapper_static",
            help="写入静态元件标记。静态空模型在编辑器中的分类和可见入口可能与动态元件不同。",
        )
        st.caption("父空模型碰撞由上方唯一的“碰撞”下拉框控制，子装饰物固定关闭碰撞和攀爬。")
        wrapper_enable_out_of_range_run = st.checkbox(
            "空模型超出范围仍运行",
            value=False,
            disabled=not wrap_decorations,
            key="gia_wrapper_enable_out_of_range_run",
            help="开启后，空模型主元件超出加载范围时仍保持运行。",
        )
        wrapper_display_label = st.selectbox(
            "空模型超出范围显示",
            ["默认", "永久显示", "永久以最高精度显示"],
            disabled=not wrap_decorations,
            key="gia_wrapper_out_of_range_display_mode",
            help="只控制外层空模型的超范围显示和 LOD 策略。",
        )
    wrapper_display_mode = {"默认": 0, "永久显示": 1, "永久以最高精度显示": 2}[
        wrapper_display_label
    ]

    template_path = st.text_input(
        "GIA 模板路径", value=str(DEFAULT_TEMPLATE_GIA), key="gia_template_path",
        help="包含所需白模资源的本地 GIA 模板路径。装饰物包装结构使用项目内已验证模板。",
    )
    output_name_raw = st.text_input(
        "导出 GIA 文件名",
        value=_default_gia_filename(source_name),
        key=f"gia_output_name_{source_digest[:16]}",
        help="默认使用“原图文件名_image.gia”；可以手动修改。浏览器不会暴露原图本地目录。",
    )
    output_name = _normalize_gia_filename(output_name_raw, source_name)
    if output_name != output_name_raw:
        st.caption(f"实际下载文件名：`{output_name}`")
    if st.button("生成 GIA", type="primary", key="export_gia"):
        collision = collision_mode != "off"
        climb = collision_mode == "native_and_climb"
        export_settings = GiaExportSettings(
            target_width_m=float(target_width),
            target_height_m=float(target_height),
            template_id=int(template_id),
            block_height_m=None if block_height <= 0 else float(block_height),
            entity_id_start=int(entity_start),
            collision=collision,
            climb=climb,
            quantization_mode=qmode,
            quantization_step_m=qstep,
            decoration_packaging=bool(wrap_decorations),
            max_decorations_per_parent=int(max_decorations),
            wrapper_static=bool(wrapper_static),
            wrapper_enable_out_of_range_run=bool(wrapper_enable_out_of_range_run),
            wrapper_out_of_range_display_mode=int(wrapper_display_mode),
        )
        data, summary, objects_json = build_gia_from_plan(
            plan,
            export_settings,
            template_path=Path(template_path),
            build_gia=build_gia,
        )
        parent_count = summary.get("gia", {}).get("parent_count", 0)
        packaging_text = f"，包装为 {parent_count} 个空模型" if wrap_decorations else ""
        collision_text = "，父空模型承担碰撞" if collision and wrap_decorations else ""
        st.success(
            f"GIA 已生成，共 {len(plan.rectangles)} 个矩形对象{packaging_text}{collision_text}。"
        )
        output_stem = Path(output_name).stem
        st.download_button(
            "下载 GIA", data, file_name=output_name, mime="application/octet-stream",
            help="下载当前参数生成的 GIA 文件。",
        )
        st.download_button(
            "下载对象 JSON", objects_json, file_name=f"{output_stem}.objects.json", mime="application/json",
            help="下载生成前的对象位置、颜色、缩放和碰撞参数，便于排查或复现。",
        )
        st.json(summary)


def render_image_to_gia_advanced_page() -> None:
    st.title("图像 → GIA 高级导出")
    with st.expander("运行入口诊断", expanded=False):
        st.code(
            "UI.page_image_to_gia → UI.page_image_to_gia_advanced\n"
            "算法核心 → miliastra_core.raster\n"
            "GIA 后端 → miliastra_core.export.gia",
            language="text",
        )
    uploaded = st.file_uploader(
        "上传 PNG/JPG/WebP",
        type=["png", "jpg", "jpeg", "webp"],
        key="image_source_upload",
        help="选择要转换的原图；默认导出文件名会使用原图文件名并追加 `_image.gia`。",
    )
    if uploaded is None:
        st.info("先上传图片。算法会生成可复用 RasterPlan，之后调整 GIA 导出参数不会重新做矩形合并。")
        return

    image_bytes = uploaded.getvalue()
    source_digest = hashlib.sha256(image_bytes).hexdigest()
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    st.image(image, caption=f"原图 {image.width}×{image.height}")
    settings = _algorithm_controls(image)
    settings = _debounce_algorithm_settings(source_digest, settings)
    st.caption("算法参数已启用 1 秒防抖：连续修改时，仅最后一次稳定参数会触发 RasterPlan 计算。")

    try:
        with st.spinner("生成或读取 RasterPlan 缓存…"):
            plan, algorithm_key = _load_or_build_plan(image_bytes, settings)
    except Exception as exc:
        st.exception(exc)
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("采样尺寸", f"{plan.sampled_size_px[0]}×{plan.sampled_size_px[1]}")
    c2.metric("矩形数量", len(plan.rectangles))
    c3.metric("算法缓存键", algorithm_key[:12])
    st.image(_plan_preview(plan), caption="RasterPlan 预览")
    with st.expander("算法结果 JSON"):
        st.download_button("下载 RasterPlan", plan.to_json(), "raster_plan.json", "application/json")
        st.json(plan.to_dict())

    _render_gia_export(
        plan,
        source_name=uploaded.name,
        source_size=image.size,
        source_digest=source_digest,
    )


if __name__ == "__main__":
    render_image_to_gia_advanced_page()
