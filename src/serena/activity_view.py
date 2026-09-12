from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from serena.errors import UserFacingError
from serena.execution_metadata import ExecutionMedia
from serena.execution_store import ExecutionRecord, ExecutionStore, ExecutionSummaryRecord, SessionExecutionSummary
from serena.git_metrics import GitLineMetrics, GitMetricsSource
from serena.jobs import JobRecord, JobSnapshot


@dataclass(frozen=True)
class ActivitySummary:
    """Concise semantic summary for one activity entry."""

    detail: str = ""
    scope: str = ""


class ActivityDetailFormatter:
    """Builds concise semantic summaries from structured tool arguments."""

    _MAX_DETAIL_CHARS = 180
    _SUBJECT_KEYS = (
        "label",
        "substring_pattern",
        "file_mask",
        "name_path_pattern",
        "name_path",
        "regex",
        "needle",
        "memory_name",
        "message",
        "command",
        "job_id",
        "query",
        "ref",
        "relative_path",
        "project",
        "topic",
        "remote",
        "branch",
        "output_id",
    )
    _SCOPE_KEYS = (
        "relative_path",
        "paths_include_glob",
        "project",
        "cwd",
        "topic",
        "remote",
        "branch",
    )

    def format(self, tool_name: str, arguments: dict[str, Any]) -> ActivitySummary:
        """Returns the most useful bounded detail and scope for one tool call."""
        summary = self._format_tool_specific(tool_name, arguments)
        if summary is not None:
            return self._bound_summary(summary)

        subject_key, subject = self._first_scalar(arguments, self._SUBJECT_KEYS)
        if not subject:
            return ActivitySummary()
        if subject_key in self._SCOPE_KEYS:
            return self._bound_summary(ActivitySummary(scope=subject))

        _, scope = self._first_scalar(arguments, self._SCOPE_KEYS, excluded_key=subject_key)
        return self._bound_summary(ActivitySummary(detail=subject, scope=scope))

    @staticmethod
    def parse_result(result: str | None) -> Any:
        """Decodes exactly one persisted JSON layer for lossless rich rendering."""
        if result is None:
            return None
        text = result.strip()
        if not text:
            return ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return result

    def _format_tool_specific(self, tool_name: str, arguments: dict[str, Any]) -> ActivitySummary | None:
        """Returns a composite summary for tools whose arguments have coupled meaning."""
        if tool_name in {"rename_symbol", "rename_memory"}:
            source_key = "name_path" if tool_name == "rename_symbol" else "old_name"
            detail = self._join_scalars(arguments, source_key, "new_name", separator=" → ")
            scope = self._scalar(arguments, "relative_path") if tool_name == "rename_symbol" else ""
            return ActivitySummary(detail=detail, scope=scope)
        if tool_name == "git_branch":
            return ActivitySummary(detail=self._join_scalars(arguments, "action", "name"))
        if tool_name == "git_pull":
            return ActivitySummary(detail=self._scalar(arguments, "branch"), scope=self._scalar(arguments, "remote"))
        if tool_name == "replace_content":
            return ActivitySummary(detail=self._scalar(arguments, "needle"), scope=self._scalar(arguments, "relative_path"))
        if tool_name == "render_pdf_page":
            page = self._scalar(arguments, "page")
            return ActivitySummary(detail=f"page {page}" if page else "", scope=self._scalar(arguments, "relative_path"))
        if tool_name == "read_tool_output":
            output_id = self._scalar(arguments, "output_id")
            if output_id:
                output_id = f"{output_id[:8]}…" if len(output_id) > 9 else output_id
            offset = self._scalar(arguments, "offset")
            detail = " · ".join(part for part in (output_id, f"offset {offset}" if offset and offset != "0" else "") if part)
            return ActivitySummary(detail=detail)
        if tool_name == "git_diff":
            return self._format_git_diff(arguments)
        return None

    def _format_git_diff(self, arguments: dict[str, Any]) -> ActivitySummary:
        """Returns the selected Git diff scope and staged qualifier."""
        scope = ""
        paths = arguments.get("paths")
        if isinstance(paths, list):
            clean_paths = [self._clean_scalar(path) for path in paths]
            clean_paths = [path for path in clean_paths if path]
            if clean_paths:
                visible = clean_paths[:3]
                scope = ", ".join(visible)
                if len(clean_paths) > len(visible):
                    scope += f" +{len(clean_paths) - len(visible)}"
        detail = "staged" if arguments.get("staged") is True else ""
        return ActivitySummary(detail=detail, scope=scope)

    def _join_scalars(
        self,
        arguments: dict[str, Any],
        first_key: str,
        second_key: str,
        separator: str = " · ",
    ) -> str:
        """Joins two non-empty scalar arguments without introducing placeholder noise."""
        return separator.join(part for part in (self._scalar(arguments, first_key), self._scalar(arguments, second_key)) if part)

    def _first_scalar(
        self,
        arguments: dict[str, Any],
        keys: tuple[str, ...],
        excluded_key: str | None = None,
    ) -> tuple[str, str]:
        """Returns the first non-empty scalar argument in precedence order."""
        for key in keys:
            if key == excluded_key:
                continue
            value = self._scalar(arguments, key)
            if value:
                return key, value
        return "", ""

    def _scalar(self, arguments: dict[str, Any], key: str) -> str:
        """Returns one normalized scalar argument or an empty string."""
        return self._clean_scalar(arguments.get(key))

    @staticmethod
    def _clean_scalar(value: object) -> str:
        """Normalizes safe scalar display values while rejecting structured content."""
        if not isinstance(value, str | int | float | bool):
            return ""
        return " ".join(str(value).split()).strip()

    def _bound_summary(self, summary: ActivitySummary) -> ActivitySummary:
        """Truncates each summary component to the activity panel's established display bound."""
        return ActivitySummary(detail=self._bound(summary.detail), scope=self._bound(summary.scope))

    def _bound(self, text: str) -> str:
        """Truncates one summary component to the activity panel's established display bound."""
        if len(text) <= self._MAX_DETAIL_CHARS:
            return text
        return text[: self._MAX_DETAIL_CHARS - 3] + "..."


