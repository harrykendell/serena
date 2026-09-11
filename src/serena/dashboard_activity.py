"""Persistent ChatGPT-session activity retained for the Serena dashboard."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from serena.activity_view import ActivityDetailFormatter
from serena.execution_store import ExecutionRecord, ExecutionStore, SessionRecord

_FILE_RESOURCE_RE = re.compile(r"serena-file://export/([0-9a-f]{64}|[0-9a-f]{48})(?![0-9a-f])")
_PANEL_ID_RE = re.compile(r"[0-9a-f]{16}")


class DashboardActivityArchive:
    """Projects canonical execution-store state into the existing Serena dashboard session shape."""

    def __init__(self, execution_store: ExecutionStore | None = None) -> None:
        self._store = execution_store or ExecutionStore()
        self._formatter = ActivityDetailFormatter()

    def list_sessions(self) -> list[dict[str, Any]]:
        """Returns retained dashboard session records newest first."""
        return [self._session_payload(session) for session in self._store.list_sessions()]

    def get_session(self, panel_id: str) -> dict[str, Any]:
        """Returns one retained session by its opaque dashboard identifier."""
        if _PANEL_ID_RE.fullmatch(panel_id) is None:
            raise KeyError(panel_id)
        session = self._store.get_session_by_panel_id(panel_id)
        if session is None:
            raise KeyError(panel_id)
        return self._session_payload(session)

    def set_display_name(self, session_id: str, display_name: str) -> str:
        """Sets the operator-facing name for one retained ChatGPT conversation."""
        return self._store.set_session_display_name(session_id, display_name)

    def get_call(self, panel_id: str, call_id: str) -> dict[str, Any]:
        """Returns one canonical execution projected as a dashboard call."""
        session = self.get_session(panel_id)
        for call in session.get("calls", []):
            if call.get("call_id") == call_id:
                return call
        raise KeyError(call_id)

    @classmethod
    def retained_file_tokens_from_disk(cls) -> set[str]:
        """Returns retained snapshot tokens from canonical execution persistence."""
        return ExecutionStore.retained_file_tokens_from_disk()

    def retained_file_tokens(self) -> set[str]:
        """Returns snapshot tokens referenced by retained canonical executions."""
        return self._store.retained_file_tokens()

    def _session_payload(self, session: SessionRecord) -> dict[str, Any]:
        executions = self._store.list_session_executions(session.session_id)
        calls = [self._call_payload(record) for record in executions]
        file_tokens: set[str] = set()
        for record in executions:
            if record.media is not None:
                file_tokens.update(_FILE_RESOURCE_RE.findall(str(record.media.get("uri") or "")))
            if record.result:
                file_tokens.update(_FILE_RESOURCE_RE.findall(record.result))
        return {
            "version": 2,
            "panel_id": session.panel_id,
            "session_id": session.session_id,
            "project_name": session.project_name,
            "display_name": session.display_name,
            "started_at": session.created_at,
            "updated_at": session.updated_at,
            "calls": calls,
            "file_tokens": sorted(file_tokens),
        }

    def _call_payload(self, record: ExecutionRecord) -> dict[str, Any]:
        summary = self._formatter.format(record.tool_name, record.arguments)
        return {
            "call_id": record.execution_id,
            "tool_name": record.tool_name,
            "detail": summary.detail,
            "scope": summary.scope,
            "status": record.status,
            "submitted_at": record.started_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "parameters": json.dumps(record.arguments, ensure_ascii=False, separators=(",", ":")),
            "result": record.result,
            "error": record.error,
            "media": record.media,
            "project_name": record.project_name,
            "job_id": record.durable_job_id,
        }

    @staticmethod
    def panel_id_for_session(session_id: str) -> str:
        """Returns the public opaque panel identifier for tests and adapters."""
        return ExecutionStore.panel_id_for_session(session_id)

    @staticmethod
    def call_fingerprint(task_name: str, session_id: str) -> str:
        """Returns a stable diagnostic fingerprint without exposing the session identifier."""
        return hashlib.sha256(f"{session_id}:{task_name}".encode()).hexdigest()[:16]
