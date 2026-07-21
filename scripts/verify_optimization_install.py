#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

MARKER = "MILIASTRA_OPTIMIZED_ENTRY_V2"


def _ok(label: str, detail: str) -> None:
    print(f"[OK] {label}: {detail}")


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Miliastra 图像优化层是否真正接管运行入口。")
    parser.add_argument("target", nargs="?", type=Path, default=Path.cwd(), help="Miliastra-Tools 仓库根目录")
    args = parser.parse_args()
    root = args.target.expanduser().resolve()

    checks: list[tuple[str, bool, str]] = []
    entry = root / "UI" / "page_image_to_gia.py"
    entry_text = entry.read_text(encoding="utf-8") if entry.is_file() else ""
    checks.append(("优化页面入口", MARKER in entry_text, str(entry)))
    checks.append(("共享算法核心", (root / "miliastra_core" / "raster" / "pipeline.py").is_file(), "miliastra_core/raster/pipeline.py"))
    checks.append(("GIA 构建器", (root / "miliastra_core" / "export" / "builder.py").is_file(), "miliastra_core/export/builder.py"))
    checks.append(("GIA 高级页面", (root / "UI" / "page_image_to_gia_advanced.py").is_file(), "UI/page_image_to_gia_advanced.py"))
    checks.append(("无 GIL 导出后端", not (root / "miliastra_core" / "gil" / "writer.py").exists(), "miliastra_core/gil/writer.py 不存在"))
    checks.append(("无本地 proto 目录", not (root / "proto").exists(), "proto/ 不存在"))

    for label, passed, detail in checks:
        (_ok if passed else _fail)(label, detail)

    if not all(item[1] for item in checks):
        return 1

    sys.path.insert(0, str(root))
    spec = importlib.util.find_spec("miliastra_core.raster.pipeline")
    if spec is None or spec.origin is None:
        _fail("Python 导入路径", "找不到 miliastra_core.raster.pipeline")
        return 1
    _ok("Python 导入路径", spec.origin)
    print("\n优化层已经真正接管入口。请彻底停止旧 Streamlit 进程后重新启动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
