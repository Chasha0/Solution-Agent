"""kb_search tool — semantic search over the internal KB.

Returns a JSON string `[{chunk, score, source}]`. If the KB is empty (not yet
ingested) or the index failed to initialize, returns
`[{"error": "KB not initialized; run scripts/ingest_kb.py"}]` so the caller
sees a clear degradation message instead of crashing.
"""
from __future__ import annotations

import json
import logging

from .base import ToolSpec
from . import kb_index

logger = logging.getLogger(__name__)


async def kb_search(query: str, top_k: int = 5) -> str:
    """Semantic KB search. Always returns a JSON string.

    Success: `[{"chunk", "score", "source"}, ...]`
    Empty KB: `[{"error": "KB not initialized; ..."}]`
    Failure: `[{"error": "<type>: <msg>"}]`
    """
    if not query or not query.strip():
        return json.dumps([{"error": "empty query"}], ensure_ascii=False)
    top_k = max(1, min(int(top_k), 50))

    try:
        index = kb_index.KBIndex.get()
        if index.init_error:
            return json.dumps(
                [{"error": f"KB init failed: {index.init_error}"}],
                ensure_ascii=False,
            )
        if index.count() == 0:
            return json.dumps(
                [{"error": "KB not initialized; run scripts/ingest_kb.py"}],
                ensure_ascii=False,
            )
        results = index.search(query, top_k=top_k)
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        logger.exception("[kb_search] search failed")
        return json.dumps(
            [{"error": f"{type(e).__name__}: {e}"}],
            ensure_ascii=False,
        )


_SPEC = ToolSpec(
    name="kb_search",
    description=(
        "Search the internal product / solution knowledge base for relevant "
        "chunks. Prefer this over web_search when the question is about our "
        "own products, templates, prior solutions, or FAQ. "
        "Returns a JSON list of {chunk, score, source} sorted by similarity."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query. Use natural language; both Chinese and English are indexed.",
            },
            "top_k": {
                "type": "integer",
                "description": "How many chunks to return. Default 5.",
                "default": 5,
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": ["query"],
    },
    handler=kb_search,
)


__all__ = ["kb_search", "_SPEC"]