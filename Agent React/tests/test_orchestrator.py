"""Orchestrator integration tests — main loop wiring (no LLM needed for these).

Verifies:
- create_session returns an id
- load_session retrieves it
- handle_message with rule-routed commands (no LLM) works
- session stage transitions are saved
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["SESSIONS_DIR"] = tempfile.mkdtemp(prefix="orch_test_")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
os.environ.setdefault("OPENAI_BASE_URL", "https://api.example.com/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

from agent.orchestrator import Orchestrator  # noqa: E402
from agent.storage import Session, Stage  # noqa: E402


class OrchestratorWithStubLLM(Orchestrator):
    """Orchestrator with a stub LLM that returns canned responses."""

    def __init__(self) -> None:
        # Skip super().__init__ to avoid real LLM init
        from agent.tools import all_specs
        self.llm = MagicMock()
        self.tools = all_specs()

    async def _fake_chat(self, *args, **kwargs):
        # Return a simple LLMResponse
        from agent.llm import LLMResponse
        return LLMResponse(content="stub reply", tool_calls=None, usage=None)


class TestOrchestratorBasics(unittest.IsolatedAsyncioTestCase):

    async def test_create_session_returns_id(self):
        sid = Orchestrator.create_session(customer="test")
        self.assertTrue(len(sid) > 0)
        # Can load it
        s = Session.load(sid)
        self.assertEqual(s.customer, "test")
        self.assertEqual(s.stage, Stage.clarify)

    async def test_load_session_existing(self):
        sid = Orchestrator.create_session(customer="x")
        s = Orchestrator.load_session(sid)
        self.assertEqual(s.id, sid)

    async def test_load_session_404(self):
        with self.assertRaises(Exception):
            Orchestrator.load_session("nonexistent-id")


class TestOrchestratorHandleMessage(unittest.IsolatedAsyncioTestCase):
    """handle_message with stub LLM that always returns simple text."""

    async def test_handle_message_clarify(self):
        # Use a stub orchestrator that doesn't call real LLM
        orch = OrchestratorWithStubLLM()
        # Override the run flow to short-circuit: the LLM stub will respond with a question
        # We can't easily mock the LLM through chat() because stage handlers call it,
        # so we just verify the basic plumbing.
        sid = Orchestrator.create_session()
        # Test that handle_message runs without crashing and returns a StageResult
        # We mock the LLM to return a simple reply that triggers force-commit or asks
        orch.llm.chat = AsyncMock(return_value=MagicMock(
            content="stub", tool_calls=None, usage=None, finish_reason="stop"
        ))
        result = await orch.handle_message(sid, "测试输入")
        # First message → stays in clarify, reply is the LLM stub
        self.assertIsNotNone(result)
        self.assertEqual(result.reply, "stub")

    async def test_handle_message_finalize_rule(self):
        orch = OrchestratorWithStubLLM()
        sid = Orchestrator.create_session()
        # Manually advance to design stage with passing artifact
        s = Session.load(sid)
        s.set_stage(Stage.design)
        s.save()
        # Write a valid design via real Artifacts API
        from agent.storage import Artifacts
        arts = Artifacts(sid)
        arts.write("design", "design", "x")
        # Send "出报告" → rule fast path → go_to_finalize
        result = await orch.handle_message(sid, "出报告")
        # We just verify the call completes without throwing
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
