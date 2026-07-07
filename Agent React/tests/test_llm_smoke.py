"""P1 LLM client smoke test (oracle recommendation #8).

Picks the configured provider, sends a 5-line test prompt, eyeball the response.
If API key missing, prints a clear instruction and exits 0 (not a failure for CI).

Run: python tests/test_llm_smoke.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Make `agent` importable when running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm import LLMConfig, LLMClient, Message


SAMPLE_PROMPT = [
    Message.system("你是一个简洁的助手。用一句话回答。"),
    Message.user("用一句话介绍 Python 是什么。"),
]


async def main() -> int:
    try:
        config = LLMConfig.from_env()
    except ValueError as e:
        print(f"[SKIP] {e}")
        print("[SKIP] Set OPENAI_API_KEY in .env or env var to enable this test.")
        return 0

    print(f"Provider: {config.base_url}")
    print(f"Model:    {config.model}")
    print("--- prompt ---")
    for m in SAMPLE_PROMPT:
        print(f"[{m.role}] {m.content}")
    print("--- response ---")

    client = LLMClient(config)
    try:
        resp = await client.chat(SAMPLE_PROMPT, stream=False)
    except Exception as e:
        print(f"[FAIL] {type(e).__name__}: {e}")
        return 1

    print(resp.content)
    print("--- usage ---")
    if resp.usage:
        print(f"prompt={resp.usage.prompt_tokens} completion={resp.usage.completion_tokens} "
              f"total={resp.usage.total_tokens}")
    print(f"finish_reason={resp.finish_reason}")

    if not resp.content.strip():
        print("[FAIL] Empty response")
        return 1
    if resp.usage and resp.usage.total_tokens <= 0:
        print("[FAIL] Zero token usage reported")
        return 1

    print("[OK] sanity check passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
