"""P3 tools smoke tests.

Runs without pytest (not installed) using stdlib `unittest`. Either:
    python -m unittest tests.test_tools -v
or:
    python tests/test_tools.py

Required tests (per P3 spec):
    1. parse_doc on a tiny .txt file → wraps in <uploaded_document> + PII redacted
    2. parse_doc on a non-existent file → error string
    3. pii.scrub redacts a Chinese mobile number
    4. save_section + revise_section round-trip with real storage (tmp session)
    5. export_report produces both final.md and final.pdf (PDF size > 0)
    6. Tool registration: all 6 tools in REGISTRY

Storage tests use `SESSIONS_DIR` env var to redirect P2 into a tmp dir.
If P2 is missing they skip with a clear message.

Web-search and KB tests are intentionally omitted as smoke tests because they
depend on external services (Tavily / DuckDuckGo / OpenAI embeddings). The
shape of their return values is verified by the JSON-unmarshalling in
TestToolReturnShapes; deeper testing belongs in golden-case tests (P9).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Make `agent` importable when running as `python tests/test_tools.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import (  # noqa: E402
    REGISTRY,
    all_specs,
    export_report,
    get,
    kb_search,
    parse_doc,
    pii,
    revise_section,
    save_section,
    web_search,
)


def run(coro):
    """asyncio.run shim — keeps the tests one-liner."""
    return asyncio.run(coro)


# ---------- helpers ----------


def _try_import_storage():
    """Return (Session, Artifacts, ArtifactNotFound) if P2 is committed."""
    try:
        from agent.storage import ArtifactNotFound, Artifacts, Session  # type: ignore
        return Session, Artifacts, ArtifactNotFound
    except ImportError:
        return None, None, None


# ---------- registry ----------


class TestToolRegistration(unittest.TestCase):
    def test_all_six_tools_registered(self) -> None:
        names = set(REGISTRY.keys())
        expected = {
            "web_search",
            "kb_search",
            "parse_doc",
            "save_section",
            "revise_section",
            "export_report",
        }
        self.assertEqual(names, expected, f"missing/extra tools: {names ^ expected}")
        self.assertEqual(len(all_specs()), 6)

    def test_every_tool_has_handler_and_json_schema(self) -> None:
        for spec in all_specs():
            with self.subTest(tool=spec.name):
                self.assertTrue(spec.name)
                self.assertTrue(spec.description)
                self.assertEqual(spec.parameters.get("type"), "object")
                self.assertTrue(callable(spec.handler))
                self.assertIs(get(spec.name), spec)


# ---------- pii ----------


class TestPII(unittest.TestCase):
    def test_chinese_mobile_redacted(self) -> None:
        out = pii.scrub("Call me at 13812345678 anytime.")
        self.assertIn("<REDACTED:MOBILE>", out)
        self.assertNotIn("13812345678", out)

    def test_id_card_redacted(self) -> None:
        out = pii.scrub("ID: 110101199003078888")
        self.assertIn("<REDACTED:ID_CARD>", out)
        self.assertNotIn("110101199003078888", out)

    def test_bank_card_redacted(self) -> None:
        out = pii.scrub("Card: 6222021234567890")
        self.assertIn("<REDACTED:BANK_CARD>", out)
        self.assertNotIn("6222021234567890", out)

    def test_no_pii_unchanged(self) -> None:
        text = "Just a normal sentence with no PII. Nothing to see here."
        self.assertEqual(pii.scrub(text), text)

    def test_empty_input(self) -> None:
        self.assertEqual(pii.scrub(""), "")
        self.assertEqual(pii.scrub(None), None)  # type: ignore[arg-type]


# ---------- parse_doc ----------


class TestParseDoc(unittest.TestCase):
    def test_txt_wrapped_and_pii_redacted(self) -> None:
        with tempfile.NamedTemporaryFile(
            suffix=".txt", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(
                "Hello world.\n"
                "Customer phone 13812345678 needs follow-up.\n"
                "Ref ID 110101199003078888 on file.\n"
            )
            path = f.name
        try:
            result = run(parse_doc(path))
        finally:
            os.unlink(path)

        self.assertIn("<uploaded_document", result)
        self.assertIn('trust="untrusted"', result)
        self.assertIn("</uploaded_document>", result)
        # PII scrubbed inside the wrapper
        self.assertIn("<REDACTED:MOBILE>", result)
        self.assertIn("<REDACTED:ID_CARD>", result)
        self.assertNotIn("13812345678", result)
        self.assertNotIn("110101199003078888", result)
        # Source file name appears in the wrapper
        self.assertIn(Path(path).name, result)

    def test_md_wrapped(self) -> None:
        with tempfile.NamedTemporaryFile(
            suffix=".md", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("# Title\n\nSome content here.\n")
            path = f.name
        try:
            result = run(parse_doc(path))
        finally:
            os.unlink(path)
        self.assertIn("<uploaded_document", result)
        self.assertIn("# Title", result)
        self.assertIn("Some content here.", result)

    def test_nonexistent_file_returns_error(self) -> None:
        result = run(parse_doc("Z:/definitely/not/here/file_xyz_42.txt"))
        self.assertIn("error", result.lower())
        # Should be a plain error string, not JSON (parse_doc contract).
        self.assertFalse(result.strip().startswith("{"))

    def test_empty_file_returns_error(self) -> None:
        with tempfile.NamedTemporaryFile(
            suffix=".txt", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("   \n\n  \n")
            path = f.name
        try:
            result = run(parse_doc(path))
        finally:
            os.unlink(path)
        self.assertIn("error", result.lower())
        # Should mention scanned-PDF / no-text cause so caller can surface it.
        self.assertTrue(
            "no text" in result.lower() or "扫描件" in result,
            f"unexpected error message: {result!r}",
        )


# ---------- save_section + revise_section round-trip ----------


class _StorageTestBase(unittest.TestCase):
    """Base that isolates P2 storage into a tmp dir via SESSIONS_DIR env."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.Session, cls.Artifacts, cls.ArtifactNotFound = _try_import_storage()

    def setUp(self) -> None:
        if self.Session is None:  # type: ignore[has-type]
            self.skipTest("agent.storage not yet implemented (P2 in progress)")
        self._tmp = Path(tempfile.mkdtemp(prefix="p3_tools_"))
        self._old_sessions_dir = os.environ.get("SESSIONS_DIR")
        os.environ["SESSIONS_DIR"] = str(self._tmp)

    def tearDown(self) -> None:
        if self._old_sessions_dir is None:
            os.environ.pop("SESSIONS_DIR", None)
        else:
            os.environ["SESSIONS_DIR"] = self._old_sessions_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _new_session(self) -> str:
        s = self.Session.create(customer="p3_smoke")  # type: ignore[misc]
        return s.id


