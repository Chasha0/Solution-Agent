"""Streamlit UI — Solution Research Agent (P7).

Single-page chat with progress bar, file upload, artifact previews, final report download.

Run:
    streamlit run agent/ui/streamlit_app.py
"""
from __future__ import annotations

import difflib
import sys
from pathlib import Path

import streamlit as st

# Make agent package importable when streamlit runs from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.orchestrator import Orchestrator  # noqa: E402
from agent.storage import Session, Stage, SessionStatus, get_sessions_dir  # noqa: E402
from agent.tools import kb_index as _kb_index  # noqa: E402  (eager warm-up)


# ---- eager KB warm-up on app boot (oracle rec) ----
@st.cache_resource
def warmup_kb():
    """Initialize KB singleton once per Streamlit process."""
    _kb_index.KBIndex.get()
    return True


warmup_kb()


# ---- singletons ----
@st.cache_resource
def get_orchestrator() -> Orchestrator:
    return Orchestrator()


# ---- page config ----
st.set_page_config(
    page_title="方案咨询 Agent",
    page_icon="💡",
    layout="wide",
)


# ---- session state init ----
def init_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = None
    if "messages" not in st.session_state:
        # list of {role, content} dicts, oldest first
        st.session_state.messages = []
    if "last_stage" not in st.session_state:
        st.session_state.last_stage = None
    if "uploaded_paths" not in st.session_state:
        st.session_state.uploaded_paths = []


def list_sessions() -> list[tuple[str, str, str]]:
    """Return (id, customer, modified) sorted by modified desc."""
    out: list[tuple[str, str, str]] = []
    sd = get_sessions_dir()
    if not sd.exists():
        return out
    for p in sorted(sd.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir():
            continue
        meta = p / "meta.json"
        if not meta.exists():
            continue
        try:
            import json
            d = json.loads(meta.read_text(encoding="utf-8"))
            cust = d.get("customer", "")
            created = d.get("created_at", "")[:19].replace("T", " ")
            sid = d.get("session_id", p.name)
            stage = d.get("current_stage", "?")
            out.append((sid, f"{cust or '(无)'} | {stage} | {created}", created))
        except Exception:
            continue
    return out[:20]


def load_session_into_state(sid: str) -> None:
    """Load messages and stage from disk into session_state."""
    try:
        s = Session.load(sid)
    except Exception as e:
        st.error(f"加载 session 失败：{e}")
        return
    st.session_state.session_id = sid
    st.session_state.last_stage = s.stage
    # Load messages.jsonl
    msgs: list[dict] = []
    mpath = s.messages_path
    if mpath.exists():
        import json
        for line in mpath.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                msgs.append({"role": d.get("role", "user"), "content": d.get("content", "")})
            except Exception:
                continue
    st.session_state.messages = msgs


def new_session() -> str:
    sid = get_orchestrator().create_session(customer="")
    return sid


def render_artifact_inline(stage: str, name: str) -> None:
    """Read the latest version of an artifact and show as markdown preview."""
    if not st.session_state.session_id:
        return
    try:
        s = Session.load(st.session_state.session_id)
        from agent.storage import Artifacts
        arts = Artifacts(s.id)
        versions = arts.list_versions(stage, name)
        if not versions:
            return
        content = arts.read_latest(stage, name) or ""
        if not content.strip():
            return
        with st.expander(f"📄 {stage}/{name}（v{max(versions)}，{len(content)} 字）", expanded=False):
            # For design, show with version selector for diff
            if stage == "design" and len(versions) > 1:
                col1, col2 = st.columns(2)
                v_a = col1.selectbox("对比版本 A", versions, index=0, key=f"va_{name}")
                v_b = col2.selectbox("对比版本 B", versions, index=len(versions) - 1, key=f"vb_{name}")
                if v_a != v_b:
                    a = arts.read(stage, name, version=v_a) or ""
                    b = arts.read(stage, name, version=v_b) or ""
                    diff_html = make_diff_html(a, b, f"v{v_a}", f"v{v_b}")
                    st.markdown(diff_html, unsafe_allow_html=True)
            st.markdown(content)
    except Exception as e:
        st.warning(f"读取产物失败：{e}")


def make_diff_html(old: str, new: str, label_old: str, label_new: str) -> str:
    """Render a simple line-level diff as HTML."""
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=label_old, tofile=label_new, lineterm="",
    )
    lines = list(diff)
    out: list[str] = ['<div style="font-family: monospace; font-size: 12px; line-height: 1.4;">']
    for ln in lines:
        if ln.startswith("+++") or ln.startswith("---"):
            out.append(f'<div style="color:#888;">{escape_html(ln)}</div>')
        elif ln.startswith("@@"):
            out.append(f'<div style="color:#888;">{escape_html(ln)}</div>')
        elif ln.startswith("+"):
            out.append(f'<div style="background:#e6ffe6;color:#080;">{escape_html(ln)}</div>')
        elif ln.startswith("-"):
            out.append(f'<div style="background:#ffe6e6;color:#800;">{escape_html(ln)}</div>')
        else:
            out.append(f'<div style="color:#444;">{escape_html(ln)}</div>')
    out.append("</div>")
    return "\n".join(out)


