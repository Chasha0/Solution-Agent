"""DesignHandler — 初版方案 + iterate 子状态（spec §3 design 行 + §3.4 3 种 iterate）。"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.llm import LLMClient, Message
from agent.storage import Session, Stage
from agent.stages.base import BaseStage, StageResult
from agent.stages.react import run_react

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_env = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    autoescape=select_autoescape(disabled_extensions=("j2",)),
)

REQUIRED_ANCHORS = ("bg", "goal", "arch", "deploy", "budget")
ANCHOR_TO_LABEL = {
    "bg": "背景 / 客户需求",
    "goal": "目标 / 预期收益",
    "arch": "架构 / 模块设计",
    "deploy": "部署 / 实施计划",
    "budget": "预算 / 风险",
}

INTENT_REWRITE_PATTERNS = (
    "整篇重新", "整篇重写", "推倒重来", "全部重写", "重新写", "从头写",
)
INTENT_RESTRUCTURE_PATTERNS = (
    "加一节", "加节", "添加一节", "删除", "去掉", "移除", "调到", "移到", "放最后", "放最前",
)


def _is_intent_rewrite(text: str) -> bool:
    s = text.lower()
    return any(p in s for p in INTENT_REWRITE_PATTERNS)


def _is_intent_restructure(text: str) -> bool:
    return any(p in text for p in INTENT_RESTRUCTURE_PATTERNS)


def _detect_anchor(text: str) -> str | None:
    """Heuristic: map natural language reference to one of the 5 anchors."""
    s = text.lower()
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("bg", ("背景", "需求", "现状")),
        ("goal", ("目标", "收益", "预期")),
        ("arch", ("架构", "模块", "设计", "技术")),
        ("deploy", ("部署", "实施", "上线", "排期", "里程碑", "计划", "时间")),
        ("budget", ("预算", "价格", "费用", "成本", "风险", "报价")),
    ]
    # Numeric references: "第一部分"=bg, "第二"=goal, "第三"=arch, "第四"=deploy, "第五"=budget
    num_map = {"一": "bg", "二": "goal", "三": "arch", "四": "deploy", "五": "budget"}
    for ch, anchor in num_map.items():
        if f"第{ch}部分" in s or f"第{ch}节" in s or f"第{ch}块" in s:
            return anchor
    # Keyword-based
    for anchor, kws in rules:
        for kw in kws:
            if kw in s:
                return anchor
    return None


def validate_design(content: str) -> tuple[bool, str]:
    """Guardrail check: ≥ 5 sections, all anchors, every section has ≥ 1 source.

    Returns (ok, reason).
    """
    if not content or not content.strip():
        return False, "empty"

    # Count sections by anchor presence
    anchor_re = re.compile(r"<!--\s*anchor:(\w+)\s*-->", re.IGNORECASE)
    anchors = set(anchor_re.findall(content))
    missing = set(REQUIRED_ANCHORS) - anchors
    if missing:
        return False, f"missing anchors: {sorted(missing)}"

    # Per-section: must contain [来源: ...] within the section
    sections = re.split(r"<!--\s*anchor:\w+\s*-->", content)
    # First split is preamble (before first anchor); the rest align with anchors in order
    for i, anchor in enumerate(anchors):
        # find index in order; use simpler: walk content and check each anchor block
        pass

    # Simpler: for each anchor, find its block and check source citation
    for anchor in REQUIRED_ANCHORS:
        m = re.search(rf"<!--\s*anchor:{anchor}\s*-->", content, re.IGNORECASE)
        if not m:
            return False, f"anchor {anchor} not found"
        block_start = m.end()
        # next anchor or end
        nxt = re.search(r"<!--\s*anchor:\w+\s*-->", content[block_start:], re.IGNORECASE)
        block_end = block_start + nxt.start() if nxt else len(content)
        block = content[block_start:block_end]
        if "[来源:" not in block and "[source:" not in block.lower():
            return False, f"section {anchor} missing source citation"

    # Count section headings (## or #) to ensure ≥ 5
    headings = len(re.findall(r"^#{1,3}\s", content, re.MULTILINE))
    if headings < 5:
        return False, f"only {headings} headings (need ≥ 5)"

    return True, "ok"


class DesignHandler(BaseStage):
    name = Stage.design
    required_tools: list[str] = ["kb_search", "save_section", "revise_section"]

    def __init__(self) -> None:
        self._tpl = _env.get_template("design.j2")
        self._revise_tpl = _env.get_template("design_revise.j2")

    @property
    def system_prompt(self) -> str:
        return self._tpl.render()

    async def guardrail(self, session: Session) -> bool:
        content = self.read_artifact(session, "design", "design")
        if not content:
            return False
        ok, _ = validate_design(content)
        return ok

    async def run(
        self,
        session: Session,
        user_msg: str | None,
        *,
        llm: LLMClient,
        tools: list,
    ) -> StageResult:
        has_design = self.has_artifact(session, "design", "design")
        current = self.read_artifact(session, "design", "design") if has_design else None

        # --- iterate sub-state ---
        if has_design and user_msg:
            if _is_intent_rewrite(user_msg):
                return await self._initial(session, llm, tools)
            if _is_intent_restructure(user_msg):
                # Try revise first via LLM, fall back to rewrite
                return await self._iterate(session, user_msg, current, llm, tools, prefer_rewrite=True)
            return await self._iterate(session, user_msg, current, llm, tools, prefer_rewrite=False)

        # --- initial generation ---
        return await self._initial(session, llm, tools)

    # ---- sub-flows ----

    async def _initial(self, session: Session, llm: LLMClient, tools: list) -> StageResult:
        req = self.read_artifact(session, "clarify", "requirements") or ""
        summary = self.read_artifact(session, "summarize", "summary") or ""
        ctx_lines = []
        if req:
            ctx_lines.append(f"[已确认需求]\n```\n{req}\n```")
        if summary:
            ctx_lines.append(f"[调研摘要]\n```\n{summary}\n```")
        ctx = "\n\n".join(ctx_lines)

        system = self._tpl.render(artifact_context=ctx)
        messages: list[Message] = [Message.system(system)]
        messages.extend(self.format_history(session, last_n=10))

        final, _ = await run_react(
            llm, messages, tools,
            allowed_tool_names=self.required_tools,
            max_iters=8,
        )

        # If LLM didn't call save_section, write final content
        if final and not self.has_artifact(session, "design", "design"):
            self.record_artifact(session, "design", "design", final)
        elif final:
            # Already exists (shouldn't happen in _initial, but defensive)
            self.record_artifact(session, "design", "design", final)

        self.append_message(session, "assistant", f"[design generated] {len(final or '')} chars")
        ok, reason = validate_design(self.read_artifact(session, "design", "design") or "")
        if not ok:
            return StageResult(
                reply=f"方案已生成但未通过 guardrail：{reason}。请提供反馈让我重写。",
            )
        return StageResult(
            reply="初版方案已生成。请查看，需要修改请直接告诉我（如'第三部分换 SaaS'）。",
        )

    async def _iterate(
        self,
        session: Session,
        user_msg: str,
        current: str | None,
        llm: LLMClient,
        tools: list,
        *,
        prefer_rewrite: bool,
    ) -> StageResult:
        # Heuristic anchor detection
        anchor = _detect_anchor(user_msg)

        if anchor and not prefer_rewrite:
            # Local revise: build new section content via LLM (no tools)
            new_section = await self._rewrite_section(llm, anchor, current or "", user_msg)
            # Replace section in current design and write as new version
            new_design = self._replace_section(current or "", anchor, new_section)
            ok, reason = validate_design(new_design)
            if not ok:
                # Validate failed: fall back to full rewrite
                return await self._initial(session, llm, tools)
            self.record_artifact(session, "design", "design", new_design)
            self.append_message(session, "user", user_msg)
            self.append_message(session, "assistant", f"[revised {anchor}]")
            return StageResult(
                reply=f"已修改 {ANCHOR_TO_LABEL.get(anchor, anchor)} 章节。",
            )

        # Fallback: full rewrite
        return await self._initial(session, llm, tools)

    async def _rewrite_section(
        self, llm: LLMClient, anchor: str, current: str, user_msg: str
    ) -> str:
        """Use LLM to draft a new section content based on user feedback."""
        # Extract the current section text
        m = re.search(rf"<!--\s*anchor:{anchor}\s*-->(.*?)(?=<!--\s*anchor:|$)", current, re.DOTALL | re.IGNORECASE)
        section_text = m.group(1).strip() if m else ""
        system = (
            "你是方案修订助手。基于客户的反馈，重写指定章节。\n"
            "硬要求：1) 保留 markdown 格式 2) 包含至少 1 处 [来源: ...] 3) 不超过原章节 1.5 倍长度"
        )
        prompt = (
            f"当前章节 ({anchor}):\n```\n{section_text[:2000]}\n```\n\n"
            f"客户反馈: {user_msg}\n\n"
            f"请只输出新章节的内容（不含 anchor 注释，不含标题 '#'）。"
        )
        resp = await llm.chat(
            [Message.system(system), Message.user(prompt)],
            tools=[],
            stream=False,
        )
        return resp.content.strip()

    @staticmethod
    def _replace_section(design: str, anchor: str, new_section: str) -> str:
        """Replace a single anchor section's content in the design markdown.

        Strategy: split on the target anchor marker, replace everything up to
        the next marker (or end), and rejoin.
        """
        marker_re = re.compile(rf"<!--\s*anchor:(\w+)\s*-->", re.IGNORECASE)
        # Find target marker position
        target_m = marker_re.search(design)
        while target_m and target_m.group(1).lower() != anchor:
            target_m = marker_re.search(design, target_m.end())
        if not target_m:
            return design
        # Find next marker position (or end)
        next_m = marker_re.search(design, target_m.end())
        cut = next_m.start() if next_m else len(design)
        head = design[: target_m.end()]
        tail = design[cut:]
        return f"{head}\n{new_section.strip()}\n\n{tail}"
