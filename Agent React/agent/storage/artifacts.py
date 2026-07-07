"""Artifacts: per-session, per-stage, versioned markdown storage.

Files live at ``<session>/artifacts/<stage>/<name>_v<N>.md`` (1-indexed).
Version counts are mirrored into ``meta.version_counts[f"{stage}/{name}"]``
so the orchestrator can read the latest version without walking the tree.
"""
from __future__ import annotations

import re
from pathlib import Path

from .lock import FileLock
from .paths import get_session_dir
from .schema import Stage


class ArtifactNotFound(Exception):
    """Raised when an artifact (specific version or latest) cannot be located."""


# Match "<name>_v<N>.md" where <name> may contain word chars, hyphens, dots,
# underscores. The version is captured as group "n" and name as group "name".
_VERSION_SUFFIX_RE = re.compile(r"^(?P<name>.+?)_v(?P<n>\d+)\.md$")


class Artifacts:
    """Bound to a ``session_id``; manages versioned markdown artifacts."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    # ---- path helpers ----

    @property
    def artifacts_root(self) -> Path:
        return get_session_dir(self.session_id) / "artifacts"

    def _stage_dir(self, stage: str) -> Path:
        return self.artifacts_root / stage

    def _lock_path(self) -> Path:
        # Single lock file per session — serializes all file ops for the session.
        return get_session_dir(self.session_id) / ".lock"

    # ---- write / read ----

    def write(self, stage: str, name: str, content: str) -> int:
        """Write a new version of ``<stage>/<name>``.

        Returns the new version number (1-indexed). Bumps
        ``meta.version_counts[f"{stage}/{name}"]`` so callers can introspect
        the latest version without re-scanning the directory.
        """
        stage_dir = self._stage_dir(stage)
        with FileLock(self._lock_path()):
            stage_dir.mkdir(parents=True, exist_ok=True)
            next_v = self._next_version_locked(stage, name)
            versioned = stage_dir / f"{name}_v{next_v}.md"
            versioned.write_text(content, encoding="utf-8")
            self._bump_version_count_locked(stage, name, next_v)
        return next_v

    def read(self, stage: str, name: str, version: int | None = None) -> str:
        """Read a specific version, or the latest when ``version`` is ``None``.

        Raises:
            ArtifactNotFound: no versions exist, or the requested version is
                missing on disk.
        """
        with FileLock(self._lock_path()):
            if version is None:
                latest = self._latest_version_locked(stage, name)
                if latest is None:
                    raise ArtifactNotFound(
                        f"No versions found for {stage}/{name} in session {self.session_id}"
                    )
                version = latest
            path = self._stage_dir(stage) / f"{name}_v{version}.md"
            if not path.is_file():
                raise ArtifactNotFound(f"Artifact not found: {path}")
            return path.read_text(encoding="utf-8")

    def read_latest(self, stage: str, name: str) -> str:
        """Convenience wrapper for :meth:`read` with ``version=None``."""
        return self.read(stage, name, version=None)

    def list_versions(self, stage: str, name: str) -> list[int]:
        """Sorted ascending list of version numbers for ``(stage, name)``."""
        stage_dir = self._stage_dir(stage)
        if not stage_dir.is_dir():
            return []
        versions: list[int] = []
        for f in stage_dir.iterdir():
            if not f.is_file():
                continue
            m = _VERSION_SUFFIX_RE.match(f.name)
            if m and m.group("name") == name:
                versions.append(int(m.group("n")))
        return sorted(versions)

    def list_artifacts(self) -> list[tuple[Stage, str, int]]:
        """Full listing: ``[(Stage, name, latest_version), ...]``.

        Walks ``artifacts/<stage>/`` and infers the latest version from
        filenames. Stage directories whose name does not match a known
        :class:`~agent.storage.schema.Stage` value are skipped silently
        (storage layer doesn't validate against arbitrary stage names on
        write; this is just a listing convenience).
        """
        result: list[tuple[Stage, str, int]] = []
        root = self.artifacts_root
        if not root.is_dir():
            return result
        for stage_dir in sorted(root.iterdir()):
            if not stage_dir.is_dir():
                continue
            try:
                stage_enum = Stage(stage_dir.name)
            except ValueError:
                # Unknown stage dir — skip rather than coerce to a synthetic Stage.
                continue
            name_versions: dict[str, int] = {}
            for f in stage_dir.iterdir():
                if not f.is_file():
                    continue
                m = _VERSION_SUFFIX_RE.match(f.name)
                if m:
                    n = m.group("name")
                    v = int(m.group("n"))
                    if n not in name_versions or v > name_versions[n]:
                        name_versions[n] = v
            for n in sorted(name_versions):
                result.append((stage_enum, n, name_versions[n]))
        return result

    # ---- internal (must run under FileLock) ----

    def _next_version_locked(self, stage: str, name: str) -> int:
        existing = self.list_versions(stage, name)
        return (max(existing) + 1) if existing else 1

    def _latest_version_locked(self, stage: str, name: str) -> int | None:
        existing = self.list_versions(stage, name)
        return max(existing) if existing else None

    def _bump_version_count_locked(self, stage: str, name: str, version: int) -> None:
        """Update meta.version_counts[f"{stage}/{name}"] = version.

        Uses ``Session._save_unsafe`` because we already hold the FileLock;
        re-locking from inside would deadlock on Windows (msvcrt.locking is
        not reentrant for separate file descriptors).
        """
        # Local import avoids a circular dependency at module load time.
        from .session import Session

        session = Session.load(self.session_id)
        session._meta.version_counts[f"{stage}/{name}"] = version
        session._save_unsafe()