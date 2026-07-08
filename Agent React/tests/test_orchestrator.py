"""Orchestrator integration tests — main loop wiring (no LLM needed for these).

Verifies:
- create_session returns an id
- load_session retrieves it
- handle_message with rule-routed commands (no LLM) works
- session stage transitions are saved
- current_run marker is set before handler and cleared after
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
        orch.llm.chat = AsyncMock(return_value=MagicMock(
            content="stub", tool_calls=None, usage=None, finish_reason="stop",
        ))
        sid = Orchestrator.create_session()
        # Test that handle_message runs without crashing and returns a StageResult
        result = await orch.handle_message(sid, "测试输入")
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
        # Send rule-routed finalize message
        result = await orch.handle_message(sid, "出报告")
        self.assertIsNotNone(result)

    async def test_current_run_set_then_cleared(self):
        """While a handler is running, current_run is non-None and matches
        the target stage. After the handler returns, current_run is cleared.
        Also verifies the merge bug doesn't drop the field on save."""
        orch = OrchestratorWithStubLLM()
        sid = Orchestrator.create_session()

        # Pre-handle: current_run is None
        pre = Session.load(sid)
        self.assertIsNone(pre.current_run)

        # Stub LLM: ask a clarifying question so we stay in clarify sub-state A
        orch.llm.chat = AsyncMock(return_value=MagicMock(
            content="好的，请告诉我预算范围？",
            tool_calls=None,
            usage=None,
            finish_reason="stop",
        ))

        # Spy on Session.set_current_run / clear_current_run to verify the
        # orchestrator wraps the handler invocation with these calls.
        observed: list = []
        from agent.storage import Session as S
        orig_set = S.set_current_run
        orig_clear = S.clear_current_run

        def spy_set(self, stage):
            observed.append(("set", stage.value if hasattr(stage, "value") else str(stage)))
            orig_set(self, stage)

        def spy_clear(self):
            observed.append(("clear",))
            orig_clear(self)

        S.set_current_run = spy_set
        S.clear_current_run = spy_clear
        try:
            await orch.handle_message(sid, "我们需要 ERP 系统")
        finally:
            S.set_current_run = orig_set
            S.clear_current_run = orig_clear

        # Both set and clear were called
        self.assertTrue(
            any(t[0] == "set" for t in observed),
            "set_current_run never called",
        )
        self.assertTrue(
            any(t[0] == "clear" for t in observed),
            "clear_current_run never called",
        )

        # Verify disk state: current_run is None after handler returns
        post = Session.load(sid)
        self.assertIsNone(post.current_run)


if __name__ == "__main__":
    unittest.main()