"""UI-independent GPT model conversion using the shared GIA exporters."""
from __future__ import annotations

import copy
import json
import math
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

from miliastra_core.export import WRAPPER_MODE_SHRINK_HIDE, build_gia, build_decorated_gia
from miliastra_core.export.builder import parse_color, resolve_template_id, vec_from_item
from .normalization import normalize_objects, parse_hex_color, OUT_OF_RANGE_DISPLAY_MODES
from .parents import prepare_parent_groups

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / 'Template'
DEFAULT_ENTITY_TEMPLATE = TEMPLATE_DIR / 'GPT2Gia_entities.gia'
DEFAULT_DECORATION_TEMPLATE = TEMPLATE_DIR / 'GPT2Gia_decorations.gia'


@dataclass(frozen=True)
class ModelGiaSettings:
    template_id: int = 10009001
    entity_id_start: int = 1_078_500_000
    default_collision: bool = True
    default_climb: bool = False
    default_out_of_range_run: bool | None = None
    default_out_of_range_display: str | None = None
    global_rotation_x_degrees: float = 0.0
    auto_optimize_object_overlaps: bool = False
    object_overlap_clearance: float = 0.01
    object_surface_tolerance: float = 0.01
    multi_face_scale_increment: float = 0.01
    object_overlap_max_passes: int = 1
    decoration_packaging: bool = False
    max_decorations_per_parent: int = 999
    decoration_id_start: int = 0x40000001
    wrapper_static: bool = False
    wrapper_collision: bool = True
    wrapper_climb: bool = False
    decoration_collision: bool = True
    decoration_climb: bool = True
    wrapper_mode: str = 'SHRINK_HIDE'
    wrapper_hidden_scale: float = 0.01
    wrapper_random_seed: int = 0


def extract_model_objects(data: Any) -> list[dict[str, Any]] | dict[str, Any]:
    """Accept model dictionaries, object lists and the scene components schema."""
    if not isinstance(data, (list, dict)):
        raise ValueError('模型 JSON 根节点必须为对象列表或名称字典')
    data = copy.deepcopy(data)
    if not isinstance(data, dict) or 'components' not in data:
        return data
    if not isinstance(data['components'], list):
        raise ValueError('components 必须为列表')
    objects = []
    seen = set()
    for index, component in enumerate(data['components'], 1):
        if not isinstance(component, dict):
            raise ValueError(f'components[{index}] 必须为对象')
        component_id = component.pop('id', None)
        if not isinstance(component_id, str) or not component_id.strip():
            raise ValueError(f'components[{index}].id 必须为非空字符串')
        if component_id in seen:
            raise ValueError(f'重复的 component id: {component_id}')
        seen.add(component_id)
        component['source_component_id'] = component_id
        component.setdefault('name', component_id)
        objects.append(component)
    return objects


def apply_materials(data: Any, materials: dict[str, Any]) -> Any:
    """Merge materials by model key/component id without mutating the caller."""
    objects = extract_model_objects(data)
    if not isinstance(materials, dict):
        raise ValueError('materials JSON 必须为名称到材质的字典')
    index = objects if isinstance(objects, dict) else {
        str(item.get('source_component_id', item.get('name', ''))): item
        for item in objects if isinstance(item, dict)
    }
    for name, material in materials.items():
        if name not in index:
            raise ValueError(f'材质对应的物体不存在：{name}')
        if not isinstance(material, dict) or 'color' not in material:
            raise ValueError(f'材质 {name} 缺少 color')
        if not isinstance(index[name], dict):
            raise ValueError(f'物体 {name} 必须为对象')
        color = parse_hex_color(material['color'])
        parse_color(color)
        index[name]['color'] = color
    return objects


def _validate_settings(settings: ModelGiaSettings) -> None:
    if not 1 <= settings.entity_id_start <= 0xFFFFFFFF:
        raise ValueError('实体起始 ID 必须为 uint32 正整数')
    if not math.isfinite(settings.global_rotation_x_degrees):
        raise ValueError('旋转角度必须为有限数')
    if settings.auto_optimize_object_overlaps:
        for key in ('object_overlap_clearance', 'object_surface_tolerance', 'multi_face_scale_increment'):
            value = getattr(settings, key)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'{key} 必须为非负有限数')
        if settings.object_overlap_clearance <= 0 or settings.object_overlap_max_passes < 1:
            raise ValueError('重叠间隙和迭代轮数必须大于 0')
    if settings.decoration_packaging:
        if not 1 <= settings.max_decorations_per_parent <= 999:
            raise ValueError('每组装饰物数量必须位于 1..999')
        if settings.wrapper_climb and not settings.wrapper_collision:
            raise ValueError('父实体启用攀爬时必须启用碰撞')
        if settings.decoration_climb and not settings.decoration_collision:
            raise ValueError('子装饰物启用攀爬时必须启用碰撞')


