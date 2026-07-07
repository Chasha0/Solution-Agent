"""Storage layer: session + artifact persistence with file locking.

Exposes the public surface that downstream phases (P3 tools, P4 stages,
P6 orchestrator, P7 UI) will import. See spec §4.3 for storage layout and
§6 for atomic-write requirements.
"""
from .artifacts import ArtifactNotFound, Artifacts
from .lock import FileLock, LockTimeout
from .paths import get_kb_dir, get_project_root, get_session_dir, get_sessions_dir
from .schema import (
    SCHEMA_VERSION,
    MetaDict,
    SchemaMismatchError,
    SessionStatus,
    Stage,
    meta_from_dict,
    meta_to_dict,
)
from .session import Session, SessionNotFound

__all__ = [
    # constants
    "SCHEMA_VERSION",
    # exceptions
    "ArtifactNotFound",
    "LockTimeout",
    "SchemaMismatchError",
    "SessionNotFound",
    # classes
    "Artifacts",
    "FileLock",
    "MetaDict",
    "Session",
    # enums
    "SessionStatus",
    "Stage",
    # helpers
    "get_kb_dir",
    "get_project_root",
    "get_session_dir",
    "get_sessions_dir",
    "meta_from_dict",
    "meta_to_dict",
]