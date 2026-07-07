"""SummarizeHandler — 调研摘要（spec §3 summarize 行：200-400 字，不新增）。"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.llm import LLMClient, Message
from agent.storage import Session, Stage
from agent.stages.base import BaseStage, StageResult

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_env = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    autoescape=select_autoescape(disabled_extensions=("j2",)),
)


class SummarizeHandler(BaseStage):
    name = Stage.summarize
    required_tools: list[str] = []

    def __init__(self) -> None:
        self._tpl = _env.get_template("summarize.j2")

    @property
    def system_prompt(self) -> str:
        return ""

    async def guardrail(self, session: Session) -> bool:
        return self.has_artifact(session, "summarize", "summary")

    async def run(
        self,
        session: Session,
        user_msg: str | None,  # noqa: ARG002
        *,
        llm: LLMClient,
        tools: list,  # noqa: ARG002
    ) -> StageResult:
        research = self.read_artifact(session, "research", "research")
        if not research:
            return StageResult(
                reply="无调研内容可摘要。请先完成 research 阶段。",
            )

        # Render prompt with research content
        user_content = self._tpl.render(research_content=research[:6000])
        messages: list[Message] = [Message.user(user_content)]

        resp = await llm.chat(messages, tools=[], stream=False)
        summary = resp.content.strip()

        # Save
        self.record_artifact(session, "summarize", "summary", summary)
        self.append_message(session, "assistant", f"[summary] {len(summary)} chars")

        return StageResult(
            reply=f"已生成调研摘要（{len(summary)} 字）。",
            next_stage=Stage.design,
        )
