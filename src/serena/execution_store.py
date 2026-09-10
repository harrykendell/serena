from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from serena.retention import DEFAULT_SESSION_RETENTION, JobRetentionState, SessionRetentionPolicy
from serena.storage_compression import RetainedTextCompression
from serena.structured_output import StructuredOutputCompactor

_FILE_RESOURCE_RE = re.compile(r"serena-file://export/([0-9a-f]{64}|[0-9a-f]{48})(?![0-9a-f])")
_JOB_ID_RE = re.compile(r'"job_id"\s*:\s*"([0-9a-f]{32})"')
_STATE_VERSION = 2


@dataclass
class SessionRecord:
    """Persistent metadata for one client session."""

    session_id: str
    panel_id: str
    created_at: float
    updated_at: float
    display_name: str = ""
    project_name: str = ""


@dataclass
class ExecutionRecord:
    """Authoritative lifecycle record for one model-visible Serena tool invocation."""

    execution_id: str
    session_id: str
    project_name: str
    tool_name: str
    arguments: str
    started_at: float
    status: str = "running"
    finished_at: float | None = None
    request_finished_at: float | None = None
    request_error: str | None = None
    result: str | None = None
    error: str | None = None
    retained_output_id: str | None = None
    retained_output_chars: int | None = None
    media: dict[str, str] | None = None
    durable_job_id: str | None = None


@dataclass
class ActivityPanelRun:
    """Persistent grouping of execution identifiers shown by one inline activity panel."""

    run_id: str
    session_id: str
    project_name: str
    started_at: float
    superseded: bool = False
    execution_ids: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    retained_jobs: list[dict[str, Any]] = field(default_factory=list)


