"""Geometry algorithms migrated from GPT2Gia's object placement exporter."""
from __future__ import annotations
import math
from typing import Any, Callable
from miliastra_core.export.builder import vec_from_item

def dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def length(vector: tuple[float, float, float]) -> float:
    return math.sqrt(dot(vector, vector))


def normalized(vector: tuple[float, float, float]) -> tuple[float, float, float] | None:
    size = length(vector)
    if size < 1e-10:
        return None
    return vector[0] / size, vector[1] / size, vector[2] / size


def euler_axes(rotation: list[float]) -> tuple[tuple[float, float, float], ...]:
    """Return local XYZ axes after the GIA X/Y/Z Euler rotation, in degrees."""
    x, y, z = (math.radians(value) for value in rotation)
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    # Rz * Ry * Rx; this is sufficient for candidate displacement checks because
    # every selected translation is verified again with the same OBB test.
    return (
        (cz * cy, sz * cy, -sy),
        (cz * sy * sx - sz * cx, sz * sy * sx + cz * cx, cy * sx),
        (cz * sy * cx + sz * sx, sz * sy * cx - cz * sx, cy * cx),
    )


def matrix_from_axes(axes: tuple[tuple[float, float, float], ...]) -> tuple[tuple[float, float, float], ...]:
    return tuple(tuple(axes[column][row] for column in range(3)) for row in range(3))


def matrix_multiply(left: tuple[tuple[float, float, float], ...], right: tuple[tuple[float, float, float], ...]) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(sum(left[row][index] * right[index][column] for index in range(3)) for column in range(3))
        for row in range(3)
    )


def axes_from_matrix(matrix: tuple[tuple[float, float, float], ...]) -> tuple[tuple[float, float, float], ...]:
    return tuple(tuple(matrix[row][column] for row in range(3)) for column in range(3))


def rotation_from_matrix(matrix: tuple[tuple[float, float, float], ...]) -> list[float]:
    # Inverse of euler_axes: Rz * Ry * Rx. Handles the gimbal-lock fallback deterministically.
    y = math.asin(max(-1.0, min(1.0, -matrix[2][0])))
    if abs(math.cos(y)) > 1e-8:
        x = math.atan2(matrix[2][1], matrix[2][2])
        z = math.atan2(matrix[1][0], matrix[0][0])
    else:
        x = math.atan2(-matrix[0][1], matrix[1][1])
        z = 0.0
    return [math.degrees(x), math.degrees(y), math.degrees(z)]


