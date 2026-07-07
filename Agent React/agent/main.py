"""Streamlit UI 入口。"""
import streamlit as st

st.set_page_config(
    page_title="Solution Research Agent",
    page_icon="",
    layout="wide",
)

st.title("Solution Research Agent")
st.caption("v0.1.0 — P0 scaffold")

with st.sidebar:
    st.header("Session")
    st.info("P0: 脚手架占位。P7 起接入真实对话。")

st.markdown("---")
st.write("**当前状态**：脚手架已就绪，等待 P7 接入 UI。")
st.write("**设计文档**：`docs/superpowers/specs/2026-07-07-solution-research-agent-design.md`")
