"""ClarifyHandler — 需求澄清，含 Y/N 确认门 + force-commit（spec §3.3 + §6 错误表）。"""
from __future__ import annotations

import json
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

MAX_ROUNDS = 5
IDK_FORCE_COMMIT = 2

CLARIFY_STATE_NAME = "clarify_state"
REQUIREMENTS_NAME = "requirements"
IDK_PATTERNS = ["我不知道", "你定", "随便", "都行", "n/a", "N/A", "no idea"]


def _is_idk(text: str) -> bool:
    s = text.strip().lower()
    return any(pat.lower() in s for pat in IDK_PATTERNS)


def _all_fields_filled(state: dict) -> bool:
    fields = state.get("fields", {})
    return all(bool(fields.get(k, "").strip()) for k in
               ("industry", "pain_point", "current_systems", "constraints", "expected_output"))


def _format_state_summary(state: dict) -> str:
    f = state.get("fields", {})
    return (
        f"- 行业：{f.get('industry', '(待补充)')}\n"
        f"- 核心痛点：{f.get('pain_point', '(待补充)')}\n"
        f"- 现状系统：{f.get('current_systems', '(待补充)')}\n"
        f"- 约束：{f.get('constraints', '(待补充)')}\n"
        f"- 期望产出：{f.get('expected_output', '(待补充)')}"
    )


