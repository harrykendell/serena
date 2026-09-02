"""Persistent ChatGPT-session activity retained for the Serena dashboard."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_FILE_RESOURCE_RE = re.compile(r"serena-file://export/([0-9a-f]{48})")
_JOB_ID_RE = re.compile(r"['\"]job_id['\"]\s*:\s*['\"]([^'\"]+)['\"]")
_MAX_SESSIONS = 128
_MAX_CALLS_PER_SESSION = 500


class DashboardActivityArchive:
    """Persists bounded Serena tool activity grouped by ChatGPT conversation."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or self._default_root()
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise RuntimeError("Serena dashboard activity archive is not a private directory")
        if self._root.stat().st_mode & 0o077:
            self._root.chmod(0o700)
        self._lock = threading.RLock()
        self._instance_id = uuid.uuid4().hex
        self._call_by_task: dict[str, tuple[str, str]] = {}
        self._interrupt_stale_calls()

    @staticmethod
    def _default_root() -> Path:
        """Returns the persistent private archive directory."""
        configured_home = os.getenv("SERENA_HOME", "").strip()
        serena_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".serena"
        return serena_home / "dashboard_activity_sessions"

    @staticmethod
    def _panel_id(session_id: str) -> str:
        """Returns an opaque stable dashboard identifier for one ChatGPT conversation."""
        return uuid.uuid5(uuid.NAMESPACE_URL, f"serena-dashboard:{session_id}").hex[:16]

    def record_start(
        self,
        *,
        task_name: str,
        session_id: str,
        tool_name: str,
        parameters: str,
        detail: str,
        project_name: str | None,
        scope: str = "",
        timestamp: float | None = None,
    ) -> str:
        """Records one started tool call and returns its persistent call identifier."""
        now = timestamp or time.time()
        call_id = uuid.uuid4().hex
        with self._lock:
            session = self._read_session(session_id)
            session["project_name"] = project_name or session.get("project_name") or ""
            session["started_at"] = min(float(session.get("started_at", now)), now)
            session["updated_at"] = now
            calls = session.setdefault("calls", [])
            calls.append(
                {
                    "call_id": call_id,
                    "instance_id": self._instance_id,
                    "task_name": task_name,
                    "tool_name": tool_name,
                    "detail": detail,
                    "scope": scope,
                    "status": "running",
                    "submitted_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "parameters": parameters,
                    "result": None,
                    "error": None,
                    "project_name": project_name or "",
                    "job_id": None,
                }
            )
            if len(calls) > _MAX_CALLS_PER_SESSION:
                del calls[: len(calls) - _MAX_CALLS_PER_SESSION]
            session["file_tokens"] = sorted(self._extract_file_tokens(session))
            self._write_session(session)
            self._call_by_task[task_name] = (session_id, call_id)
            self._prune()
        return call_id

    def record_result(self, task_name: str, *, result: str | None = None, error: str | None = None) -> None:
        """Marks one observed tool call terminal and persists its bounded result."""
        with self._lock:
            owner = self._call_by_task.get(task_name)
            if owner is None:
                owner = self._find_task_owner(task_name)
            if owner is None:
                return
            session_id, call_id = owner
            session = self._read_session(session_id)
            call = next((item for item in session.get("calls", []) if item.get("call_id") == call_id), None)
            if call is None:
                return
            call["status"] = "failed" if error is not None else "completed"
            call["finished_at"] = time.time()
            call["result"] = result
            call["error"] = error
            if result and str(call.get("tool_name")) == "start_job":
                match = _JOB_ID_RE.search(result)
                if match is not None:
                    call["job_id"] = match.group(1)
            session["updated_at"] = call["finished_at"]
            session["file_tokens"] = sorted(self._extract_file_tokens(session))
            self._write_session(session)

    def reconcile_executions(self, executions: Iterable[dict[str, Any]]) -> None:
        """Reconciles current-process task timing and cancellation state into the persistent archive."""
        by_task = {str(item.get("name")): item for item in executions if item.get("name")}
        with self._lock:
            for path in self._session_paths():
                session = self._read_path(path)
                session_changed = False
                for call in session.get("calls", []):
                    if call.get("instance_id") != self._instance_id:
                        continue
                    item = by_task.get(str(call.get("task_name")))
                    if item is None:
                        continue

                    call_changed = False
                    for key, source in (
                        ("submitted_at", "submitted_at"),
                        ("started_at", "started_at"),
                        ("finished_at", "finished_at"),
                        ("project_name", "project"),
                    ):
                        value = item.get(source)
                        if value is not None and call.get(key) != value:
                            call[key] = value
                            call_changed = True
                    status = item.get("status")
                    if status and call.get("status") != status:
                        call["status"] = status
                        call_changed = True
                    if call_changed:
                        session_changed = True
                if session_changed:
                    session["updated_at"] = time.time()
                    self._write_path(path, session)

    def list_sessions(self) -> list[dict[str, Any]]:
        """Returns retained session records newest first."""
        with self._lock:
            sessions = [self._read_path(path) for path in self._session_paths()]
        sessions.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
        return sessions

    def get_session(self, panel_id: str) -> dict[str, Any]:
        """Returns one retained session by its opaque dashboard identifier."""
        for session in self.list_sessions():
            if session.get("panel_id") == panel_id:
                return session
        raise KeyError(panel_id)

    def set_display_name(self, session_id: str, display_name: str) -> str:
        """Sets the operator-facing name for one retained ChatGPT conversation."""
        normalized = " ".join(display_name.split())
        if not normalized:
            raise ValueError("Conversation names must not be empty")
        if len(normalized) > 80:
            raise ValueError("Conversation names must be at most 80 characters")

        with self._lock:
            session = self._read_session(session_id)
            session["display_name"] = normalized
            session["updated_at"] = time.time()
            self._write_session(session)
        return normalized

    def get_call(self, panel_id: str, call_id: str) -> dict[str, Any]:
        """Returns one persisted call belonging to a retained session panel."""
        session = self.get_session(panel_id)
        for call in session.get("calls", []):
            if call.get("call_id") == call_id:
                return call
        raise KeyError(call_id)

    @classmethod
    def retained_file_tokens_from_disk(cls) -> set[str]:
        """Returns retained snapshot tokens without mutating archive lifecycle state."""
        root = cls._default_root()
        if not root.is_dir():
            return set()
        tokens: set[str] = set()
        for path in root.glob("*.json"):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            tokens.update(str(token) for token in value.get("file_tokens", []))
        return tokens

    def retained_file_tokens(self) -> set[str]:
        """Returns snapshot tokens referenced by retained ChatGPT sessions."""
        tokens: set[str] = set()
        for session in self.list_sessions():
            tokens.update(str(token) for token in session.get("file_tokens", []))
        return tokens

    def _read_session(self, session_id: str) -> dict[str, Any]:
        panel_id = self._panel_id(session_id)
        path = self._root / f"{panel_id}.json"
        if path.is_file():
            return self._read_path(path)
        now = time.time()
        return {
            "version": 1,
            "panel_id": panel_id,
            "session_id": session_id,
            "project_name": "",
            "started_at": now,
            "updated_at": now,
            "calls": [],
            "file_tokens": [],
        }

    def _find_task_owner(self, task_name: str) -> tuple[str, str] | None:
        for path in self._session_paths():
            session = self._read_path(path)
            for call in reversed(session.get("calls", [])):
                if call.get("instance_id") == self._instance_id and call.get("task_name") == task_name and call.get("status") == "running":
                    owner = (str(session["session_id"]), str(call["call_id"]))
                    self._call_by_task[task_name] = owner
                    return owner
        return None

    def _interrupt_stale_calls(self) -> None:
        now = time.time()
        with self._lock:
            for path in self._session_paths():
                session = self._read_path(path)
                changed = False
                for call in session.get("calls", []):
                    if call.get("status") != "running":
                        continue
                    call["status"] = "failed"
                    call["finished_at"] = now
                    call["error"] = "Serena restarted before this tool call reached a terminal state."
                    changed = True
                if changed:
                    session["updated_at"] = now
                    self._write_path(path, session)
            self._prune()

    def _prune(self) -> None:
        paths = self._session_paths()
        if len(paths) <= _MAX_SESSIONS:
            return
        candidates: list[tuple[float, Path]] = []
        for path in paths:
            session = self._read_path(path)
            if any(call.get("status") == "running" for call in session.get("calls", [])):
                continue
            candidates.append((float(session.get("updated_at", 0.0)), path))
        candidates.sort()
        excess = len(paths) - _MAX_SESSIONS
        for _, path in candidates[:excess]:
            path.unlink(missing_ok=True)

    def _session_paths(self) -> list[Path]:
        return [path for path in self._root.glob("*.json") if path.is_file() and not path.name.startswith(".")]

    def _write_session(self, session: dict[str, Any]) -> None:
        self._write_path(self._root / f"{session['panel_id']}.json", session)

    @staticmethod
    def _read_path(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read Serena dashboard activity archive {path}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"Invalid Serena dashboard activity archive {path}")
        return value

    @staticmethod
    def _write_path(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _extract_file_tokens(session: dict[str, Any]) -> set[str]:
        tokens: set[str] = set()
        for call in session.get("calls", []):
            for key in ("parameters", "result", "error"):
                value = call.get(key)
                if value:
                    tokens.update(_FILE_RESOURCE_RE.findall(str(value)))
        return tokens

    @staticmethod
    def panel_id_for_session(session_id: str) -> str:
        """Returns the public opaque panel identifier for tests and adapters."""
        return DashboardActivityArchive._panel_id(session_id)

    @staticmethod
    def call_fingerprint(task_name: str, session_id: str) -> str:
        """Returns a stable diagnostic fingerprint without exposing the session identifier."""
        return hashlib.sha256(f"{session_id}:{task_name}".encode()).hexdigest()[:16]
