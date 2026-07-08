"""Schema constants and Pydantic models for session metadata.

See: docs/superpowers/specs/2026-07-07-solution-research-agent-design.md §4.3
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# Bump only when meta.json's on-disk shape changes in a breaking way.
# The validator in meta_from_dict() will raise SchemaMismatchError for any
# existing on-disk value that disagrees with this constant.
SCHEMA_VERSION = 1


class Stage(str, Enum):
    """5+1 stages per spec §3. `iterate` is a sub-state of `design`, not a stage."""

    clarify = "clarify"
    research = "research"
    summarize = "summarize"
    design = "design"
    finalize = "finalize"
    completed = "completed"


class SessionStatus(str, Enum):
    """Lifecycle states for a session."""

    active = "active"
    paused = "paused"
    completed = "completed"
    failed = "failed"


class SchemaMismatchError(Exception):
    """Raised when meta.json's schema_version does not match SCHEMA_VERSION.

    Callers (typically Session.load) propagate this so the orchestrator can
    decide to backup-and-recreate or migrate.
    """

    def __init__(self, old: int, new: int) -> None:
        super().__init__(
            f"meta.json schema_version mismatch: on-disk={old} expected={new}"
        )
        self.old = old
        self.new = new


class MetaDict(BaseModel):
    """Schema for ``sessions/<id>/meta.json``.

    Mirrors spec §4.3. ``version_counts`` is keyed by ``f"{stage}/{name}"``
    (e.g. ``"design/design"`` -> ``2``) because each artifact can be revised
    independently within its stage.
    """

    # We mutate fields in-place (stage_history.append, version_counts[k] = v),
    # so keep the default mutable behaviour (do NOT set frozen=True).
    model_config = ConfigDict(extra="ignore", validate_assignment=False)

    schema_version: int = SCHEMA_VERSION
    session_id: str
    created_at: str  # ISO 8601 UTC, e.g. "2026-07-07T08:30:00.123456+00:00"
    customer: str = ""
    current_stage: Stage = Stage.clarify
    status: SessionStatus = SessionStatus.active
    stage_history: list[Stage] = Field(default_factory=list)
    version_counts: dict[str, int] = Field(default_factory=dict)
    # Marker for an in-flight handler invocation. Set by the orchestrator
    # right before dispatching to a stage handler; cleared when the handler
    # returns (success or failure). Lets the UI distinguish "stage that
    # completed last" from "stage currently executing".
    # Shape: {"stage": "research", "started_at": "2026-07-08T12:34:56+00:00"}
    # or None when no handler is running.
    current_run: dict | None = None


def meta_to_dict(meta: MetaDict) -> dict:
    """Serialize MetaDict to a JSON-safe dict.

    Enums become their string values (``Stage.clarify`` -> ``"clarify"``) so the
    resulting dict can be passed straight to ``json.dumps``.
    """
    return meta.model_dump(mode="json")


def meta_from_dict(d: dict) -> MetaDict:
    """Validate a dict against MetaDict.

    Raises:
        SchemaMismatchError: when ``schema_version`` is present and not equal
            to :data:`SCHEMA_VERSION`. Missing key is treated as the default
            (match), per spec §4.3 "缺字段给默认值".
        pydantic.ValidationError: on any other shape mismatch.
    """
    raw_version = d.get("schema_version", SCHEMA_VERSION)
    if raw_version != SCHEMA_VERSION:
        raise SchemaMismatchError(old=int(raw_version), new=SCHEMA_VERSION)
    return MetaDict.model_validate(d)