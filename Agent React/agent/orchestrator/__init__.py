"""Orchestrator package — router + main loop."""
from __future__ import annotations

from .orchestrator import Orchestrator, PER_STAGE_TIMEOUT_S
from .router import (
    INTENT_REVISE_PATTERNS,
    INTENT_REWRITE_PATTERNS,
    LOW_CONFIDENCE,
    RouteDecision,
    _looks_like_iterate,
    _match_rule,
    route,
)

__all__ = [
    "Orchestrator",
    "PER_STAGE_TIMEOUT_S",
    "RouteDecision",
    "route",
    "_match_rule",
    "_looks_like_iterate",
    "LOW_CONFIDENCE",
    "INTENT_REVISE_PATTERNS",
    "INTENT_REWRITE_PATTERNS",
]
