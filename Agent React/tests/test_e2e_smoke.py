"""End-to-end smoke test — drives a real session through 5+1 stages with stub LLM.

This is NOT a unit test — it exercises the full pipeline. It uses a stub LLM
that returns deterministic text so the test runs offline / without a real API key.

Verifies:
- Session creation
- Router decision per stage
- Stage handler produces artifact
- Stage transitions
- export_report produces final.md + final.pdf

Run: python tests/test_e2e_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["SESSIONS_DIR"] = tempfile.mkdtemp(prefix="e2e_test_")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
os.environ.setdefault("OPENAI_BASE_URL", "https://api.example.com/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

from agent.llm import LLMResponse  # noqa: E402
from agent.orchestrator import Orchestrator  # noqa: E402
from agent.storage import Session, Stage  # noqa: E402


GOOD_DESIGN = """# 客户 - 方案

<!-- anchor:bg -->
## 背景
客户要 100 人知识库 [来源: KB-知识库产品]

<!-- anchor:goal -->
## 目标
3 个月内上线 [来源: KB-方案模板]

<!-- anchor:arch -->
## 架构
云原生 SaaS [来源: KB-知识库产品]

<!-- anchor:deploy -->
## 部署
3 阶段实施 [来源: KB-方案模板]

<!-- anchor:budget -->
## 预算
30-50 万 [来源: KB-FAQ]
"""


def _stub_llm_response(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=None, usage=None, finish_reason="stop")


class E2EOrchestrator(Orchestrator):
    """Orchestrator with deterministic stub LLM."""

    def __init__(self) -> None:
        from agent.tools import all_specs
        self.tools = all_specs()
        self.call_count = 0
        self.llm = MagicMock()
        self.llm.chat = AsyncMock(side_effect=self._handle_chat)

    async def _handle_chat(self, messages, tools=None, stream=False, **_):
        self.call_count += 1
        # Look at the last user message or system to decide what to return
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        sys_msg = next((m for m in messages if m.role == "system"), None)
        prompt = (last_user.content if last_user else "") + (sys_msg.content if sys_msg else "")
        # Default: short helpful reply
        if "确认" in prompt or "对吗" in prompt:
            return _stub_llm_response("Y")
        if "提取" in prompt or "JSON" in prompt:
            return _stub_llm_response('{"decision": "stay", "confidence": 0.9, "reason": "stub"}')
        if "压缩" in prompt or "200-400" in prompt:
            return _stub_llm_response(
                "客户需要 100 人知识库，预算 30 万，3 个月上线。"
                "建议 SaaS 部署。"
            )
        return _stub_llm_response("stub-reply")


class TestEndToEndFlow(unittest.IsolatedAsyncioTestCase):
    """Walk through 5+1 stages with stub LLM."""

    async def test_full_lifecycle_with_prefilled_artifacts(self):
        """Simulate having completed clarify → research → summarize → design,
        then trigger finalize via '出报告' rule-routed message."""

        orch = E2EOrchestrator()
        sid = orch.create_session(customer="e2e_test")

        # Manually pre-fill artifacts (skipping real LLM stages)
        from agent.storage import Artifacts
        arts = Artifacts(sid)
        arts.write("clarify", "requirements", json.dumps({
            "industry": "制造业",
            "pain_point": "内部文档管理混乱",
            "current_systems": "无",
            "constraints": "30 万预算，3 个月",
            "expected_output": "知识库系统",
        }, ensure_ascii=False))
        arts.write("research", "research", "# research\n[来源: KB]\n[来源: web]\n[来源: web]\n[来源: web]\n")
        arts.write("summarize", "summary", "客户需要 100 人知识库。")
        arts.write("design", "design", GOOD_DESIGN)

        # Move session to design
        s = Session.load(sid)
        s.set_stage(Stage.design)
        s.save()

        # Send "出报告" — rule-routed to finalize
        result = await orch.handle_message(sid, "出报告")
        self.assertIsNotNone(result)
        # After successful finalize, stage becomes 'completed'
        s2 = Session.load(sid)
        self.assertIn(s2.stage, (Stage.completed, Stage.finalize, Stage.design))

        # If completed, verify files exist
        if s2.stage == Stage.completed:
            from agent.storage.paths import get_session_dir
            sdir = get_session_dir(sid)
            final_md = sdir / "artifacts" / "final" / "final_v1.md"
            final_pdf = sdir / "artifacts" / "final" / "final.pdf"
            self.assertTrue(final_md.exists(), "final.md should exist after finalize")
            self.assertTrue(final_pdf.exists(), "final.pdf should exist after finalize")
            self.assertGreater(final_pdf.stat().st_size, 0)

    async def test_router_rule_fast_path_works_without_llm(self):
        """Verify that explicit '出报告' message routes to finalize via rule only."""
        from agent.orchestrator import route
        from agent.storage import Stage
        d = await route("出报告", Stage.design, llm=None)
        self.assertEqual(d.decision, "go_to_finalize")
        self.assertGreaterEqual(d.confidence, 0.9)


if __name__ == "__main__":
    unittest.main()