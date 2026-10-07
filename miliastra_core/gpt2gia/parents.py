"""Resolve an optional parent configuration without changing legacy packaging."""
from __future__ import annotations

import math
from typing import Any, Sequence

from miliastra_core.export.decoration import (
    MAX_DECORATIONS_PER_PARENT, DecorationGroup, geometry_bounds,
)


def _vector(value: Any, field: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f'{field} 必须为 [x,y,z]')
    if any(isinstance(component, bool) or not isinstance(component, (int, float)) for component in value):
        raise ValueError(f'{field} 三轴必须为数值')
    result = tuple(float(component) for component in value)
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f'{field} 三轴必须为有限数')
    if any(not 0 <= component <= 1 for component in result):
        raise ValueError(f'{field} 三轴必须位于 0..1')
    return result


def prepare_parent_groups(
    objects: Sequence[dict[str, Any]],
    source_names: Sequence[Any],
    config: Any,
    *,
    hidden_scale: float,
    max_per_parent: int,
) -> list[DecorationGroup]:
    """Build named groups and one default group in the Y-UP coordinate system."""
    if not isinstance(config, dict):
        raise ValueError('父级配置 JSON 必须为父级名称到配置对象的字典')
    if not 1 <= max_per_parent <= MAX_DECORATIONS_PER_PARENT:
        raise ValueError(f'每组装饰物数量必须位于 1..{MAX_DECORATIONS_PER_PARENT}')
    if not math.isfinite(hidden_scale) or not 0 < hidden_scale <= 1:
        raise ValueError('wrapper_hidden_scale 必须位于 (0, 1]')
    scale = (float(hidden_scale),) * 3

    by_name: dict[str, list[int]] = {}
    for index, name in enumerate(source_names):
        if isinstance(name, str):
            by_name.setdefault(name, []).append(index)
    assigned: set[int] = set()
    parent_names: set[str] = set()
    groups: list[DecorationGroup] = []

    def append_group(name: str, items: tuple[dict[str, Any], ...], anchor: tuple[float, float, float]) -> None:
        # Every split uses the full logical group's bounds, including the default group.
        bounds = geometry_bounds(items)
        position = tuple(bounds.minimum[axis] + anchor[axis] * bounds.size[axis] for axis in range(3))
        count = (len(items) + max_per_parent - 1) // max_per_parent
        for part, start in enumerate(range(0, len(items), max_per_parent), 1):
            exported_name = name if count == 1 else f'{name}_{part:03d}'
            if exported_name in parent_names:
                raise ValueError(f'拆分后父实体名称冲突：{exported_name}，请修改父级名称')
            parent_names.add(exported_name)
            groups.append(DecorationGroup(
                parent_position=position, parent_scale=scale, parent_rotation=(0.0, 0.0, 0.0),
                objects=items[start:start + max_per_parent], parent_name=exported_name,
                parent_group=name, parent_anchor=anchor,
            ))

    for name, entry in config.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('父级名称必须为非空字符串')
        if not isinstance(entry, dict):
            raise ValueError(f'父级 {name} 的配置必须为对象')
        unknown = set(entry) - {'children', 'anchor'}
        if unknown:
            raise ValueError(f'父级 {name} 的配置包含不支持的字段：{", ".join(sorted(map(str, unknown)))}')
        children = entry.get('children')
        if not isinstance(children, list) or not children:
            raise ValueError(f'父级 {name}.children 必须为非空子元件名称列表')
        indices = []
        for child in children:
            if not isinstance(child, str) or not child.strip():
                raise ValueError(f'父级 {name}.children 中的名称必须为非空字符串')
            matches = by_name.get(child, [])
            if not matches:
                raise ValueError(f'父级 {name} 引用了不存在的子元件：{child}')
            if len(matches) != 1:
                raise ValueError(f'子元件名称不唯一：{child}，请为物体列表使用唯一 name')
            index = matches[0]
            if index in assigned:
                raise ValueError(f'子元件 {child} 被重复分配到父级 {name}')
            assigned.add(index)
            indices.append(index)
        items = tuple(objects[index] for index in indices)
        anchor = _vector(entry.get('anchor', (0.5, 0.5, 0.5)), f'父级 {name}.anchor')
        append_group(name, items, anchor)

    remaining = tuple(item for index, item in enumerate(objects) if index not in assigned)
    if remaining:
        count = (len(remaining) + max_per_parent - 1) // max_per_parent
        name = '默认父级'
        serial = 1
        while any((name if count == 1 else f'{name}_{part:03d}') in parent_names
                  for part in range(1, count + 1)):
            serial += 1
            name = f'默认父级_{serial}'
        append_group(name, remaining, (0.5, 0.5, 0.5))
    return groups
