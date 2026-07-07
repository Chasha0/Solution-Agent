"""P2 storage layer tests — uses real filesystem in a temp directory.

No mocking. Runnable two ways::

    python tests/test_storage.py                       # direct (unittest)
    pytest tests/test_storage.py                       # if pytest is installed

Each test gets a fresh ``SESSIONS_DIR`` / ``KB_DIR`` pointing at a private
temp directory so they cannot interfere with each other or with the real
``sessions/`` and ``kb/`` trees.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Allow `import agent.*` when this file is executed directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.storage import (  # noqa: E402
    ArtifactNotFound,
    Artifacts,
    LockTimeout,
    MetaDict,
    SchemaMismatchError,
    Session,
    SessionNotFound,
    SessionStatus,
    Stage,
    get_kb_dir,
    get_session_dir,
    get_sessions_dir,
)
from agent.storage.lock import FileLock  # noqa: E402


class _TempEnvMixin:
    """Point SESSIONS_DIR / KB_DIR at a private tmp dir for each test."""

    def setUp(self) -> None:  # noqa: D401  (unittest API)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._old_sessions = os.environ.get("SESSIONS_DIR")
        self._old_kb = os.environ.get("KB_DIR")
        os.environ["SESSIONS_DIR"] = str(self.tmp_path / "sessions")
        os.environ["KB_DIR"] = str(self.tmp_path / "kb")

    def tearDown(self) -> None:  # noqa: D401
        if self._old_sessions is not None:
            os.environ["SESSIONS_DIR"] = self._old_sessions
        else:
            os.environ.pop("SESSIONS_DIR", None)
        if self._old_kb is not None:
            os.environ["KB_DIR"] = self._old_kb
        else:
            os.environ.pop("KB_DIR", None)
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 1. Session.create() — creates dir + meta.json with defaults
# ---------------------------------------------------------------------------


class TestSessionCreate(_TempEnvMixin, unittest.TestCase):
    def test_creates_dir_and_meta_with_all_defaults(self):
        s = Session.create(customer="Alice")

        # Directory exists and contains meta.json
        self.assertTrue(s.path.is_dir())
        self.assertTrue((s.path / "meta.json").is_file())

        # Re-load from disk to verify persistence + defaults
        s2 = Session.load(s.id)
        self.assertEqual(s2.id, s.id)
        self.assertEqual(s2.customer, "Alice")
        self.assertEqual(s2.stage, Stage.clarify)
        self.assertEqual(s2.status, SessionStatus.active)
        self.assertEqual(s2.stage_history, [])
        self.assertEqual(s2.version_counts, {})

        # 12-char hex id, UUID4-derived
        self.assertEqual(len(s2.id), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in s2.id))

        # messages_path is the canonical location
        self.assertEqual(s2.messages_path, s2.path / "messages.jsonl")


# ---------------------------------------------------------------------------
# 2. Session.load() — round-trip
# ---------------------------------------------------------------------------


class TestSessionRoundTrip(_TempEnvMixin, unittest.TestCase):
    def test_round_trip_preserves_fields(self):
        s = Session.create(customer="Round Trip")
        s.customer = "Modified"
        s.set_stage(Stage.research)
        s.set_status(SessionStatus.paused)

        # Re-load from disk
        s2 = Session.load(s.id)
        self.assertEqual(s2.customer, "Modified")
        self.assertEqual(s2.stage, Stage.research)
        self.assertEqual(s2.status, SessionStatus.paused)
        self.assertIn(Stage.research, s2.stage_history)


# ---------------------------------------------------------------------------
# 3. Session.load() — missing session
# ---------------------------------------------------------------------------


class TestSessionNotFound(_TempEnvMixin, unittest.TestCase):
    def test_load_missing_session_raises(self):
        with self.assertRaises(SessionNotFound):
            Session.load("nonexistent_id_xx")

    def test_load_missing_meta_file_raises(self):
        # Session dir exists but meta.json does not
        d = get_sessions_dir() / "broken_session"
        d.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(SessionNotFound):
            Session.load("broken_session")


# ---------------------------------------------------------------------------
# 4. Session.load() — corrupted meta.json
# ---------------------------------------------------------------------------


class TestCorruptedMeta(_TempEnvMixin, unittest.TestCase):
    def test_corrupted_json_raises(self):
        s = Session.create()
        (s.path / "meta.json").write_text("THIS IS NOT VALID JSON {{{", encoding="utf-8")
        with self.assertRaises(SessionNotFound):
            Session.load(s.id)

    def test_wrong_shape_raises(self):
        # Valid JSON, missing required fields → SessionNotFound (we wrap ValidationError).
        s = Session.create()
        (s.path / "meta.json").write_text('{"random": "object"}', encoding="utf-8")
        with self.assertRaises(SessionNotFound):
            Session.load(s.id)


# ---------------------------------------------------------------------------
# 5. Session.load() — schema_version mismatch
# ---------------------------------------------------------------------------


class TestSchemaMismatch(_TempEnvMixin, unittest.TestCase):
    def test_schema_version_mismatch_raises(self):
        s = Session.create()
        meta_path = s.path / "meta.json"
        d = json.loads(meta_path.read_text(encoding="utf-8"))
        d["schema_version"] = 999
        meta_path.write_text(json.dumps(d), encoding="utf-8")
        with self.assertRaises(SchemaMismatchError):
            Session.load(s.id)


# ---------------------------------------------------------------------------
# 6. Session.set_stage() — appends to history + updates current_stage
# ---------------------------------------------------------------------------


class TestStageHistory(_TempEnvMixin, unittest.TestCase):
    def test_set_stage_appends_and_updates(self):
        s = Session.create()
        self.assertEqual(s.stage, Stage.clarify)
        self.assertEqual(s.stage_history, [])

        s.set_stage(Stage.research)
        self.assertEqual(s.stage, Stage.research)
        self.assertEqual(list(s.stage_history), [Stage.research])

        s.set_stage(Stage.summarize)
        self.assertEqual(s.stage, Stage.summarize)
        self.assertEqual(list(s.stage_history), [Stage.research, Stage.summarize])

        # Reload from disk to confirm persistence
        s2 = Session.load(s.id)
        self.assertEqual(s2.stage, Stage.summarize)
        self.assertEqual(list(s2.stage_history), [Stage.research, Stage.summarize])

    def test_record_stage_is_alias_for_set_stage(self):
        s = Session.create()
        s.record_stage(Stage.design)
        self.assertEqual(s.stage, Stage.design)
        self.assertIn(Stage.design, s.stage_history)


# ---------------------------------------------------------------------------
# 7. Session.save() — atomic write-rename survives stale .tmp
# ---------------------------------------------------------------------------


class TestAtomicSave(_TempEnvMixin, unittest.TestCase):
    def test_save_atomic_with_stale_tmp(self):
        # Baseline save
        s = Session.create(customer="Original")
        s.customer = "After first save"
        s.save()

        # Simulate crash mid-write: leave a corrupted .tmp behind
        tmp_file = s.path / "meta.json.tmp"
        tmp_file.write_text("CORRUPTED MID-WRITE GARBAGE", encoding="utf-8")
        self.assertTrue(tmp_file.exists())

        # Loading must still succeed — it reads meta.json, not .tmp
        s2 = Session.load(s.id)
        self.assertEqual(s2.customer, "After first save")

        # Next save overwrites .tmp then renames; .tmp must not remain
        s2.customer = "After second save"
        s2.save()
        self.assertFalse(
            tmp_file.exists(),
            f"meta.json.tmp should have been renamed, but still exists at {tmp_file}",
        )

        # Final on-disk state is the latest, valid JSON
        s3 = Session.load(s.id)
        self.assertEqual(s3.customer, "After second save")
        raw = json.loads((s.path / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["customer"], "After second save")
        self.assertEqual(raw["schema_version"], 1)


# ---------------------------------------------------------------------------
# 8–12. Artifacts — write / read / list / cross-instance
# ---------------------------------------------------------------------------


class TestArtifactsWriteRead(_TempEnvMixin, unittest.TestCase):
    def test_write_creates_v1_then_v2_and_bumps_count(self):
        s = Session.create()
        a = Artifacts(s.id)

        v1 = a.write("design", "design", "First draft body")
        self.assertEqual(v1, 1)
        self.assertTrue((s.path / "artifacts" / "design" / "design_v1.md").is_file())

        v2 = a.write("design", "design", "Second draft body")
        self.assertEqual(v2, 2)
        self.assertTrue((s.path / "artifacts" / "design" / "design_v2.md").is_file())

        # v1 still on disk (we keep history)
        self.assertTrue((s.path / "artifacts" / "design" / "design_v1.md").is_file())

        # version_counts in meta reflects the latest
        s2 = Session.load(s.id)
        self.assertEqual(s2.version_counts.get("design/design"), 2)

    def test_read_latest_returns_newest(self):
        s = Session.create()
        a = Artifacts(s.id)
        a.write("design", "design", "v1 content")
        a.write("design", "design", "v2 content")
        self.assertEqual(a.read_latest("design", "design"), "v2 content")

    def test_read_specific_version(self):
        s = Session.create()
        a = Artifacts(s.id)
        a.write("design", "design", "v1 content")
        a.write("design", "design", "v2 content")
        self.assertEqual(a.read("design", "design", version=1), "v1 content")
        self.assertEqual(a.read("design", "design", version=2), "v2 content")

    def test_list_versions_sorted_ascending(self):
        s = Session.create()
        a = Artifacts(s.id)
        a.write("design", "design", "v1")
        a.write("design", "design", "v2")
        self.assertEqual(a.list_versions("design", "design"), [1, 2])

    def test_two_instances_share_state(self):
        s = Session.create()
        a1 = Artifacts(s.id)
        a2 = Artifacts(s.id)

        a1.write("research", "research", "from instance 1")
        # Instance 2 must see the write from instance 1
        self.assertEqual(a2.read_latest("research", "research"), "from instance 1")

    def test_missing_artifact_raises(self):
        s = Session.create()
        a = Artifacts(s.id)
        with self.assertRaises(ArtifactNotFound):
            a.read_latest("design", "design")
        # Also when version is explicit but file is missing
        a.write("design", "design", "only v1")
        with self.assertRaises(ArtifactNotFound):
            a.read("design", "design", version=99)


# ---------------------------------------------------------------------------
# 13. FileLock — second acquisition times out
# ---------------------------------------------------------------------------


class TestFileLock(_TempEnvMixin, unittest.TestCase):
    def test_second_lock_times_out(self):
        lock_path = self.tmp_path / "test.lock"
        # Outer holder with long timeout; inner with 0.1s to fail fast.
        with FileLock(lock_path, timeout_s=2.0):
            start = time.monotonic()
            with self.assertRaises(LockTimeout):
                with FileLock(lock_path, timeout_s=0.1):
                    self.fail("Inner FileLock should not have been acquired")
            elapsed = time.monotonic() - start
            # Should have waited ~0.1s before giving up.
            self.assertGreaterEqual(
                elapsed,
                0.05,
                f"Inner lock should have waited at least ~0.1s, elapsed={elapsed:.3f}s",
            )
            self.assertLess(
                elapsed,
                1.0,
                f"Inner lock should not have waited too long, elapsed={elapsed:.3f}s",
            )

    def test_lock_released_after_context(self):
        lock_path = self.tmp_path / "serial.lock"
        with FileLock(lock_path, timeout_s=1.0):
            pass
        # If release worked, we can grab it again immediately.
        with FileLock(lock_path, timeout_s=0.1):
            pass


# ---------------------------------------------------------------------------
# 14. End-to-end — full lifecycle through one Session + Artifacts pair
# ---------------------------------------------------------------------------


class TestEndToEnd(_TempEnvMixin, unittest.TestCase):
    def test_full_flow(self):
        # Create
        s = Session.create(customer="Acme Corp")
        self.assertEqual(s.stage, Stage.clarify)
        self.assertEqual(s.status, SessionStatus.active)

        # Write artifacts across 3 stages, 2 of which share stage="design"
        a = Artifacts(s.id)
        a.write("clarify", "requirements", '{"industry": "education", "budget": 300000}')
        a.write("research", "research", "## Findings\n- web source 1\n- kb source 1")
        a.write("design", "design", "## Plan\n- Step 1\n- Step 2")

        # Stage transitions
        s.set_stage(Stage.research)
        s.set_stage(Stage.summarize)
        s.set_stage(Stage.design)

        # Status change
        s.set_status(SessionStatus.paused)

        # Re-load everything and verify
        s2 = Session.load(s.id)
        self.assertEqual(s2.customer, "Acme Corp")
        self.assertEqual(s2.stage, Stage.design)
        self.assertEqual(s2.status, SessionStatus.paused)
        self.assertEqual(
            list(s2.stage_history),
            [Stage.research, Stage.summarize, Stage.design],
        )

        # version_counts mirrors the three artifacts
        self.assertEqual(s2.version_counts.get("clarify/requirements"), 1)
        self.assertEqual(s2.version_counts.get("research/research"), 1)
        self.assertEqual(s2.version_counts.get("design/design"), 1)

        # Content survives reload
        a2 = Artifacts(s.id)
        self.assertEqual(
            a2.read_latest("clarify", "requirements"),
            '{"industry": "education", "budget": 300000}',
        )
        self.assertEqual(
            a2.read_latest("design", "design"),
            "## Plan\n- Step 1\n- Step 2",
        )

        # Full listing — sorted, every entry accounted for
        listing = a2.list_artifacts()
        self.assertEqual(
            sorted(((st.value, n, v) for st, n, v in listing)),
            [
                ("clarify", "requirements", 1),
                ("design", "design", 1),
                ("research", "research", 1),
            ],
        )

        # Revise the design draft → v2
        a2.write("design", "design", "## Plan (revised)\n- Step 1\n- Step 2\n- Step 3")
        s3 = Session.load(s.id)
        self.assertEqual(s3.version_counts.get("design/design"), 2)
        self.assertEqual(a2.read("design", "design", version=1), "## Plan\n- Step 1\n- Step 2")
        self.assertEqual(
            a2.read("design", "design", version=2),
            "## Plan (revised)\n- Step 1\n- Step 2\n- Step 3",
        )
        self.assertEqual(a2.list_versions("design", "design"), [1, 2])


# ---------------------------------------------------------------------------
# Helper sanity checks (small, kept here so the test file is self-contained)
# ---------------------------------------------------------------------------


class TestPathHelpers(_TempEnvMixin, unittest.TestCase):
    def test_sessions_dir_creates_on_access(self):
        d = get_sessions_dir()
        self.assertTrue(d.is_dir())

    def test_session_dir_creates_on_access(self):
        d = get_session_dir("phantom")
        self.assertTrue(d.is_dir())
        # Cleanup so other tests don't see it
        import shutil

        shutil.rmtree(d)

    def test_kb_dir_creates_on_access(self):
        d = get_kb_dir()
        self.assertTrue(d.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)