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


class OrchestratorDashboardSessionArchive:
    """Persists lightweight operator-facing metadata for ChatGPT conversations."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._config.ensure_state_layout()
        self._lock = FileLock(str(self._config.dashboard_sessions_dir / ".store.lock"))

    @staticmethod
    def panel_id_for_session(session_id: str) -> str:
        """Returns the stable dashboard panel identifier for one ChatGPT session."""
        return uuid.uuid5(uuid.NAMESPACE_URL, f"orchestrator:{session_id}").hex[:16]

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
        return normalized

    def list_sessions(self) -> list[dict[str, Any]]:
        """Returns retained conversation metadata newest first."""
        with self._lock:
            records = [self._read_path(path) for path in self._config.dashboard_sessions_dir.glob("*.json")]
        records.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
        return records

    def _path(self, panel_id: str) -> Path:
        return self._config.dashboard_sessions_dir / f"{panel_id}.json"

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
