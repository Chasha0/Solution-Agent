"""Router — 规则快路径 + LLM 兜底（spec §3.1 + §3.2）。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from agent.llm import LLMClient, Message
from agent.storage import Session, Stage

logger = logging.getLogger(__name__)

Decision = Literal["stay", "go_to_clarify", "go_to_research", "go_to_summarize", "go_to_design", "go_to_finalize"]


@dataclass
class RouteDecision:
    decision: Decision
    confidence: float
    reason: str


# ---- rule fast path ----
# Order matters: more specific patterns first.

RULES: list[tuple[re.Pattern, Decision, str]] = [
    (re.compile(r"重新调研|再调研|再查查|重新搜|重新研究"), "go_to_research", "rule: 重新调研"),
    (re.compile(r"回到方案|看方案|之前的方案|回到设计"), "go_to_design", "rule: 回到方案"),
    (re.compile(r"出报告|可以了|出最终|最终报告|没问题|确认|生成报告|出调研报告"), "go_to_finalize", "rule: 出报告"),
    (re.compile(r"重新开始|重来|从头来|重新澄清|回到澄清"), "go_to_clarify", "rule: 重新开始"),
    (re.compile(r"回到调研|看调研|再总结"), "go_to_summarize", "rule: 回到调研"),
]

INTENT_REWRITE_PATTERNS = ("整篇重新", "整篇重写", "推倒重来", "全部重写", "重新写", "从头写")
INTENT_REVISE_PATTERNS = ("换", "改成", "调整", "优化", "缩短", "扩充", "改", "调", "压缩", "删", "减", "加", "扩")

LOW_CONFIDENCE = 0.7


def _match_rule(text: str) -> RouteDecision | None:
    for pat, dec, reason in RULES:
        if pat.search(text):
            return RouteDecision(decision=dec, confidence=0.95, reason=reason)
    return None


def _looks_like_iterate(text: str) -> bool:
    """True if the text looks like design-stage feedback (iterate, not stage switch)."""
    if any(p in text for p in INTENT_REWRITE_PATTERNS):
        return False  # Rewrite is a special sub-state, but doesn't change stage
    if any(p in text for p in INTENT_REVISE_PATTERNS):
        return True
    return False


# ---- LLM fallback prompt ----

_LLM_ROUTER_PROMPT = """你是"方案咨询 agent"的路由器。判断用户消息应该让 agent 留在当前阶段，还是切换到别的阶段。

## 阶段定义
- `clarify`: 需求澄清（问客户问题）
- `research`: 调研（联网 + KB 搜索）
- `summarize`: 调研摘要
- `design`: 方案设计 / 修订
- `finalize`: 终稿生成

## 当前阶段
{current_stage}

## 客户最近 3 条消息
{recent_messages}

## 客户最新消息
"{user_msg}"

## 决策
只输出一个 JSON 对象（不要其他文字）：
{{"decision": "stay" | "go_to_clarify" | "go_to_research" | "go_to_summarize" | "go_to_design" | "go_to_finalize", "confidence": 0.0-1.0, "reason": "简短理由"}}

## 规则
- 客户只是补充信息/回答问题/追问 → "stay"
- 客户明确说"回到 X"或"重新 X" → 切到对应阶段
- 客户对当前产物提意见但没说"重新" → "stay"（handler 内部处理）
- 拿不准时 → confidence < 0.7
"""


async def route(
    user_msg: str,
    current_stage: Stage,
    session: Session | None = None,
    *,
    llm: LLMClient | None = None,
    confidence_threshold: float = LOW_CONFIDENCE,
) -> RouteDecision:
    """Decide whether to switch stages or stay.

    1. Rule-based fast path (covers high-frequency cases).
    2. LLM fallback for ambiguous cases.

    Returns RouteDecision; caller should apply.
    """
    # 1) Rule fast path
    rule_decision = _match_rule(user_msg)
    if rule_decision is not None:
        # Special case: in design stage, "换/改/调整" is iterate, not stage switch
        if current_stage == Stage.design and rule_decision.decision == "go_to_research":
            # don't go to research; stay and let design handler run iterate
            return RouteDecision(decision="stay", confidence=0.9, reason="design 阶段：迭代子状态")
        return rule_decision

    # 1.5) No rule match, but in design stage and looks like iterate → stay
    if current_stage == Stage.design and _looks_like_iterate(user_msg):
        return RouteDecision(decision="stay", confidence=0.85, reason="design 阶段：迭代子状态")

    # 2) LLM fallback
    if llm is None:
        # No LLM available → default to stay with low confidence
        return RouteDecision(decision="stay", confidence=0.5, reason="no LLM; default stay")

    # Build prompt
    recent = ""
    stage_value = current_stage.value if hasattr(current_stage, "value") else str(current_stage)
    if session is not None:
        try:
            from pathlib import Path
            mpath = Path(session.messages_path)
            if mpath.exists():
                lines = mpath.read_text(encoding="utf-8", errors="ignore").splitlines()[-6:]
                recent = "\n".join(lines)
        except Exception:
            recent = "(无历史)"

    prompt = _LLM_ROUTER_PROMPT.format(
        current_stage=stage_value,
        recent_messages=recent or "(无历史)",
        user_msg=user_msg[:500],
    )
    try:
        resp = await llm.chat(
            [Message.system("你输出严格 JSON。"), Message.user(prompt)],
            tools=[],
            stream=False,
        )
        raw = resp.content.strip()
        # Find JSON object in response
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if not m:
            return RouteDecision(decision="stay", confidence=0.5, reason="LLM no JSON")
        d = json.loads(m.group(0))
        dec = d.get("decision", "stay")
        conf = float(d.get("confidence", 0.5))
        reason = d.get("reason", "")
        # Validate decision
        if dec not in ("stay", "go_to_clarify", "go_to_research", "go_to_summarize", "go_to_design", "go_to_finalize"):
            return RouteDecision(decision="stay", confidence=0.4, reason=f"invalid decision: {dec}")
        # In design stage, force stay on ambiguous feedback
        if current_stage == Stage.design and dec == "go_to_research" and _looks_like_iterate(user_msg):
            return RouteDecision(decision="stay", confidence=0.85, reason="design 阶段：iterate 子状态")
        # Low confidence → default stay
        if conf < confidence_threshold:
            return RouteDecision(decision="stay", confidence=conf, reason=f"low conf: {reason}")
        return RouteDecision(decision=dec, confidence=conf, reason=reason)
    except Exception as e:
        logger.warning(f"LLM router failed: {e}")
        return RouteDecision(decision="stay", confidence=0.4, reason=f"LLM error: {e}")