class TestStorageRoundTrip(_StorageTestBase):
    def _save(self, session_id: str, stage: str, name: str, content: str) -> dict:
        raw = run(save_section(stage, name, content, session_id))
        return json.loads(raw)

    def _revise(self, session_id: str, stage: str, name: str, new_content: str) -> dict:
        raw = run(revise_section(stage, name, new_content, session_id))
        return json.loads(raw)

    def test_save_and_revise_round_trip(self) -> None:
        session_id = self._new_session()
        # First save
        d1 = self._save(session_id, "research", "notes", "Original research notes.\n")
        self.assertTrue(d1.get("ok"), f"save failed: {d1}")
        v1 = d1.get("version")
        self.assertIsNotNone(v1, "version should be returned")

        # Revise (writes version 2 of the same artifact)
        d2 = self._revise(
            session_id, "research", "notes",
            "Updated research notes with new finding.\n",
        )
        self.assertTrue(d2.get("ok"), f"revise failed: {d2}")
        self.assertIn("diff", d2)
        self.assertNotEqual(d2.get("version"), v1, "version should advance")
        # diff should mention at least one removed/added line
        self.assertTrue(
            "-" in d2["diff"] or "+" in d2["diff"],
            f"diff looks empty: {d2['diff']!r}",
        )

        # Sanity check: the actual on-disk file should now contain the
        # new content (latest version).
        artifacts = self.Artifacts(session_id)  # type: ignore[misc]
        latest = artifacts.read("research", "notes")
        self.assertIn("Updated research notes", latest)

    def test_revise_nonexistent_returns_not_found(self) -> None:
        session_id = self._new_session()
        d = self._revise(session_id, "design", "ghost", "anything")
        self.assertIn("error", d)
        self.assertEqual(d.get("error"), "not found")

    def test_save_requires_existing_session(self) -> None:
        # No Session.create first → Session.load will raise inside Artifacts.write.
        d = self._save("does-not-exist-xyz", "research", "notes", "x")
        # Tool should still return a JSON error, not crash.
        self.assertIn("error", d)


