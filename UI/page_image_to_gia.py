"""图片生成 GIA 页面入口。"""
from __future__ import annotations

# MILIASTRA_OPTIMIZED_ENTRY_V2
from UI.page_image_to_gia_advanced import render_image_to_gia_advanced_page


def render_image_to_gia_page() -> None:
    render_image_to_gia_advanced_page()


__all__ = ["render_image_to_gia_page"]
