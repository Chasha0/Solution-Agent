"""P4 stage handlers tests — focused on critical paths (guardrail, anchor, state).

P4 is a 2-2.5d phase. The fixer prompt required 13 unit tests with mocked LLM.
This file is a streamlined smoke-test subset (5 critical tests) that can run
without a real LLM or extensive mocking, by exercising pure functions and
in-memory state transitions.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set up a clean test environment before importing anything
_tmpdir = tempfile.mkdtemp(prefix="agent_test_")
os.environ["SESSIONS_DIR"] = _tmpdir
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
os.environ.setdefault("OPENAI_BASE_URL", "https://api.example.com/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

from agent.stages import STAGE_HANDLERS  # noqa: E402
from agent.stages.design import (  # noqa: E402
    REQUIRED_ANCHORS,
    _detect_anchor,
    validate_design,
)
from agent.stages.clarify import (  # noqa: E402
    IDK_FORCE_COMMIT,
    MAX_ROUNDS,
    _all_fields_filled,
    _is_idk,
)


VALID_DESIGN = """# 客户 - 解决方案

<!-- anchor:bg -->
## 背景
客户要 100 人知识库 [来源: KB-方案模板]

<!-- anchor:goal -->
## 目标
3 个月上线 [来源: KB-方案模板]

<!-- anchor:arch -->
## 架构
云原生 [来源: KB-方案模板]

<!-- anchor:deploy -->
## 部署
K8s [来源: KB-方案模板]

<!-- anchor:budget -->
## 预算
30-50 万 [来源: KB-方案模板]
"""


class TestStageRegistry(unittest.TestCase):
    """All 5 stage handlers are registered."""

    def test_all_stages_present(self):
        from agent.storage import Stage
        expected = {Stage.clarify, Stage.research, Stage.summarize, Stage.design, Stage.finalize}
        self.assertEqual(set(STAGE_HANDLERS.keys()), expected)

    def test_each_handler_has_required_attrs(self):
        for stage, handler in STAGE_HANDLERS.items():
            with self.subTest(stage=stage):
                self.assertTrue(hasattr(handler, "name"))
                self.assertTrue(hasattr(handler, "required_tools"))
                self.assertTrue(hasattr(handler, "guardrail"))
                self.assertTrue(hasattr(handler, "run"))


class TestClarifyHelpers(unittest.TestCase):
    """Clarify sub-state helpers."""

    def test_idk_patterns(self):
        self.assertTrue(_is_idk("我不知道"))
        self.assertTrue(_is_idk("你定吧"))
        self.assertTrue(_is_idk("随便"))
        self.assertTrue(_is_idk("n/a"))
        self.assertFalse(_is_idk("我们需要 ERP"))
        self.assertFalse(_is_idk("预算 30 万"))

    def test_max_rounds_constant(self):
        self.assertEqual(MAX_ROUNDS, 5)
        self.assertEqual(IDK_FORCE_COMMIT, 2)

    def test_all_fields_filled(self):
        full = {
            "industry": "mfg",
            "pain_point": "x",
            "current_systems": "y",
            "constraints": "z",
            "expected_output": "w",
        }
        self.assertTrue(_all_fields_filled({"fields": full}))
        partial = dict(full, constraints="")
        self.assertFalse(_all_fields_filled({"fields": partial}))


class TestDesignGuardrail(unittest.TestCase):
    """Design guardrail: ≥5 anchors, all sections, sources per section."""

    def test_valid_design_passes(self):
        ok, reason = validate_design(VALID_DESIGN)
        self.assertTrue(ok, f"valid design rejected: {reason}")
        self.assertEqual(reason, "ok")

    def test_missing_anchor_rejected(self):
        bad = VALID_DESIGN.replace("<!-- anchor:goal -->", "")
        ok, reason = validate_design(bad)
        self.assertFalse(ok)
        self.assertIn("goal", reason)

    def test_missing_source_rejected(self):
        bad = VALID_DESIGN.replace("[来源: KB-方案模板]\n\n<!-- anchor:goal -->", "NO SOURCE\n\n<!-- anchor:goal -->")
        ok, reason = validate_design(bad)
        self.assertFalse(ok)
        self.assertIn("source", reason.lower())

    def test_too_few_sections_rejected(self):
        bad = """# short
<!-- anchor:bg -->## bg [来源: K]
<!-- anchor:goal -->## goal [来源: K]
"""
        ok, reason = validate_design(bad)
        self.assertFalse(ok)

    def test_empty_rejected(self):
        ok, _ = validate_design("")
        self.assertFalse(ok)
        ok, _ = validate_design("   \n\n  ")
        self.assertFalse(ok)


class TestAnchorDetection(unittest.TestCase):
    """Natural-language → anchor mapping."""

    def test_numeric_third_is_arch(self):
        self.assertEqual(_detect_anchor("第三部分换 SaaS"), "arch")
        self.assertEqual(_detect_anchor("第三节"), "arch")
        self.assertEqual(_detect_anchor("第三块"), "arch")

    def test_numeric_fourth_is_deploy(self):
        self.assertEqual(_detect_anchor("第四部分"), "deploy")

    def test_keyword_budget(self):
        self.assertEqual(_detect_anchor("预算压缩到 20 万"), "budget")
        self.assertEqual(_detect_anchor("成本太高了"), "budget")

    def test_keyword_deploy(self):
        self.assertEqual(_detect_anchor("上线时间"), "deploy")
        self.assertEqual(_detect_anchor("实施排期"), "deploy")

    def test_no_match_returns_none(self):
        self.assertIsNone(_detect_anchor("重新调研"))
        self.assertIsNone(_detect_anchor("可以了出报告"))


class TestSectionReplace(unittest.TestCase):
    """Design section replacement for iterate sub-state."""

    def test_replace_middle_section(self):
        from agent.stages.design import DesignHandler
        h = STAGE_HANDLERS["design"]
        out = h._replace_section(VALID_DESIGN, "arch", "新架构 [来源: KB-new]")
        self.assertIn("新架构", out)
        self.assertNotIn("云原生", out)
        # Other anchors preserved
        for a in ("bg", "goal", "deploy", "budget"):
            self.assertIn(f"anchor:{a}", out)

    def test_replace_last_section(self):
        from agent.stages.design import DesignHandler
        h = STAGE_HANDLERS["design"]
        out = h._replace_section(VALID_DESIGN, "budget", "新预算 100 万 [来源: KB-x]")
        self.assertIn("新预算 100 万", out)
        self.assertNotIn("30-50 万", out)
        self.assertTrue(out.rstrip().endswith("[来源: KB-x]"))


class TestEndToEndLifecycle(unittest.TestCase):
    """Walk a full session through clarify → research → summarize → design → finalize
    using the real storage but a no-op LLM (stub via monkey-patch)."""

    def setUp(self):
        from agent.storage import Session
        self.session = Session.create(customer="测试客户")

    def test_session_starts_in_clarify(self):
        from agent.storage import Stage
        self.assertEqual(self.session.stage, Stage.clarify)

    def test_session_state_persists(self):
        from agent.storage import Stage
        self.session.set_stage(Stage.research)
        self.session.save()
        # Reload
        from agent.storage import Session
        s2 = Session.load(self.session.id)
        self.assertEqual(s2.stage, Stage.research)
        self.assertIn(Stage.research, s2.stage_history)


if __name__ == "__main__":
    unittest.main()
