"""web_search tool.

Two-tier implementation:
1. Tavily (`TAVILY_API_KEY` env present) — preferred, structured results.
2. DuckDuckGo HTML scraping (`https://html.duckduckgo.com/html/`) — fallback,
   parsed with plain `re` (BeautifulSoup not in requirements).

Result is **always** a JSON string so the LLM can `json.loads` it regardless
of success or failure. Errors are wrapped as `[{"error": "..."}]` so the
caller never crashes. Timeout 8s per request.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

from .base import ToolSpec

logger = logging.getLogger(__name__)

_TIMEOUT_S = 8.0
# DDG fallback uses a tighter timeout because (a) DuckDuckGo HTML scraping
# is unreliable and (b) this machine often can't reach it at all. Failing fast
# lets the agent fall back to KB-only results instead of burning the whole
# stage budget on connection timeouts.
_DDG_TIMEOUT_S = 3.0

# DDG HTML result markup (subject to change; this is what works as of 2024).
_DDG_TITLE_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HTML_ENTS = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#x27;": "'",
    "&nbsp;": " ",
}


def _strip_html(s: str) -> str:
    """Drop tags and decode common entities. Best-effort — does not handle all."""
    s = _TAG_RE.sub("", s)
    for ent, ch in _HTML_ENTS.items():
        s = s.replace(ent, ch)
    return _WS_RE.sub(" ", s).strip()


def _unwrap_ddg_href(href: str) -> str:
    """DDG wraps real URLs in `/l/?uddg=...` — extract the inner URL when present."""
    if "uddg=" in href:
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            from urllib.parse import unquote

            return unquote(m.group(1))
    return href


async def _search_tavily(query: str, max_results: int) -> list[dict[str, Any]]:
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY set but empty")
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results") or []
    return [
        {
            "title": r.get("title", "") or "",
            "url": r.get("url", "") or "",
            "snippet": r.get("content", "") or "",
        }
        for r in results[:max_results]
    ]


async def _search_duckduckgo(query: str, max_results: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        timeout=_DDG_TIMEOUT_S,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    ) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
        )
        resp.raise_for_status()
        html = resp.text

    titles = _DDG_TITLE_RE.findall(html)
    snippets = _DDG_SNIPPET_RE.findall(html)
    out: list[dict[str, Any]] = []
    for i, (href, raw_title) in enumerate(titles[:max_results]):
        title = _strip_html(raw_title)
        if not title:
            continue
        snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
        out.append(
            {
                "title": title,
                "url": _unwrap_ddg_href(href),
                "snippet": snippet,
            }
        )
    return out


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web. Returns a JSON string.

    Success: `[{"title", "url", "snippet"}, ...]`
    Failure: `[{"error": "..."}]` — caller never crashes.
    """
    if not query or not query.strip():
        return json.dumps([{"error": "empty query"}], ensure_ascii=False)
    max_results = max(1, min(int(max_results), 20))

    try:
        if os.getenv("TAVILY_API_KEY", "").strip():
            try:
                results = await _search_tavily(query, max_results)
                if results:
                    return json.dumps(results, ensure_ascii=False)
                # Tavily returned nothing — fall through to DDG for a second try.
                logger.info("[web_search] Tavily returned 0 results; trying DDG fallback.")
            except Exception as e:
                logger.warning(f"[web_search] Tavily failed: {type(e).__name__}: {e}")
        results = await _search_duckduckgo(query, max_results)
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return json.dumps(
            [{"error": f"{type(e).__name__}: {e}"}],
            ensure_ascii=False,
        )


_SPEC = ToolSpec(
    name="web_search",
    description=(
        "Search the public web for current information. Use for market trends, "
        "vendor comparisons, recent news, or any topic not covered by the internal KB. "
        "Returns up to max_results items as a JSON list of {title, url, snippet}."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query. Use the customer's language; prefer specific phrases over keywords.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return. Default 5, max 20.",
                "default": 5,
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
    },
    handler=web_search,
)


__all__ = ["web_search", "_SPEC"]