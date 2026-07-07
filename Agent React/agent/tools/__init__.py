"""Tools layer for the Solution Research Agent.

Exposes the 6 spec-mandated tools plus the kb_index singleton and the
ToolSpec / REGISTRY plumbing. Stage handlers import from here; tests
import handler functions directly.

Importing this package registers all 6 tools via `register()`. To turn a
ToolSpec into the `Tool` shape the OpenAI API expects, use
`agent.llm.types.Tool.from_spec(...)` (added in P1 / extended here if needed).
"""
from __future__ import annotations

# Base registry pieces.
from .base import REGISTRY, ToolHandler, ToolSpec, all_specs, get, register, reset

# Internal helpers (re-exported for tests).
from . import pii

# The 6 tools — each module defines an `async def <name>` and a `_SPEC`.
from .web_search import _SPEC as _spec_web_search
from .web_search import web_search
from .kb_search import _SPEC as _spec_kb_search
from .kb_search import kb_search
from .parse_doc import _SPEC as _spec_parse_doc
from .parse_doc import parse_doc
from .save_section import _SPEC as _spec_save_section
from .save_section import save_section
from .revise_section import _SPEC as _spec_revise_section
from .revise_section import revise_section
from .export_report import _SPEC as _spec_export_report
from .export_report import export_report

# KB index — separate from kb_search but used by it and by the P8 ingest script.
from . import kb_index

# Register all 6. Idempotent: re-running this module (e.g. in tests) just
# overwrites the same entries.
register(_spec_web_search)
register(_spec_kb_search)
register(_spec_parse_doc)
register(_spec_save_section)
register(_spec_revise_section)
register(_spec_export_report)


__all__ = [
    # Registry
    "REGISTRY",
    "ToolSpec",
    "ToolHandler",
    "register",
    "get",
    "all_specs",
    "reset",
    # 6 tool handlers
    "web_search",
    "kb_search",
    "parse_doc",
    "save_section",
    "revise_section",
    "export_report",
    # Helpers
    "pii",
    "kb_index",
]