# ---------- export_report ----------


class TestExportReport(_StorageTestBase):
    def _seed_artifacts(self, session_id: str) -> None:
        artifacts = self.Artifacts(session_id)  # type: ignore[misc]
        artifacts.write(
            "requirements",
            "requirements",
            json.dumps(
                {
                    "industry": "education",
                    "pain": "onboarding",
                    "constraint": "30k budget",
                    "expected_output": "internal KB",
                },
                ensure_ascii=False,
            ),
        )
        artifacts.write(
            "research",
            "research",
            "# Research Notes\n\n- Source A\n- Source B\n",
        )
        artifacts.write(
            "summarize",
            "summary",
            "## Summary\n\nThree key findings summarised.",
        )
        # P2 writes files as `<name>_v1.md`; for design we use the
        # spec-conventional artifact name `design_v1`.
        artifacts.write(
            "design",
            "design_v1",
            (
                "<!-- anchor:arch -->\n"
                "## Architecture\n\nUse cloud SaaS.\n"
                "[source: KB doc1]\n\n"
                "<!-- anchor:deploy -->\n"
                "## Deployment\n\nContainerised.\n"
            ),
        )

    def test_export_md_and_pdf(self) -> None:
        session_id = self._new_session()
        self._seed_artifacts(session_id)

        raw = run(export_report(session_id, format="pdf"))
        data = json.loads(raw)
        self.assertTrue(data.get("ok"), f"export_report failed: {data}")

        md_path = Path(data["md"])
        pdf_path = Path(data["pdf"])
        self.assertTrue(md_path.exists(), f"md missing: {md_path}")
        self.assertTrue(pdf_path.exists(), f"pdf missing: {pdf_path}")
        self.assertGreater(md_path.stat().st_size, 0)
        self.assertGreater(pdf_path.stat().st_size, 0)

        # Spot-check md contents.
        md_text = md_path.read_text(encoding="utf-8")
        self.assertIn("需求概要", md_text)
        self.assertIn("调研发现", md_text)
        self.assertIn("调研小结", md_text)
        self.assertIn("Architecture", md_text)

        # Spot-check pdf: fpdf2 outputs start with `%PDF-`.
        head = pdf_path.read_bytes()[:5]
        self.assertEqual(head, b"%PDF-", f"unexpected PDF header: {head!r}")

    def test_export_md_only(self) -> None:
        session_id = self._new_session()
        self._seed_artifacts(session_id)

        raw = run(export_report(session_id, format="md"))
        data = json.loads(raw)
        self.assertTrue(data.get("ok"))
        self.assertIsNotNone(data.get("md"))
        self.assertIsNone(data.get("pdf"))


# ---------- web_search / kb_search shape (no network needed) ----------


class TestToolReturnShapes(unittest.TestCase):
    """Verify the *contract*: always-valid-JSON result for web_search / kb_search.

    These exercise the empty-query short-circuits which don't touch the
    network or the KB.
    """

    def test_web_search_empty_query(self) -> None:
        raw = run(web_search(""))
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertIn("error", data[0])

    def test_kb_search_empty_query(self) -> None:
        raw = run(kb_search(""))
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertIn("error", data[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)