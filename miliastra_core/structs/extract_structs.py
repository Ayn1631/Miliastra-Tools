from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .struct_schema import extract_gil_structs


def extract_structs(gil_path: Path) -> dict[str, Any]:
    """Compatibility entrypoint for callers that provide a GIL file."""

    return extract_gil_structs(gil_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract struct schemas from a GIL with the lossless wire parser."
    )
    parser.add_argument("gil", type=Path, help="input map/project .gil")
    parser.add_argument("--output", type=Path, help="write extracted structs JSON")
    args = parser.parse_args()

    result = extract_structs(args.gil)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
