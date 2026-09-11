from __future__ import annotations

import ast
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from serena.retention import DEFAULT_SESSION_RETENTION, JobRetentionState, SessionRetentionPolicy
from serena.storage_compression import RetainedTextCompression
from serena.structured_output import StructuredOutputCompactor

_FILE_RESOURCE_RE = re.compile(r"serena-file://export/([0-9a-f]{64}|[0-9a-f]{48})(?![0-9a-f])")
_STATE_VERSION = 3
_MIN_MIGRATABLE_STATE_VERSION = 2


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
    arguments: dict[str, Any]
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
    durable_job_label: str | None = None


@dataclass(frozen=True)
class ExecutionSummaryRecord:
    """Bounded execution facts required by activity discovery and collapsed rows."""

    execution_id: str
    session_id: str
    project_name: str
    tool_name: str
    arguments: dict[str, Any]
    started_at: float
    status: str
    finished_at: float | None
    durable_job_id: str | None
    durable_job_label: str | None


@dataclass
class ActivityPanelRun:
    """Persistent grouping of execution identifiers shown by one inline activity panel."""

    run_id: str
    session_id: str
    project_name: str
    started_at: float
    superseded: bool = False
    execution_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SessionExecutionSummary:
    """Lightweight retained-session facts backed by the execution index."""

    session_id: str
    panel_id: str
    display_name: str
    project_name: str
    created_at: float
    updated_at: float
    execution_count: int
    running_execution_count: int
    durable_job_count: int
    first_execution_started_at: float | None
    latest_execution_started_at: float | None
    latest_execution: ExecutionSummaryRecord | None