def build_model_gia_bytes(
    data: Any,
    settings: ModelGiaSettings = ModelGiaSettings(),
    *,
    materials: dict[str, Any] | None = None,
    parent_config: dict[str, Any] | None = None,
    template_path: Path = DEFAULT_ENTITY_TEMPLATE,
    decoration_template_path: Path = DEFAULT_DECORATION_TEMPLATE,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[bytes, dict[str, Any], list[dict[str, Any]]]:
    """Return GIA bytes, export summary and normalized JSON; temp files are isolated."""
    _validate_settings(settings)
    report = progress_callback or (lambda percent, message: None)
    report(1, '正在读取模型 JSON')
    model = apply_materials(data, materials) if materials is not None else extract_model_objects(data)
    # Validate vectors before spatial indexing and rotation, so invalid input cannot hang a worker.
    raw = model.values() if isinstance(model, dict) else model
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('每个模型物体必须为 JSON 对象')
        for key, aliases, default in (
            ('position', ('position', 'pos'), [0, 0, 0]),
            ('rotation', ('rotation', 'rot'), [0, 0, 0]),
            ('scale', ('scale',), [1, 1, 1]),
        ):
            value = vec_from_item(item, aliases, default)
            if not all(math.isfinite(v) for v in value) or (key == 'scale' and min(value) <= 0):
                raise ValueError(f'{key} 必须为有限数，scale 三轴必须大于 0')
            item[key] = value
    options = asdict(settings)
    normal_keys = (
        'default_collision', 'default_climb', 'default_out_of_range_run',
        'default_out_of_range_display', 'global_rotation_x_degrees',
        'auto_optimize_object_overlaps', 'object_overlap_clearance',
        'object_surface_tolerance', 'multi_face_scale_increment', 'object_overlap_max_passes',
    )
    objects, normalization = normalize_objects(
        model, default_template_id=settings.template_id,
        **{key: options[key] for key in normal_keys},
        optimization_progress_callback=lambda stage, current, total: report(
            5 + round(25 * current / max(total, 1)), f'优化 {stage}：{current}/{total}'),
    )
    if not objects:
        raise ValueError('模型 JSON 中没有可导出的对象')
    used_ids = set()
    for index, item in enumerate(objects):
        item['template_id'] = resolve_template_id(item)
        color = item.get('color', item.get('rgb'))
        enabled = item.get('custom_color_enabled')
        enabled = color is not None if enabled is None else bool(enabled)
        if enabled and color is None:
            raise ValueError('启用自定义颜色时必须提供 color/rgb')
        item['custom_color_enabled'] = enabled
        rgb, opacity = parse_color(parse_hex_color(color) if enabled else None)
        if not math.isfinite(opacity):
            raise ValueError('颜色透明度必须为有限数')
        item['color'] = [*rgb, opacity]
        object_id = int(item.get('entity_id', item.get('object_id', item.get('id', settings.entity_id_start + index))))
        if not 1 <= object_id <= 0xFFFFFFFF or object_id in used_ids:
            raise ValueError(f'实体 ID 重复或超出 uint32 正整数范围：{object_id}')
        used_ids.add(object_id)
        item['entity_id'] = object_id
        if 'out_of_range_run' in item:
            item['enable_out_of_range_run'] = bool(item['out_of_range_run'])
        if 'out_of_range_display' in item:
            item['out_of_range_display_mode'] = OUT_OF_RANGE_DISPLAY_MODES[item['out_of_range_display']]
        if settings.decoration_packaging:
            item['enable_collision'] = settings.decoration_collision
            item['enable_climb'] = settings.decoration_climb
    export_progress = lambda percent, message: report(30 + round(percent * 0.7), message)
    parent_groups = None
    if (parent_config is not None and settings.decoration_packaging
            and str(settings.wrapper_mode).strip().upper() == WRAPPER_MODE_SHRINK_HIDE):
        source_names = list(model) if isinstance(model, dict) else [
            item.get('source_component_id', item.get('name')) for item in model
        ]
        parent_groups = prepare_parent_groups(
            objects, source_names, parent_config,
            hidden_scale=settings.wrapper_hidden_scale, max_per_parent=settings.max_decorations_per_parent,
        )
    with tempfile.TemporaryDirectory(prefix='miliastra_gpt2gia_') as directory:
        output = Path(directory) / 'model.gia'
        if settings.decoration_packaging:
            summary = build_decorated_gia(
                objects=objects, decoration_template_path=Path(decoration_template_path),
                output_path=output, max_per_parent=settings.max_decorations_per_parent,
                entity_id_start=settings.entity_id_start, decoration_id_start=settings.decoration_id_start,
                wrapper_static=settings.wrapper_static, wrapper_collision=settings.wrapper_collision,
                wrapper_climb=settings.wrapper_climb, wrapper_mode=settings.wrapper_mode,
                wrapper_hidden_scale=settings.wrapper_hidden_scale, random_seed=settings.wrapper_random_seed,
                decoration_groups=parent_groups,
                progress_callback=export_progress,
            )
        else:
            objects_path = Path(directory) / 'objects.json'
            objects_path.write_text(json.dumps(objects, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            summary = build_gia(
                template_path=Path(template_path), objects_path=objects_path, output_path=output,
                summary_path=None, entity_id_start=settings.entity_id_start,
                no_transparency_export=False, preserve_template_load_settings=True,
                progress_callback=export_progress,
            )
        result = output.read_bytes()
    summary.update(mode='decoration_packaging' if settings.decoration_packaging else 'entities',
                   converter='GPT2Gia', normalization=normalization, settings=options, output='model.gia')
    summary.pop('objects', None)
    report(100, 'GPT2Gia 转换完成')
    return result, summary, objects
