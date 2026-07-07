"""Orchestrator — main loop: load session, route, dispatch to handler, save.

Per spec §3 + §6 (per-stage timeout, per-process budget, auto-inject artifact).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from agent.llm import LLMClient, LLMConfig
from agent.stages import STAGE_HANDLERS, StageResult
from agent.storage import Session, SessionStatus, Stage
from agent.tools import all_specs

from .router import RouteDecision, route

logger = logging.getLogger(__name__)

PER_STAGE_TIMEOUT_S = 120.0


def _stage_from_decision(decision_str: str) -> Stage | None:
    if decision_str == "stay" or not decision_str.startswith("go_to_"):
        return None
    name = decision_str[len("go_to_"):]
    try:
        return Stage(name)
    except ValueError:
        return None


class Orchestrator:
    """Main loop: load session → route → dispatch handler → save → return."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient(LLMConfig.from_env())
        self.tools = all_specs()

    @staticmethod
    def create_session(customer: str = "") -> str:
        s = Session.create(customer=customer)
        return s.id

    @staticmethod
    def load_session(session_id: str) -> Session:
        return Session.load(session_id)

    async def handle_message(
        self,
        session_id: str,
        user_msg: str,
        files: list[str] | None = None,
    ) -> StageResult:
        """Single message in → StageResult out.

        Steps:
        1. Load session
        2. Append user message to messages.jsonl
        3. (Optional) parse uploaded files via parse_doc
        4. Route via router (rule + LLM)
        5. Dispatch to appropriate stage handler
        6. Save session state
        7. Return StageResult
        """
        session = Session.load(session_id)
        if session.status == SessionStatus.completed:
            return StageResult(reply="本次咨询已完成。如需新咨询，请创建新 session。")

        # 2) Append user message
        if user_msg:
            from agent.stages.base import BaseStage
            bs = BaseStage()
            bs.append_message(session, "user", user_msg)

        # 3) Parse uploaded files (best-effort, pre-routing context)
        file_context = ""
        if files:
            file_context = await self._parse_files(files)

        # 4) Route
        decision: RouteDecision = await route(
            user_msg or "(file upload)", session.stage, session, llm=self.llm
        )
        logger.info(f"Route: {decision.decision} (conf={decision.confidence:.2f}) — {decision.reason}")

        # 5) Resolve target stage
        target_stage = _stage_from_decision(decision.decision) or session.stage

        # 6) Get handler
        handler = STAGE_HANDLERS.get(target_stage)
        if handler is None:
            # terminal stage
            return StageResult(reply=f"阶段 {target_stage.value} 无对应 handler。")

        # 7) Run with timeout
        try:
            effective_user_msg = user_msg
            if file_context:
                effective_user_msg = (user_msg or "") + "\n\n[客户上传文件]\n" + file_context
            result: StageResult = await asyncio.wait_for(
                handler.run(session, effective_user_msg, llm=self.llm, tools=self.tools),
                timeout=PER_STAGE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Stage {target_stage.value} timed out after {PER_STAGE_TIMEOUT_S}s")
            result = StageResult(
                reply=f"阶段 {target_stage.value} 执行超时（{int(PER_STAGE_TIMEOUT_S)}s）。已保存当前进度。",
            )
        except Exception as e:
            logger.exception(f"Handler {target_stage.value} crashed: {e}")
            result = StageResult(
                reply=f"执行出错：{type(e).__name__}: {e}。可重试或联系管理员。",
            )

        # 8) Save
        if result.next_stage is not None and result.next_stage != session.stage:
            try:
                session.set_stage(result.next_stage)
            except Exception as e:
                logger.warning(f"set_stage({result.next_stage}) failed: {e}")
        try:
            session.save()
        except Exception as e:
            logger.warning(f"session.save() failed: {e}")

        # 9) Return
        return result

    async def _parse_files(self, files: list[str]) -> str:
        """Parse uploaded files using parse_doc tool. Concatenate wrapped content."""
        try:
            import agent.tools.parse_doc as parse_doc_mod
        except ImportError:
            return ""
        out: list[str] = []
        for fp in files:
            try:
                content = await parse_doc_mod.parse_doc(file_path=fp)
                out.append(f"\n--- {fp} ---\n{content}\n")
            except Exception as e:
                logger.warning(f"parse_doc({fp}) failed: {e}")
                out.append(f"\n--- {fp} ---\n[parse error: {e}]\n")
        return "".join(out)
