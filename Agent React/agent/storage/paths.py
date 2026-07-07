"""Filesystem path resolution for sessions/ and kb/.

Lazy mkdir on access per spec §4.3 and the project rules. Env-var overrides
(``SESSIONS_DIR`` / ``KB_DIR``) win over the project-root default.
"""
from __future__ import annotations

import os
from pathlib import Path


def get_project_root() -> Path:
    """Find project root by walking up from CWD until we see an ``agent/`` package.

    Fallback: CWD itself. Used to default ``sessions/`` and ``kb/`` next to
    the agent package even when the process is launched from elsewhere.
    """
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "agent").is_dir() and (candidate / "agent" / "__init__.py").is_file():
            return candidate
    return cwd


def get_sessions_dir() -> Path:
    """Resolve the sessions/ directory. Override via ``SESSIONS_DIR``.

    Creates the directory on first access (lazy).
    """
    override = os.environ.get("SESSIONS_DIR", "").strip()
    base = Path(override) if override else (get_project_root() / "sessions")
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_kb_dir() -> Path:
    """Resolve the kb/ directory. Override via ``KB_DIR``.

    Creates the directory on first access (lazy).
    """
    override = os.environ.get("KB_DIR", "").strip()
    base = Path(override) if override else (get_project_root() / "kb")
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_session_dir(session_id: str) -> Path:
    """Resolve ``sessions/<id>/`` and create the directory if missing.

    Callers that need to detect a missing session (e.g. ``Session.load``)
    must check existence **before** calling this helper to avoid the
    implicit mkdir side effect.
    """
    d = get_sessions_dir() / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d