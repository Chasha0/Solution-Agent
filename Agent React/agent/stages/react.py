"""Shared ReAct loop helper for stages that use tool calling (research, design).

Per spec §3 research / design rows: ReAct loop with explicit tool budgets.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from agent.llm import LLMClient, Message, Tool
from agent.tools import REGISTRY

logger = logging.getLogger(__name__)


async def run_react(
    client: LLMClient,
    messages: list[Message],
    tools: list[Tool],
    *,
    allowed_tool_names: list[str] | None = None,
    max_iters: int = 8,
    extra_tool_specs: list[Tool] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run a ReAct loop until the LLM produces a final answer (no tool calls).

    Args:
        client: LLMClient instance.
        messages: Initial message list (system + history + user).
        tools: All available ToolSpec list.
        allowed_tool_names: If set, only these tool names are exposed to the LLM.
        max_iters: Max ReAct iterations.
        extra_tool_specs: Additional inline tools to merge into `tools`.

    Returns:
        (final_content, tool_call_history) where tool_call_history is a list of
        {name, arguments, result} dicts.
    """
    # Filter tool list if a whitelist is given
    if allowed_tool_names is not None:
        active_tools = [t for t in tools if t.name in allowed_tool_names]
    else:
        active_tools = list(tools)
    if extra_tool_specs:
        active_tools = list(active_tools) + list(extra_tool_specs)

    tool_history: list[dict[str, Any]] = []
    current: list[Message] = list(messages)

    for iteration in range(max_iters):
        resp = await client.chat(current, tools=active_tools, stream=False)
        if not resp.tool_calls:
            return resp.content, tool_history

        # Record the assistant's tool call
        tool_calls_openai = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                },
            }
            for tc in resp.tool_calls
        ]
        current.append(
            Message(
                role="assistant",
                content=resp.content or "",
                tool_calls=tool_calls_openai,
            )
        )

        # Execute each tool call
        for tc in resp.tool_calls:
            name = tc["name"]
            args_raw = tc["arguments"]
            tool_call_id = tc["id"]
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except Exception as e:
                result_str = json.dumps(
                    {"error": f"invalid arguments JSON: {e}"}, ensure_ascii=False
                )
                current.append(
                    Message(role="tool", content=result_str, tool_call_id=tool_call_id)
                )
                tool_history.append({"name": name, "arguments": args_raw, "result": result_str})
                continue

            spec = REGISTRY.get(name)
            if spec is None:
                result_str = json.dumps(
                    {"error": f"unknown tool: {name}"}, ensure_ascii=False
                )
            else:
                try:
                    result_str = await spec.handler(**args)
                except Exception as e:
                    logger.warning(f"Tool {name} failed: {e}")
                    result_str = json.dumps(
                        {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False
                    )

            current.append(
                Message(role="tool", content=result_str, tool_call_id=tool_call_id)
            )
            tool_history.append({"name": name, "arguments": args, "result": result_str})

    logger.warning(f"ReAct loop hit max_iters={max_iters}; returning last assistant content")
    # Fallback: return the last assistant content we have, or empty
    for m in reversed(current):
        if m.role == "assistant":
            return m.content, tool_history
    return "", tool_history