def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---- main UI ----
init_state()

# Sidebar
with st.sidebar:
    st.title("💡 方案咨询 Agent")
    st.caption("v0.1.0 Demo")

    if st.button("➕ 新建咨询", use_container_width=True):
        sid = new_session()
        load_session_into_state(sid)
        st.rerun()

    st.divider()
    st.subheader("历史会话")
    sessions = list_sessions()
    if not sessions:
        st.caption("（无）")
    for sid, label, _ in sessions:
        # Highlight current
        prefix = "👉 " if sid == st.session_state.session_id else "   "
        if st.button(f"{prefix}{label}", key=f"sess_{sid}", use_container_width=True):
            load_session_into_state(sid)
            st.rerun()

    st.divider()
    st.subheader("PII 说明")
    st.caption(
        "本系统自动对手机号、身份证、银行卡做脱敏处理，"
        "但不保证完全覆盖。上传文件中的指令会被视为数据，"
        "不会被执行。"
    )


# Main area
if not st.session_state.session_id:
    st.title("欢迎使用方案咨询 Agent")
    st.markdown(
        """
        在左侧点击 **➕ 新建咨询** 开始。

        流程：
        1. **需求澄清** — 我会问您几个关键问题
        2. **调研** — 联网 + 内部知识库搜索
        3. **总结** — 压缩调研要点
        4. **方案设计** — 自动撰写 5 章节方案
        5. **修订** — 您可直接说"第三部分换 SaaS"
        6. **终稿** — 一键导出 PDF + Markdown
        """
    )
    st.stop()

