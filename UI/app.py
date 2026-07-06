from __future__ import annotations

from pathlib import Path

import streamlit as st
from UI.page_image_to_gia import render_image_to_gia_page


TOOL_PAGES = [
    "图片生成 GIA",
]

UI_DIR = Path(__file__).resolve().parent
APP_PAGE_CSS = (UI_DIR / 'APP_PAGE_CSS.css').read_text(encoding='utf-8')
    
def render_tool_page_header(page: str) -> None:
    descriptions = {
        "图片生成 GIA": "把图片像素解析为白模单位并生成 GIA。",
    }
    st.markdown(
        f"""
        <div class="qx-page-hero">
          <div class="qx-page-kicker">Miliastra Wonderland Tooling</div>
          <div class="qx-page-title">{page}</div>
          <div class="qx-shell-title">{descriptions.get(page, "千星工具箱")}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def render_standard_tool_page(page: str) -> None:
    st.markdown(APP_PAGE_CSS, unsafe_allow_html=True)
    render_tool_page_header(page)
    if page == "图片生成 GIA":
        render_image_to_gia_page()


def main() -> None:
    st.set_page_config(page_title="千星工具箱", layout="wide")

    st.sidebar.title("千星工具箱")
    st.sidebar.caption("GIL / GIA workflow")
    page = st.sidebar.radio("功能", TOOL_PAGES)

    render_standard_tool_page(page)
