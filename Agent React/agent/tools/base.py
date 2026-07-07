"""Tool registry base — ToolSpec dataclass and module-level REGISTRY.

Pattern: each tool module defines a ToolSpec at the bottom, then
`agent.tools.__init__` registers them on import. Stage handlers and the LLM
client consume `all_specs()` to build the `Tool` list sent to the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

# Async handler signature: takes kwargs, returns a string result.
# Result is always a string (JSON for structured tools, plain text or wrapped
# text for parse_doc).
ToolHandler = Callable[..., Awaitable[str]]


@dataclass
class ToolSpec:
    """Declarative description of a tool for the LLM and the registry."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: ToolHandler


# Module-level registry — populated by `register()` in `agent/tools/__init__.py`.
REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    """Add or replace a tool spec in the registry (idempotent on name)."""
    if not spec.name:
        raise ValueError("ToolSpec.name must be non-empty")
    REGISTRY[spec.name] = spec


def get(name: str) -> ToolSpec | None:
    """Look up a tool spec by name; returns None if not registered."""
    return REGISTRY.get(name)


def all_specs() -> list[ToolSpec]:
    """Return all registered tool specs (insertion order)."""
    return list(REGISTRY.values())


def reset() -> None:
    """Clear the registry. Test helper only — do not call from production."""
    REGISTRY.clear()


__all__ = ["ToolSpec", "ToolHandler", "REGISTRY", "register", "get", "all_specs", "reset"]