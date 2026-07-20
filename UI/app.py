from __future__ import annotations

from pathlib import Path

import streamlit as st
from UI.page_image_to_gia import render_image_to_gia_page
from UI.page_sketch_to_gia import render_sketch_to_gia_page
from UI.task_queue import queue_enabled


TOOL_PAGES = [
    "图片生成 GIA",
    "线稿转 GIA",
]
_DISCLAIMER_ACCEPTED_KEY = "site_disclaimer_accepted"
_DISCLAIMER_CHECKBOX_KEY = "site_disclaimer_confirmed"

UI_DIR = Path(__file__).resolve().parent
APP_PAGE_CSS = (UI_DIR / 'APP_PAGE_CSS.css').read_text(encoding='utf-8')


@st.dialog("使用须知与免责声明", width="large")
def render_disclaimer_dialog() -> None:
    st.warning("请完整阅读以下内容。未明确勾选同意前，无法进入或使用本网站。")
    st.markdown(
        """
1. **非官方声明**：本网站为非官方社区工具，与米哈游、HoYoverse 及其关联方不存在隶属、授权、赞助或背书关系。相关游戏名称、素材及商标权利归各自权利人所有。
2. **使用责任**：本工具仅用于合法的学习、研究与创作辅助。你应确保上传、处理、导出及发布的内容拥有合法来源和必要授权，不得用于违法、侵权、破坏游戏或平台规则的行为。
3. **结果风险**：算法输出可能存在误差、遗漏、兼容性问题或无法使用的情况。本网站不保证结果的准确性、完整性、稳定性、适用性或持续可用性。
4. **数据与隐私**：请勿上传隐私、机密或敏感内容。为执行任务，上传内容及中间结果可能在服务器上临时处理和保存，并按系统清理策略删除；你应自行保留原始文件和重要结果。
5. **损失承担**：因使用或无法使用本网站、导入生成文件、数据丢失、账号或设备异常等造成的直接或间接损失，由使用者自行承担。
6. **服务调整**：网站维护者可根据运行、安全或合规需要暂停、限制、修改或终止部分功能。
7. **同意条件**：继续使用即表示你已阅读、理解并自愿接受以上内容；如有任何不同意或不确定，请关闭页面并停止使用。
        """
    )
    confirmed = st.checkbox(
        "我已完整阅读、理解并同意上述使用须知与免责声明",
        key=_DISCLAIMER_CHECKBOX_KEY,
    )
    if st.button(
        "同意并进入网站",
        type="primary",
        disabled=not confirmed,
        use_container_width=True,
    ):
        st.session_state[_DISCLAIMER_ACCEPTED_KEY] = True
        st.rerun()


def require_disclaimer_acceptance() -> None:
    if st.session_state.get(_DISCLAIMER_ACCEPTED_KEY) is True:
        return
    render_disclaimer_dialog()
    st.stop()


def render_tool_page_header(page: str) -> None:
    descriptions = {
        "图片生成 GIA": "把图片像素解析为白模单位并生成 GIA。",
        "线稿转 GIA": "把图片边缘或线稿拟合为少量连续变换基元并生成 GIA。",
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


def render_site_footer() -> None:
    st.markdown(
        """
        <footer class="qx-site-footer">
          <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">
            黔ICP备2026012245号
          </a>
        </footer>
        """,
        unsafe_allow_html=True,
    )

def render_standard_tool_page(page: str) -> None:
    st.markdown(APP_PAGE_CSS, unsafe_allow_html=True)
    render_tool_page_header(page)
    if page == "图片生成 GIA":
        render_image_to_gia_page()
    elif page == "线稿转 GIA":
        render_sketch_to_gia_page()
    render_site_footer()


def main() -> None:
    st.set_page_config(page_title="千星工具箱", layout="wide")
    require_disclaimer_acceptance()

    st.sidebar.title("千星工具箱")
    st.sidebar.caption("GIL / GIA workflow")
    st.sidebar.caption(
        "计算模式：RQ 单 Worker 排队"
        if queue_enabled()
        else "计算模式：本地同步（开发环境）"
    )
    page = st.sidebar.radio("功能", TOOL_PAGES)

    render_standard_tool_page(page)