class ActivityJobSource(Protocol):
    """Provides lightweight and detailed durable-job state to the activity view."""

    @property
    def max_concurrent_jobs(self) -> int:
        """Returns the hard global concurrent-job limit."""
        ...

    def list_running_jobs(self) -> list[JobRecord]:
        """Returns current running-job metadata without runtime telemetry."""
        ...

    def get_job_record(self, job_id: str) -> JobRecord:
        """Returns current lightweight metadata for one job."""
        ...

    def get_job_records(self, job_ids: set[str]) -> list[JobRecord]:
        """Returns current lightweight metadata for selected jobs."""
        ...

    def get_job(self, job_id: str) -> JobSnapshot:
        """Returns one detailed job snapshot with bounded output."""
        ...


@dataclass(frozen=True)
class ActivityEntrySummary:
    """Renderer-facing lightweight state for one Serena execution."""

    call_id: str
    tool_name: str
    detail: str
    scope: str
    project_name: str
    started_at: float
    finished_at: float | None
    status: str
    job_id: str | None = None
    job_label: str | None = None


@dataclass(frozen=True)
class ActivityJobSummary:
    """Renderer-facing lightweight state for one durable job."""

    job_id: str
    label: str
    project: str
    status: str
    started_at: float
    finished_at: float | None
    current_turn: bool
    session_id: str | None
    panel_id: str | None = None


@dataclass(frozen=True)
class ActivityLatestSummary:
    """Exact renderer-facing summary of the most recently submitted activity."""

    label: str
    detail: str
    scope: str
    status: str
    started_at: float
    finished_at: float | None


@dataclass(frozen=True)
class ActivityCallDetail:
    """Bounded structured detail for one Serena execution."""

    call_id: str
    tool_name: str
    status: str
    arguments: dict[str, Any]
    result: str | None
    structured_result: Any
    error: str | None
    media: ExecutionMedia | None


@dataclass(frozen=True)
class ActivityJobDetail:
    """Runtime metadata and bounded output for one durable job."""

    job_id: str
    label: str
    project: str
    cwd: str
    status: str
    status_message: str | None
    return_code: int | None
    timeout_seconds: int | None
    elapsed_seconds: float
    seconds_since_last_output: float | None
    memory_bytes: int | None
    cpu_seconds: float | None
    process_count: int | None
    output: str
    output_truncated: bool
    earlier_output_omitted: bool
    has_earlier_output: bool
    cursor_reset: bool


