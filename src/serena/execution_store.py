from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from serena.retention import (
    DEFAULT_SESSION_RETENTION,
    JobRetentionState,
    SessionRetentionPolicy,
)
from serena.structured_output import StructuredOutputCompactor

_FILE_RESOURCE_RE = re.compile(r"serena-file://export/([0-9a-f]{64}|[0-9a-f]{48})(?![0-9a-f])")


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


class ExecutionStore:
    """Owns indexed persistent Serena session, execution and activity-panel state.

    The store is the single persistence boundary for model-visible tool execution state and reads
    and mutates normalized SQLite rows directly.
    """

    _DATABASE_SCHEMA_VERSION = 1
    _DATABASE_FILENAME = "state.sqlite3"
    _BUSY_TIMEOUT_MS = 5_000

    def __init__(
        self,
        root: Path | None = None,
        *,
        retention: SessionRetentionPolicy = DEFAULT_SESSION_RETENTION,
    ) -> None:
        use_default_root = root is None
        self._root = root or self._default_root()
        self._database_path = self._root / self._DATABASE_FILENAME
        self._retention = retention
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._running_job_sessions: dict[str, str] = {}
        self._pending_artifact_cleanup: set[tuple[str, str]] = set()

        # configure one process-local connection behind the store lock
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._root, 0o700)
        self._connection = sqlite3.connect(
            self._database_path,
            timeout=self._BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_database()
        self._create_schema()

        self._interrupt_stale_state()
        if use_default_root:
            self._cleanup_unreferenced_artifacts()

    def dashboard_revision(self) -> str:
        """Returns an O(1) process-local revision for dashboard-visible execution state."""
        with self._lock:
            data_version = int(self._connection.execute("PRAGMA data_version").fetchone()[0])
            total_changes = self._connection.total_changes
        return f"{total_changes}:{data_version}"

    def session_revision(self, panel_id: str) -> str | None:
        """Returns the indexed revision for one retained dashboard session."""
        with self._lock:
            row = self._connection.execute(
                "SELECT updated_at FROM sessions WHERE panel_id = ?",
                (panel_id,),
            ).fetchone()
        if row is None:
            return None
        return f"{float(row['updated_at']):.9f}"

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
        """Returns the retained dashboard panel that originally started ``job_id``.

        Execution rows may observe the same durable job from multiple sessions. The ``start_job``
        execution is the ownership-bearing fallback when canonical ``JobStore`` metadata is absent.
        """
        with self._lock:
            row = self._connection.execute(
                """
                SELECT sessions.panel_id
                FROM executions
                JOIN sessions ON sessions.session_id = executions.session_id
                WHERE executions.durable_job_id = ?
                ORDER BY CASE WHEN executions.tool_name = 'start_job' THEN 0 ELSE 1 END,
                         executions.started_at ASC,
                         executions.execution_id ASC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            return str(row["panel_id"]) if row is not None else None

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
        """Creates one running execution record in a row-local transaction."""
        now = started_at if started_at is not None else time.time()
        record = ExecutionRecord(
            execution_id=execution_id,
            session_id=session_id,
            project_name=project_name,
            tool_name=tool_name,
            arguments=self.compact_arguments(arguments),
            started_at=now,
        )
        with self._transaction():
            # expire old ownership before a reused session identifier can refresh it
            self._prune()
            self._ensure_session(session_id, now)
            self._connection.execute(
                """
                UPDATE sessions
                SET project_name = CASE WHEN ? <> '' THEN ? ELSE project_name END,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (project_name, project_name, now, session_id),
            )
            self._connection.execute(
                """
                INSERT INTO executions (
                    execution_id, session_id, project_name, tool_name, arguments_json, started_at,
                    status, finished_at, request_finished_at, request_error, result, error,
                    retained_output_id, retained_output_chars, media_json, durable_job_id,
                    durable_job_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    record.execution_id,
                    record.session_id,
                    record.project_name,
                    record.tool_name,
                    self._dump_json(record.arguments),
                    record.started_at,
                    record.status,
                ),
            )
        return record

    def mark_request_abandoned(
        self,
        execution_id: str,
        *,
        error: str,
        request_finished_at: float | None = None,
    ) -> None:
        """Records that a model-visible request ended while its worker is still running."""
        now = request_finished_at if request_finished_at is not None else time.time()
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT session_id
                FROM executions
                WHERE execution_id = ?
                  AND status IN ('running', 'queued')
                  AND request_finished_at IS NULL
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                return
            self._connection.execute(
                """
                UPDATE executions
                SET request_finished_at = ?, request_error = ?
                WHERE execution_id = ?
                """,
                (now, error, execution_id),
            )
            self._connection.execute(
                """
                UPDATE sessions
                SET updated_at = MAX(updated_at, ?)
                WHERE session_id = ?
                """,
                (now, str(row["session_id"])),
            )

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
        now = finished_at if finished_at is not None else time.time()
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT session_id, project_name, request_finished_at, request_error
                FROM executions
                WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                return

            # preserve an earlier request timeout/cancellation while finalising the worker lifecycle
            request_error = str(row["request_error"]) if row["request_error"] is not None else error
            effective_project = project_name if project_name is not None else str(row["project_name"])
            stored_result = result if media is None else None
            self._connection.execute(
                """
                UPDATE executions
                SET status = ?,
                    finished_at = ?,
                    request_finished_at = COALESCE(request_finished_at, ?),
                    request_error = COALESCE(request_error, ?),
                    result = ?,
                    error = ?,
                    retained_output_id = ?,
                    retained_output_chars = ?,
                    media_json = ?,
                    durable_job_id = ?,
                    durable_job_label = ?,
                    project_name = ?
                WHERE execution_id = ?
                """,
                (
                    "completed" if succeeded else "failed",
                    now,
                    now,
                    error,
                    stored_result,
                    request_error,
                    retained_output_id,
                    retained_output_chars,
                    self._dump_json(media) if media is not None else None,
                    durable_job_id,
                    durable_job_label,
                    effective_project,
                    execution_id,
                ),
            )
            self._replace_execution_resources(
                execution_id,
                result=stored_result,
                media=media,
                retained_output_id=retained_output_id,
            )
            self._connection.execute(
                """
                UPDATE sessions
                SET project_name = CASE WHEN ? <> '' THEN ? ELSE project_name END,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (effective_project, effective_project, now, str(row["session_id"])),
            )
            self._prune()

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        """Returns one execution record if retained."""
        with self._lock:
            row = self._connection.execute("SELECT * FROM executions WHERE execution_id = ?", (execution_id,)).fetchone()
            return self._execution_from_row(row) if row is not None else None

    def get_execution_summary(self, execution_id: str) -> ExecutionSummaryRecord | None:
        """Returns bounded row metadata for one retained execution without loading its result body."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT execution_id, session_id, project_name, tool_name, arguments_json, started_at,
                       status, finished_at, durable_job_id, durable_job_label
                FROM executions
                WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            return self._execution_summary_from_row(row) if row is not None else None

    def list_executions(self, *, newest_first: bool = True, limit: int | None = None) -> list[ExecutionRecord]:
        """Returns retained executions ordered by submission time."""
        direction = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM executions ORDER BY started_at {direction}, execution_id {direction}"
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            return [self._execution_from_row(row) for row in self._connection.execute(sql, params).fetchall()]

    def list_session_executions(self, session_id: str) -> list[ExecutionRecord]:
        """Returns executions belonging to one session from oldest to newest."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM executions
                WHERE session_id = ?
                ORDER BY started_at ASC, execution_id ASC
                """,
                (session_id,),
            ).fetchall()
            return [self._execution_from_row(row) for row in rows]

    def list_session_execution_items(self, session_id: str) -> list[ExecutionSummaryRecord]:
        """Returns bounded execution rows for one session from oldest to newest."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT execution_id, session_id, project_name, tool_name, arguments_json, started_at,
                       status, finished_at, durable_job_id, durable_job_label
                FROM executions
                WHERE session_id = ?
                ORDER BY started_at ASC, execution_id ASC
                """,
                (session_id,),
            ).fetchall()
            return [self._execution_summary_from_row(row) for row in rows]

    def list_session_execution_summaries(self) -> list[SessionExecutionSummary]:
        """Returns compact retained-session facts with set-based SQL aggregation."""
        with self._lock:
            aggregate_rows = self._connection.execute(
                """
                SELECT
                    sessions.session_id,
                    sessions.panel_id,
                    sessions.display_name,
                    sessions.project_name,
                    sessions.created_at,
                    sessions.updated_at,
                    COUNT(executions.execution_id) AS execution_count,
                    COALESCE(SUM(CASE WHEN executions.status IN ('running', 'queued') THEN 1 ELSE 0 END), 0)
                        AS running_execution_count,
                    COUNT(DISTINCT executions.durable_job_id) AS durable_job_count,
                    MIN(executions.started_at) AS first_execution_started_at,
                    MAX(executions.started_at) AS latest_execution_started_at
                FROM sessions
                LEFT JOIN executions ON executions.session_id = sessions.session_id
                GROUP BY sessions.session_id
                """
            ).fetchall()
            latest_rows = self._connection.execute(
                """
                SELECT execution_id, session_id, project_name, tool_name, arguments_json, started_at,
                       status, finished_at, durable_job_id, durable_job_label
                FROM (
                    SELECT
                        execution_id, session_id, project_name, tool_name, arguments_json, started_at,
                        status, finished_at, durable_job_id, durable_job_label,
                        ROW_NUMBER() OVER (
                            PARTITION BY session_id
                            ORDER BY started_at DESC, execution_id DESC
                        ) AS row_number
                    FROM executions
                )
                WHERE row_number = 1
                """
            ).fetchall()
            latest = {str(row["session_id"]): self._execution_summary_from_row(row) for row in latest_rows}

            return [
                SessionExecutionSummary(
                    session_id=str(row["session_id"]),
                    panel_id=str(row["panel_id"]),
                    display_name=str(row["display_name"]),
                    project_name=str(row["project_name"]),
                    created_at=float(row["created_at"]),
                    updated_at=float(row["updated_at"]),
                    execution_count=int(row["execution_count"]),
                    running_execution_count=int(row["running_execution_count"]),
                    durable_job_count=int(row["durable_job_count"]),
                    first_execution_started_at=(
                        float(row["first_execution_started_at"]) if row["first_execution_started_at"] is not None else None
                    ),
                    latest_execution_started_at=(
                        float(row["latest_execution_started_at"]) if row["latest_execution_started_at"] is not None else None
                    ),
                    latest_execution=latest.get(str(row["session_id"])),
                )
                for row in aggregate_rows
            ]

    def set_session_display_name(self, session_id: str, display_name: str) -> str:
        """Sets the normalized operator-facing conversation title."""
        normalized = " ".join(display_name.split())
        if not normalized:
            raise ValueError("Conversation names must not be empty")
        if len(normalized) > 80:
            raise ValueError("Conversation names must be at most 80 characters")
        now = time.time()
        with self._transaction():
            self._ensure_session(session_id, now)
            self._connection.execute(
                "UPDATE sessions SET display_name = ?, updated_at = ? WHERE session_id = ?",
                (normalized, now, session_id),
            )
        return normalized

    def update_session_project(self, session_id: str, project_name: str) -> None:
        """Updates the latest project identity associated with one session."""
        if not project_name:
            return
        now = time.time()
        with self._transaction():
            self._ensure_session(session_id, now)
            self._connection.execute(
                "UPDATE sessions SET project_name = ?, updated_at = ? WHERE session_id = ?",
                (project_name, now, session_id),
            )

    def list_sessions(self) -> list[SessionRecord]:
        """Returns retained sessions from newest to oldest update time."""
        with self._lock:
            rows = self._connection.execute("SELECT * FROM sessions ORDER BY updated_at DESC, created_at DESC, session_id DESC").fetchall()
            return [self._session_from_row(row) for row in rows]

    def get_session_by_panel_id(self, panel_id: str) -> SessionRecord | None:
        """Returns one retained session by its dashboard panel identifier."""
        with self._lock:
            row = self._connection.execute("SELECT * FROM sessions WHERE panel_id = ?", (panel_id,)).fetchone()
            return self._session_from_row(row) if row is not None else None

    def start_activity_run(self, session_id: str, project_name: str) -> ActivityPanelRun:
        """Starts a panel run and carries live executions from its predecessor."""
        now = time.time()
        run_id = uuid.uuid4().hex
        with self._transaction():
            self._prune()
            previous = self._connection.execute(
                """
                SELECT run_id
                FROM activity_runs
                WHERE session_id = ? AND superseded = 0
                ORDER BY started_at DESC, run_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            continuing: list[str] = []
            if previous is not None:
                previous_id = str(previous["run_id"])
                continuing = [
                    str(row["execution_id"])
                    for row in self._connection.execute(
                        """
                        SELECT activity_run_executions.execution_id
                        FROM activity_run_executions
                        JOIN executions ON executions.execution_id = activity_run_executions.execution_id
                        WHERE activity_run_executions.run_id = ?
                          AND executions.status IN ('running', 'queued')
                        ORDER BY activity_run_executions.position ASC
                        """,
                        (previous_id,),
                    ).fetchall()
                ]
                self._connection.execute(
                    "UPDATE activity_runs SET superseded = 1 WHERE run_id = ?",
                    (previous_id,),
                )

            self._ensure_session(session_id, now)
            self._connection.execute(
                """
                INSERT INTO activity_runs (run_id, session_id, project_name, started_at, superseded)
                VALUES (?, ?, ?, ?, 0)
                """,
                (run_id, session_id, project_name, now),
            )
            self._connection.executemany(
                """
                INSERT INTO activity_run_executions (run_id, execution_id, position)
                VALUES (?, ?, ?)
                """,
                [(run_id, execution_id, position) for position, execution_id in enumerate(continuing)],
            )

        return ActivityPanelRun(
            run_id=run_id,
            session_id=session_id,
            project_name=project_name,
            started_at=now,
            execution_ids=continuing,
        )

    def append_execution_to_current_run(self, session_id: str, execution_id: str, *, project_name: str = "") -> None:
        """Adds one execution identifier to the active panel run for ``session_id`` if present."""
        with self._transaction():
            run = self._connection.execute(
                """
                SELECT run_id
                FROM activity_runs
                WHERE session_id = ? AND superseded = 0
                ORDER BY started_at DESC, run_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if run is None:
                return
            run_id = str(run["run_id"])
            position_row = self._connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM activity_run_executions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            next_position = int(position_row["next_position"]) if position_row is not None else 0
            self._connection.execute(
                """
                INSERT OR IGNORE INTO activity_run_executions (run_id, execution_id, position)
                VALUES (?, ?, ?)
                """,
                (run_id, execution_id, next_position),
            )
            if project_name:
                self._connection.execute(
                    "UPDATE activity_runs SET project_name = ? WHERE run_id = ?",
                    (project_name, run_id),
                )

    def update_activity_run_project(self, session_id: str, project_name: str) -> None:
        """Updates the active panel run's project label."""
        if not project_name:
            return
        with self._transaction():
            self._connection.execute(
                """
                UPDATE activity_runs
                SET project_name = ?
                WHERE run_id = (
                    SELECT run_id
                    FROM activity_runs
                    WHERE session_id = ? AND superseded = 0
                    ORDER BY started_at DESC, run_id DESC
                    LIMIT 1
                )
                """,
                (project_name, session_id),
            )

    def get_activity_run(self, run_id: str) -> ActivityPanelRun | None:
        """Returns one retained activity-panel run."""
        with self._lock:
            row = self._connection.execute("SELECT * FROM activity_runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._activity_run_from_row(row) if row is not None else None

    def get_current_activity_run(self, session_id: str) -> ActivityPanelRun | None:
        """Returns the active activity-panel run for one session."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT *
                FROM activity_runs
                WHERE session_id = ? AND superseded = 0
                ORDER BY started_at DESC, run_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return self._activity_run_from_row(row) if row is not None else None

    def list_activity_runs(self) -> list[ActivityPanelRun]:
        """Returns retained activity-panel runs from oldest to newest."""
        with self._lock:
            rows = self._connection.execute("SELECT * FROM activity_runs ORDER BY started_at ASC, run_id ASC").fetchall()
            membership_rows = self._connection.execute(
                """
                SELECT run_id, execution_id
                FROM activity_run_executions
                ORDER BY run_id ASC, position ASC
                """
            ).fetchall()
            membership: dict[str, list[str]] = {}
            for item in membership_rows:
                membership.setdefault(str(item["run_id"]), []).append(str(item["execution_id"]))
            return [
                ActivityPanelRun(
                    run_id=str(row["run_id"]),
                    session_id=str(row["session_id"]),
                    project_name=str(row["project_name"]),
                    started_at=float(row["started_at"]),
                    superseded=bool(row["superseded"]),
                    execution_ids=membership.get(str(row["run_id"]), []),
                )
                for row in rows
            ]

    def maintain_retention(self) -> bool:
        """Runs deterministic retention pruning outside ordinary read paths."""
        with self._transaction():
            return self._prune()

    def sync_job_retention(self, jobs: list[JobRetentionState]) -> set[str]:
        """Synchronizes durable-job lifecycle facts with retained session ownership."""
        with self._transaction():
            running_job_sessions: dict[str, str] = {}
            for job in jobs:
                session_id = job.session_id
                if session_id is None:
                    row = self._connection.execute(
                        """
                        SELECT session_id
                        FROM executions
                        WHERE durable_job_id = ?
                        ORDER BY started_at DESC, execution_id DESC
                        LIMIT 1
                        """,
                        (job.job_id,),
                    ).fetchone()
                    session_id = str(row["session_id"]) if row is not None else None
                if session_id is None:
                    continue
                if job.is_running:
                    running_job_sessions[job.job_id] = session_id
                if job.finished_at is not None:
                    self._connection.execute(
                        """
                        UPDATE sessions
                        SET updated_at = MAX(updated_at, ?)
                        WHERE session_id = ?
                        """,
                        (job.finished_at, session_id),
                    )
            self._running_job_sessions = running_job_sessions
            rows = self._connection.execute(
                """
                SELECT DISTINCT executions.durable_job_id
                FROM executions
                JOIN sessions ON sessions.session_id = executions.session_id
                WHERE executions.durable_job_id IS NOT NULL
                """
            ).fetchall()
            retained_job_ids = {str(row["durable_job_id"]) for row in rows}
            retained_sessions = {str(row["session_id"]) for row in self._connection.execute("SELECT session_id FROM sessions").fetchall()}
            retained_job_ids.update(job.job_id for job in jobs if job.session_id is not None and job.session_id in retained_sessions)
            return retained_job_ids

    def retained_file_tokens(self) -> set[str]:
        """Returns snapshot tokens referenced by retained executions."""
        with self._lock:
            rows = self._connection.execute("SELECT DISTINCT resource_id FROM retained_resources WHERE kind = 'snapshot'").fetchall()
            return {str(row["resource_id"]) for row in rows}

    @classmethod
    def retained_file_tokens_from_disk(cls) -> set[str]:
        """Returns snapshot tokens from canonical persisted state without constructing runtime services."""
        return cls._retained_resource_ids_from_database("snapshot")

    def retained_output_ids(self) -> set[str]:
        """Returns pageable-output identifiers referenced by retained executions."""
        with self._lock:
            rows = self._connection.execute("SELECT DISTINCT resource_id FROM retained_resources WHERE kind = 'output'").fetchall()
            return {str(row["resource_id"]) for row in rows}

    @classmethod
    def retained_output_ids_from_disk(cls) -> set[str]:
        """Returns pageable-output identifiers from canonical persisted state."""
        return cls._retained_resource_ids_from_database("output")

    @contextmanager
    def batch_updates(self) -> Iterator[None]:
        """Groups execution-store mutations in one real SQLite transaction."""
        with self._transaction():
            yield

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serializes connection use and commits only at the outer mutation boundary."""
        with self._lock:
            outermost = self._transaction_depth == 0
            if outermost:
                self._connection.execute("BEGIN IMMEDIATE")
                self._pending_artifact_cleanup.clear()
            self._transaction_depth += 1
            try:
                yield
            except Exception:
                self._transaction_depth -= 1
                if outermost:
                    self._connection.rollback()
                    self._pending_artifact_cleanup.clear()
                raise
            else:
                self._transaction_depth -= 1
                if outermost:
                    self._connection.commit()
                    pending = set(self._pending_artifact_cleanup)
                    self._pending_artifact_cleanup.clear()
                    self._cleanup_candidate_artifacts(pending)

    def _configure_database(self) -> None:
        """Configures SQLite for the single-process, multi-threaded Serena service."""
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")

    def _create_schema(self) -> None:
        """Creates the normalized execution/activity schema and access-path indexes."""
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                panel_id TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                project_name TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                project_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                started_at REAL NOT NULL,
                status TEXT NOT NULL,
                finished_at REAL,
                request_finished_at REAL,
                request_error TEXT,
                result TEXT,
                error TEXT,
                retained_output_id TEXT,
                retained_output_chars INTEGER,
                media_json TEXT,
                durable_job_id TEXT,
                durable_job_label TEXT
            );

            CREATE TABLE IF NOT EXISTS activity_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                project_name TEXT NOT NULL,
                started_at REAL NOT NULL,
                superseded INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS activity_run_executions (
                run_id TEXT NOT NULL REFERENCES activity_runs(run_id) ON DELETE CASCADE,
                execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                PRIMARY KEY (run_id, execution_id),
                UNIQUE (run_id, position)
            );

            CREATE TABLE IF NOT EXISTS retained_resources (
                execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (execution_id, kind, resource_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                ON sessions(updated_at, created_at, session_id);
            CREATE INDEX IF NOT EXISTS idx_executions_session_order
                ON executions(session_id, started_at, execution_id);
            CREATE INDEX IF NOT EXISTS idx_executions_status_session
                ON executions(status, session_id);
            CREATE INDEX IF NOT EXISTS idx_executions_durable_job
                ON executions(durable_job_id, session_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_activity_runs_session_current
                ON activity_runs(session_id, superseded, started_at, run_id);
            CREATE INDEX IF NOT EXISTS idx_activity_run_membership
                ON activity_run_executions(run_id, position);
            CREATE INDEX IF NOT EXISTS idx_retained_resources_kind_id
                ON retained_resources(kind, resource_id);
            """
        )
        self._connection.execute(f"PRAGMA user_version={self._DATABASE_SCHEMA_VERSION}")

    def _ensure_session(self, session_id: str, timestamp: float) -> SessionRecord:
        """Creates one session if absent and returns its canonical metadata."""
        self._connection.execute(
            """
            INSERT OR IGNORE INTO sessions (
                session_id, panel_id, created_at, updated_at, display_name, project_name
            ) VALUES (?, ?, ?, ?, '', '')
            """,
            (session_id, self.panel_id_for_session(session_id), timestamp, timestamp),
        )
        row = self._connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to create Serena session {session_id!r}")
        return self._session_from_row(row)

    def _interrupt_stale_state(self) -> None:
        """Marks execution and panel state left live by a previous Serena process as historical."""
        now = time.time()
        message = "Serena restarted before this tool call reached a terminal state."
        with self._transaction():
            self._connection.execute(
                """
                UPDATE sessions
                SET updated_at = MAX(updated_at, ?)
                WHERE session_id IN (
                    SELECT DISTINCT session_id
                    FROM executions
                    WHERE status IN ('running', 'queued')
                )
                """,
                (now,),
            )
            self._connection.execute(
                """
                UPDATE executions
                SET status = 'failed',
                    finished_at = COALESCE(finished_at, ?),
                    request_finished_at = COALESCE(request_finished_at, ?),
                    request_error = COALESCE(request_error, ?),
                    error = COALESCE(error, request_error, ?)
                WHERE status IN ('running', 'queued')
                """,
                (now, now, message, message),
            )
            self._connection.execute("UPDATE activity_runs SET superseded = 1 WHERE superseded = 0")

    @staticmethod
    def _dump_json(value: object) -> str:
        """Serializes one structured database field at the persistence boundary."""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load_json_object(value: str | None) -> dict[str, Any]:
        """Deserializes one persisted JSON object."""
        if not value:
            return {}
        parsed = json.loads(value)
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}

    @classmethod
    def _execution_from_row(cls, row: sqlite3.Row) -> ExecutionRecord:
        """Projects one SQLite row to the canonical execution dataclass."""
        media = cls._load_json_object(str(row["media_json"])) if row["media_json"] is not None else None
        return ExecutionRecord(
            execution_id=str(row["execution_id"]),
            session_id=str(row["session_id"]),
            project_name=str(row["project_name"]),
            tool_name=str(row["tool_name"]),
            arguments=cls._load_json_object(str(row["arguments_json"])),
            started_at=float(row["started_at"]),
            status=str(row["status"]),
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            request_finished_at=float(row["request_finished_at"]) if row["request_finished_at"] is not None else None,
            request_error=str(row["request_error"]) if row["request_error"] is not None else None,
            result=str(row["result"]) if row["result"] is not None else None,
            error=str(row["error"]) if row["error"] is not None else None,
            retained_output_id=str(row["retained_output_id"]) if row["retained_output_id"] is not None else None,
            retained_output_chars=int(row["retained_output_chars"]) if row["retained_output_chars"] is not None else None,
            media=cast(dict[str, str] | None, media),
            durable_job_id=str(row["durable_job_id"]) if row["durable_job_id"] is not None else None,
            durable_job_label=str(row["durable_job_label"]) if row["durable_job_label"] is not None else None,
        )

    @classmethod
    def _execution_summary_from_row(cls, row: sqlite3.Row) -> ExecutionSummaryRecord:
        """Projects one SQLite row to bounded activity-row metadata."""
        return ExecutionSummaryRecord(
            execution_id=str(row["execution_id"]),
            session_id=str(row["session_id"]),
            project_name=str(row["project_name"]),
            tool_name=str(row["tool_name"]),
            arguments=cls._load_json_object(str(row["arguments_json"])),
            started_at=float(row["started_at"]),
            status=str(row["status"]),
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            durable_job_id=str(row["durable_job_id"]) if row["durable_job_id"] is not None else None,
            durable_job_label=str(row["durable_job_label"]) if row["durable_job_label"] is not None else None,
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        """Projects one SQLite row to retained session metadata."""
        return SessionRecord(
            session_id=str(row["session_id"]),
            panel_id=str(row["panel_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            display_name=str(row["display_name"]),
            project_name=str(row["project_name"]),
        )

    def _activity_run_from_row(self, row: sqlite3.Row) -> ActivityPanelRun:
        """Projects one SQLite row plus ordered membership to an activity run."""
        execution_ids = [
            str(item["execution_id"])
            for item in self._connection.execute(
                """
                SELECT execution_id
                FROM activity_run_executions
                WHERE run_id = ?
                ORDER BY position ASC
                """,
                (str(row["run_id"]),),
            ).fetchall()
        ]
        return ActivityPanelRun(
            run_id=str(row["run_id"]),
            session_id=str(row["session_id"]),
            project_name=str(row["project_name"]),
            started_at=float(row["started_at"]),
            superseded=bool(row["superseded"]),
            execution_ids=execution_ids,
        )

    @classmethod
    def _execution_values(cls, record: ExecutionRecord) -> tuple[object, ...]:
        """Returns one execution's ordered SQLite column values."""
        return (
            record.execution_id,
            record.session_id,
            record.project_name,
            record.tool_name,
            cls._dump_json(record.arguments),
            record.started_at,
            record.status,
            record.finished_at,
            record.request_finished_at,
            record.request_error,
            record.result,
            record.error,
            record.retained_output_id,
            record.retained_output_chars,
            cls._dump_json(record.media) if record.media is not None else None,
            record.durable_job_id,
            record.durable_job_label,
        )

    def _replace_execution_resources(
        self,
        execution_id: str,
        *,
        result: str | None,
        media: dict[str, str] | None,
        retained_output_id: str | None,
    ) -> None:
        """Replaces explicit retained-artifact references owned by one execution."""
        self._connection.execute("DELETE FROM retained_resources WHERE execution_id = ?", (execution_id,))
        entries = self._resource_entries(result=result, media=media, retained_output_id=retained_output_id)
        self._connection.executemany(
            """
            INSERT INTO retained_resources (execution_id, kind, resource_id, size_bytes)
            VALUES (?, ?, ?, ?)
            """,
            [(execution_id, kind, resource_id, size_bytes) for kind, resource_id, size_bytes in entries],
        )

    def _resource_entries(
        self,
        *,
        result: str | None,
        media: dict[str, str] | None,
        retained_output_id: str | None,
    ) -> list[tuple[str, str, int]]:
        """Extracts explicit retained resources once when execution output is persisted."""
        resources: set[tuple[str, str]] = set()
        if media is not None:
            resources.update(("snapshot", token) for token in _FILE_RESOURCE_RE.findall(media.get("uri", "")))
        if result:
            resources.update(("snapshot", token) for token in _FILE_RESOURCE_RE.findall(result))
        if retained_output_id is not None:
            resources.add(("output", retained_output_id))
        return [(kind, resource_id, self._resource_size(kind, resource_id)) for kind, resource_id in sorted(resources)]

    def _resource_size(self, kind: str, resource_id: str) -> int:
        """Returns current on-disk bytes for one retained resource, or zero if unavailable."""
        if kind == "snapshot":
            path = self._serena_home() / "chat_file_snapshots" / resource_id
        elif kind == "output":
            path = self._serena_home() / "tool_outputs" / f"{resource_id}.txt"
        else:
            return 0
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    @classmethod
    def _retained_resource_ids_from_database(cls, kind: str) -> set[str]:
        """Reads retained resource identifiers directly from the authoritative SQLite database."""
        path = cls._default_root() / cls._DATABASE_FILENAME
        if not path.is_file():
            return set()
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT DISTINCT resource_id FROM retained_resources WHERE kind = ?",
                    (kind,),
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return set()
        return {str(row[0]) for row in rows}

    def _drop_session(self, session_id: str) -> bool:
        """Drops one retained session with cascading execution/run/resource deletion."""
        resources = {
            (str(row["kind"]), str(row["resource_id"]))
            for row in self._connection.execute(
                """
                SELECT retained_resources.kind, retained_resources.resource_id
                FROM retained_resources
                JOIN executions ON executions.execution_id = retained_resources.execution_id
                WHERE executions.session_id = ?
                """,
                (session_id,),
            ).fetchall()
        }
        cursor = self._connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        if cursor.rowcount:
            self._pending_artifact_cleanup.update(resources)
            for job_id, owner in list(self._running_job_sessions.items()):
                if owner == session_id:
                    self._running_job_sessions.pop(job_id, None)
            return True
        return False

    def _protected_session_ids(self) -> set[str]:
        """Returns sessions that cannot be evicted while tools or durable jobs are running."""
        rows = self._connection.execute("SELECT DISTINCT session_id FROM executions WHERE status IN ('running', 'queued')").fetchall()
        protected = {str(row["session_id"]) for row in rows}
        protected.update(self._running_job_sessions.values())
        return protected

    def _cleanup_candidate_artifacts(self, candidates: set[tuple[str, str]]) -> None:
        """Deletes newly-unreferenced blobs after the owning SQL transaction commits."""
        for kind, resource_id in candidates:
            retained = self._connection.execute(
                "SELECT 1 FROM retained_resources WHERE kind = ? AND resource_id = ? LIMIT 1",
                (kind, resource_id),
            ).fetchone()
            if retained is not None:
                continue
            if kind == "snapshot":
                path = self._serena_home() / "chat_file_snapshots" / resource_id
            elif kind == "output":
                path = self._serena_home() / "tool_outputs" / f"{resource_id}.txt"
            else:
                continue
            path.unlink(missing_ok=True)

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
        """Returns indexed retained artifact bytes, counting shared blobs once."""
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(size_bytes), 0) AS total_bytes
            FROM (
                SELECT kind, resource_id, MAX(size_bytes) AS size_bytes
                FROM retained_resources
                GROUP BY kind, resource_id
            )
            """
        ).fetchone()
        return int(row["total_bytes"]) if row is not None else 0

    def _prune(self) -> bool:
        """Prunes complete inactive sessions through indexed/set-based queries."""
        changed = False
        protected = self._protected_session_ids()
        cutoff = time.time() - self._retention.max_age.total_seconds()
        exclusion = ""
        params: list[object] = [cutoff]
        if protected:
            placeholders = ",".join("?" for _ in protected)
            exclusion = f" AND session_id NOT IN ({placeholders})"
            params.extend(sorted(protected))
        expired = self._connection.execute(
            f"SELECT session_id FROM sessions WHERE updated_at <= ?{exclusion} ORDER BY updated_at ASC",
            tuple(params),
        ).fetchall()
        for row in expired:
            changed = self._drop_session(str(row["session_id"])) or changed

        # enforce the emergency artifact budget by evicting oldest unprotected sessions whole
        while self._retained_artifact_bytes() > self._retention.max_artifact_bytes:
            protected = self._protected_session_ids()
            exclusion = ""
            params = []
            if protected:
                placeholders = ",".join("?" for _ in protected)
                exclusion = f" WHERE session_id NOT IN ({placeholders})"
                params.extend(sorted(protected))
            row = self._connection.execute(
                f"SELECT session_id FROM sessions{exclusion} ORDER BY updated_at ASC, created_at ASC, session_id ASC LIMIT 1",
                tuple(params),
            ).fetchone()
            if row is None:
                break
            changed = self._drop_session(str(row["session_id"])) or changed
        return changed