@dataclass
class _SessionExecutionIndex:
    """Rebuildable execution identifiers and derived facts for one retained session."""

    execution_ids: list[str] = field(default_factory=list)
    running_execution_count: int = 0
    durable_job_ids: set[str] = field(default_factory=set)
    first_execution_started_at: float | None = None
    latest_execution_started_at: float | None = None
    latest_execution_id: str | None = None


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
        self._session_execution_index: dict[str, _SessionExecutionIndex] = {}
        self._panel_session_index: dict[str, str] = {}
        self._job_session_index: dict[str, str] = {}
        self._save_batch_depth = 0
        self._save_pending = False

        migrated = self._load()
        self._interrupt_stale_state()
        self._rebuild_session_execution_index()
        pruned = self._prune()
        if migrated or pruned:
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

    def panel_id_for_job(self, job_id: str) -> str | None:
        """Returns the retained dashboard panel owning ``job_id`` from the rebuildable index."""
        with self._lock:
            session_id = self._job_session_index.get(job_id)
            if session_id is None or session_id not in self._sessions:
                return None
            return self.panel_id_for_session(session_id)

    @staticmethod
    def compact_arguments(value: dict[str, Any]) -> dict[str, Any]:
        """Returns bounded JSON-safe keyword arguments without pre-serializing them."""
        compacted = StructuredOutputCompactor().compact(value, max_chars=8_000)
        if isinstance(compacted, dict):
            return cast(dict[str, Any], compacted)
        return {"_serena_truncated": True}

    @staticmethod
    def serialize_auxiliary_value(value: object) -> str:
        """Serializes one bounded auxiliary text field such as an unexpected error."""
        return StructuredOutputCompactor().serialize_for_storage(value, max_chars=8_000)

    def start_execution(
        self,
        *,
        execution_id: str,
        session_id: str,
        project_name: str,
        tool_name: str,
        arguments: dict[str, Any],
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
                arguments=self.compact_arguments(arguments),
                started_at=now,
            )
            self._executions[execution_id] = record
            self._index_execution_start(record)
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
        durable_job_label: str | None = None,
        finished_at: float | None = None,
    ) -> None:
        """Marks one execution terminal after its underlying worker has actually stopped."""
        now = finished_at or time.time()
        with self._lock:
            record = self._executions.get(execution_id)
            if record is None:
                return
            was_running = record.status in {"running", "queued"}
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
            record.durable_job_id = durable_job_id
            record.durable_job_label = durable_job_label
            if project_name is not None:
                record.project_name = project_name

            session = self._ensure_session(record.session_id, record.started_at)
            if record.project_name:
                session.project_name = record.project_name
            session.updated_at = now
            self._index_execution_finish(record, was_running=was_running)
            self._prune()
            self._save()

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        """Returns one execution record if retained."""
        with self._lock:
            record = self._executions.get(execution_id)
            return ExecutionRecord(**asdict(record)) if record is not None else None

    def get_execution_summary(self, execution_id: str) -> ExecutionSummaryRecord | None:
        """Returns bounded row metadata for one retained execution without copying its result body."""
        with self._lock:
            record = self._executions.get(execution_id)
            return self._execution_summary_record(record) if record is not None else None

    def list_executions(self, *, newest_first: bool = True, limit: int | None = None) -> list[ExecutionRecord]:
        """Returns retained executions ordered by submission time."""
        with self._lock:
            records = sorted(self._executions.values(), key=lambda item: item.started_at, reverse=newest_first)
            if limit is not None:
                records = records[:limit]
            return [ExecutionRecord(**asdict(record)) for record in records]

    def list_session_executions(self, session_id: str) -> list[ExecutionRecord]:
        """Returns executions belonging to one session from oldest to newest."""
        with self._lock:
            index = self._session_execution_index.get(session_id)
            if index is None:
                return []
            return [
                ExecutionRecord(**asdict(record))
                for execution_id in index.execution_ids
                if (record := self._executions.get(execution_id)) is not None
            ]

    def list_session_execution_items(self, session_id: str) -> list[ExecutionSummaryRecord]:
        """Returns bounded execution rows for one session from oldest to newest."""
        with self._lock:
            index = self._session_execution_index.get(session_id)
            if index is None:
                return []
            return [
                self._execution_summary_record(record)
                for execution_id in index.execution_ids
                if (record := self._executions.get(execution_id)) is not None
            ]

    def list_session_execution_summaries(self) -> list[SessionExecutionSummary]:
        """Returns lightweight retained-session facts without expanding execution payloads."""
        with self._lock:
            summaries: list[SessionExecutionSummary] = []
            for session in self._sessions.values():
                index = self._session_execution_index.get(session.session_id, _SessionExecutionIndex())
                latest_record = self._executions.get(index.latest_execution_id) if index.latest_execution_id is not None else None
                summaries.append(
                    SessionExecutionSummary(
                        session_id=session.session_id,
                        panel_id=session.panel_id,
                        display_name=session.display_name,
                        project_name=session.project_name,
                        created_at=session.created_at,
                        updated_at=session.updated_at,
                        execution_count=len(index.execution_ids),
                        running_execution_count=index.running_execution_count,
                        durable_job_count=len(index.durable_job_ids),
                        first_execution_started_at=index.first_execution_started_at,
                        latest_execution_started_at=index.latest_execution_started_at,
                        latest_execution=self._execution_summary_record(latest_record) if latest_record is not None else None,
                    )
                )
            return summaries

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
            sessions = sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)
            return [SessionRecord(**asdict(session)) for session in sessions]

    def get_session_by_panel_id(self, panel_id: str) -> SessionRecord | None:
        """Returns one retained session by its dashboard panel identifier."""
        with self._lock:
            session_id = self._panel_session_index.get(panel_id)
            session = self._sessions.get(session_id) if session_id is not None else None
            return SessionRecord(**asdict(session)) if session is not None else None

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

    def maintain_retention(self) -> bool:
        """Runs deterministic retention pruning outside ordinary read paths."""
        with self._lock:
            changed = self._prune()
            if changed:
                self._save()
            return changed

    def sync_job_retention(self, jobs: list[JobRetentionState]) -> set[str]:
        """Synchronize durable-job lifecycle facts with retained session ownership.

        Terminal job completion extends the owning session's retention window and running jobs
        protect their owning sessions from later eviction. Retention pruning itself is owned by
        lifecycle mutations and :meth:`maintain_retention`, not this synchronization read.
        """
        with self._lock:
            running_job_sessions: dict[str, str] = {}
            persistence_changed = False
            for job in jobs:
                session_id = job.session_id or self._job_session_index.get(job.job_id)
                if session_id is None:
                    continue
                if job.is_running:
                    running_job_sessions[job.job_id] = session_id
                if job.finished_at is not None and (session := self._sessions.get(session_id)) is not None:
                    updated_at = max(session.updated_at, job.finished_at)
                    if updated_at != session.updated_at:
                        session.updated_at = updated_at
                        persistence_changed = True

            self._running_job_sessions = running_job_sessions
            if persistence_changed:
                self._save()

            return {job_id for job_id, session_id in self._job_session_index.items() if session_id in self._sessions}

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
            self._panel_session_index[session.panel_id] = session_id
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

    def _load(self) -> bool:
        """Loads canonical state and reports whether an on-disk schema migration occurred."""
        if not self._state_path.is_file():
            return False
        try:
            payload = json.loads(RetainedTextCompression.read_text(self._state_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False

        version = payload.get("version")
        if version not in {_MIN_MIGRATABLE_STATE_VERSION, _STATE_VERSION}:
            raise RuntimeError(
                f"Unsupported Serena execution-store schema version {version!r}; Serena requires schema version "
                f"{_MIN_MIGRATABLE_STATE_VERSION} or {_STATE_VERSION}."
            )
        migrated = version != _STATE_VERSION

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
                if not isinstance(item, dict):
                    continue
                normalized = dict(item)
                arguments = normalized.get("arguments", {})
                if isinstance(arguments, str):
                    normalized["arguments"] = self._migrate_arguments(arguments)
                    migrated = True
                elif isinstance(arguments, dict):
                    normalized["arguments"] = self.compact_arguments(cast(dict[str, Any], arguments))
                else:
                    normalized["arguments"] = {}
                    migrated = True
                try:
                    self._executions[str(execution_id)] = ExecutionRecord(**normalized)
                except (TypeError, ValueError):
                    continue
        if isinstance(activity_runs, dict):
            for run_id, item in activity_runs.items():
                if not isinstance(item, dict):
                    continue
                normalized = {
                    key: value
                    for key, value in item.items()
                    if key in {"run_id", "session_id", "project_name", "started_at", "superseded", "execution_ids"}
                }
                if len(normalized) != len(item):
                    migrated = True
                try:
                    run = ActivityPanelRun(**normalized)
                except (TypeError, ValueError):
                    continue
                self._activity_runs[str(run_id)] = run
                if not run.superseded:
                    current = self._current_run_by_session.get(run.session_id)
                    if current is None or self._activity_runs[current].started_at < run.started_at:
                        self._current_run_by_session[run.session_id] = run.run_id
        return migrated

    @classmethod
    def _migrate_arguments(cls, serialized: str) -> dict[str, Any]:
        """Converts legacy serialized keyword arguments to bounded structured storage."""
        if not serialized:
            return {}
        for parser in (json.loads, ast.literal_eval):
            try:
                value = parser(serialized)
            except (json.JSONDecodeError, SyntaxError, ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return cls.compact_arguments({str(key): item for key, item in value.items()})

        try:
            expression = ast.parse(f"_tool({serialized})", mode="eval").body
            if isinstance(expression, ast.Call) and not expression.args:
                arguments = {keyword.arg: ast.literal_eval(keyword.value) for keyword in expression.keywords if keyword.arg is not None}
                return cls.compact_arguments(arguments)
        except (SyntaxError, ValueError, TypeError):
            pass
        return {"_legacy_arguments": StructuredOutputCompactor.truncate_text(serialized, 7_900)}

    def _rebuild_session_execution_index(self) -> None:
        """Rebuilds non-authoritative retained-session, panel and durable-job lookup indexes."""
        self._session_execution_index = {session_id: _SessionExecutionIndex() for session_id in self._sessions}
        self._panel_session_index = {session.panel_id: session.session_id for session in self._sessions.values()}
        self._job_session_index = {}
        records = sorted(self._executions.values(), key=lambda record: (record.started_at, record.execution_id))
        for record in records:
            if record.session_id not in self._sessions:
                continue
            self._index_execution_start(record)
            if record.status not in {"running", "queued"}:
                index = self._session_execution_index[record.session_id]
                index.running_execution_count = max(0, index.running_execution_count - 1)
            if record.durable_job_id is not None:
                self._index_durable_job(record.session_id, record.durable_job_id)

    def _index_durable_job(self, session_id: str, job_id: str) -> None:
        """Indexes unique durable-job ownership for one retained session."""
        previous_session_id = self._job_session_index.get(job_id)
        if previous_session_id == session_id:
            return
        if previous_session_id is not None:
            previous_index = self._session_execution_index.get(previous_session_id)
            if previous_index is not None:
                previous_index.durable_job_ids.discard(job_id)
        self._session_execution_index.setdefault(session_id, _SessionExecutionIndex()).durable_job_ids.add(job_id)
        self._job_session_index[job_id] = session_id

    @staticmethod
    def _execution_summary_record(record: ExecutionRecord) -> ExecutionSummaryRecord:
        """Projects one canonical execution to bounded activity-row metadata."""
        return ExecutionSummaryRecord(
            execution_id=record.execution_id,
            session_id=record.session_id,
            project_name=record.project_name,
            tool_name=record.tool_name,
            arguments=dict(record.arguments),
            started_at=record.started_at,
            status=record.status,
            finished_at=record.finished_at,
            durable_job_id=record.durable_job_id,
            durable_job_label=record.durable_job_label,
        )

    def _index_execution_start(self, record: ExecutionRecord) -> None:
        """Adds one execution to the rebuildable session index."""
        index = self._session_execution_index.setdefault(record.session_id, _SessionExecutionIndex())
        if record.execution_id in index.execution_ids:
            return
        index.execution_ids.append(record.execution_id)
        if record.status in {"running", "queued"}:
            index.running_execution_count += 1
        if index.first_execution_started_at is None:
            index.first_execution_started_at = record.started_at
        if index.latest_execution_started_at is None or record.started_at >= index.latest_execution_started_at:
            index.latest_execution_started_at = record.started_at
            index.latest_execution_id = record.execution_id

    def _index_execution_finish(self, record: ExecutionRecord, *, was_running: bool) -> None:
        """Updates mutable counts and durable-job ownership after one execution becomes terminal."""
        index = self._session_execution_index.setdefault(record.session_id, _SessionExecutionIndex())
        if record.execution_id not in index.execution_ids:
            self._index_execution_start(record)
        if was_running:
            index.running_execution_count = max(0, index.running_execution_count - 1)
        if record.durable_job_id is not None:
            self._index_durable_job(record.session_id, record.durable_job_id)

    @contextmanager
    def batch_updates(self) -> Iterator[None]:
        """Persists a group of execution-store mutations with one final state write."""
        with self._lock:
            self._save_batch_depth += 1
            try:
                yield
            finally:
                self._save_batch_depth -= 1
                if self._save_batch_depth == 0 and self._save_pending:
                    self._save_pending = False
                    self._write_state()

    def _save(self) -> None:
        """Persists state immediately unless an enclosing update batch defers the write."""
        if self._save_batch_depth > 0:
            self._save_pending = True
            return
        self._write_state()

    def _write_state(self) -> None:
        """Writes the complete retained execution state atomically."""
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

        session = self._sessions.pop(session_id, None)
        changed = session is not None
        for execution_id in [execution_id for execution_id, record in self._executions.items() if record.session_id == session_id]:
            self._executions.pop(execution_id, None)
            changed = True
        for run_id in [run_id for run_id, run in self._activity_runs.items() if run.session_id == session_id]:
            self._activity_runs.pop(run_id, None)
            changed = True
        self._current_run_by_session.pop(session_id, None)
        self._session_execution_index.pop(session_id, None)
        if session is not None:
            self._panel_session_index.pop(session.panel_id, None)
        for job_id, owning_session_id in list(self._job_session_index.items()):
            if owning_session_id == session_id:
                self._job_session_index.pop(job_id, None)

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

    def _sessions_with_dangling_execution_references(self) -> set[str]:
        """Returns sessions whose activity runs reference executions outside their retained index."""
        retained_by_session = {session_id: set(index.execution_ids) for session_id, index in self._session_execution_index.items()}
        dangling: set[str] = set()
        for run in self._activity_runs.values():
            retained_ids = retained_by_session.get(run.session_id)
            if retained_ids is None:
                continue
            if any(execution_id not in retained_ids for execution_id in run.execution_ids):
                dangling.add(run.session_id)
        return dangling

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
        for session_id in self._sessions_with_dangling_execution_references() - protected:
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
