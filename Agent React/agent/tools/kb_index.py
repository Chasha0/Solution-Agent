"""KB index — thin wrapper over a persistent Chroma collection.

Used by:
- `agent.tools.kb_search` (read path) — singleton access
- `scripts/ingest_kb.py` (P8, write path) — instance access for batch ingest

Embeddings: same provider as the LLM (OpenAI-compatible `embeddings.create`).
On embed failure (rate limit, missing key, network) the helper falls back to
**deterministic hash-based pseudo-vectors**. This is documented as DEMO ONLY —
real production must hit the embed API for retrieval to make any sense.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Fallback pseudo-embed dimension. Matches text-embedding-3-small default so
# the Chroma collection stays consistent whether we use real or fake vectors.
_PSEUDO_DIM = 1536

# Singleton holder (process-local). Tests may `reset_singleton()`.
_INSTANCE: "KBIndex | None" = None


class KBIndex:
    """Persistent Chroma-backed KB index.

    Lifecycle:
        idx = KBIndex.get()           # singleton, auto-init from KB_DIR env
        idx = KBIndex.get()           # same instance on subsequent calls
        idx.add(doc_id, text, meta)   # write path (used by ingest script)
        idx.search(query, top_k=5)    # read path (used by kb_search tool)
        idx.count()                   # how many docs are in the collection
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._collection: Any = None
        self._persist_dir: Path | None = None
        self._embed_dim: int = _PSEUDO_DIM
        self._init_error: str | None = None  # surface init failures to callers

    # ----- singleton -----

    @classmethod
    def get(cls) -> "KBIndex":
        """Return the process-wide singleton, initializing on first access.

        Init dir comes from env `KB_DIR` (default `./kb`). If init fails (e.g.
        Chroma can't open the directory), the singleton is still returned but
        `count()` will report 0 and `search()` will return an error JSON shape
        so the kb_search tool degrades gracefully (spec §6).
        """
        global _INSTANCE
        if _INSTANCE is None:
            instance = cls()
            instance._auto_init()
            _INSTANCE = instance
        return _INSTANCE

    @classmethod
    def reset_singleton(cls) -> None:
        """Drop the cached singleton. Test helper — does not delete data."""
        global _INSTANCE
        _INSTANCE = None

    def _auto_init(self) -> None:
        """Resolve KB_DIR from env and init(). Skips silently on failure."""
        try:
            from dotenv import load_dotenv

            load_dotenv()
            kb_dir_str = os.getenv("KB_DIR", "./kb").strip() or "./kb"
            kb_dir = Path(kb_dir_str).resolve()
            kb_dir.mkdir(parents=True, exist_ok=True)
            self.init(kb_dir)
        except Exception as e:
            self._init_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[KBIndex] auto-init failed: {self._init_error}")

    # ----- public API -----

    def init(self, persist_dir: Path) -> None:
        """Open a persistent Chroma client and get/create the collection.

        Idempotent: calling init() twice with the same dir is a no-op.
        """
        persist_dir = Path(persist_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)

        if self._collection is not None and self._persist_dir == persist_dir:
            return

        import chromadb

        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(
            name="solution_kb",
            metadata={"hnsw:space": "cosine"},
        )
        self._persist_dir = persist_dir
        self._init_error = None

    def reset(self) -> None:
        """Drop the KB collection entirely. Used by `--reset` in ingest script.

        After reset() the index is unusable until init() is called again.
        """
        if self._client is None:
            return
        try:
            self._client.delete_collection(name="solution_kb")
        except Exception as e:
            logger.warning(f"[KBIndex] delete_collection failed: {e}")
        self._collection = None
        self._init_error = None

    async def add(self, doc_id: str, text: str, metadata: dict[str, Any]) -> None:
        """Add (or upsert) a single document chunk into the KB. Async."""
        self._require_init()
        emb = await self.embed([text])
        # Chroma upserts on duplicate ids by default
        self._collection.upsert(
            ids=[doc_id],
            documents=[text],
            embeddings=emb,
            metadatas=[metadata or {}],
        )

    async def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Semantic search; returns `[{chunk, score, source}]`. Async.

        Empty list if the collection is empty. Score is cosine similarity
        (distance → similarity via `1 - d`).
        """
        self._require_init()
        total = self._collection.count()
        if total == 0:
            return []
        n = min(max(top_k, 1), total)
        query_emb = await self.embed([query])
        res = self._collection.query(
            query_embeddings=query_emb,
            n_results=n,
        )
        documents = (res.get("documents") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]
        metadatas = (res.get("metadatas") or [[]])[0]
        out: list[dict[str, Any]] = []
        for i, doc in enumerate(documents):
            dist = distances[i] if i < len(distances) else 0.0
            meta = metadatas[i] if i < len(metadatas) else {}
            out.append(
                {
                    "chunk": doc,
                    "score": float(1.0 - dist),
                    "source": meta.get("source") or meta.get("source_path") or "unknown",
                }
            )
        return out

    def count(self) -> int:
        """Total docs in the collection. 0 if not initialized."""
        if self._collection is None:
            return 0
        try:
            return int(self._collection.count())
        except Exception:
            return 0

    @property
    def persist_dir(self) -> Path | None:
        return self._persist_dir

    @property
    def init_error(self) -> str | None:
        """Last init error message, or None. Useful for kb_search diagnostics."""
        return self._init_error

    # ----- internals -----

    def _require_init(self) -> None:
        if self._collection is None:
            raise RuntimeError(
                "KBIndex not initialized; call init(persist_dir) first. "
                f"Last error: {self._init_error}"
            )

    def _embed_blocking(self, texts: list[str]) -> list[list[float]]:
        """Synchronous embed for ingest scripts. Awaits `embed()` internally."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return asyncio.run(self.embed(texts))
        # Already in an async context — fall back to pseudo-vectors to avoid
        # nested loop issues; ingest scripts should call `await embed()` directly.
        logger.warning(
            "[KBIndex] _embed_blocking called inside a running loop; "
            "using pseudo-vectors. Prefer `await KBIndex.embed(texts)`."
        )
        return [self._pseudo_embed(t) for t in texts]

    def _embed_config(self) -> tuple[str, str, str]:
        """Resolve embed API key/base_url/model from env.

        EMBED_* takes precedence; falls back to OPENAI_* for backward compat.
        """
        api_key = (
            os.getenv("EMBED_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        base_url = (
            os.getenv("EMBED_BASE_URL", "").strip()
            or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
        )
        model = os.getenv("EMBED_MODEL", "text-embedding-3-small").strip()
        return api_key, base_url, model

    def _get_embed_client(self):
        """Return a cached AsyncOpenAI client for the embed provider.

        Reusing one client (instead of per-call new) prevents the
        "Event loop is closed" cleanup errors that surface as warnings
        after script exit.
        """
        if getattr(self, "_embed_client", None) is None:
            from openai import AsyncOpenAI

            api_key, base_url, _ = self._embed_config()
            self._embed_client = AsyncOpenAI(
                api_key=api_key, base_url=base_url, timeout=15.0
            )
            self._embed_config_cached_at = (api_key, base_url)
        return self._embed_client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed via OpenAI-compatible API; fall back to pseudo-vectors.

        The fallback is **demo only** — pseudo-vectors are deterministic hashes
        and do not encode semantics. Spec §4.4 requires real embeddings for
        meaningful retrieval; this path exists so the demo doesn't crash when
        the provider is unreachable.
        """
        api_key, base_url, embed_model = self._embed_config()

        if api_key and api_key != "sk-xxx":
            try:
                client = self._get_embed_client()
                resp = await client.embeddings.create(model=embed_model, input=texts)
                vectors = [list(d.embedding) for d in resp.data]
                if vectors:
                    self._embed_dim = len(vectors[0])
                # Reset fallback flag on success
                self._fallback_warned = False
                return vectors
            except Exception as e:
                self._warn_fallback_once(e)
        else:
            self._warn_fallback_once("OPENAI/EMBED API key not set")

        return [self._pseudo_embed(t) for t in texts]

    def _warn_fallback_once(self, cause: Any) -> None:
        """Emit fallback warning only on the first occurrence per process.

        Subsequent calls log at debug level to avoid spamming the ingest log.
        """
        if not getattr(self, "_fallback_warned", False):
            logger.warning(
                f"[KBIndex] embed API failed ({type(cause).__name__}: {cause}); "
                "falling back to deterministic pseudo-vectors (DEMO ONLY). "
                "Further failures will log at debug level."
            )
            self._fallback_warned = True
        else:
            logger.debug(f"[KBIndex] embed fallback (cause: {cause})")

    def _pseudo_embed(self, text: str) -> list[float]:
        """Deterministic random unit vector derived from SHA-256(text).

        Same text → same vector. Not semantic. For demo / fallback only.
        """
        h = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(h[:8], "big")
        rng = random.Random(seed)
        vec = [rng.gauss(0.0, 1.0) for _ in range(self._embed_dim)]
        norm = sum(x * x for x in vec) ** 0.5
        if norm > 0:
            vec = [x / norm for x in vec]
        else:
            vec = [0.0] * self._embed_dim
        return vec


# ----- module-level shortcuts (used by kb_search tool) -----


async def search(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Async singleton shortcut: `await KBIndex.get().search(query, top_k)`."""
    return await KBIndex.get().search(query, top_k=top_k)


async def add(doc_id: str, text: str, metadata: dict[str, Any]) -> None:
    """Async singleton shortcut: `await KBIndex.get().add(doc_id, text, metadata)`."""
    await KBIndex.get().add(doc_id, text, metadata)


def count() -> int:
    """Sync shortcut: `KBIndex.get().count()`. No embed involved, safe."""
    return KBIndex.get().count()


__all__ = ["KBIndex", "search", "add", "count"]