def matrix_apply(matrix: tuple[tuple[float, float, float], ...], vector: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(sum(matrix[row][column] * vector[column] for column in range(3)) for row in range(3))


def matrix_to_euler(matrix: tuple[tuple[float, float, float], ...]) -> list[float]:
    """Convert Rz*Ry*Rx matrix back to the Euler convention used by euler_axes."""
    y = math.asin(max(-1.0, min(1.0, -matrix[2][0])))
    if abs(math.cos(y)) > 1e-8:
        x = math.atan2(matrix[2][1], matrix[2][2])
        z = math.atan2(matrix[1][0], matrix[0][0])
    else:
        x = 0.0
        z = math.atan2(-matrix[0][1], matrix[1][1])
    return [math.degrees(x), math.degrees(y), math.degrees(z)]


def obb_from_item(item: dict[str, Any]) -> dict[str, Any]:
    scale = vec_from_item(item, ("scale",), [1.0, 1.0, 1.0])
    return {
        "center": tuple(vec_from_item(item, ("position", "pos"), [0.0, 0.0, 0.0])),
        "axes": euler_axes(vec_from_item(item, ("rotation", "rot"), [0.0, 0.0, 0.0])),
        "half": tuple(abs(value) / 2.0 for value in scale),
    }


def rotate_scene_about_x(objects: list[dict[str, Any]], degrees: float) -> list[float]:
    """Rotate positions and orientations around the model bounding-box center."""
    if not objects or not degrees:
        return [0.0, 0.0, 0.0]
    corners: list[tuple[float, float, float]] = []
    for item in objects:
        obb = obb_from_item(item)
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                for sz in (-1.0, 1.0):
                    offset = tuple(sum(obb["axes"][axis][coordinate] * obb["half"][axis] * (sx, sy, sz)[axis] for axis in range(3)) for coordinate in range(3))
                    corners.append(tuple(obb["center"][coordinate] + offset[coordinate] for coordinate in range(3)))
    center = [(min(corner[index] for corner in corners) + max(corner[index] for corner in corners)) / 2.0 for index in range(3)]
    angle = math.radians(degrees)
    rotation_x = ((1.0, 0.0, 0.0), (0.0, math.cos(angle), -math.sin(angle)), (0.0, math.sin(angle), math.cos(angle)))
    for item in objects:
        position = vec_from_item(item, ("position", "pos"), [0.0, 0.0, 0.0])
        relative = tuple(position[index] - center[index] for index in range(3))
        rotated = matrix_apply(rotation_x, relative)
        item["position"] = [rotated[index] + center[index] for index in range(3)]
        local_matrix = tuple(zip(*euler_axes(vec_from_item(item, ("rotation", "rot"), [0.0, 0.0, 0.0]))))
        item["rotation"] = matrix_to_euler(matrix_multiply(rotation_x, local_matrix))
    return center


def obbs_intersect(left: dict[str, Any], right: dict[str, Any]) -> bool:
    delta = tuple(right["center"][index] - left["center"][index] for index in range(3))
    candidate_axes = [*left["axes"], *right["axes"]]
    candidate_axes.extend(cross(a_axis, b_axis) for a_axis in left["axes"] for b_axis in right["axes"])
    for raw_axis in candidate_axes:
        axis = normalized(raw_axis)
        if axis is None:
            continue
        left_radius = sum(left["half"][index] * abs(dot(axis, left["axes"][index])) for index in range(3))
        right_radius = sum(right["half"][index] * abs(dot(axis, right["axes"][index])) for index in range(3))
        if abs(dot(delta, axis)) > left_radius + right_radius:
            return False
    return True


def face_conflicts(left: dict[str, Any], left_axis_index: int, left_sign: float, right: dict[str, Any], tolerance: float) -> bool:
    from shapely.geometry import Polygon

    left_normal = left["axes"][left_axis_index]
    left_center = tuple(left["center"][i] + left_normal[i] * left["half"][left_axis_index] * left_sign for i in range(3))
    left_face_axes = [index for index in range(3) if index != left_axis_index]
    for right_axis_index, right_normal in enumerate(right["axes"]):
        if abs(abs(dot(left_normal, right_normal)) - 1.0) > 1e-6:
            continue
        for right_sign in (-1.0, 1.0):
            right_center = tuple(right["center"][i] + right_normal[i] * right["half"][right_axis_index] * right_sign for i in range(3))
            if abs(dot(tuple(right_center[i] - left_center[i] for i in range(3)), left_normal)) > tolerance:
                continue
            u_axis, v_axis = (left["axes"][index] for index in left_face_axes)
            def face_polygon(center, box, axis_index):
                face_axes = [index for index in range(3) if index != axis_index]
                corners = []
                for u_sign, v_sign in ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)):
                    point = tuple(
                        center[i]
                        + box["axes"][face_axes[0]][i] * box["half"][face_axes[0]] * u_sign
                        + box["axes"][face_axes[1]][i] * box["half"][face_axes[1]] * v_sign
                        for i in range(3)
                    )
                    corners.append((dot(point, u_axis), dot(point, v_axis)))
                return Polygon(corners)
            left_polygon = face_polygon(left_center, left, left_axis_index)
            right_polygon = face_polygon(right_center, right, right_axis_index)
            if left_polygon.intersection(right_polygon).area > tolerance * tolerance:
                return True
    return False


def has_coplanar_surface_conflict(left: dict[str, Any], right: dict[str, Any], tolerance: float) -> bool:
    """Detect z-fighting candidates: overlapping faces lying on the same geometric plane."""
    for left_axis_index in range(3):
        for left_sign in (-1.0, 1.0):
            if face_conflicts(left, left_axis_index, left_sign, right, tolerance):
                return True
    return False


def opposite_face_is_exposed(objects: list[dict[str, Any]], object_index: int, axis_index: int, direction: float, tolerance: float) -> bool:
    candidate = obb_from_item(objects[object_index])
    opposite_sign = -direction
    return not any(
        face_conflicts(candidate, axis_index, opposite_sign, obb_from_item(other), tolerance)
        for index, other in enumerate(objects)
        if index != object_index
    )


def count_object_surface_conflicts(objects: list[dict[str, Any]], object_index: int, tolerance: float) -> int:
    target = obb_from_item(objects[object_index])
    return sum(
        has_coplanar_surface_conflict(target, obb_from_item(other), tolerance)
        for index, other in enumerate(objects)
        if index != object_index
    )


