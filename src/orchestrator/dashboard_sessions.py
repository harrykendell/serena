"""Persistent ChatGPT-session metadata for the Orchestrator dashboard."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock

from orchestrator.config import OrchestratorConfig


def dashboard_panel_id_for_session(session_id: str) -> str:
    """Returns the stable dashboard panel identifier for one Orchestrator session."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"orchestrator:{session_id}").hex[:16]


class OrchestratorDashboardSessionArchive:
    """Persists lightweight operator-facing metadata for ChatGPT conversations."""

    _REVISION_FILENAME = ".dashboard-revision"

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._config.ensure_state_layout()
        self._lock = FileLock(str(self._config.dashboard_sessions_dir / ".store.lock"))
        self._cached_revision: str | None = None
        self._cached_sessions: tuple[dict[str, Any], ...] = ()
        with self._lock:
            if not self._revision_path().is_file():
                self._touch_revision()

    @staticmethod
    def panel_id_for_session(session_id: str) -> str:
        """Returns the stable dashboard panel identifier for one ChatGPT session."""
        return dashboard_panel_id_for_session(session_id)

    def set_display_name(self, session_id: str, display_name: str) -> str:
        """Sets the retained dashboard name for one ChatGPT conversation."""
        normalized = " ".join(display_name.split())
        if not normalized:
            raise ValueError("Conversation names must not be empty")
        if len(normalized) > 80:
            raise ValueError("Conversation names must be at most 80 characters")

        now = time.time()
        panel_id = self.panel_id_for_session(session_id)
        path = self._path(panel_id)
        with self._lock:
            existing = self._read_path(path) if path.is_file() else None
            record = {
                "version": 1,
                "panel_id": panel_id,
                "session_id": session_id,
                "display_name": normalized,
                "started_at": float(existing.get("started_at", now)) if existing is not None else now,
                "updated_at": now,
            }
            self._write_path(path, record)
            self._touch_revision()
            self._cached_revision = None
        return normalized

    def list_sessions(self) -> list[dict[str, Any]]:
        """Returns retained conversation metadata newest first without rescanning unchanged files."""
        with self._lock:
            revision = self._revision_path().read_text(encoding="utf-8").strip()
            if revision != self._cached_revision:
                records = [self._read_path(path) for path in self._config.dashboard_sessions_dir.glob("*.json")]
                records.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
                self._cached_sessions = tuple(records)
                self._cached_revision = revision
            return [dict(record) for record in self._cached_sessions]

    def get_session(self, panel_id: str) -> dict[str, Any] | None:
        """Returns one retained conversation directly by dashboard panel identifier."""
        path = self._path(panel_id)
        with self._lock:
            if not path.is_file():
                return None
            return self._read_path(path)

    def dashboard_revision(self, panel_id: str | None = None) -> str:
        """Returns cheap invalidation state for all sessions or one selected session."""
        with self._lock:
            if panel_id is None:
                return self._revision_path().read_text(encoding="utf-8").strip()
            try:
                stat = self._path(panel_id).stat()
            except FileNotFoundError:
                return "0"
            return f"{stat.st_mtime_ns}:{stat.st_size}"

    def _path(self, panel_id: str) -> Path:
        return self._config.dashboard_sessions_dir / f"{panel_id}.json"

    def _revision_path(self) -> Path:
        return self._config.dashboard_sessions_dir / self._REVISION_FILENAME

    @staticmethod
    def _read_path(path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid Orchestrator dashboard session record: {path}")
        return data

    @staticmethod
    def _write_path(path: Path, record: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _touch_revision(self) -> None:
        """Atomically advances the global dashboard-session revision marker."""
        path = self._revision_path()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(uuid.uuid4().hex, encoding="utf-8")
        os.replace(temporary, path)
