
"""Structure schema parsing and data JSON conversion."""

from .structs import (
    build_components_gil,
    extract_structs_from_uploaded,
    normalize_structs_doc,
    parse_structs_json,
)

__all__ = [
    "build_components_gil",
    "extract_structs_from_uploaded",
    "normalize_structs_doc",
    "parse_structs_json",
]