def spatial_candidate_pairs(
    objects: list[dict[str, Any]],
    tolerance: float,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> list[tuple[int, int]]:
    """Use SciPy cKDTree as a broad-phase index; narrow phase remains exact face testing."""
    if len(objects) < 2:
        return []
    import numpy as np
    from scipy.spatial import cKDTree

    obbs = [obb_from_item(item) for item in objects]
    centers = np.asarray([obb["center"] for obb in obbs], dtype=float)
    radii = np.asarray([math.sqrt(sum(value * value for value in obb["half"])) for obb in obbs])
    tree = cKDTree(centers)
    max_radius = float(radii.max())
    candidates: list[tuple[int, int]] = []
    for left_index, center in enumerate(centers):
        nearby = tree.query_ball_point(center, float(radii[left_index] + max_radius + tolerance))
        for right_index in nearby:
            if right_index <= left_index:
                continue
            if np.linalg.norm(centers[right_index] - center) <= radii[left_index] + radii[right_index] + tolerance:
                candidates.append((left_index, right_index))
        if progress_callback:
            progress_callback("index", left_index + 1, len(objects))
    return candidates


def clear_object_overlaps(
    objects: list[dict[str, Any]],
    *,
    clearance: float,
    surface_tolerance: float,
    multi_face_scale_increment: float,
    max_passes: int,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[int, int]:
    """Clear intersecting cuboids using the shortest verified local-axis displacement."""
    if clearance <= 0:
        raise ValueError("object_overlap_clearance must be > 0")
    moved = 0
    scaled = 0
    for _ in range(max_passes):
        changed = False
        candidate_pairs = spatial_candidate_pairs(objects, surface_tolerance, progress_callback)
        for candidate_index, (left_index, right_index) in enumerate(candidate_pairs, start=1):
            left = obb_from_item(objects[left_index])
            right = obb_from_item(objects[right_index])
            if has_coplanar_surface_conflict(left, right, surface_tolerance):
                conflict_count = count_object_surface_conflicts(objects, right_index, surface_tolerance)
                if conflict_count > 1 and multi_face_scale_increment > 0:
                    original_scale = vec_from_item(objects[right_index], ("scale",), [1.0, 1.0, 1.0])
                    scale_candidates: list[tuple[int, int]] = []
                    for axis_index in range(3):
                        if not any(opposite_face_is_exposed(objects, right_index, axis_index, direction, surface_tolerance) for direction in (-1.0, 1.0)):
                            continue
                        trial_scale = list(original_scale)
                        trial_scale[axis_index] += multi_face_scale_increment
                        objects[right_index]["scale"] = trial_scale
                        clears_current = not has_coplanar_surface_conflict(left, obb_from_item(objects[right_index]), surface_tolerance)
                        remaining = count_object_surface_conflicts(objects, right_index, surface_tolerance)
                        objects[right_index]["scale"] = original_scale
                        if clears_current and remaining < conflict_count:
                            scale_candidates.append((remaining, axis_index))
                    if scale_candidates:
                        _, axis_index = min(scale_candidates)
                        expanded_scale = list(original_scale)
                        expanded_scale[axis_index] += multi_face_scale_increment
                        objects[right_index]["scale"] = expanded_scale
                        changed = True
                        scaled += 1
                        if progress_callback:
                            progress_callback("detect", candidate_index, len(candidate_pairs))
                        continue

                original = right["center"]
                candidates: list[tuple[float, float, float, float]] = []
                for axis in right["axes"]:
                    for direction in (-1.0, 1.0):
                        distance = clearance
                        # Grow until this exact candidate direction actually clears the overlap.
                        while distance <= 1_000_000:
                            right["center"] = tuple(original[i] + axis[i] * direction * distance for i in range(3))
                            if not has_coplanar_surface_conflict(left, right, surface_tolerance):
                                low, high = 0.0, distance
                                for _ in range(24):
                                    middle = (low + high) / 2.0
                                    right["center"] = tuple(original[i] + axis[i] * direction * middle for i in range(3))
                                    if has_coplanar_surface_conflict(left, right, surface_tolerance):
                                        low = middle
                                    else:
                                        high = middle
                                candidates.append((high + clearance, axis[0] * direction, axis[1] * direction, axis[2] * direction))
                                break
                            distance *= 2.0
                        right["center"] = original
                if not candidates:
                    raise ValueError("unable to find an axis that clears planar overlap")
                distance, dx, dy, dz = min(candidates, key=lambda candidate: candidate[0])
                position = vec_from_item(objects[right_index], ("position", "pos"), [0.0, 0.0, 0.0])
                objects[right_index]["position"] = [position[0] + dx * distance, position[1] + dy * distance, position[2] + dz * distance]
                changed = True
                moved += 1
            if progress_callback:
                progress_callback("detect", candidate_index, len(candidate_pairs))
        if not changed:
            return moved, scaled
    return moved, scaled


