"""save_section tool — write an artifact into a session's artifact store.

Expected `Artifacts` contract (from agent.storage.artifacts, owned by P2):

    class Artifacts:
        def __init__(self, session_id: str): ...
        def write(self, stage: str, name: str, content: str) -> dict:
            '''Write a new version. Returns {"version": int, "path": Path}.'''

We lazy-import `Artifacts` so this module imports cleanly before P2 lands.
If `Artifacts` is not importable, the tool returns a clear error JSON.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .base import ToolSpec

logger = logging.getLogger(__name__)


def _get_artifacts_cls() -> Any:
    """Lazy import of `Artifacts` — keeps this module import-safe pre-P2."""
    try:
        from agent.storage.artifacts import Artifacts  # type: ignore
        return Artifacts
    except ImportError as e:
        logger.warning(f"[save_section] agent.storage.artifacts not available: {e}")
        return None


async def save_section(stage: str, name: str, content: str, session_id: str) -> str:
    """Persist *content* under artifacts/<stage>/<name>.

    Returns JSON: `{"ok": true, "version": N, "path": "..."}`
    On missing storage: `{"error": "agent.storage not available; P2 pending"}`
    On other failures: `{"ok": false, "error": "..."}`
    """
    if not session_id or not stage or not name:
        return json.dumps(
            {"ok": False, "error": "session_id, stage, name are required"},
            ensure_ascii=False,
        )

    Artifacts = _get_artifacts_cls()
    if Artifacts is None:
        return json.dumps(
            {"ok": False, "error": "agent.storage not available (P2 pending)"},
            ensure_ascii=False,
        )

    try:
        artifacts = Artifacts(session_id)
        result = artifacts.write(stage, name, content)
    except Exception as e:
        logger.exception("[save_section] write failed")
        return json.dumps(
            {"ok": False, "error": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )

    # Tolerate either dict-shaped or int-shaped return from Artifacts.write.
    if isinstance(result, dict):
        version = result.get("version")
        path = result.get("path")
    elif isinstance(result, int):
        version = result
        path = None
    else:
        version = None
        path = None

    return json.dumps(
        {
            "ok": True,
            "version": version,
            "path": str(path) if path is not None else None,
        },
        ensure_ascii=False,
    )


_SPEC = ToolSpec(
    name="save_section",
    description=(
        "Persist a section of the current stage output into the session's "
        "artifact store. Use this whenever a stage produces durable content "
        "(requirements, research notes, summary, design sections). "
        "Returns the new version number and on-disk path."
    ),
    parameters={
        "type": "object",
        "properties": {
            "stage": {
                "type": "string",
                "description": "Stage id: requirements | research | summarize | design | final.",
            },
            "name": {
                "type": "string",
                "description": "Section file name, e.g. 'requirements.json', 'research.md', 'design_v1.md'.",
            },
            "content": {
                "type": "string",
                "description": "Full file content (UTF-8).",
            },
            "session_id": {
                "type": "string",
                "description": "Current session id.",
            },
        },
        "required": ["stage", "name", "content", "session_id"],
    },
    handler=save_section,
)


__all__ = ["save_section", "_SPEC"]