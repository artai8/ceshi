# nb/web_ui/0_👋_Hello.py

import streamlit as st
from nb.web_ui.utils import hide_st, switch_theme
from nb.config import read_config

CONFIG = read_config()

st.set_page_config(
    page_title="NB - Home",
    page_icon="👋",
    layout="wide"
)
switch_theme(st, CONFIG)

# --- Hero Header ---
col_logo, col_txt = st.columns([1, 6])
with col_logo:
    st.markdown("<div style='font-size:4rem;padding-top:10px;'>🚀</div>", unsafe_allow_html=True)
with col_txt:
    st.markdown("""
    <div style='padding-top: 10px;'>
        <h1 style='margin-bottom: 0; font-size: 2.5rem; background: -webkit-linear-gradient(45deg, #6366f1, #ec4899); -webkit-background-clip: text; -webkit-text-fill-color: transparent;'>欢迎使用 NB 管理器</h1>
        <p style='font-size: 1.1rem; opacity: 0.8;'>终极 Telegram 转发工具</p>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# --- Feature Cards ---
st.markdown("### 🚀 功能特性")
c1, c2, c3 = st.columns(3)

def card(icon, title, desc, color):
    st.markdown(f"""
    <div class="neu-card">
        <div style="
            width: 40px; height: 40px; 
            background: {color}20; 
            border-radius: 8px; 
            display: flex; align-items: center; justify-content: center;
            font-size: 1.2rem; margin-bottom: 12px;
        ">
            {icon}
        </div>
        <h3 style="margin: 0 0 8px 0; font-size: 1.1rem;">{title}</h3>
        <p style="margin: 0; opacity: 0.8; font-size: 0.9rem; line-height: 1.5;">{desc}</p>
    </div>
    """, unsafe_allow_html=True)

with c1:
    card("📤", "智能转发", "轻松实现频道、群组和机器人之间的自动化消息转发。", "#6366f1")
with c2:
    card("⚡", "实时与历史模式", "支持实时消息同步或带有偏移量控制的历史记录迁移。", "#10b981")
with c3:
    card("🧩", "强大插件", "过滤、替换、水印、OCR、格式化以及实时修改消息。", "#f59e0b")

st.markdown("---")

# --- Quick Links ---
c_info, c_dev = st.columns([2, 1])

with c_info:
    st.info("""
    **如何开始？**
    请查看侧边栏菜单来配置您的 **TG 登录**，设置 **连接**，并启动 **运行仪表盘**。
    """)
    st.markdown("查看文档以了解更多信息。")

with c_dev:
    st.markdown("""
    <div class="glass-card" style="border-left: 4px solid #6366f1;">
        <small style="opacity: 0.8"><b>开发者提示：</b><br>
        插件允许您在消息发送前对其进行修改。完全可定制！</small>
    </div>
    """, unsafe_allow_html=True)