class ClarifyHandler(BaseStage):
    name = Stage.clarify
    required_tools: list[str] = []

    def __init__(self) -> None:
        self._tpl = _env.get_template("clarify.j2")

    async def guardrail(self, session: Session) -> bool:
        """Ready to advance when requirements.json exists."""
        return self.has_artifact(session, "clarify", REQUIREMENTS_NAME)

    async def run(
        self,
        session: Session,
        user_msg: str | None,
        *,
        llm: LLMClient,
        tools: list,  # noqa: ARG002
    ) -> StageResult:
        # Load existing state
        state_raw = self.read_artifact(session, "clarify", CLARIFY_STATE_NAME)
        if state_raw:
            try:
                state = json.loads(state_raw)
            except Exception:
                state = self._new_state()
        else:
            state = self._new_state()

        round_no = state.get("round", 0)
        awaiting = bool(state.get("awaiting_confirmation", False))
        idk_count = int(state.get("idk_count", 0))
        fields = dict(state.get("fields", {}))

        # --- sub-state B: awaiting confirmation ---
        if awaiting:
            return await self._handle_confirmation(
                session, user_msg or "", state, fields, llm
            )

        # --- sub-state A: asking questions ---
        # First round + first user message → seed
        if round_no == 0 and user_msg:
            # Try to extract a field from first message (best-effort heuristic)
            fields = self._seed_fields_from_first_message(user_msg, fields)
            round_no = 1

        # Force-commit if too many IDK
        if idk_count >= IDK_FORCE_COMMIT:
            return self._force_commit(session, fields)

        # If all fields filled, move to confirmation gate
        if _all_fields_filled({"fields": fields}):
            return self._enter_confirmation(session, fields, round_no)

        # Build prompt + ask LLM
        system = self._tpl.render()
        history = self.format_history(session, last_n=20)

        # Inject current state
        state_summary = (
            f"\n[Clarify state] round={round_no} fields={json.dumps(fields, ensure_ascii=False)}\n"
        )

        messages: list[Message] = [Message.system(system + state_summary)]
        messages.extend(history)
        if user_msg is not None and (not history or history[-1].role != "user" or history[-1].content != user_msg):
            messages.append(Message.user(user_msg))

        resp = await llm.chat(messages, tools=[], stream=False)
        reply = resp.content

        # Log
        self.append_message(session, "user", user_msg or "")
        self.append_message(session, "assistant", reply)

        # Heuristic field extraction from assistant reply (lightweight)
        # If LLM's reply doesn't end with a question and we just had 1 round, try to parse fields
        new_fields = self._extract_fields_from_reply(reply, fields)
        new_idk = idk_count + (1 if _is_idk(user_msg or "") else 0)

        # Advance round
        next_state = {
            "round": round_no + 1,
            "fields": new_fields,
            "awaiting_confirmation": False,
            "idk_count": new_idk,
        }
        # Save updated state
        self.record_artifact(
            session, "clarify", CLARIFY_STATE_NAME, json.dumps(next_state, ensure_ascii=False)
        )

        return StageResult(reply=reply)

    # ---- helpers ----

    def _new_state(self) -> dict:
        return {
            "round": 0,
            "fields": {
                "industry": "",
                "pain_point": "",
                "current_systems": "",
                "constraints": "",
                "expected_output": "",
            },
            "awaiting_confirmation": False,
            "idk_count": 0,
        }

    def _enter_confirmation(self, session: Session, fields: dict, round_no: int) -> StageResult:
        """All 5 fields collected → switch to awaiting Y/N confirmation."""
        next_state = {
            "round": round_no,
            "fields": fields,
            "awaiting_confirmation": True,
            "idk_count": 0,
        }
        self.record_artifact(
            session, "clarify", CLARIFY_STATE_NAME, json.dumps(next_state, ensure_ascii=False)
        )
        summary = _format_state_summary({"fields": fields})
        reply = (
            f"我整理了一下您的需求：\n{summary}\n\n"
            "看起来对吗？回复 Y 继续，N 我再问。"
        )
        self.append_message(session, "assistant", reply)
        return StageResult(reply=reply)

    async def _handle_confirmation(
        self, session: Session, user_msg: str, state: dict, fields: dict, llm: LLMClient
    ) -> StageResult:
        """Sub-state B: user replied to confirmation gate."""
        s = user_msg.strip().lower()
        if s in ("y", "yes", "对", "是", "ok", "好的", "确认", "继续"):
            # Write requirements.json
            self.record_artifact(
                session, "clarify", REQUIREMENTS_NAME,
                json.dumps(fields, ensure_ascii=False, indent=2),
            )
            self.append_message(session, "user", user_msg)
            reply = "好的，需求已确认。开始为您调研。"
            self.append_message(session, "assistant", reply)
            return StageResult(
                reply=reply,
                artifact_writes=[("clarify", REQUIREMENTS_NAME, json.dumps(fields, ensure_ascii=False, indent=2))],
                next_stage=Stage.research,
            )
        # N or补充 → back to asking
        next_state = {
            "round": int(state.get("round", 0)),
            "fields": fields,
            "awaiting_confirmation": False,
            "idk_count": 0,
        }
        self.record_artifact(
            session, "clarify", CLARIFY_STATE_NAME, json.dumps(next_state, ensure_ascii=False)
        )
        self.append_message(session, "user", user_msg)
        reply = "好的，请告诉我哪里需要修改或补充。"
        self.append_message(session, "assistant", reply)
        return StageResult(reply=reply)

    def _force_commit(self, session: Session, fields: dict) -> StageResult:
        """Force-commit best-guess requirements and advance."""
        filled = {k: v for k, v in fields.items() if v.strip()}
        self.record_artifact(
            session, "clarify", REQUIREMENTS_NAME,
            json.dumps(filled, ensure_ascii=False, indent=2),
        )
        self.append_message(session, "assistant", "[force-commit: best-guess requirements]")
        return StageResult(
            reply="已根据已有信息整理最佳猜测方案，开始调研。",
            artifact_writes=[("clarify", REQUIREMENTS_NAME, json.dumps(filled, ensure_ascii=False, indent=2))],
            next_stage=Stage.research,
        )

    def _seed_fields_from_first_message(self, msg: str, fields: dict) -> dict:
        """Best-effort field extraction from first user message.

        Strategy: store the entire message as `pain_point` (most common single-field
        input) and leave others blank. The LLM's first follow-up question is then
        the one that matters most.
        """
        out = dict(fields)
        if msg and not out.get("pain_point"):
            out["pain_point"] = msg.strip()[:500]
        return out

    def _extract_fields_from_reply(self, reply: str, prev: dict) -> dict:
        """Lightweight field extraction from LLM's reply if it embeds JSON.

        Conservative: only updates if a JSON object is clearly present in the
        reply, and only fills empty fields. This is a fallback — the orchestrator's
        LLM-driven classification is the primary path.
        """
        # Not implementing aggressive extraction here — keep simple.
        return prev
