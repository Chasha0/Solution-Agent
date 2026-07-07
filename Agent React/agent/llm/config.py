"""LLM config: load from env (.env supported via python-dotenv)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    embed_model: str = "text-embedding-3-small"
    temperature: float | None = 0.2
    max_tokens: int | None = None
    timeout_s: float = 60.0
    max_retries: int = 3
    retry_backoff: tuple[float, ...] = (1.0, 3.0, 9.0)
    process_token_budget: int = 1_000_000  # per-process, oracle rec
    session_token_budget: int = 100_000     # per-session, spec §6.1
    project_root: Path = field(default_factory=Path.cwd)

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "LLMConfig":
        from dotenv import load_dotenv

        root = project_root or Path.cwd()
        # Load .env (not .env.example) from project root
        env_path = root / ".env"
        if env_path.exists():
            load_dotenv(env_path)

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key or api_key == "sk-xxx":
            raise ValueError(
                "OPENAI_API_KEY is not configured. "
                "Copy .env.example to .env and set a real key, "
                "or set the env var OPENAI_API_KEY."
            )

        return cls(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
            embed_model=os.getenv("EMBED_MODEL", "text-embedding-3-small").strip(),
            project_root=root,
        )
