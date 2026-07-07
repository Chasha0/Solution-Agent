"""Stage handler base: Protocol, StageResult, and shared helpers.

Per spec §4.1 + §3.5 (orchestrator auto-injects current artifact into context).
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agent.llm import LLMClient, Message
from agent.storage import Artifacts, Session, Stage


@dataclass
class StageResult:
    """Outcome of a single stage run."""
    reply: str                                # Assistant text to show user
    artifact_writes: list[tuple[str, str, str]] = field(default_factory=list)
    #                                       (stage, name, content) tuples
    next_stage: Stage | None = None            # None = stay in current stage
    extra: dict[str, Any] = field(default_factory=dict)
    # Per-handler state (e.g. clarify sub-state transition, iterate intent)


@runtime_checkable
class StageHandler(Protocol):
    """Stage handler interface. See spec §4.1."""
    name: Stage
    system_prompt: str
    required_tools: list[str]

    async def guardrail(self, session: Session) -> bool:
        """Return True if the session is ready to advance past this stage."""
        ...

    async def run(
        self,
        session: Session,
        user_msg: str | None,
        *,
        llm: LLMClient,
        tools: list[Any],  # list[ToolSpec] from agent.tools.all_specs()
    ) -> StageResult:
        ...


class BaseStage:
    """Common helpers shared by all stage handlers.

    Subclasses MUST provide:
    - name: Stage
    - system_prompt: str   (class attribute OR @property)
    - required_tools: list[str]
    """

    name: Stage
    # system_prompt: subclasses override (class attr or @property)
    required_tools: list[str] = []

    # ---- message history (messages.jsonl) ----

    def format_history(self, session: Session, last_n: int = 20) -> list[Message]:
        """Read last N messages from the session's messages.jsonl."""
        path = session.messages_path
        if not path.exists():
            return []
        out: list[Message] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return []
        for line in lines[-last_n:]:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            out.append(
                Message(
                    role=d.get("role", "user"),
                    content=d.get("content", ""),
                    name=d.get("name"),
                    tool_call_id=d.get("tool_call_id"),
                    tool_calls=d.get("tool_calls"),
                )
            )
        return out

    def append_message(
        self,
        session: Session,
        role: str,
        content: str,
        **extra: Any,
    ) -> None:
        """Append one line to messages.jsonl (auto-creates file)."""
        path = session.messages_path
        path.parent.mkdir(parents=True, exist_ok=True)
        d: dict[str, Any] = {"role": role, "content": content, **extra}
        with path.open("a", encoding="utf-8", errors="ignore") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    # ---- artifacts ----

    def record_artifact(
        self,
        session: Session,
        stage: str,
        name: str,
        content: str,
    ) -> int:
        """Write an artifact, return the version number."""
        arts = Artifacts(session.id)
        return arts.write(stage, name, content)

    def read_artifact(
        self,
        session: Session,
        stage: str,
        name: str,
        version: int | None = None,
    ) -> str | None:
        """Read latest or specific version. None if not found."""
        arts = Artifacts(session.id)
        try:
            if version is None:
                return arts.read_latest(stage, name)
            return arts.read(stage, name, version=version)
        except Exception:
            return None

    def has_artifact(self, session: Session, stage: str, name: str) -> bool:
        arts = Artifacts(session.id)
        try:
            versions = arts.list_versions(stage, name)
            return bool(versions)
        except Exception:
            return False

    # ---- auto-inject current stage artifact (spec §4.2 oracle rec #4) ----

    def inject_artifact_context(
        self,
        session: Session,
        stage: str,
        name: str,
    ) -> str:
        """Return the latest artifact content for inclusion in the LLM context.

        Empty string if not found. The orchestrator calls this for every turn.
        """
        content = self.read_artifact(session, stage, name)
        if not content:
            return ""
        snippet = content if len(content) <= 4000 else content[:4000] + "\n... (truncated)"
        return (
            f"\n\n[Current artifact `{stage}/{name}`]\n```\n{snippet}\n```\n"
        )