@dataclass(frozen=True)
class ActivitySnapshot:
    """Complete immutable renderer document for one run or retained session."""

    session_id: str
    panel_id: str
    run_id: str | None
    project_name: str
    session_title: str
    started_at: float
    updated_at: float
    superseded: bool
    submission_span_seconds: float | None
    latest_activity: ActivityLatestSummary | None
    git_metrics: GitLineMetrics
    calls: tuple[ActivityEntrySummary, ...]
    jobs: tuple[ActivityJobSummary, ...]
    expanded_call: ActivityCallDetail | None = None
    expanded_job: ActivityJobDetail | None = None


@dataclass(frozen=True)
class ActivitySessionSummary:
    """Compact dashboard discovery state for one retained Serena session."""

    session_id: str
    panel_id: str
    project_name: str
    display_name: str
    started_at: float
    updated_at: float
    active: bool
    tool_count: int
    job_count: int
    latest_activity: ActivityLatestSummary | None
    submission_span_seconds: float | None
    git_metrics: GitLineMetrics


@dataclass(frozen=True)
class ActivityRunningJobs:
    """Compact global durable-job metadata for dashboard chrome."""

    running_jobs: tuple[ActivityJobSummary, ...]
    max_concurrent_jobs: int


@dataclass(frozen=True)
class ActivityOverview:
    """Complete compact retained-session overview document."""

    sessions: tuple[ActivitySessionSummary, ...]
    running_jobs: tuple[ActivityJobSummary, ...]
    max_concurrent_jobs: int


