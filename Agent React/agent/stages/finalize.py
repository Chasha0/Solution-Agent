"""FinalizeHandler — 终稿导出（spec §3 finalize 行）。"""
from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.llm import LLMClient
from agent.storage import Session, SessionStatus, Stage
from agent.stages.base import BaseStage, StageResult
from agent.stages.design import validate_design

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_env = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    autoescape=select_autoescape(disabled_extensions=("j2",)),
)


class FinalizeHandler(BaseStage):
    name = Stage.finalize
    required_tools: list[str] = ["export_report"]

    def __init__(self) -> None:
        self._tpl = _env.get_template("finalize.j2")

    @property
    def system_prompt(self) -> str:
        return self._tpl.render()

    async def guardrail(self, session: Session) -> bool:
        # All upstream artifacts must exist + design must pass guardrail
        for stage, name in [
            ("clarify", "requirements"),
            ("research", "research"),
            ("summarize", "summary"),
            ("design", "design"),
        ]:
            if not self.has_artifact(session, stage, name):
                return False
        content = self.read_artifact(session, "design", "design") or ""
        ok, _ = validate_design(content)
        return ok

    async def run(
        self,
        session: Session,
        user_msg: str | None,  # noqa: ARG002
        *,
        llm: LLMClient,  # noqa: ARG002
        tools: list,  # noqa: ARG002
    ) -> StageResult:
        # Re-validate design
        content = self.read_artifact(session, "design", "design") or ""
        ok, reason = validate_design(content)
        if not ok:
            return StageResult(reply=f"方案未通过校验：{reason}。请先修正再生成最终报告。")

        # Call export_report tool. Note: agent.tools/__init__.py shadows the
        # submodule with the function, so the function lives at agent.tools.export_report.
        from agent.tools import export_report as export_fn
        res_str = await export_fn(session_id=session.id, format="pdf")
        try:
            res = json.loads(res_str)
        except Exception:
            res = {"ok": False, "error": res_str[:200]}

        if not res.get("ok"):
            return StageResult(reply=f"导出失败：{res.get('error', 'unknown')}")

        # Mark session as completed
        session.set_status(SessionStatus.completed)
        session.set_stage(Stage.completed)
        session.save()

        return StageResult(
            reply=f"已生成最终报告。\n- Markdown: {res.get('md', '?')}\n- PDF: {res.get('pdf', '?')}",
            next_stage=Stage.completed,
        )
