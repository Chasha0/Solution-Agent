"""LLM client: OpenAI-compatible async wrapper with budget guard and 429 backoff."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, overload

from openai import APIError, AsyncOpenAI, RateLimitError

from .config import LLMConfig
from .types import LLMResponse, Message, Tool, UsageStats

logger = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """Per-process token budget exceeded."""


class LLMClient:
    """Async wrapper around OpenAI-compatible chat completions.

    Features:
    - Tool/function calling (for ReAct)
    - Streaming and non-streaming
    - 429 exponential backoff (1s/3s/9s, 3 attempts)
    - Per-process token budget
    - Usage stats tracking
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_s,
        )
        # Per-process budget tracking
        self._process_token_used = 0
        self._process_budget = config.process_token_budget
        # Per-session tracking (caller passes session_id to charge)
        self._session_tokens: dict[str, int] = {}

    # ----- public API -----

    @overload
    async def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        *,
        stream: bool = ...,
        session_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...

    @overload
    async def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        *,
        stream: bool = ...,
        session_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...

    @overload
    async def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        *,
        stream: bool = False,
        session_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    async def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        stream: bool = False,
        session_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse | AsyncIterator[str]:
        """Send a chat completion request.

        If stream=True, returns an async iterator of content delta strings.
        Otherwise, returns a single LLMResponse.
        """
        self._check_budget(session_id)

        if stream:
            return self._stream_chat(messages, tools, temperature, max_tokens)
        result = await self._blocking_chat(messages, tools, temperature, max_tokens)
        self._charge(result.usage, session_id)
        return result

    def session_tokens(self, session_id: str) -> int:
        return self._session_tokens.get(session_id, 0)

    def process_tokens(self) -> int:
        return self._process_token_used

    def reset_session(self, session_id: str) -> None:
        self._session_tokens.pop(session_id, None)

    # ----- internals -----

    def _check_budget(self, session_id: str | None) -> None:
        if self._process_token_used >= self._process_budget:
            raise BudgetExceeded(
                f"Process budget exhausted: {self._process_token_used} >= {self._process_budget}"
            )
        if session_id and self._session_tokens.get(session_id, 0) >= self.config.session_token_budget:
            raise BudgetExceeded(
                f"Session {session_id} budget exhausted: {self._session_tokens[session_id]} "
                f">= {self.config.session_token_budget}"
            )

    def _charge(self, usage: UsageStats | None, session_id: str | None) -> None:
        if not usage:
            return
        self._process_token_used += usage.total_tokens
        if session_id:
            self._session_tokens[session_id] = (
                self._session_tokens.get(session_id, 0) + usage.total_tokens
            )

    async def _blocking_chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> LLMResponse:
        kwargs = self._build_kwargs(messages, tools, temperature, max_tokens)
        for attempt in range(self.config.max_retries):
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                return self._parse_response(resp)
            except RateLimitError as e:
                if attempt == self.config.max_retries - 1:
                    raise
                wait = self.config.retry_backoff[attempt]
                logger.warning(f"429 hit, backoff {wait}s (attempt {attempt+1})")
                await asyncio.sleep(wait)
            except APIError as e:
                if attempt == self.config.max_retries - 1:
                    raise
                wait = self.config.retry_backoff[attempt]
                logger.warning(f"API error {e}, backoff {wait}s (attempt {attempt+1})")
                await asyncio.sleep(wait)
        # Unreachable; loop either returns or raises. Keep mypy happy:
        raise RuntimeError("unreachable: retry loop exited without return")

    async def _stream_chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> AsyncIterator[str]:
        kwargs = self._build_kwargs(messages, tools, temperature, max_tokens)
        kwargs["stream"] = True
        async for chunk in await self._client.chat.completions.create(**kwargs):
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def _build_kwargs(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.to_dict() for m in messages],
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        elif self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        elif self.config.max_tokens is not None:
            kwargs["max_tokens"] = self.config.max_tokens
        if tools:
            kwargs["tools"] = [t.to_dict() for t in tools]
            kwargs["tool_choice"] = "auto"
        return kwargs

    def _parse_response(self, resp: Any) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        usage = None
        if resp.usage:
            usage = UsageStats(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
                total_tokens=resp.usage.total_tokens,
            )
        tool_calls = None
        if getattr(msg, "tool_calls", None):
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in msg.tool_calls
            ]
        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
        )
