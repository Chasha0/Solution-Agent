"""Router unit tests — rule fast path + LLM fallback decision validation."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["SESSIONS_DIR"] = tempfile.mkdtemp(prefix="router_test_")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
os.environ.setdefault("OPENAI_BASE_URL", "https://api.example.com/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

from agent.orchestrator import (  # noqa: E402
    LOW_CONFIDENCE,
    _looks_like_iterate,
    _match_rule,
    route,
    RouteDecision,
)
from agent.storage import Stage  # noqa: E402


class TestRuleFastPath(unittest.TestCase):

    def test_rewrite_triggers_research(self):
        d = _match_rule("重新调研一下")
        self.assertIsNotNone(d)
        self.assertEqual(d.decision, "go_to_research")
        self.assertGreaterEqual(d.confidence, 0.9)

    def test_finalize_keywords(self):
        for text in ("出报告", "可以了", "没问题", "确认", "生成报告", "最终报告"):
            with self.subTest(text=text):
                d = _match_rule(text)
                self.assertIsNotNone(d)
                self.assertEqual(d.decision, "go_to_finalize")

    def test_back_to_design(self):
        d = _match_rule("回到方案")
        self.assertIsNotNone(d)
        self.assertEqual(d.decision, "go_to_design")

    def test_clarify_restart(self):
        d = _match_rule("重新开始")
        self.assertIsNotNone(d)
        self.assertEqual(d.decision, "go_to_clarify")

    def test_no_match(self):
        self.assertIsNone(_match_rule("请把第二段再写详细点"))


class TestIterateDetection(unittest.TestCase):

    def test_revise_keywords(self):
        for t in ("第三部分换 SaaS", "预算压缩到 20 万", "调整一下", "改短点"):
            with self.subTest(t=t):
                self.assertTrue(_looks_like_iterate(t))

    def test_rewrite_is_not_iterate(self):
        for t in ("整篇重写", "推倒重来", "重新写"):
            with self.subTest(t=t):
                self.assertFalse(_looks_like_iterate(t))

    def test_unrelated_is_not_iterate(self):
        self.assertFalse(_looks_like_iterate("你好"))


class TestRouteAsync(unittest.IsolatedAsyncioTestCase):
    """route() with rule fast path (no LLM needed)."""

    async def test_rule_fast_path_skips_llm(self):
        d = await route("出报告吧", Stage.design, llm=None)
        self.assertEqual(d.decision, "go_to_finalize")
        self.assertIn("rule", d.reason)

    async def test_design_stage_revise_stays(self):
        d = await route("第三部分换 SaaS", Stage.design, llm=None)
        self.assertEqual(d.decision, "stay")
        self.assertIn("迭代", d.reason)

    async def test_no_llm_no_match_defaults_stay(self):
        d = await route("请把第二段再写详细点", Stage.design, llm=None)
        self.assertEqual(d.decision, "stay")
        self.assertLess(d.confidence, 0.7)


class TestConstants(unittest.TestCase):

    def test_low_confidence_threshold(self):
        self.assertEqual(LOW_CONFIDENCE, 0.7)


if __name__ == "__main__":
    unittest.main()