class ExecutionStore:
    """Owns persistent Serena session, execution and activity-panel state.

    The store is the single persistence boundary for model-visible tool execution state. Legacy
    activity/dashboard JSON files are imported once and removed only after the canonical state has
    been written successfully.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        retention: SessionRetentionPolicy = DEFAULT_SESSION_RETENTION,
    ) -> None:
        use_default_root = root is None
        self._root = root or self._default_root()
        self._state_path = self._root / "state.json"
        self._retention = retention
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionRecord] = {}
        self._executions: dict[str, ExecutionRecord] = {}
        self._activity_runs: dict[str, ActivityPanelRun] = {}
        self._current_run_by_session: dict[str, str] = {}
        self._running_job_sessions: dict[str, str] = {}
        self._load()
        self._interrupt_stale_state()
        if self._prune():
            self._save()
        if use_default_root:
            self._cleanup_unreferenced_artifacts()

    @staticmethod
    def _serena_home() -> Path:
        configured_home = os.getenv("SERENA_HOME", "").strip()
        return Path(configured_home).expanduser() if configured_home else Path.home() / ".serena"

    @classmethod
    def _default_root(cls) -> Path:
        return cls._serena_home() / "execution_store"

    @staticmethod
    def panel_id_for_session(session_id: str) -> str:
        """Returns the stable opaque dashboard identifier for ``session_id``."""
        return uuid.uuid5(uuid.NAMESPACE_URL, f"serena-dashboard:{session_id}").hex[:16]

    @staticmethod
    def serialize_value(value: object) -> str:
        """Serializes one bounded display-safe execution field for persistence."""
        return StructuredOutputCompactor().serialize_for_storage(value, max_chars=8_000)

    def start_execution(
        self,
        *,
        execution_id: str,
        session_id: str,
        project_name: str,
        tool_name: str,
        arguments: str,
        started_at: float | None = None,
    ) -> ExecutionRecord:
        """Creates one running execution record before dispatch leaves the MCP event loop."""
        now = started_at or time.time()
        with self._lock:
            self._prune()
            session = self._ensure_session(session_id, now)
            if project_name:
                session.project_name = project_name
            session.updated_at = now
            record = ExecutionRecord(
                execution_id=execution_id,
                session_id=session_id,
                project_name=project_name,
                tool_name=tool_name,
                arguments=arguments,
                started_at=now,
            )
            self._executions[execution_id] = record
            self._prune()
            self._save()
            return record

    def mark_request_abandoned(
        self,
        execution_id: str,
        *,
        error: str,
        request_finished_at: float | None = None,
    ) -> None:
        """Records that a model-visible request ended while its worker is still running."""
        now = request_finished_at or time.time()
        with self._lock:
            record = self._executions.get(execution_id)
            if record is None or record.status not in {"running", "queued"}:
                return
            if record.request_finished_at is not None:
                return
            record.request_finished_at = now
            record.request_error = error
            session = self._ensure_session(record.session_id, record.started_at)
            session.updated_at = max(session.updated_at, now)
            self._save()

    def finish_execution(
        self,
        execution_id: str,
        *,
        succeeded: bool,
        result: str | None = None,
        error: str | None = None,
        project_name: str | None = None,
        retained_output_id: str | None = None,
        retained_output_chars: int | None = None,
        media: dict[str, str] | None = None,
        durable_job_id: str | None = None,
        finished_at: float | None = None,
    ) -> None:
        """Marks one execution terminal after its underlying worker has actually stopped."""
        now = finished_at or time.time()
        with self._lock:
            record = self._executions.get(execution_id)
            if record is None:
                return

            # finalize worker lifecycle without erasing an earlier request timeout/cancellation
            record.status = "completed" if succeeded else "failed"
            record.finished_at = now
            if record.request_finished_at is None:
                record.request_finished_at = now
            if record.request_error is None:
                record.request_error = error
            record.result = result if media is None else None
            record.error = record.request_error or error
            record.retained_output_id = retained_output_id
            record.retained_output_chars = retained_output_chars
            record.media = media
            record.durable_job_id = durable_job_id or self._extract_job_id(result)
            if project_name is not None:
                record.project_name = project_name

            session = self._ensure_session(record.session_id, record.started_at)
            if record.project_name:
                session.project_name = record.project_name
            session.updated_at = now
            self._prune()
            self._save()

    def set_retained_output(self, execution_id: str, output_id: str | None, total_chars: int | None) -> None:
        """Associates retained-output metadata with one existing execution."""
        if output_id is None:
            return
        with self._lock:
            record = self._executions.get(execution_id)
            if record is None:
                return
            record.retained_output_id = output_id
            record.retained_output_chars = total_chars
            self._save()

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        """Returns one execution record if retained."""
        with self._lock:
            record = self._executions.get(execution_id)
            return ExecutionRecord(**asdict(record)) if record is not None else None

    def list_executions(self, *, newest_first: bool = True, limit: int | None = None) -> list[ExecutionRecord]:
        """Returns retained executions ordered by submission time."""
        with self._lock:
            records = sorted(self._executions.values(), key=lambda item: item.started_at, reverse=newest_first)
            if limit is not None:
                records = records[:limit]
            return [ExecutionRecord(**asdict(record)) for record in records]

    def list_session_executions(self, session_id: str) -> list[ExecutionRecord]:
        """Returns executions belonging to one session from oldest to newest."""
        records = [record for record in self.list_executions(newest_first=False) if record.session_id == session_id]
        return records

    def set_session_display_name(self, session_id: str, display_name: str) -> str:
        """Sets the normalized operator-facing conversation title."""
        normalized = " ".join(display_name.split())
        if not normalized:
            raise ValueError("Conversation names must not be empty")
        if len(normalized) > 80:
            raise ValueError("Conversation names must be at most 80 characters")
        with self._lock:
            session = self._ensure_session(session_id, time.time())
            session.display_name = normalized
            session.updated_at = time.time()
            self._save()
        return normalized

    def update_session_project(self, session_id: str, project_name: str) -> None:
        """Updates the latest project identity associated with one session."""
        if not project_name:
            return
        with self._lock:
            session = self._ensure_session(session_id, time.time())
            session.project_name = project_name
            session.updated_at = time.time()
            self._save()

    def list_sessions(self) -> list[SessionRecord]:
        """Returns retained sessions from newest to oldest update time."""
        with self._lock:
            if self._prune():
                self._save()
            sessions = sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)
            return [SessionRecord(**asdict(session)) for session in sessions]

    def get_session_by_panel_id(self, panel_id: str) -> SessionRecord | None:
        """Returns one retained session by its dashboard panel identifier."""
        with self._lock:
            for session in self._sessions.values():
                if session.panel_id == panel_id:
                    return SessionRecord(**asdict(session))
        return None

    def start_activity_run(self, session_id: str, project_name: str) -> ActivityPanelRun:
        """Starts a panel run and carries any currently running executions from its predecessor."""
        now = time.time()
        with self._lock:
            self._prune()
            previous_id = self._current_run_by_session.get(session_id)
            continuing: list[str] = []
            if previous_id is not None and (previous := self._activity_runs.get(previous_id)) is not None:
                previous.superseded = True
                continuing = [
                    execution_id
                    for execution_id in previous.execution_ids
                    if (record := self._executions.get(execution_id)) is not None and record.status in {"running", "queued"}
                ]
            run = ActivityPanelRun(
                run_id=uuid.uuid4().hex,
                session_id=session_id,
                project_name=project_name,
                started_at=now,
                execution_ids=continuing,
            )
            self._activity_runs[run.run_id] = run
            self._current_run_by_session[session_id] = run.run_id
            self._ensure_session(session_id, now)
            self._save()
            return ActivityPanelRun(**asdict(run))

    def append_execution_to_current_run(self, session_id: str, execution_id: str, *, project_name: str = "") -> None:
        """Adds one execution identifier to the active panel run for ``session_id`` if present."""
        with self._lock:
            run_id = self._current_run_by_session.get(session_id)
            run = self._activity_runs.get(run_id) if run_id is not None else None
            if run is None:
                return
            if execution_id not in run.execution_ids:
                run.execution_ids.append(execution_id)
            if project_name:
                run.project_name = project_name
            self._save()

    def update_activity_run_project(self, session_id: str, project_name: str) -> None:
        """Updates the active panel run's project label."""
        if not project_name:
            return
        with self._lock:
            run_id = self._current_run_by_session.get(session_id)
            run = self._activity_runs.get(run_id) if run_id is not None else None
            if run is not None:
                run.project_name = project_name
                self._save()

    def update_activity_run_jobs(
        self,
        run_id: str,
        *,
        job_ids: list[str] | None = None,
        retained_jobs: list[dict[str, Any]] | None = None,
    ) -> None:
        """Updates durable-job references retained by one panel run."""
        with self._lock:
            run = self._activity_runs.get(run_id)
            if run is None:
                return
            if job_ids is not None:
                run.job_ids = list(dict.fromkeys(job_ids))
            if retained_jobs is not None:
                run.retained_jobs = retained_jobs
            self._save()

    def get_activity_run(self, run_id: str) -> ActivityPanelRun | None:
        """Returns one retained activity-panel run."""
        with self._lock:
            run = self._activity_runs.get(run_id)
            return ActivityPanelRun(**asdict(run)) if run is not None else None

    def get_current_activity_run(self, session_id: str) -> ActivityPanelRun | None:
        """Returns the active activity-panel run for one session."""
        with self._lock:
            run_id = self._current_run_by_session.get(session_id)
            run = self._activity_runs.get(run_id) if run_id is not None else None
            return ActivityPanelRun(**asdict(run)) if run is not None else None

    def list_activity_runs(self) -> list[ActivityPanelRun]:
        """Returns retained activity-panel runs from oldest to newest."""
        with self._lock:
            runs = sorted(self._activity_runs.values(), key=lambda item: item.started_at)
            return [ActivityPanelRun(**asdict(run)) for run in runs]

    def sync_job_retention(self, jobs: list[JobRetentionState]) -> set[str]:
        """Synchronize durable-job state with unified session retention.

        Terminal job completion extends the owning session's retention window. Running jobs protect
        their owning sessions from eviction. The returned job identifiers are those still owned by
        retained sessions.
        """
        with self._lock:
            self._running_job_sessions = {}
            for job in jobs:
                session_id = job.session_id
                if session_id is None:
                    session_id = next(
                        (record.session_id for record in self._executions.values() if record.durable_job_id == job.job_id),
                        None,
                    )
                if session_id is None:
                    continue
                if job.is_running:
                    self._running_job_sessions[job.job_id] = session_id
                if job.finished_at is not None and (session := self._sessions.get(session_id)) is not None:
                    session.updated_at = max(session.updated_at, job.finished_at)

            changed = self._prune()
            if changed:
                self._save()

            return {
                record.durable_job_id
                for record in self._executions.values()
                if record.durable_job_id is not None and record.session_id in self._sessions
            }

    def retained_file_tokens(self) -> set[str]:
        """Returns snapshot tokens referenced by retained execution media/results."""
        with self._lock:
            tokens: set[str] = set()
            for record in self._executions.values():
                if record.media is not None:
                    uri = record.media.get("uri", "")
                    tokens.update(_FILE_RESOURCE_RE.findall(uri))
                if record.result:
                    tokens.update(_FILE_RESOURCE_RE.findall(record.result))
            return tokens

    @classmethod
    def retained_file_tokens_from_disk(cls) -> set[str]:
        """Returns snapshot tokens from canonical persisted state without constructing runtime services."""
        path = cls._default_root() / "state.json"
        try:
            payload = json.loads(RetainedTextCompression.read_text(path))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return set()
        tokens: set[str] = set()
        executions = payload.get("executions", {}) if isinstance(payload, dict) else {}
        if isinstance(executions, dict):
            for execution in executions.values():
                if not isinstance(execution, dict):
                    continue
                media = execution.get("media")
                if isinstance(media, dict):
                    tokens.update(_FILE_RESOURCE_RE.findall(str(media.get("uri") or "")))
                tokens.update(_FILE_RESOURCE_RE.findall(str(execution.get("result") or "")))
        return tokens

    def retained_output_ids(self) -> set[str]:
        """Returns pageable-output identifiers referenced by retained executions."""
        with self._lock:
            return {record.retained_output_id for record in self._executions.values() if record.retained_output_id is not None}

    @classmethod
    def retained_output_ids_from_disk(cls) -> set[str]:
        """Returns pageable-output identifiers referenced by canonical persisted state."""
        path = cls._default_root() / "state.json"
        try:
            with path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            return set()
        executions = payload.get("executions", {}) if isinstance(payload, dict) else {}
        if not isinstance(executions, dict):
            return set()
        return {
            str(execution.get("retained_output_id"))
            for execution in executions.values()
            if isinstance(execution, dict) and execution.get("retained_output_id")
        }

    def _ensure_session(self, session_id: str, timestamp: float) -> SessionRecord:
        session = self._sessions.get(session_id)
        if session is None:
            session = SessionRecord(
                session_id=session_id,
                panel_id=self.panel_id_for_session(session_id),
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._sessions[session_id] = session
        return session

    def _interrupt_stale_state(self) -> None:
        """Marks executions and panel runs left live by a previous Serena process as historical."""
        now = time.time()
        changed = False
        with self._lock:
            for record in self._executions.values():
                if record.status not in {"running", "queued"}:
                    continue
                record.status = "failed"
                record.finished_at = record.finished_at or now
                record.request_finished_at = record.request_finished_at or now
                record.request_error = record.request_error or "Serena restarted before this tool call reached a terminal state."
                record.error = record.error or record.request_error
                session = self._sessions.get(record.session_id)
                if session is not None:
                    session.updated_at = max(session.updated_at, record.finished_at)
                changed = True
            for run in self._activity_runs.values():
                if run.superseded:
                    continue
                run.superseded = True
                changed = True
            self._current_run_by_session.clear()
            if changed:
                self._save()

    def _load(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            payload = json.loads(RetainedTextCompression.read_text(self._state_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return

        version = payload.get("version")
        if version != _STATE_VERSION:
            raise RuntimeError(
                f"Unsupported Serena execution-store schema version {version!r}; Serena 2.1 requires schema version {_STATE_VERSION}."
            )

        sessions = payload.get("sessions", {})
        executions = payload.get("executions", {})
        activity_runs = payload.get("activity_runs", {})
        if isinstance(sessions, dict):
            for session_id, item in sessions.items():
                if isinstance(item, dict):
                    try:
                        self._sessions[str(session_id)] = SessionRecord(**item)
                    except (TypeError, ValueError):
                        continue
        if isinstance(executions, dict):
            for execution_id, item in executions.items():
                if isinstance(item, dict):
                    try:
                        self._executions[str(execution_id)] = ExecutionRecord(**item)
                    except (TypeError, ValueError):
                        continue
        if isinstance(activity_runs, dict):
            for run_id, item in activity_runs.items():
                if isinstance(item, dict):
                    try:
                        run = ActivityPanelRun(**item)
                    except (TypeError, ValueError):
                        continue
                    self._activity_runs[str(run_id)] = run
                    if not run.superseded:
                        current = self._current_run_by_session.get(run.session_id)
                        if current is None or self._activity_runs[current].started_at < run.started_at:
                            self._current_run_by_session[run.session_id] = run.run_id

    def _save(self) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "version": _STATE_VERSION,
            "sessions": {key: asdict(value) for key, value in self._sessions.items()},
            "executions": {key: asdict(value) for key, value in self._executions.items()},
            "activity_runs": {key: asdict(value) for key, value in self._activity_runs.items()},
        }
        fd, temporary_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=self._root)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
            os.chmod(temporary_path, 0o600)
            RetainedTextCompression.compress_file(temporary_path)
            os.replace(temporary_path, self._state_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _drop_session(self, session_id: str) -> bool:
        """Drops one retained session and its now-unowned artifact blobs atomically."""
        owned_snapshot_tokens: set[str] = set()
        owned_output_ids: set[str] = set()
        for record in self._executions.values():
            if record.session_id != session_id:
                continue
            if record.media is not None:
                owned_snapshot_tokens.update(_FILE_RESOURCE_RE.findall(record.media.get("uri", "")))
            if record.result:
                owned_snapshot_tokens.update(_FILE_RESOURCE_RE.findall(record.result))
            if record.retained_output_id is not None:
                owned_output_ids.add(record.retained_output_id)

        changed = self._sessions.pop(session_id, None) is not None
        for execution_id in [execution_id for execution_id, record in self._executions.items() if record.session_id == session_id]:
            self._executions.pop(execution_id, None)
            changed = True
        for run_id in [run_id for run_id, run in self._activity_runs.items() if run.session_id == session_id]:
            self._activity_runs.pop(run_id, None)
            changed = True
        self._current_run_by_session.pop(session_id, None)

        # remove immutable blobs only when no retained session still references them
        retained_snapshot_tokens = self.retained_file_tokens()
        retained_output_ids = self.retained_output_ids()
        serena_home = self._serena_home()
        for token in owned_snapshot_tokens - retained_snapshot_tokens:
            (serena_home / "chat_file_snapshots" / token).unlink(missing_ok=True)
        for output_id in owned_output_ids - retained_output_ids:
            (serena_home / "tool_outputs" / f"{output_id}.txt").unlink(missing_ok=True)
        return changed

    def _protected_session_ids(self) -> set[str]:
        """Returns sessions that cannot be evicted while tools or durable jobs are still running."""
        protected = {record.session_id for record in self._executions.values() if record.status in {"running", "queued"}}
        protected.update(self._running_job_sessions.values())
        return protected

    def _session_has_dangling_execution_reference(self, session_id: str) -> bool:
        """Returns whether an activity run references an execution no longer retained by its session."""
        records = {record.execution_id for record in self._executions.values() if record.session_id == session_id}
        runs = [run for run in self._activity_runs.values() if run.session_id == session_id]
        return any(execution_id not in records for run in runs for execution_id in run.execution_ids)

    def _cleanup_unreferenced_artifacts(self) -> None:
        """Deletes crash-orphaned artifact blobs not referenced by retained sessions."""
        serena_home = self._serena_home()
        snapshot_root = serena_home / "chat_file_snapshots"
        output_root = serena_home / "tool_outputs"
        retained_snapshots = self.retained_file_tokens()
        retained_outputs = self.retained_output_ids()

        if snapshot_root.is_dir():
            for path in snapshot_root.iterdir():
                if path.name.startswith(".") or not path.is_file():
                    continue
                if path.name not in retained_snapshots:
                    path.unlink(missing_ok=True)
        if output_root.is_dir():
            for path in output_root.glob("*.txt"):
                if path.stem not in retained_outputs:
                    path.unlink(missing_ok=True)

    def _retained_artifact_bytes(self) -> int:
        """Returns on-disk bytes referenced by retained sessions, counting shared blobs once."""
        serena_home = self._serena_home()
        snapshot_root = serena_home / "chat_file_snapshots"
        output_root = serena_home / "tool_outputs"
        total = 0

        for token in self.retained_file_tokens():
            try:
                total += (snapshot_root / token).stat().st_size
            except FileNotFoundError:
                pass
        for output_id in self.retained_output_ids():
            try:
                total += (output_root / f"{output_id}.txt").stat().st_size
            except FileNotFoundError:
                pass
        return total

    def _prune(self) -> bool:
        """Prunes retained work only by complete inactive session ownership units."""
        changed = False

        # discard orphaned state that cannot be represented as a complete dashboard session
        for execution_id, record in list(self._executions.items()):
            if record.session_id not in self._sessions:
                self._executions.pop(execution_id, None)
                changed = True
        for run_id, run in list(self._activity_runs.items()):
            if run.session_id not in self._sessions:
                self._activity_runs.pop(run_id, None)
                changed = True
        for session_id, run_id in list(self._current_run_by_session.items()):
            if session_id not in self._sessions or run_id not in self._activity_runs:
                self._current_run_by_session.pop(session_id, None)
                changed = True

        # remove incomplete historical panels atomically rather than exposing partial state
        protected = self._protected_session_ids()
        for session_id in list(self._sessions):
            if session_id in protected:
                continue
            if self._session_has_dangling_execution_reference(session_id):
                changed = self._drop_session(session_id) or changed

        # expire inactive sessions after the configured retention period
        now = time.time()
        for session in sorted(self._sessions.values(), key=lambda item: item.updated_at):
            if session.session_id in protected:
                continue
            if self._retention.is_expired(session.updated_at, now):
                changed = self._drop_session(session.session_id) or changed

        # emergency capacity guard: evict the oldest inactive sessions whole
        retained_bytes = self._retained_artifact_bytes()
        while retained_bytes > self._retention.max_artifact_bytes:
            candidates = [session for session in self._sessions.values() if session.session_id not in protected]
            if not candidates:
                break
            oldest = min(candidates, key=lambda session: (session.updated_at, session.created_at, session.session_id))
            changed = self._drop_session(oldest.session_id) or changed
            retained_bytes = self._retained_artifact_bytes()

        return changed

    @staticmethod
    def _extract_job_id(result: str | None) -> str | None:
        if not result:
            return None
        match = _JOB_ID_RE.search(result)
        return match.group(1) if match is not None else None