class ActivityView:
    """Projects canonical execution, job and Git state into immutable UI snapshots."""

    def __init__(
        self,
        execution_store: ExecutionStore,
        job_source: ActivityJobSource,
        git_metrics_source: GitMetricsSource | None = None,
    ) -> None:
        self._execution_store = execution_store
        self._job_source = job_source
        self._git_metrics_source = git_metrics_source
        self._formatter = ActivityDetailFormatter()

    def dashboard_overview(self) -> ActivityOverview:
        """Returns every retained session as one compact active-first overview document."""
        job_overview = self.dashboard_jobs()
        running_jobs = job_overview.running_jobs
        running_by_session: dict[str, list[ActivityJobSummary]] = {}
        for job in running_jobs:
            if job.session_id:
                running_by_session.setdefault(job.session_id, []).append(job)

        sessions: list[ActivitySessionSummary] = []
        for summary in self._execution_store.list_session_execution_summaries():
            latest_call = self._entry_summary(summary.latest_execution) if summary.latest_execution is not None else None
            calls = (latest_call,) if latest_call is not None else ()
            sessions.append(
                ActivitySessionSummary(
                    session_id=summary.session_id,
                    panel_id=summary.panel_id,
                    project_name=summary.project_name,
                    display_name=summary.display_name,
                    started_at=summary.created_at,
                    updated_at=summary.updated_at,
                    active=summary.running_execution_count > 0 or bool(running_by_session.get(summary.session_id)),
                    tool_count=summary.execution_count,
                    job_count=summary.durable_job_count,
                    latest_activity=self._latest_activity(calls, running_by_session.get(summary.session_id, ())),
                    submission_span_seconds=self._submission_span(summary),
                    git_metrics=summary.git_metrics,
                )
            )
        sessions.sort(key=lambda item: (not item.active, -item.updated_at, item.panel_id))
        return ActivityOverview(
            sessions=tuple(sessions),
            running_jobs=running_jobs,
            max_concurrent_jobs=job_overview.max_concurrent_jobs,
        )

    def dashboard_jobs(self) -> ActivityRunningJobs:
        """Returns current global durable-job metadata without scanning retained sessions."""
        running_jobs = tuple(self._job_summary(record, current_turn=False) for record in self._list_running_jobs_safely())
        return ActivityRunningJobs(
            running_jobs=running_jobs,
            max_concurrent_jobs=self._job_source.max_concurrent_jobs,
        )

    def for_run(self, session_id: str, run_id: str, *, refresh_git_metrics: bool = False) -> ActivitySnapshot:
        """Returns one ChatGPT-owned activity run with current-turn/background-job semantics."""
        run = self._execution_store.get_activity_run(run_id)
        if run is None or run.session_id != session_id:
            raise ValueError("Activity run is not available in this session")
        records = [
            record
            for execution_id in run.execution_ids
            if (record := self._execution_store.get_execution_summary(execution_id)) is not None
        ]
        current_job_ids = tuple(dict.fromkeys(record.durable_job_id for record in records if record.durable_job_id))
        jobs = self._run_jobs(current_job_ids, superseded=run.superseded)
        session = self._execution_store.get_session_by_panel_id(self._execution_store.panel_id_for_session(session_id))
        updated_at = max(
            [
                run.started_at,
                *(record.finished_at or record.started_at for record in records),
                *(job.finished_at or job.started_at for job in jobs),
            ],
        )
        known_job_labels = self._job_labels(records)
        calls = tuple(self._entry_summary(record, known_job_labels=known_job_labels) for record in records)
        git_metrics = session.git_metrics if session is not None else GitLineMetrics()
        if refresh_git_metrics:
            git_metrics = self._refresh_git_metrics(run.project_name)
            self._execution_store.update_session_git_metrics(session_id, git_metrics)
        return ActivitySnapshot(
            session_id=session_id,
            panel_id=self._execution_store.panel_id_for_session(session_id),
            run_id=run.run_id,
            project_name=run.project_name,
            session_title=session.display_name if session is not None else "",
            started_at=run.started_at,
            updated_at=updated_at,
            superseded=run.superseded,
            submission_span_seconds=self._submission_span_records(records),
            latest_activity=self._latest_activity(calls, jobs),
            git_metrics=git_metrics,
            calls=calls,
            jobs=jobs,
        )

    def for_session(self, panel_id: str, expanded_entry_id: str | None = None) -> ActivitySnapshot:
        """Returns one complete retained-session document with at most one expanded entry detail."""
        session = self._execution_store.get_session_by_panel_id(panel_id)
        if session is None:
            raise KeyError(panel_id)
        records = self._execution_store.list_session_execution_items(session.session_id)
        job_ids = tuple(dict.fromkeys(record.durable_job_id for record in records if record.durable_job_id))
        jobs_by_id = {record.job_id: record for record in self._get_job_records_safely(set(job_ids))}
        jobs = tuple(self._job_summary(jobs_by_id[job_id], current_turn=False) for job_id in job_ids if job_id in jobs_by_id)
        expanded_call: ActivityCallDetail | None = None
        expanded_job: ActivityJobDetail | None = None
        if expanded_entry_id is not None:
            matching_execution = next((record for record in records if record.execution_id == expanded_entry_id), None)
            if matching_execution is not None:
                full_record = self._execution_store.get_execution(matching_execution.execution_id)
                if full_record is None:
                    raise ValueError("Activity entry is no longer available in this session")
                expanded_call = self._call_detail(full_record)
            elif expanded_entry_id in job_ids:
                expanded_job = self._job_detail(expanded_entry_id)
            else:
                raise ValueError("Activity entry is not available in this session")
        updated_at = max(
            [
                session.updated_at,
                *(record.finished_at or record.started_at for record in records),
                *(job.finished_at or job.started_at for job in jobs),
            ],
        )
        known_job_labels = self._job_labels(records)
        calls = tuple(self._entry_summary(record, known_job_labels=known_job_labels) for record in records)
        return ActivitySnapshot(
            session_id=session.session_id,
            panel_id=session.panel_id,
            run_id=None,
            project_name=session.project_name,
            session_title=session.display_name,
            started_at=session.created_at,
            updated_at=updated_at,
            superseded=False,
            submission_span_seconds=self._submission_span_records(records),
            latest_activity=self._latest_activity(calls, jobs),
            git_metrics=session.git_metrics,
            calls=calls,
            jobs=jobs,
            expanded_call=expanded_call,
            expanded_job=expanded_job,
        )

    def call_detail(self, session_id: str, run_id: str, call_id: str) -> ActivityCallDetail:
        """Returns bounded call detail after validating run/session ownership."""
        record = self._execution_in_run(session_id, run_id, call_id)
        return self._call_detail(record)

    def call_media(self, session_id: str, run_id: str, call_id: str) -> ExecutionMedia:
        """Returns retrievable media metadata after validating run/session ownership."""
        record = self._execution_in_run(session_id, run_id, call_id)
        media = ExecutionMedia.from_storage_dict(record.media)
        if media is None:
            raise ValueError("Activity call has no retrievable media")
        return media

    def job_detail(self, session_id: str, run_id: str, job_id: str) -> ActivityJobDetail:
        """Returns job detail after validating current-run/background visibility."""
        run = self._execution_store.get_activity_run(run_id)
        if run is None or run.session_id != session_id:
            raise ValueError("Activity run is not available in this session")
        current_job_ids = {
            record.durable_job_id
            for execution_id in run.execution_ids
            if (record := self._execution_store.get_execution_summary(execution_id)) is not None and record.durable_job_id is not None
        }
        if job_id not in current_job_ids:
            visible_background = any(record.job_id == job_id for record in self._list_running_jobs_safely())
            if run.superseded or not visible_background:
                raise ValueError("Activity job is not available in this run")
        return self._job_detail(job_id)

    def panel_id_for_job(self, job_id: str) -> str | None:
        """Resolves a durable job through canonical job ownership with execution-index fallback."""
        record = self._get_job_record_safely(job_id)
        if record is not None and record.session_id is not None:
            return self._execution_store.panel_id_for_session(record.session_id)
        return self._execution_store.panel_id_for_job(job_id)

    def _execution_in_run(self, session_id: str, run_id: str, call_id: str) -> ExecutionRecord:
        """Returns one execution after validating run and session ownership."""
        run = self._execution_store.get_activity_run(run_id)
        if run is None or run.session_id != session_id or call_id not in run.execution_ids:
            raise ValueError("Activity call is not available in this run")
        record = self._execution_store.get_execution(call_id)
        if record is None:
            raise ValueError("Activity call is not available in this run")
        return record

    def _entry_summary(
        self,
        record: ExecutionRecord | ExecutionSummaryRecord,
        *,
        known_job_labels: dict[str, str] | None = None,
    ) -> ActivityEntrySummary:
        """Projects one canonical execution into its lightweight renderer row."""
        arguments = record.arguments
        if record.tool_name == "job_status":
            job_id = arguments.get("job_id")
            if isinstance(job_id, str) and job_id and known_job_labels is not None:
                known_label = known_job_labels.get(job_id)
                if known_label:
                    arguments = {**arguments, "label": known_label}
        summary = self._formatter.format(record.tool_name, arguments)
        label = record.durable_job_label or self._argument_label(record.arguments)
        return ActivityEntrySummary(
            call_id=record.execution_id,
            tool_name=record.tool_name,
            detail=label or summary.detail,
            scope=summary.scope,
            project_name=record.project_name,
            started_at=record.started_at,
            finished_at=record.finished_at,
            status=record.status,
            job_id=record.durable_job_id,
            job_label=label or None,
        )

    def _call_detail(self, record: ExecutionRecord) -> ActivityCallDetail:
        """Projects one canonical execution into bounded on-demand detail."""
        media = ExecutionMedia.from_storage_dict(record.media)
        raw_result = record.error or record.result
        return ActivityCallDetail(
            call_id=record.execution_id,
            tool_name=record.tool_name,
            status=record.status,
            arguments=record.arguments,
            result=None if media is not None else raw_result,
            structured_result=None if media is not None else self._formatter.parse_result(raw_result),
            error=record.error,
            media=media,
        )

    def _job_detail(self, job_id: str) -> ActivityJobDetail:
        """Projects one explicit durable-job snapshot into bounded on-demand detail."""
        snapshot = self._job_source.get_job(job_id)
        record = snapshot.record
        runtime = snapshot.runtime
        output = snapshot.output
        return ActivityJobDetail(
            job_id=record.job_id,
            label=record.label or "background job",
            project=record.project_name or "",
            cwd=record.cwd,
            status=record.status.value,
            status_message=record.status_message,
            return_code=record.return_code,
            timeout_seconds=record.timeout_seconds,
            elapsed_seconds=runtime.elapsed_seconds,
            seconds_since_last_output=runtime.seconds_since_last_output,
            memory_bytes=runtime.memory_bytes,
            cpu_seconds=runtime.cpu_seconds,
            process_count=runtime.process_count,
            output=output.output if output is not None else "",
            output_truncated=output.output_truncated if output is not None else False,
            earlier_output_omitted=output.earlier_output_omitted if output is not None else False,
            has_earlier_output=output.has_earlier_output if output is not None else False,
            cursor_reset=output.cursor_reset if output is not None else False,
        )

    def _run_jobs(self, current_job_ids: tuple[str, ...], *, superseded: bool) -> tuple[ActivityJobSummary, ...]:
        """Returns current-turn jobs plus unrelated running jobs for one active run."""
        visible: list[ActivityJobSummary] = []
        current_records = {record.job_id: record for record in self._get_job_records_safely(set(current_job_ids))}
        for job_id in current_job_ids:
            record = current_records.get(job_id)
            if record is not None:
                visible.append(self._job_summary(record, current_turn=True))
        if not superseded:
            current = set(current_job_ids)
            visible.extend(
                self._job_summary(record, current_turn=False) for record in self._list_running_jobs_safely() if record.job_id not in current
            )
        visible.sort(key=lambda job: job.started_at, reverse=True)
        return tuple(visible)

    @staticmethod
    def _job_labels(records: Sequence[ExecutionRecord | ExecutionSummaryRecord]) -> dict[str, str]:
        """Collects durable-job labels from executions already present in one selected document."""
        labels: dict[str, str] = {}
        for record in records:
            job_id = record.durable_job_id
            if not job_id:
                continue
            label = record.durable_job_label or ActivityView._argument_label(record.arguments)
            if label:
                labels[job_id] = label
        return labels

    def _get_job_record_safely(self, job_id: str) -> JobRecord | None:
        """Returns lightweight job metadata without letting backend failure break activity rendering."""
        try:
            return self._job_source.get_job_record(job_id)
        except (KeyError, OSError, RuntimeError, UserFacingError, ValueError):
            return None

    def _get_job_records_safely(self, job_ids: set[str]) -> list[JobRecord]:
        """Returns selected lightweight job metadata without letting backend failure break rendering."""
        if not job_ids:
            return []
        try:
            return self._job_source.get_job_records(job_ids)
        except (OSError, RuntimeError, UserFacingError, ValueError):
            return []

    def _list_running_jobs_safely(self) -> list[JobRecord]:
        """Returns current running-job metadata without letting backend failure break polling."""
        try:
            return self._job_source.list_running_jobs()
        except (OSError, RuntimeError, ValueError):
            return []

    def _refresh_git_metrics(self, project_name: str) -> GitLineMetrics:
        """Recomputes Git metrics for one project when a session explicitly requests a snapshot."""
        if self._git_metrics_source is None or not project_name:
            return GitLineMetrics()
        return self._git_metrics_source.refresh_project_git_metrics(project_name) or GitLineMetrics()

    def _job_summary(self, record: JobRecord, *, current_turn: bool) -> ActivityJobSummary:
        """Projects lightweight durable-job metadata into a renderer row."""
        return ActivityJobSummary(
            job_id=record.job_id,
            label=record.label or "background job",
            project=record.project_name or "",
            status=record.status.value,
            started_at=datetime.fromisoformat(record.created_at).timestamp(),
            finished_at=datetime.fromisoformat(record.finished_at).timestamp() if record.finished_at else None,
            current_turn=current_turn,
            session_id=record.session_id,
            panel_id=self._execution_store.panel_id_for_session(record.session_id) if record.session_id else None,
        )

    @staticmethod
    def _latest_activity(
        calls: Sequence[ActivityEntrySummary],
        jobs: Sequence[ActivityJobSummary],
    ) -> ActivityLatestSummary | None:
        """Returns the most recently submitted call or job using one renderer-wide rule."""
        latest_call = max(calls, key=lambda item: item.started_at, default=None)
        latest_job = max(jobs, key=lambda item: item.started_at, default=None)
        if latest_call is None and latest_job is None:
            return None
        if latest_job is not None and (latest_call is None or latest_job.started_at > latest_call.started_at):
            origin = next((call for call in calls if call.job_id == latest_job.job_id), None)
            detail = (origin.scope if origin is not None else "") or latest_job.project or "durable job"
            return ActivityLatestSummary(
                label=latest_job.label or "Job",
                detail=detail,
                scope=latest_job.project,
                status=latest_job.status,
                started_at=latest_job.started_at,
                finished_at=latest_job.finished_at,
            )
        assert latest_call is not None
        return ActivityLatestSummary(
            label=latest_call.tool_name,
            detail=latest_call.detail,
            scope=latest_call.scope or latest_call.project_name,
            status=latest_call.status,
            started_at=latest_call.started_at,
            finished_at=latest_call.finished_at,
        )

    @staticmethod
    def _submission_span_records(records: Sequence[ExecutionSummaryRecord]) -> float | None:
        """Returns the first-to-latest execution submission span for one materialized document."""
        if not records:
            return None
        starts = [record.started_at for record in records]
        return max(starts) - min(starts)

    @staticmethod
    def _submission_span(summary: SessionExecutionSummary) -> float | None:
        """Returns the retained session's first-to-latest execution submission span."""
        if summary.first_execution_started_at is None or summary.latest_execution_started_at is None:
            return None
        return summary.latest_execution_started_at - summary.first_execution_started_at

    @staticmethod
    def _argument_label(arguments: dict[str, Any]) -> str:
        """Returns an explicit label argument when present."""
        label = arguments.get("label")
        return label if isinstance(label, str) else ""
