"""Session: per-customer state machine, backed by meta.json on disk.

Storage layout (see spec §4.3)::

    sessions/<session_id>/
        .lock          # FileLock target
        meta.json      # atomically written via write-rename
        messages.jsonl # chat history (owned by P6 orchestrator; path exposed)
        artifacts/     # managed by Artifacts class
        uploads/       # customer uploads (out of scope here)
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .lock import FileLock
from .paths import get_sessions_dir
from .schema import (
    MetaDict,
    SchemaMismatchError,
    SessionStatus,
    Stage,
    meta_from_dict,
    meta_to_dict,
)


class SessionNotFound(Exception):
    """Raised by :meth:`Session.load` when the session dir / meta.json is missing
    or when meta.json fails validation. ``SchemaMismatchError`` propagates
    separately so callers can decide whether to back up + migrate.
    """


class Session:
    """Wraps a single :class:`MetaDict` plus its on-disk path.

    Mutations go through the explicit setters (``set_stage`` / ``set_status`` /
    ``record_stage``) or the ``customer`` property and call :meth:`save` to
    persist atomically under the per-session FileLock.
    """

    # ---- factories ----

    @classmethod
    def create(cls, customer: str = "") -> "Session":
        """Create a new session dir + write initial ``meta.json``.

        The session id is ``uuid.uuid4().hex[:12]`` — 12 hex chars (~48 bits)
        is plenty for a single-demo process and keeps paths short.
        """
        sid = uuid.uuid4().hex[:12]
        meta = MetaDict(
            session_id=sid,
            created_at=datetime.now(timezone.utc).isoformat(),
            customer=customer,
            current_stage=Stage.clarify,
            status=SessionStatus.active,
            stage_history=[],
            version_counts={},
        )
        # NOTE: do not call get_session_dir() here — that would mkdir and
        # hide creation side effects from the caller. mkdir explicitly.
        session_dir = get_sessions_dir() / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        session = cls(meta, session_dir)
        session.save()
        return session

    @classmethod
    def load(cls, session_id: str) -> "Session":
        """Load an existing session.

        Raises:
            SessionNotFound: missing dir, missing meta.json, invalid JSON, or
                pydantic validation failure.
            SchemaMismatchError: meta.json's ``schema_version`` disagrees with
                :data:`agent.storage.schema.SCHEMA_VERSION`.
        """
        sessions_dir = get_sessions_dir()
        session_dir = sessions_dir / session_id
        if not session_dir.is_dir():
            raise SessionNotFound(f"Session directory not found: {session_dir}")
        meta_path = session_dir / "meta.json"
        if not meta_path.is_file():
            raise SessionNotFound(f"meta.json not found in {session_dir}")
        try:
            raw: Any = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SessionNotFound(
                f"meta.json is corrupted (not valid JSON): {meta_path}: {e}"
            ) from e
        try:
            meta = meta_from_dict(raw)
        except SchemaMismatchError:
            raise  # caller decides whether to back up + migrate
        except ValidationError as e:
            raise SessionNotFound(
                f"meta.json failed validation: {meta_path}: {e}"
            ) from e
        return cls(meta, session_dir)

    # ---- construction ----

    def __init__(self, meta: MetaDict, path: Path) -> None:
        self._meta = meta
        self._path = path

    # ---- properties (read-mostly API) ----

    @property
    def id(self) -> str:
        return self._meta.session_id

    @property
    def created_at(self) -> str:
        return self._meta.created_at

    @property
    def customer(self) -> str:
        return self._meta.customer

    @customer.setter
    def customer(self, value: str) -> None:
        self._meta.customer = value

    @property
    def stage(self) -> Stage:
        return self._meta.current_stage

    @property
    def status(self) -> SessionStatus:
        return self._meta.status

    @property
    def stage_history(self) -> list[Stage]:
        # Return a copy so callers can't mutate our internal state.
        return list(self._meta.stage_history)

    @property
    def version_counts(self) -> dict[str, int]:
        return dict(self._meta.version_counts)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def messages_path(self) -> Path:
        return self._path / "messages.jsonl"

    @property
    def meta(self) -> MetaDict:
        """Underlying :class:`MetaDict` — escape hatch for power users.

        Prefer the dedicated properties / setters on ``Session``. Direct
        mutation of ``self.meta`` will NOT auto-save; call :meth:`save`.
        """
        return self._meta

    # ---- mutations (all persist via save()) ----

    def set_stage(self, new_stage: Stage) -> None:
        """Update ``current_stage``, append to ``stage_history``, and save.

        Per the task spec: ``set_stage`` records the stage transition (not just
        a silent set), so the history list stays accurate even if the caller
        doesn't separately call ``record_stage``.
        """
        self._meta.current_stage = new_stage
        self._meta.stage_history.append(new_stage)
        self.save()

    def set_status(self, status: SessionStatus) -> None:
        """Update ``status`` and save."""
        self._meta.status = status
        self.save()

    # ---- current_run (real-time in-flight marker) ----

    @property
    def current_run(self) -> dict | None:
        return self._meta.current_run

    def set_current_run(self, stage: Stage) -> None:
        """Mark that a handler is currently executing ``stage``. Saved immediately.

        Cleared by :meth:`clear_current_run` when the handler returns. The
        UI uses this to show "⟳ research" instead of "✓ research" while a
        stage is actively running.
        """
        from datetime import datetime, timezone

        self._meta.current_run = {
            "stage": stage.value if hasattr(stage, "value") else str(stage),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()

    def clear_current_run(self) -> None:
        """Clear the in-flight marker. Called when a handler finishes."""
        self._meta.current_run = None
        self.save()

    def record_stage(self, stage: Stage) -> None:
        """Alias for :meth:`set_stage` — semantically "this stage completed".

        Provided for readability at call sites that want to emphasize the
        milestone nature of the transition (vs. an ad-hoc ``set_stage``).
        """
        self.set_stage(stage)

    # ---- persistence ----

    def save(self) -> None:
        """Write ``meta.json`` atomically under the session FileLock.

        Performs a **read-merge-write** so external writers (most notably
        :meth:`Artifacts.write` bumping ``version_counts``) are preserved
        across save cycles. Fields the Session mutates directly
        (``current_stage``, ``status``, ``stage_history``, ``customer``)
        come from the in-memory copy; ``version_counts`` is merged as
        ``{**disk, **memory}`` so neither side's contributions are lost.

        Uses the write-rename pattern from spec §4.3: serialize to
        ``meta.json.tmp`` then :meth:`Path.replace` onto ``meta.json``.
        """
        self._path.mkdir(parents=True, exist_ok=True)
        lock_path = self._path / ".lock"
        with FileLock(lock_path):
            self._meta = self._merge_for_save(self._read_disk_meta_unsafe(), self._meta)
            self._save_unsafe()

    def _save_unsafe(self) -> None:
        """Write the current ``self._meta`` to disk atomically (no merge).

        Caller MUST already hold the FileLock for this session's ``.lock``
        file. Used by :meth:`Artifacts._bump_version_count_locked`, which
        loads disk meta, mutates ``version_counts`` in memory, and wants a
        straight write — no need to re-read disk and re-merge because the
        lock guarantees no concurrent writer has changed anything since the
        load.
        """
        meta_path = self._path / "meta.json"
        tmp_path = self._path / "meta.json.tmp"
        payload = json.dumps(meta_to_dict(self._meta), ensure_ascii=False, indent=2)
        tmp_path.write_text(payload, encoding="utf-8")
        # os.replace / Path.replace is atomic on both POSIX and Windows
        # (Win32 MoveFileEx with MOVEFILE_REPLACE_EXISTING).
        tmp_path.replace(meta_path)

    def _read_disk_meta_unsafe(self) -> MetaDict | None:
        """Read meta.json from disk under the (held) lock.

        Returns ``None`` if the file is missing, corrupted, or has a
        mismatched schema_version. Caller should fall back to in-memory
        state in those cases (schema migrations are an orchestrator concern,
        not the storage layer's).
        """
        meta_path = self._path / "meta.json"
        if not meta_path.is_file():
            return None
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta_from_dict(raw)
        except (json.JSONDecodeError, SchemaMismatchError):
            return None

    @staticmethod
    def _merge_for_save(
        disk_meta: MetaDict | None,
        memory_meta: MetaDict,
    ) -> MetaDict:
        """Combine disk state (external writes) with in-memory state.

        Authoritative fields (in-memory wins):
            ``current_stage``, ``status``, ``stage_history``, ``customer``,
            ``current_run``, and the immutable ``schema_version`` /
            ``session_id`` / ``created_at``.

        External-owned field (disk wins, augmented by memory):
            ``version_counts`` — merged as ``{**disk, **memory}`` so both
            writers' contributions survive.
        """
        if disk_meta is None:
            return memory_meta.model_copy(deep=True)
        return MetaDict(
            schema_version=memory_meta.schema_version,
            session_id=memory_meta.session_id,
            created_at=memory_meta.created_at,
            customer=memory_meta.customer,
            current_stage=memory_meta.current_stage,
            status=memory_meta.status,
            stage_history=list(memory_meta.stage_history),
            version_counts={**disk_meta.version_counts, **memory_meta.version_counts},
            current_run=memory_meta.current_run,
        )

    # ---- destructive ----

    def delete(self, confirm: bool = True) -> None:
        """Recursively remove the session directory.

        Refuses to act unless ``confirm=True`` (default). Intended for test
        cleanup and explicit user-initiated deletion in the UI (P7).
        """
        if not confirm:
            raise RuntimeError(
                "Session.delete is destructive; pass confirm=True to proceed."
            )
        if self._path.is_dir():
            shutil.rmtree(self._path)

    def __repr__(self) -> str:
        return (
            f"Session(id={self.id!r}, stage={self.stage.value}, "
            f"status={self.status.value}, customer={self.customer!r})"
        )