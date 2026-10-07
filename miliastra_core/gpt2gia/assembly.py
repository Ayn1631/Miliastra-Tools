"""Furniture grid assembly adapted from GPT2Gia/assemble_furniture_modules.py."""
from __future__ import annotations
import copy
import math
from dataclasses import dataclass
from typing import Any
from .geometry import euler_axes, matrix_apply, matrix_multiply, matrix_to_euler


@dataclass(frozen=True)
class AssemblySettings:
    columns: int = 12
    fixed_y: float = 1.0
    grid_spacing_x: float = 14.0
    grid_spacing_z: float = 14.0
    grid_origin_x: float = 0.0
    grid_origin_z: float = 0.0
    rotation_x_degrees: float = -90.0
    default_template_id: int = 10009001


def assemble_modules(modules: dict[str, Any], settings: AssemblySettings = AssemblySettings()) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Assemble uploaded modules, ordered by name; no paths are accessed."""
    if not modules:
        raise ValueError('请至少提供一个家具模块')
    if settings.columns < 1:
        raise ValueError('columns 必须大于 0')
    if not all(math.isfinite(getattr(settings, key)) for key in (
        'fixed_y', 'grid_spacing_x', 'grid_spacing_z', 'grid_origin_x', 'grid_origin_z', 'rotation_x_degrees',
    )):
        raise ValueError('家具布局参数必须为有限数')
    angle = math.radians(settings.rotation_x_degrees)
    rotation_x = ((1.0, 0.0, 0.0), (0.0, math.cos(angle), -math.sin(angle)), (0.0, math.sin(angle), math.cos(angle)))
    objects, placements = {}, []
    for index, (key, module) in enumerate(sorted(modules.items())):
        if not isinstance(module, dict) or not isinstance(module.get('model'), dict):
            raise ValueError(f'模块 {key} 缺少 model 字典')
        bounds = module.get('bounding_box')
        if not isinstance(bounds, dict):
            raise ValueError(f'模块 {key} 缺少 bounding_box')
        minimum, maximum = bounds.get('min'), bounds.get('max')
        if not all(isinstance(v, list) and len(v) == 3 for v in (minimum, maximum)):
            raise ValueError(f'模块 {key} 的 bounding_box 必须有三维 min/max')
        minimum, maximum = [float(v) for v in minimum], [float(v) for v in maximum]
        if not all(math.isfinite(v) for v in minimum + maximum) or any(a > b for a, b in zip(minimum, maximum)):
            raise ValueError(f'模块 {key} 的 bounding_box 范围非法')
        center = [(a + b) / 2 for a, b in zip(minimum, maximum)]
        target = [settings.grid_origin_x + index % settings.columns * settings.grid_spacing_x,
                  settings.fixed_y, settings.grid_origin_z + index // settings.columns * settings.grid_spacing_z]
        for name, source in module['model'].items():
            if not isinstance(source, dict):
                raise ValueError(f'模块 {key} 的物体 {name} 必须为对象')
            item = copy.deepcopy(source)
            position, rotation = item.get('position'), item.get('rotation', [0, 0, 0])
            if not all(isinstance(v, list) and len(v) == 3 for v in (position, rotation)):
                raise ValueError(f'模块 {key} 的物体 {name} 缺少三维 position/rotation')
            if not all(math.isfinite(float(v)) for v in position + rotation):
                raise ValueError('家具坐标和角度必须为有限数')
            relative = tuple(float(position[i]) - center[i] for i in range(3))
            rotated = matrix_apply(rotation_x, relative)
            item['position'] = [rotated[i] + target[i] for i in range(3)]
            item['rotation'] = matrix_to_euler(matrix_multiply(rotation_x, tuple(zip(*euler_axes(rotation)))))
            item['name'] = f'{key}__{name}'
            if item['name'] in objects:
                raise ValueError(f'合并后物体重名：{item["name"]}')
            if not any(k in item for k in ('template_id', 'type_id', 'type')):
                item['template_id'] = int(module.get('template_id', settings.default_template_id))
            objects[item['name']] = item
        placements.append({'module_key': key, 'module_id': module.get('module_id', key),
                           'object_count': len(module['model']), 'grid_position': dict(zip(('x', 'y', 'z'), target)),
                           'global_rotation_x_degrees': settings.rotation_x_degrees})
    return objects, placements
