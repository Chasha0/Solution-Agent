"""revise_section tool — write a new version of an artifact and return a diff.

Flow:
    1. Read latest version of artifacts/<stage>/<name> via `Artifacts.read`.
    2. Compute unified diff (difflib.unified_diff).
    3. Write new version via `Artifacts.write`.
    4. Return JSON: {"ok": true, "version": N, "diff": "..."}.

If the artifact does not exist: return `{"error": "not found"}` so the
caller can decide whether to escalate (e.g. fall back to full rewrite).

Lazy-imports `Artifacts` to stay import-safe pre-P2.
"""
from __future__ import annotations

import difflib
import json
import logging
from typing import Any

from .base import ToolSpec

logger = logging.getLogger(__name__)


def _get_storage_classes() -> tuple[Any, Any]:
    """Return `(Artifacts, ArtifactNotFound)` or `(None, None)` if P2 missing."""
    try:
        from agent.storage.artifacts import ArtifactNotFound, Artifacts  # type: ignore
        return Artifacts, ArtifactNotFound
    except ImportError as e:
        logger.warning(f"[revise_section] agent.storage.artifacts not available: {e}")
        return None, None


def _compute_diff(old: str, new: str, name: str) -> str:
    """Return a unified-diff string. Empty on no-op."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff_iter = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"{name} (old)",
        tofile=f"{name} (new)",
    )
    return "".join(diff_iter)


async def revise_section(
    stage: str, name: str, new_content: str, session_id: str
) -> str:
    """Replace artifacts/<stage>/<name> with *new_content* and return diff.

    Returns JSON: `{"ok": true, "version": N, "diff": "..."}`
    Not found:    `{"error": "not found"}`
    Bad input:    `{"error": "..."}`
    """
    if not session_id or not stage or not name:
        return json.dumps(
            {"error": "session_id, stage, name are required"},
            ensure_ascii=False,
        )

    Artifacts, ArtifactNotFound = _get_storage_classes()
    if Artifacts is None:
        return json.dumps(
            {"error": "agent.storage not available (P2 pending)"},
            ensure_ascii=False,
        )

    # Build a tuple of "not found" exception types. ArtifactNotFound is the
    # P2-canonical one; KeyError/FileNotFoundError cover other impls.
    not_found_types: tuple[type[BaseException], ...] = tuple(
        t for t in (ArtifactNotFound, KeyError, FileNotFoundError) if t is not None
    )

    try:
        artifacts = Artifacts(session_id)
        try:
            old_content = artifacts.read(stage, name)
        except not_found_types:
            return json.dumps({"error": "not found"}, ensure_ascii=False)

        diff = _compute_diff(old_content, new_content, name)
        result = artifacts.write(stage, name, new_content)
    except Exception as e:
        logger.exception("[revise_section] failed")
        return json.dumps(
            {"error": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )

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
            "diff": diff,
        },
        ensure_ascii=False,
    )


_SPEC = ToolSpec(
    name="revise_section",
    description=(
        "Replace an existing artifact with a new version and return a unified "
        "diff of the change. Use for partial edits during the 'iterate' "
        "sub-state of the design stage (e.g. 'change section 3 to SaaS'). "
        "Returns the new version number and a text diff for the UI to render."
    ),
    parameters={
        "type": "object",
        "properties": {
            "stage": {
                "type": "string",
                "description": "Stage id of the artifact to revise.",
            },
            "name": {
                "type": "string",
                "description": "Section file name, e.g. 'design_v1.md'.",
            },
            "new_content": {
                "type": "string",
                "description": "Replacement full content (UTF-8).",
            },
            "session_id": {
                "type": "string",
                "description": "Current session id.",
            },
        },
        "required": ["stage", "name", "new_content", "session_id"],
    },
    handler=revise_section,
)


__all__ = ["revise_section", "_SPEC"]