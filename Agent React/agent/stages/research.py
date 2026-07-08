"""ResearchHandler — 调研，ReAct 循环（spec §3 research 行 + §6 错误表）。"""
from __future__ import annotations

import logging
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


class ResearchHandler(BaseStage):
    name = Stage.research
    required_tools: list[str] = ["web_search", "kb_search", "save_section"]

    def __init__(self) -> None:
        self._tpl = _env.get_template("research.j2")

    @property
    def system_prompt(self) -> str:
        return self._tpl.render()

    async def guardrail(self, session: Session) -> bool:
        return self.has_artifact(session, "research", "research")

    async def run(
        self,
        session: Session,
        user_msg: str | None,  # noqa: ARG002
        *,
        llm: LLMClient,
        tools: list,
    ) -> StageResult:
        # Inject requirements + current state
        req = self.read_artifact(session, "clarify", "requirements") or "(无已确认需求)"
        artifact_ctx = self.inject_artifact_context(session, "clarify", "requirements")

        system = self.system_prompt + f"\n\n[已确认需求]\n```\n{req}\n```\n{artifact_ctx}"
        messages: list[Message] = [Message.system(system)]
        messages.extend(self.format_history(session, last_n=10))

        # ReAct loop (6 iters × ~15s avg per iter ≈ 90s, under the 120s stage cap).
# Tavily is fast (~2-5s) and per-tool timeout is 20s, so this fits with margin.
final_content, tool_history = await run_react(
            llm, messages, tools,
            allowed_tool_names=self.required_tools,
            max_iters=6,
            per_tool_timeout_s=20.0,
        )

        # Count sources
        web_count = sum(1 for c in tool_history if c["name"] == "web_search")
        kb_count = sum(1 for c in tool_history if c["name"] == "kb_search")
        save_count = sum(1 for c in tool_history if c["name"] == "save_section")

        # If LLM didn't call save_section, write the final content as research
        if save_count == 0 and final_content:
            self.record_artifact(session, "research", "research", final_content)
        elif save_count > 0:
            # save_section already wrote the artifact
            pass

        # Append source count note
        coverage_note = (
            f"\n\n## 覆盖度\n- KB 来源数：{kb_count}\n- Web 来源数：{web_count}\n"
            f"- 总来源数：{kb_count + web_count}"
        )
        if kb_count + web_count < 4:
            coverage_note += "\n⚠️ 来源不足（目标：KB ≥ 1 + Web ≥ 3）"

        # Append coverage note to the artifact (use latest version)
        latest = self.read_artifact(session, "research", "research") or ""
        if latest and coverage_note.strip() not in latest:
            self.record_artifact(session, "research", "research", latest + coverage_note)

        self.append_message(session, "assistant", f"[research done] KB={kb_count} Web={web_count}")
        reply = f"调研完成。KB 来源 {kb_count} 条，联网 {web_count} 条。"

        return StageResult(
            reply=reply,
            next_stage=Stage.summarize if self.has_artifact(session, "research", "research") else None,
        )
