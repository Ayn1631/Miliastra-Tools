#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from miliastra_core.gil import GilDocument


def main() -> int:
    parser = argparse.ArgumentParser(description="读取 GIL 容器并输出 lossless Wire 结构摘要。")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--no-strict-sizes", action="store_true")
    args = parser.parse_args()

    document = GilDocument.load(args.input, strict_sizes=not args.no_strict_sizes)
    document.validate_roundtrip()
    text = json.dumps(document.inspect_summary(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