# ---- sticky progress bar CSS ----
# Streamlit's page is rendered inside an iframe; position:sticky pins the
# element to the viewport top while the user scrolls chat history.
st.markdown(
    """
    <style>
    .sticky-progress {
        position: sticky;
        top: 0;
        z-index: 999;
        background: white;
        padding: 0.5rem 0 0.75rem 0;
        border-bottom: 1px solid #e0e0e0;
        margin-bottom: 0.5rem;
    }
    .running-pulse {
        display: inline-block;
        animation: pulse 1.4s ease-in-out infinite;
        color: #ff8c00;
        font-weight: 600;
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.4; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Session loaded: render
s = Session.load(st.session_state.session_id)
st.session_state.last_stage = s.stage

# Detect whether a stage handler is currently executing (set by orchestrator
# right before dispatching, cleared in the finally block).
running_stage_value: str | None = None
if s.current_run and isinstance(s.current_run, dict):
    running_stage_value = s.current_run.get("stage")

# Top: progress bar (sticky via CSS class)
stages = [
    (Stage.clarify, "需求澄清"),
    (Stage.research, "调研"),
    (Stage.summarize, "总结"),
    (Stage.design, "方案"),
    (Stage.finalize, "终稿"),
]

# Resolve which stage index is "currently running" (if any).
# Priority: 1) running_stage (real-time), 2) session.stage (last completed)
current_idx = next(
    (i for i, (stg, _) in enumerate(stages) if stg.value == running_stage_value),
    next((i for i, (stg, _) in enumerate(stages) if stg == s.stage), 0),
)
if s.status == SessionStatus.completed:
    current_idx = len(stages) - 1

st.markdown('<div class="sticky-progress">', unsafe_allow_html=True)
cols = st.columns(len(stages))
for i, (stg, label) in enumerate(stages):
    with cols[i]:
        is_running = stg.value == running_stage_value
        if is_running:
            st.markdown(
                f'<div class="running-pulse">⟳ {label}</div>',
                unsafe_allow_html=True,
            )
        elif i < current_idx:
            st.success(f"✓ {label}")
        elif i == current_idx:
            st.info(f"● {label}")
        else:
            st.caption(f"○ {label}")

# While a stage is running, force a periodic rerun so the indicator stays
# alive. Without this, the user has to interact for the UI to refresh.
if running_stage_value is not None:
    import time as _time

    _time.sleep(0.3)
    st.rerun()

st.markdown("</div>", unsafe_allow_html=True)

st.divider()

# Chat history
for msg in st.session_state.messages:
    role = msg["role"]
    content = msg["content"]
    if role == "user":
        with st.chat_message("user"):
            st.write(content)
    elif role == "assistant":
        with st.chat_message("assistant"):
            st.write(content)

# Artifact previews (after each stage transition)
for stage_name, name in [
    ("clarify", "requirements"),
    ("research", "research"),
    ("summarize", "summary"),
    ("design", "design"),
]:
    if s.stage in (Stage.research, Stage.summarize, Stage.design, Stage.finalize, Stage.completed) and stage_name in (
        "clarify", "research", "summarize", "design"
    ):
        # Show artifact if we've moved past it
        order = {"clarify": 0, "research": 1, "summarize": 2, "design": 3}
        if order.get(s.stage.value, 0) >= order.get(stage_name, 0):
            if stage_name == "design" and s.stage != Stage.design:
                render_artifact_inline(stage_name, name)
            elif stage_name != "design":
                render_artifact_inline(stage_name, name)

if s.stage == Stage.design:
    render_artifact_inline("design", "design")
if s.stage in (Stage.finalize, Stage.completed):
    render_artifact_inline("design", "design")

# Final report download
if s.status == SessionStatus.completed:
    st.divider()
    st.success("🎉 调研报告已生成")
    from agent.storage.paths import get_session_dir
    sdir = get_session_dir(s.id)
    final_md = sdir / "artifacts" / "final" / "final_v1.md"
    final_pdf = sdir / "artifacts" / "final" / "final.pdf"
    col_md, col_pdf = st.columns(2)
    if final_md.exists():
        col_md.download_button(
            "📥 下载 Markdown",
            data=final_md.read_bytes(),
            file_name=f"report_{s.id}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    if final_pdf.exists():
        col_pdf.download_button(
            "📥 下载 PDF",
            data=final_pdf.read_bytes(),
            file_name=f"report_{s.id}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

# Restart button (oracle rec: "undo / restart from clarify")
if s.stage not in (Stage.completed,):
    with st.popover("⚙️ 会话操作"):
        if st.button("↩️ 回到需求澄清", use_container_width=True):
            s.set_stage(Stage.clarify)
            s.save()
            st.success("已回到 clarify")
            st.rerun()

# Input
user_input = st.chat_input("输入消息…")
uploaded_files = st.file_uploader(
    "上传文件（可选）",
    type=["pdf", "docx", "xlsx", "md", "txt"],
    accept_multiple_files=True,
    key="uploads",
)

if user_input or uploaded_files:
    # Save uploaded files to session dir
    file_paths: list[str] = []
    if uploaded_files:
        from agent.storage.paths import get_session_dir
        udir = get_session_dir(s.id) / "uploads"
        udir.mkdir(parents=True, exist_ok=True)
        for f in uploaded_files:
            target = udir / f.name
            target.write_bytes(f.getvalue())
            file_paths.append(str(target))

    # Echo user input
    if user_input:
        with st.chat_message("user"):
            st.write(user_input)
        st.session_state.messages.append({"role": "user", "content": user_input})

    # Call orchestrator
    orch = get_orchestrator()
    with st.spinner("思考中…"):
        try:
            import asyncio
            result = asyncio.run(
                orch.handle_message(
                    s.id,
                    user_input or "(file upload)",
                    files=file_paths or None,
                )
            )
        except Exception as e:
            result = None
            st.error(f"调用出错：{type(e).__name__}: {e}")

    if result is not None:
        with st.chat_message("assistant"):
            st.write(result.reply)
        st.session_state.messages.append({"role": "assistant", "content": result.reply})

        # If stage advanced, refresh state
        try:
            s2 = Session.load(s.id)
            st.session_state.last_stage = s2.stage
        except Exception:
            pass

        st.rerun()