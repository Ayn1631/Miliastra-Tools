from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

import streamlit as st

from UI.task_ui import run_heavy_action
from miliastra_core.export import TYPE_NAME_TO_TEMPLATE_ID
from miliastra_core.gpt2gia import (
    ModelGiaSettings, build_model_gia_bytes,
)

EXAMPLE = {
    'Cube': {'position': [0, 0.5, 0], 'rotation': [0, 0, 0], 'scale': [1, 1, 1], 'color': '#66aaff'},
    'Sphere': {'template_id': 10009002, 'position': [2, 0.5, 0], 'scale': [1, 1, 1], 'color': '#ff8844'},
}
PARENT_EXAMPLE = {
    '左侧动画组': {'children': ['Cube'], 'anchor': [0, 0.5, 0.5]},
    '右侧动画组': {'children': ['Sphere'], 'anchor': [1, 0.5, 0.5]},
}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _convert_job(data, materials, settings, progress, *, parent_config=None):
    """Serializable queue action; never accesses Streamlit session state."""
    gia, summary, objects = build_model_gia_bytes(
        data, settings, materials=materials, parent_config=parent_config, progress_callback=progress,
    )
    return gia, _json_bytes(summary), _json_bytes(objects)


def render_gpt2gia_page() -> None:
    st.caption('将 GPT 生成的模型 JSON 转为 GIA；支持材质配色。此功能不调用 AI 服务。')
    st.download_button('下载模型 JSON 示例', _json_bytes(EXAMPLE), 'gpt2gia.model.json', 'application/json')
    with st.expander('输入格式说明'):
        st.markdown('支持名称到物体的字典、物体列表，以及含 `components` 列表的场景 JSON。'
                    '坐标格式为 **Y-UP（Y 轴向上，X/Z 为水平轴）**。'
                    '物体使用 `position`、`rotation`（角度）、`scale` 和 `color`；颜色支持 `#RRGGBB`。'
                    '每个物体有 `template_id` 时使用该 ID；缺省时使用下方默认基础体的 ID。'
                    '材质文件采用 `{"物体名": {"color": "#RRGGBB"}}`。')
    text_input, model_upload = '', None
    parent_config_upload, parent_config_text = None, ''
    input_mode = st.radio('输入方式', ['上传文件', '直接粘贴'], horizontal=True, key='gpt2gia_input_mode')
    if input_mode == '上传文件':
        model_upload = st.file_uploader('模型 JSON', type=['json'], key='gpt2gia_model')
    else:
        text_input = st.text_area('模型 JSON 内容', value=_json_bytes(EXAMPLE).decode(), height=250, key='gpt2gia_text')
    materials_upload = st.file_uploader('可选：materials.json 材质配色', type=['json'], key='gpt2gia_materials')
    a, b = st.columns(2)
    export_mode = a.selectbox('导出方式', ['普通实体', '装饰物包装'], key='gpt2gia_export_mode')
    primitive = b.selectbox('默认基础体', list(TYPE_NAME_TO_TEMPLATE_ID), key='gpt2gia_primitive',
                            help='仅用于未提供 template_id 的物体；已有 template_id 不会被覆盖。')
    options: dict[str, Any] = {'template_id': TYPE_NAME_TO_TEMPLATE_ID[primitive],
                              'decoration_packaging': export_mode == '装饰物包装'}
    with st.expander('转换参数'):
        a, b = st.columns(2)
        options['global_rotation_x_degrees'] = a.number_input('整体绕 X 轴旋转（度）', value=0.0)
        options['entity_id_start'] = b.number_input('实体起始 ID', min_value=1, max_value=0xFFFFFFFF, value=1_078_500_000)
        options['default_collision'] = a.checkbox('普通实体默认碰撞', value=True)
        options['default_climb'] = b.checkbox('普通实体默认攀爬', value=False)
        run = a.selectbox('超范围运行', ['保留模板', '关闭', '开启'])
        display = b.selectbox('超范围显示', ['保留模板', '默认', '永久显示', '永久最高精度'])
        options['default_out_of_range_run'] = {'保留模板': None, '关闭': False, '开启': True}[run]
        options['default_out_of_range_display'] = {
            '保留模板': None, '默认': 'default', '永久显示': 'permanent', '永久最高精度': 'permanent_highest_precision',
        }[display]
        st.checkbox(
            '自动优化共面重叠（会调整坐标或缩放）',
            value=False,
            disabled=True,
            help='计算过慢，暂不可开启。',
            key='gpt2gia_overlap_disabled',
        )
        st.caption('计算过慢，暂不可开启。')
        options['auto_optimize_object_overlaps'] = False
    if options['decoration_packaging']:
        with st.expander('装饰物包装参数', expanded=True):
            wrappers = {'缩小空模型': 'SHRINK_HIDE', '空模型包围盒': 'BOUNDING_BOX', '随机选取组内物体作为父实体': 'RANDOM_GROUP_OBJECT'}
            chosen = st.selectbox('父实体模式', list(wrappers))
            options['wrapper_mode'] = wrappers[chosen]
            a, b = st.columns(2)
            options['max_decorations_per_parent'] = a.number_input('每组最大物体数', min_value=1, max_value=999, value=999)
            options['decoration_id_start'] = b.number_input('装饰物起始 ID', min_value=1, max_value=0xFFFFFFFF, value=0x40000001)
            if options['wrapper_mode'] == 'SHRINK_HIDE':
                options['wrapper_hidden_scale'] = a.number_input('父实体缩放', min_value=0.0001, max_value=1.0, value=0.01, format='%.4f')
            if options['wrapper_mode'] == 'RANDOM_GROUP_OBJECT':
                options['wrapper_random_seed'] = a.number_input('随机种子', value=0, step=1)
            options['wrapper_static'] = st.checkbox('父实体为静态元件', value=False)
            options['wrapper_collision'] = a.checkbox('父实体碰撞', value=True)
            options['wrapper_climb'] = b.checkbox('父实体攀爬', value=False)
            options['decoration_collision'] = a.checkbox('子装饰物碰撞', value=True)
            options['decoration_climb'] = b.checkbox('子装饰物攀爬', value=True)
        if options['wrapper_mode'] == 'SHRINK_HIDE':
            with st.expander('父级配置 JSON（可选）', expanded=True):
                st.caption('仅在缩小空模型模式生效。未列入配置的元件统一放入默认父级；'
                           '不提供配置时使用原有自动打包逻辑。同组超限拆分的父实体共享变换。')
                parent_input_mode = st.radio(
                    '父级配置输入方式', ['不使用', '上传文件', '直接粘贴'], horizontal=True,
                    key='gpt2gia_parent_input_mode',
                )
                st.download_button('下载父级配置 JSON 示例', _json_bytes(PARENT_EXAMPLE),
                                   'gpt2gia.parents.json', 'application/json')
                st.markdown('坐标格式：**Y-UP（Y 轴向上，X/Z 为水平轴）**。'
                            '配置格式：`{"父级名": {"children": ["子元件名"], "anchor": [0, 0.5, 0.5]}}`。'
                            '`children` 引用模型字典键、物体列表的 `name` 或场景 `components` 的 `id`。'
                            '`anchor` 三轴为 0–1，从整组包围盒最小角到最大角。'
                            '父级旋转固定为 `[0,0,0]`，三轴缩放均使用“父实体缩放”的值；这两项不在 JSON 中配置。')
                if parent_input_mode == '上传文件':
                    parent_config_upload = st.file_uploader('父级配置 JSON', type=['json'], key='gpt2gia_parents')
                elif parent_input_mode == '直接粘贴':
                    parent_config_text = st.text_area(
                        '父级配置 JSON 内容', value=_json_bytes(PARENT_EXAMPLE).decode(), height=230,
                        key='gpt2gia_parents_text',
                    )
    output_name = st.text_input('导出文件名', value='model', key='gpt2gia_output_name')
    if st.button('生成 GIA', type='primary', key='gpt2gia_generate'):
        st.session_state.pop('gpt2gia_result', None)
        try:
            if model_upload is None and not text_input.strip():
                raise ValueError('请上传或粘贴模型 JSON')
            data = json.loads(model_upload.getvalue().decode('utf-8-sig') if model_upload is not None else text_input)
            materials = None if materials_upload is None else json.loads(materials_upload.getvalue().decode('utf-8-sig'))
            parent_config = None
            if parent_config_upload is not None or parent_config_text.strip():
                parent_source = (parent_config_upload.getvalue().decode('utf-8-sig')
                                 if parent_config_upload is not None else parent_config_text)
                parent_config = json.loads(parent_source)
                if not isinstance(parent_config, dict):
                    raise ValueError('父级配置 JSON 必须为父级名称到配置对象的字典')
            action = partial(_convert_job, data, materials, ModelGiaSettings(**options), parent_config=parent_config)
            result = run_heavy_action('GPT2Gia', action)
            name = Path(output_name.replace('\\', '/')).name
            if name.lower().endswith('.gia'):
                name = name[:-4]
            st.session_state['gpt2gia_result'] = (name or 'model', result)
        except Exception as exc:
            st.error(f'转换失败：{exc}')
    if 'gpt2gia_result' in st.session_state:
        name, (gia, summary_bytes, objects_bytes) = st.session_state['gpt2gia_result']
        summary = json.loads(summary_bytes)
        st.success(f'上次生成结果：{summary["normalization"]["source_count"]} 个输入物体，{len(gia):,} 字节')
        if summary.get('mode') == 'decoration_packaging':
            st.caption(f'生成 {summary["parent_count"]} 个父实体、{summary["decoration_count"]} 个子装饰物。')
        a, b, c = st.columns(3)
        a.download_button('下载 GIA', gia, f'{name}.gia', 'application/octet-stream')
        b.download_button('下载转换摘要', summary_bytes, f'{name}.summary.json', 'application/json')
        c.download_button('下载归一化模型', objects_bytes, f'{name}.model.json', 'application/json')
        with st.expander('转换摘要'):
            st.json(summary)
