from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miliastra_core.image import ImageGiaSettings, build_image_gia_bytes
from miliastra_core.protobuf.wire import parse_fields, rebuild_message


DEFAULT_PNG = ROOT / "100x100装饰物包装测试.png"
DEFAULT_GIA = ROOT / "100x100装饰物包装测试.gia"
DEFAULT_SUMMARY = ROOT / "100x100装饰物包装测试.summary.json"


def build_source_image() -> Image.Image:
    image = Image.new("RGBA", (100, 100))
    pixels = []
    for y in range(100):
        for x in range(100):
            red = round(x * 255 / 99)
            green = round(y * 255 / 99)
            blue = 255 if (x // 10 + y // 10) % 2 else 0
            pixels.append((red, green, blue, 255))
    image.putdata(pixels)
    return image


def verify_container(data: bytes, expected_parents: int, expected_decorations: int) -> dict[str, int | bool]:
    if len(data) < 24:
        raise AssertionError("生成的 GIA 小于最小容器长度")
    payload_size = int.from_bytes(data[16:20], "big")
    if len(data) != 20 + payload_size + 4:
        raise AssertionError("GIA header 中的 payload 长度不正确")
    payload = data[20 : 20 + payload_size]
    fields = parse_fields(payload, context="100x100 GIA root")
    parent_count = sum(field.number == 1 and field.wire_type == 2 for field in fields)
    decoration_count = sum(field.number == 2 and field.wire_type == 2 for field in fields)
    if parent_count != expected_parents:
        raise AssertionError(f"主元件数量应为 {expected_parents}，实际为 {parent_count}")
    if decoration_count != expected_decorations:
        raise AssertionError(f"装饰物数量应为 {expected_decorations}，实际为 {decoration_count}")
    return {
        "payload_size": payload_size,
        "parent_count": parent_count,
        "decoration_count": decoration_count,
        "lossless_roundtrip": rebuild_message(fields) == payload,
    }


def build(png_path: Path, gia_path: Path, summary_path: Path) -> dict[str, object]:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    gia_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    source = build_source_image()
    source.save(png_path, format="PNG")
    image = Image.open(png_path).convert("RGBA")
    if image.size != (100, 100):
        raise AssertionError(f"PNG 尺寸错误：{image.size}")

    runtime_temp = ROOT / ".runtime_100x100"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(runtime_temp)

    settings = ImageGiaSettings(
        target_width_m=100.0,
        target_height_m=100.0,
        max_pixels=10_000,
        max_width_px=100,
        max_height_px=100,
        alpha_threshold=1,
        alpha_threshold_num=-1,
        template_id=10009001,
        block_height_m=1.0,
        merge_rectangles=False,
        collision_mode="off",
        decoration_packaging=True,
        max_decorations_per_parent=999,
    )
    data, export_summary, objects_json = build_image_gia_bytes(image=image, settings=settings)
    gia_path.write_bytes(data)

    objects = json.loads(objects_json)
    gia_summary = export_summary["gia"]
    expected_parent_count = math.ceil(len(objects) / 999)
    group_sizes = [int(parent["decoration_count"]) for parent in gia_summary["parents"]]
    if len(objects) != 10_000:
        raise AssertionError(f"图片导入应生成 10000 个对象，实际为 {len(objects)}")
    if group_sizes != [999] * 10 + [10]:
        raise AssertionError(f"装饰物分组错误：{group_sizes}")
    if any(size > 999 for size in group_sizes):
        raise AssertionError("存在超过 999 个装饰物的空模型")
    parent_centers = [parent["position"] for parent in gia_summary["parents"]]
    if any(center != parent_centers[0] for center in parent_centers[1:]):
        raise AssertionError(f"所有空模型必须共享同一个整体几何中心：{parent_centers}")
    if any(abs(float(value)) > 1e-6 for value in parent_centers[0]):
        raise AssertionError(f"100x100 对称图片的整体几何中心应为原点：{parent_centers[0]}")

    reconstructed = 0
    for parent in gia_summary["parents"]:
        parent_position = parent["position"]
        size = int(parent["decoration_count"])
        for record, source_object in zip(
            gia_summary["records"][reconstructed : reconstructed + size],
            objects[reconstructed : reconstructed + size],
            strict=True,
        ):
            world_position = [
                float(parent_position[axis]) + float(record["local_position"][axis])
                for axis in range(3)
            ]
            if any(abs(world_position[axis] - float(source_object["position"][axis])) > 1e-5 for axis in range(3)):
                raise AssertionError("装饰物局部坐标无法还原原始世界坐标")
        reconstructed += size
    if reconstructed != 10_000:
        raise AssertionError("并非全部装饰物都完成了坐标还原验证")

    container = verify_container(data, expected_parent_count, len(objects))
    if not container["lossless_roundtrip"]:
        raise AssertionError("生成的 GIA 无法 lossless roundtrip")

    result: dict[str, object] = {
        "png": str(png_path),
        "png_size": list(image.size),
        "gia": str(gia_path),
        "gia_file_size": len(data),
        "pixel_object_count": len(objects),
        "parent_count": expected_parent_count,
        "group_sizes": group_sizes,
        "max_group_size": max(group_sizes),
        "all_world_positions_reconstructed": reconstructed == len(objects),
        "container": container,
        "all_parent_centers_identical": all(center == parent_centers[0] for center in parent_centers),
        "parent_centers": parent_centers,
    }
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="生成并验证 100x100 PNG 的装饰物包装 GIA。")
    parser.add_argument("--png", type=Path, default=DEFAULT_PNG)
    parser.add_argument("--gia", type=Path, default=DEFAULT_GIA)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()
    print(json.dumps(build(args.png, args.gia, args.summary), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
