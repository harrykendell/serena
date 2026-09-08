import ast
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink

from serena.activity_history import ActivityHistoryStore
from serena.jobs import JobManager, JobRecord, JobSnapshot, JobStatus
from serena.session import get_mcp_session_id  # noqa: F401 - compatibility re-export

ACTIVITY_RESOURCE_URI = "ui://serena/activity-v25.html"
_ACTIVITY_RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"
_MAX_RUNS = 128
_MAX_CALLS_PER_RUN = 100


@dataclass(frozen=True)
class ActivityMedia:
    """Retrievable media or file metadata associated with one tool result."""

    media_type: Literal["image", "audio", "file"]
    name: str
    mime_type: str
    uri: str

    @classmethod
    def from_result(cls, result: object) -> "ActivityMedia | None":
        """Extracts a persistent Serena file resource carried by a tool result."""
        link: ResourceLink | None
        if isinstance(result, ResourceLink):
            link = result
        elif isinstance(result, CallToolResult):
            link = next((block for block in result.content if isinstance(block, ResourceLink)), None)
        else:
            candidate = getattr(result, "file_link", None)
            link = candidate if isinstance(candidate, ResourceLink) else None
        if link is None:
            return None

        uri = str(link.uri)
        if not uri.startswith("serena-file://export/"):
            return None
        mime_type = str(link.mimeType or "application/octet-stream")
        if mime_type.startswith("image/"):
            media_type: Literal["image", "audio", "file"] = "image"
        elif mime_type.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "file"
        return cls(
            media_type=media_type,
            name=str(link.name or "Serena file"),
            mime_type=mime_type,
            uri=uri,
        )

    @classmethod
    def from_serialized_result(cls, result: str | None) -> "ActivityMedia | None":
        """Recovers media metadata from activity history written before structured media storage."""
        if not result:
            return None
        token_match = re.search(r"serena-file://export/([0-9a-f]{48})", result)
        if token_match is None:
            return None
        name_match = re.search(r"ResourceLink\(name=(['\"])(.*?)\1", result, re.DOTALL)
        mime_match = re.search(r"mimeType=(['\"])(.*?)\1", result, re.DOTALL)
        mime_type = mime_match.group(2) if mime_match is not None else "application/octet-stream"
        if mime_type.startswith("image/"):
            media_type: Literal["image", "audio", "file"] = "image"
        elif mime_type.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "file"
        return cls(
            media_type=media_type,
            name=name_match.group(2) if name_match is not None else "Serena file",
            mime_type=mime_type,
            uri=f"serena-file://export/{token_match.group(1)}",
        )

    @classmethod
    def from_storage_dict(cls, payload: object) -> "ActivityMedia | None":
        """Reconstructs media metadata from persisted activity state."""
        if not isinstance(payload, dict):
            return None
        mapping = cast(dict[str, Any], payload)
        media_type = mapping.get("type")
        uri = mapping.get("uri")
        if media_type not in {"image", "audio", "file"} or not isinstance(uri, str):
            return None
        return cls(
            media_type=cast(Literal["image", "audio", "file"], media_type),
            name=str(mapping.get("name") or "Serena file"),
            mime_type=str(mapping.get("mime_type") or "application/octet-stream"),
            uri=uri,
        )

    def public_dict(self) -> dict[str, str]:
        """Returns media metadata safe to expose to the activity widget."""
        return {"type": self.media_type, "name": self.name, "mime_type": self.mime_type}

    def storage_dict(self) -> dict[str, str]:
        """Returns complete metadata required to reopen the retained file resource."""
        return {**self.public_dict(), "uri": self.uri}


@dataclass
class ActivityCall:
    """One tool invocation displayed in the ChatGPT activity panel."""

    call_id: str
    tool_name: str
    detail: str
    started_at: float
    scope: str = ""
    project_name: str = ""
    arguments: str = field(default="{}", repr=False)
    finished_at: float | None = None
    status: str = "running"
    result: str | None = field(default=None, repr=False)
    media: ActivityMedia | None = field(default=None, repr=False)
    job_id: str | None = None
    job_label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serializes the lightweight call state used by the activity widget."""
        payload: dict[str, Any] = {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "detail": self.detail,
            "project_name": self.project_name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
        }
        if self.scope:
            payload["scope"] = self.scope
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        if self.job_label is not None:
            payload["job_label"] = self.job_label
        return payload

    def storage_dict(self) -> dict[str, Any]:
        """Serializes complete bounded call state required for historical rehydration."""
        return {
            **self.as_dict(),
            "scope": self.scope,
            "arguments": self.arguments,
            "result": self.result,
            "media": self.media.storage_dict() if self.media is not None else None,
            "job_id": self.job_id,
            "job_label": self.job_label,
        }

    @classmethod
    def from_storage_dict(cls, payload: dict[str, Any]) -> "ActivityCall":
        """Reconstructs one retained call from its persisted representation."""
        finished_at = payload.get("finished_at")
        stored_result = str(payload["result"]) if payload.get("result") is not None else None
        media = ActivityMedia.from_storage_dict(payload.get("media")) or ActivityMedia.from_serialized_result(stored_result)
        return cls(
            call_id=str(payload["call_id"]),
            tool_name=str(payload.get("tool_name") or ""),
            detail=str(payload.get("detail") or ""),
            scope=str(payload.get("scope") or ""),
            project_name=str(payload.get("project_name") or ""),
            arguments=str(payload.get("arguments") or "{}"),
            started_at=float(payload.get("started_at") or 0.0),
            finished_at=float(finished_at) if isinstance(finished_at, int | float) else None,
            status=str(payload.get("status") or "completed"),
            result=None if media is not None else stored_result,
            media=media,
            job_id=str(payload["job_id"]) if payload.get("job_id") is not None else None,
            job_label=str(payload["job_label"]) if payload.get("job_label") is not None else None,
        )


@dataclass
class ActivityRun:
    """One model-driven Serena activity panel within a client session."""

    run_id: str
    session_id: str
    project_name: str
    started_at: float
    calls: list[ActivityCall] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    retained_jobs: list[dict[str, Any]] = field(default_factory=list, repr=False)
    superseded: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serializes the run-local state used by the activity widget."""
        return {
            "run_id": self.run_id,
            "project_name": self.project_name,
            "started_at": self.started_at,
            "superseded": self.superseded,
            "calls": [call.as_dict() for call in self.calls],
        }

    def storage_dict(self) -> dict[str, Any]:
        """Serializes complete bounded run state required for historical rehydration."""
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "project_name": self.project_name,
            "started_at": self.started_at,
            "superseded": self.superseded,
            "calls": [call.storage_dict() for call in self.calls],
            "job_ids": list(self.job_ids),
            "retained_jobs": self.retained_jobs,
        }

    @classmethod
    def from_storage_dict(cls, payload: dict[str, Any]) -> "ActivityRun":
        """Reconstructs one retained run from its persisted representation."""
        calls = payload.get("calls")
        job_ids = payload.get("job_ids")
        retained_jobs = payload.get("retained_jobs")
        return cls(
            run_id=str(payload["run_id"]),
            session_id=str(payload["session_id"]),
            project_name=str(payload.get("project_name") or ""),
            started_at=float(payload.get("started_at") or 0.0),
            calls=[ActivityCall.from_storage_dict(call) for call in calls if isinstance(call, dict)] if isinstance(calls, list) else [],
            job_ids=[str(job_id) for job_id in job_ids if isinstance(job_id, str)] if isinstance(job_ids, list) else [],
            retained_jobs=[dict(job) for job in retained_jobs if isinstance(job, dict)] if isinstance(retained_jobs, list) else [],
            superseded=bool(payload.get("superseded")),
        )


class ActivityJobSource(Protocol):
    """Provides retained durable-job metadata and output for the activity panel."""

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        """Returns running jobs followed by recent terminal jobs."""
        ...

    def get_job(self, job_id: str) -> JobSnapshot:
        """Returns one job snapshot with bounded retained output."""
        ...


@dataclass(frozen=True)
class ActivitySummary:
    """Concise semantic summary for one activity entry."""

    detail: str = ""
    scope: str = ""


class ActivityDetailFormatter:
    """Builds concise semantic summaries from tool arguments."""

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
        # prefer composites whose meaning cannot be recovered from generic precedence
        summary = self._format_tool_specific(tool_name, arguments)
        if summary is not None:
            return self._bound_summary(summary)

        # choose the semantic subject, then separate any distinct scope for top-line display
        subject_key, subject = self._first_scalar(arguments, self._SUBJECT_KEYS)
        if not subject:
            return ActivitySummary()
        if subject_key in self._SCOPE_KEYS:
            return self._bound_summary(ActivitySummary(scope=subject))

        _, scope = self._first_scalar(arguments, self._SCOPE_KEYS, excluded_key=subject_key)
        return self._bound_summary(ActivitySummary(detail=subject, scope=scope))

    def format_parameters(self, tool_name: str, parameters: str | None) -> ActivitySummary:
        """Returns a semantic summary from Serena's logged keyword-argument representation."""
        arguments = self.parse_parameters(parameters)
        if arguments is None:
            if not parameters:
                return ActivitySummary()
            return ActivitySummary(detail=self._bound(" ".join(parameters.split())))
        return self.format(tool_name, arguments)

    def parse_parameters(self, parameters: str | None) -> dict[str, Any] | None:
        """Parses logged keyword arguments into JSON-safe values for rich detail rendering."""
        if not parameters:
            return {}

        # parse Serena's Python-like keyword argument representation without evaluating code
        try:
            expression = ast.parse(f"_tool({parameters})", mode="eval").body
            if not isinstance(expression, ast.Call) or expression.args:
                raise ValueError("tool parameters were not keyword arguments")
            arguments: dict[str, Any] = {}
            for keyword in expression.keywords:
                if keyword.arg is None:
                    continue
                arguments[keyword.arg] = self._json_safe_literal(ast.literal_eval(keyword.value))
            return arguments
        except (SyntaxError, ValueError, TypeError):
            pass

        # accept older detail sources that stored a complete JSON or Python-literal mapping
        for parser in (json.loads, ast.literal_eval):
            try:
                value = parser(parameters)
            except (json.JSONDecodeError, SyntaxError, ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return {str(key): self._json_safe_literal(item) for key, item in value.items()}
        return None

    @classmethod
    def _json_safe_literal(cls, value: Any) -> Any:
        """Normalizes literal values so Flask can serialize them without losing useful structure."""
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, dict):
            return {str(key): cls._json_safe_literal(item) for key, item in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [cls._json_safe_literal(item) for item in value]
        return str(value)

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
            return ActivitySummary(
                detail=self._scalar(arguments, "branch"),
                scope=self._scalar(arguments, "remote"),
            )
        if tool_name == "replace_content":
            return ActivitySummary(
                detail=self._scalar(arguments, "needle"),
                scope=self._scalar(arguments, "relative_path"),
            )
        if tool_name == "render_pdf_page":
            page = self._scalar(arguments, "page")
            return ActivitySummary(
                detail=f"page {page}" if page else "",
                scope=self._scalar(arguments, "relative_path"),
            )
        if tool_name == "read_tool_output":
            output_id = self._scalar(arguments, "output_id")
            if output_id:
                output_id = f"{output_id[:8]}…" if len(output_id) > 9 else output_id
            offset = self._scalar(arguments, "offset")
            detail = " · ".join(
                part
                for part in (
                    output_id,
                    f"offset {offset}" if offset and offset != "0" else "",
                )
                if part
            )
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
        return separator.join(
            part
            for part in (
                self._scalar(arguments, first_key),
                self._scalar(arguments, second_key),
            )
            if part
        )

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


class ActivityTracker:
    """Tracks Serena tool activity and lightweight durable-job state per ChatGPT conversation."""

    _JOB_LIST_LIMIT = 100
    _JOB_CACHE_SECONDS = 2.0

    def __init__(
        self,
        job_source: ActivityJobSource | None = None,
        history_store: ActivityHistoryStore | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, ActivityRun] = OrderedDict()
        self._current_run_by_session: dict[str, str] = {}
        self._job_source = job_source or JobManager()
        self._job_cache_at = 0.0
        self._job_cache: list[JobRecord] = []
        self._history_store = history_store if history_store is not None else (ActivityHistoryStore() if job_source is None else None)
        self._load_history()

    def _load_history(self) -> None:
        """Loads persisted runs as historical panels without making them current."""
        if self._history_store is None:
            return

        loaded_at = time.time()
        for payload in self._history_store.load():
            try:
                run = ActivityRun.from_storage_dict(payload)
            except (KeyError, TypeError, ValueError):
                continue

            changed = not run.superseded
            run.superseded = True
            for call in run.calls:
                if call.status != "running":
                    continue
                call.status = "cancelled"
                call.finished_at = call.finished_at or loaded_at
                changed = True
            self._runs[run.run_id] = run

            if changed:
                self._save_run(run)
        self._prune_runs()

    def _snapshot_jobs(self, run: ActivityRun, records: list[JobRecord]) -> None:
        """Retains lightweight rows for jobs owned by one activity turn."""
        if not run.job_ids:
            return

        owned = {record.job_id: record for record in records if record.job_id in run.job_ids}
        retained = {str(job.get("job_id")): dict(job) for job in run.retained_jobs if job.get("job_id")}
        for job_id, record in owned.items():
            retained[job_id] = self._job_payload(record) | {"current_turn": True}
        next_jobs = [retained[job_id] for job_id in run.job_ids if job_id in retained]
        if next_jobs == run.retained_jobs:
            return
        run.retained_jobs = next_jobs
        self._save_run(run)

    def _save_run(self, run: ActivityRun) -> None:
        """Persists one run when history retention is enabled."""
        if self._history_store is None:
            return
        try:
            self._history_store.save(run.storage_dict())
        except (OSError, ValueError):
            pass

    def start_run(self, session_id: str, project_name: str) -> dict[str, Any]:
        """Starts a new activity run for ``session_id`` and returns its initial snapshot.

        Any tool still running in the superseded panel is carried into the new run so
        opening another activity panel cannot orphan its live state.
        """
        with self._lock:
            previous_run_id = self._current_run_by_session.get(session_id)
            previous_run = self._runs.get(previous_run_id) if previous_run_id is not None else None
            continuing_calls: list[ActivityCall] = []
            if previous_run is not None:
                previous_run.superseded = True
                continuing_calls = [call for call in previous_run.calls if call.status == "running"]
                self._save_run(previous_run)

            run = ActivityRun(
                run_id=uuid.uuid4().hex,
                session_id=session_id,
                project_name=project_name,
                started_at=time.time(),
                calls=continuing_calls,
            )
            self._runs[run.run_id] = run
            self._current_run_by_session[session_id] = run.run_id
            self._save_run(run)
            self._prune_runs()
            run_id = run.run_id

        return self.get_run(session_id, run_id)

    def update_project(self, session_id: str, project_name: str) -> None:
        """Updates project attribution for the current activity run of ``session_id``."""
        with self._lock:
            run_id = self._current_run_by_session.get(session_id)
            run = self._runs.get(run_id) if run_id is not None else None
            if run is not None:
                run.project_name = project_name
                self._save_run(run)

    def start_tool(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        project_name: str = "",
    ) -> str | None:
        """Records one tool invocation when an activity run is active for ``session_id``."""
        with self._lock:
            run_id = self._current_run_by_session.get(session_id)
            run = self._runs.get(run_id) if run_id is not None else None
            if run is None:
                return None

            summary = self._summarize_arguments(tool_name, arguments)
            if tool_name == "job_status":
                job_id = arguments.get("job_id")
                if isinstance(job_id, str) and job_id:
                    label = self._known_job_label(job_id)
                    if label:
                        summary = self._summarize_arguments(tool_name, {**arguments, "label": label})
            call = ActivityCall(
                call_id=uuid.uuid4().hex,
                tool_name=tool_name,
                detail=summary.detail,
                scope=summary.scope,
                started_at=time.time(),
                project_name=project_name or run.project_name,
                arguments=self._serialize_value(arguments),
            )
            run.calls.append(call)
            if len(run.calls) > _MAX_CALLS_PER_RUN:
                del run.calls[: len(run.calls) - _MAX_CALLS_PER_RUN]
            self._save_run(run)
            return call.call_id

    def finish_tool(
        self,
        call_id: str | None,
        succeeded: bool,
        result: object | None = None,
        project_name: str | None = None,
    ) -> None:
        """Marks one tracked call terminal in every activity run retaining it."""
        if call_id is None:
            return

        with self._lock:
            # collect every panel that retains the carried call
            owners: list[tuple[ActivityRun, ActivityCall]] = []
            for run in self._runs.values():
                owners.extend((run, call) for call in run.calls if call.call_id == call_id)
            if not owners:
                return

            # update the call lifecycle consistently across old and continuing panels
            status = "completed" if succeeded else "failed"
            finished_at = time.time()
            media = ActivityMedia.from_result(result) if result is not None else None
            serialized_result = self._serialize_value(result) if result is not None and media is None else None
            for run, call in owners:
                call.status = status
                call.finished_at = finished_at
                call.result = serialized_result
                call.media = media
                if project_name is not None:
                    call.project_name = project_name
                self._save_run(run)

            # enrich job-related calls with the durable job identity returned by the tool
            first_call = owners[0][1]
            if not succeeded or first_call.tool_name not in {"start_job", "job_status"}:
                return

            job_id, label = self._extract_job_identity(result)
            if job_id is None:
                return

            for run, call in owners:
                call.job_id = job_id
                call.job_label = label
                if label is not None:
                    call.detail = label
                if first_call.tool_name == "start_job" and job_id not in run.job_ids:
                    run.job_ids.append(job_id)
                self._save_run(run)
            if first_call.tool_name == "start_job":
                self._job_cache_at = 0.0

    def get_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        """Returns one session-owned run enriched with the jobs relevant to that panel."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")
            payload = run.as_dict()
            current_job_ids = list(run.job_ids)
            superseded = run.superseded
            retained_jobs = {str(job.get("job_id")): dict(job) for job in run.retained_jobs if job.get("job_id")}

        records = self._list_jobs_safely()
        records_by_id = {record.job_id: record for record in records}
        visible_jobs: list[dict[str, Any]] = []
        for job_id in current_job_ids:
            record = records_by_id.get(job_id)
            if record is not None:
                visible_jobs.append(self._job_payload(record) | {"current_turn": True})
            elif job_id in retained_jobs:
                visible_jobs.append(retained_jobs[job_id] | {"current_turn": True})

        if not superseded:
            visible_jobs.extend(
                self._job_payload(record) | {"current_turn": False}
                for record in records
                if record.job_id not in current_job_ids and record.status is JobStatus.RUNNING
            )

        visible_jobs.sort(key=lambda job: float(job.get("started_at") or 0.0), reverse=True)
        payload["jobs"] = visible_jobs

        with self._lock:
            current = self._runs.get(run_id)
            if current is run:
                self._snapshot_jobs(run, records)
        return payload

    def get_call_detail(self, session_id: str, run_id: str, call_id: str) -> dict[str, Any]:
        """Returns bounded parameters and result detail for one call in a session-owned run."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")
            for call in run.calls:
                if call.call_id == call_id:
                    return {
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "status": call.status,
                        "arguments": call.arguments,
                        "result": call.result,
                        "media": call.media.public_dict() if call.media is not None else None,
                    }
        raise ValueError("Activity call is not available in this run")

    def get_call_media(self, session_id: str, run_id: str, call_id: str) -> ActivityMedia:
        """Returns retrievable media metadata for one session-owned activity call."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")
            for call in run.calls:
                if call.call_id == call_id:
                    if call.media is None:
                        raise ValueError("Activity call has no retained media")
                    return call.media
        raise ValueError("Activity call is not available in this run")

    def get_job_detail(self, session_id: str, run_id: str, job_id: str) -> dict[str, Any]:
        """Returns runtime metadata and bounded output for one job visible in a session-owned run."""
        # validate panel ownership and retained current-turn jobs
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")
            current_turn = job_id in run.job_ids
            superseded = run.superseded

        # admit globally running jobs only while this is the current panel
        if not current_turn:
            visible_background_job = any(
                record.job_id == job_id and record.status is JobStatus.RUNNING for record in self._list_jobs_safely()
            )
            if superseded or not visible_background_job:
                raise ValueError("Activity job is not available in this run")

        # read one bounded latest-output snapshot without blocking the activity poller
        snapshot = self._job_source.get_job(job_id)
        record = snapshot.record
        runtime = snapshot.runtime
        output = snapshot.output
        return {
            "job_id": record.job_id,
            "label": record.label or "background job",
            "project": record.project_name or "",
            "cwd": record.cwd,
            "status": record.status.value,
            "status_message": record.status_message,
            "return_code": record.return_code,
            "timeout_seconds": record.timeout_seconds,
            "elapsed_seconds": runtime.elapsed_seconds,
            "seconds_since_last_output": runtime.seconds_since_last_output,
            "memory_bytes": runtime.memory_bytes,
            "cpu_seconds": runtime.cpu_seconds,
            "process_count": runtime.process_count,
            "output": output.output if output is not None else "",
            "output_truncated": output.output_truncated if output is not None else False,
            "earlier_output_omitted": output.earlier_output_omitted if output is not None else False,
            "has_earlier_output": output.has_earlier_output if output is not None else False,
            "cursor_reset": output.cursor_reset if output is not None else False,
        }

    def _known_job_label(self, job_id: str) -> str:
        """Returns a retained job label without triggering additional backend work."""
        for run in reversed(self._runs.values()):
            for call in reversed(run.calls):
                if call.job_id == job_id and call.job_label:
                    return call.job_label

        for record in self._job_cache:
            if record.job_id == job_id and record.label:
                return record.label
        return ""

    def _list_jobs_safely(self) -> list[JobRecord]:
        """Returns cached durable-job metadata without allowing job-backend failures to break activity polling."""
        now = time.monotonic()
        with self._lock:
            if self._job_cache and now - self._job_cache_at < self._JOB_CACHE_SECONDS:
                return list(self._job_cache)

        try:
            records = self._job_source.list_jobs(limit=self._JOB_LIST_LIMIT)
        except (OSError, RuntimeError, ValueError):
            return []

        with self._lock:
            self._job_cache_at = now
            self._job_cache = list(records)
        return records

    def _prune_runs(self) -> None:
        """Bounds retained activity state while preserving current runs."""
        while len(self._runs) > _MAX_RUNS:
            run_id, run = self._runs.popitem(last=False)
            if self._current_run_by_session.get(run.session_id) == run_id:
                self._current_run_by_session.pop(run.session_id, None)

    @staticmethod
    def _job_payload(record: JobRecord) -> dict[str, Any]:
        """Serializes the lightweight durable-job state used by the activity widget."""
        return {
            "job_id": record.job_id,
            "label": record.label or "background job",
            "project": record.project_name or "",
            "status": record.status.value,
            "started_at": datetime.fromisoformat(record.created_at).timestamp(),
            "finished_at": datetime.fromisoformat(record.finished_at).timestamp() if record.finished_at is not None else None,
        }

    @staticmethod
    def _extract_job_identity(result: object | None) -> tuple[str | None, str | None]:
        """Extracts a durable-job identifier from the normal ``start_job`` result shape."""
        payload: object = result
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None, None
        elif isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[1], dict):
            payload = payload[1]
        elif not isinstance(payload, dict):
            structured = getattr(payload, "structuredContent", None)
            if isinstance(structured, dict):
                payload = structured
            else:
                content = getattr(payload, "content", None)
                if isinstance(content, list):
                    for block in content:
                        text = getattr(block, "text", None)
                        if not isinstance(text, str):
                            continue
                        try:
                            candidate = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(candidate, dict):
                            payload = candidate
                            break

        if not isinstance(payload, dict):
            return None, None
        payload_dict = cast(dict[str, Any], payload)
        if "job_id" not in payload_dict:
            nested = payload_dict.get("result")
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested)
                except json.JSONDecodeError:
                    nested = None
            if isinstance(nested, dict):
                payload_dict = cast(dict[str, Any], nested)

        job_id = payload_dict.get("job_id")
        label = payload_dict.get("label")
        return (
            job_id if isinstance(job_id, str) and job_id else None,
            label if isinstance(label, str) and label else None,
        )

    @staticmethod
    def _serialize_value(value: object) -> str:
        """Serializes bounded activity detail without making tool execution depend on display formatting."""
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            text = str(value)
        if len(text) <= 8000:
            return text
        return f"{text[:3900]}\n... detail omitted ...\n{text[-3900:]}"

    @staticmethod
    def _summarize_arguments(tool_name: str, arguments: dict[str, Any]) -> ActivitySummary:
        """Builds bounded, low-noise semantic summary fields from safe display arguments."""
        return ActivityDetailFormatter().format(tool_name, arguments)


def register_activity_resource(mcp: FastMCP) -> None:
    """Registers the compact ChatGPT activity-panel resource."""

    @mcp.resource(
        ACTIVITY_RESOURCE_URI,
        name="Serena activity",
        description="Compact live view of Serena tool calls with lightweight durable-job visibility.",
        mime_type=_ACTIVITY_RESOURCE_MIME_TYPE,
        meta={
            "openai/widgetDescription": "Shows Serena tool calls, current-turn jobs, and a compact indicator for other running jobs.",
        },
    )
    def activity_resource() -> str:
        return activity_widget_html()


def activity_widget_html() -> str:
    """Returns the self-contained activity widget HTML."""
    return r"""
<div id="serena-activity" class="activity">
  <button id="activity-header" class="header" type="button" aria-expanded="true">
    <span class="title">
      <span id="activity-logo" class="logo" aria-hidden="true">
        <svg viewBox="0 0 256 256" width="21" height="21" focusable="false">
          <rect x="24" y="24" width="208" height="208" rx="48" fill="#ffffff" stroke="#00491e" stroke-width="12"/>
          <path d="M104 76 64 128l40 52M152 76l40 52-40 52" fill="none" stroke="#00491e" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M116 128h24" fill="none" stroke="#00491e" stroke-width="18" stroke-linecap="round"/>
          <circle cx="128" cy="128" r="9" fill="#00491e"/>
        </svg>
      </span>
      <span class="header-tool">
        <span class="header-tool-line"><strong id="activity-header-tool">Waiting for activity</strong><span id="activity-header-scope" class="header-scope"></span></span>
        <span id="activity-header-detail" class="header-detail"></span>
      </span>
      <span class="header-overview">
        <strong>Serena</strong>
        <span id="activity-header-stats" class="header-stats">0 tools · 0 jobs</span>
      </span>
    </span>
    <span class="header-meta">
      <span class="header-times">
        <span id="activity-header-submitted" class="header-submitted"></span>
        <span id="activity-header-elapsed" class="summary"></span>
      </span>
      <span id="activity-header-status" class="header-status">Idle</span>
    </span>
    <span id="activity-chevron" class="chevron" aria-hidden="true">⌄</span>
  </button>
  <div id="activity-body" class="body" aria-live="polite">
    <div class="body-scroll">
      <div id="activity-empty" class="empty">Waiting for commands...</div>
      <ol id="activity-calls" class="calls"></ol>
      <button id="activity-other-jobs" class="other-jobs" type="button" aria-expanded="false" hidden>
        <span id="activity-other-jobs-label"></span><span class="other-jobs-chevron" aria-hidden="true">⌄</span>
      </button>
      <ol id="activity-background-jobs" class="calls background-jobs"></ol>
    </div>
    <div id="activity-resize-handle" class="resize-handle" role="separator" aria-orientation="horizontal" aria-label="Resize Serena activity panel" aria-valuemin="72" aria-valuemax="720" tabindex="0" title="Drag to resize; double-click to reset"></div>
  </div>
</div>
<style>
  :root {
    color-scheme: light dark;
    --surface: #ffffff;
    --border: #dfe5ec;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface: #161f29;
      --border: #2c3a49;
    }
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body { margin: 0; font: 12px/1.35 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: CanvasText; background: transparent; -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
  button { font: inherit; }
  .activity { width: 100%; min-width: 0; overflow: hidden; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }
  .resize-handle { height: 7px; cursor: ns-resize; position: relative; touch-action: none; user-select: none; }
  .resize-handle::before { content: ""; position: absolute; left: 50%; top: 2px; width: 34px; height: 2px; transform: translateX(-50%); border-radius: 999px; background: color-mix(in srgb, CanvasText 22%, transparent); }
  .resize-handle:hover::before, .resize-handle:focus-visible::before, .activity.resizing .resize-handle::before { background: color-mix(in srgb, #00491e 60%, CanvasText); }
  .activity.collapsed .resize-handle { display: none; }
  .header { width: 100%; min-height: 42px; display: grid; grid-template-columns: minmax(0, 1fr) auto 12px; gap: 5px; align-items: center; padding: 6px 7px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
  .title { min-width: 0; display: flex; gap: 7px; align-items: center; white-space: nowrap; overflow: hidden; }
  .logo { width: 21px; height: 21px; flex: 0 0 auto; color: #00491e; opacity: .82; }
  .logo svg { display: block; width: 100%; height: 100%; }
  .header-tool, .header-overview { min-width: 0; display: grid; gap: 1px; overflow: hidden; }
  .header-tool-line { min-width: 0; display: flex; gap: 5px; align-items: baseline; overflow: hidden; }
  #activity-header-tool { flex: 0 0 auto; white-space: nowrap; }
  .header-scope { min-width: 0; flex: 1 1 0; overflow: hidden; direction: rtl; text-align: left; text-overflow: ellipsis; white-space: nowrap; font: 10.5px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .48; }
  .header-detail, .header-stats { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10.5px; line-height: 1.2; opacity: .58; }
  .header-detail { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .header-meta { justify-self: end; min-width: 0; }
  .header-times { display: grid; gap: 1px; justify-items: end; }
  .header-submitted, .summary, .header-status { white-space: nowrap; font-size: 11px; font-variant-numeric: tabular-nums; }
  .header-submitted { opacity: .52; }
  .summary { opacity: .66; }
  .header-status { opacity: .58; }
  .header-status.running { color: #00491e; opacity: 1; font-weight: 650; }
  .header-status.failed { color: #dc2626; opacity: 1; font-weight: 650; }
  .activity.collapsed .header-overview, .activity.collapsed .header-status { display: none; }
  .activity:not(.collapsed) .header-tool, .activity:not(.collapsed) .header-times { display: none; }
  .activity.collapsed.empty-state .header-tool, .activity.collapsed.empty-state .header-times,
  .activity.collapsed.summary-collapsed .header-tool, .activity.collapsed.summary-collapsed .header-times { display: none; }
  .activity.collapsed.empty-state .header-overview, .activity.collapsed.summary-collapsed .header-overview { display: grid; }
  .activity.collapsed.empty-state .header-status, .activity.collapsed.summary-collapsed .header-status { display: inline; }
  .chevron, .other-jobs-chevron { width: 14px; text-align: center; transition: transform .14s ease; opacity: .58; }
  .activity.collapsed .chevron, .other-jobs[aria-expanded="false"] .other-jobs-chevron { transform: rotate(-90deg); }
  .body { max-height: var(--activity-body-height, 202px); overflow: hidden; border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }
  .body-scroll { max-height: calc(var(--activity-body-height, 202px) - 7px); overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; padding: 3px 7px 6px; }
  .activity.resized .body { height: var(--activity-body-height); }
  .activity.resized .body-scroll { height: calc(var(--activity-body-height) - 7px); }
  .activity.collapsed .body { display: none; }
  .calls { list-style: none; padding: 0; margin: 0; }
  .call { min-width: 0; }
  .row-header { width: 100%; display: grid; grid-template-columns: 15px minmax(0, 1fr) auto 12px; grid-template-areas: "status tool submitted chevron" "status detail elapsed chevron"; column-gap: 5px; row-gap: 0; align-items: start; min-height: 0; padding: 3px 0; border: 0; background: transparent; color: inherit; text-align: left; }
  button.row-header { cursor: pointer; }
  .job-entry { margin: 1px 0; border-radius: 6px; }
  .job-entry .row-header { padding-left: 4px; padding-right: 4px; }
  .status { grid-area: status; align-self: center; width: 15px; text-align: center; opacity: .78; font-size: larger; }
  .call.running .status { color: #00491e; animation: pulse 1.1s ease-in-out infinite; }
  .call.completed .status { color: #16a34a; }
  .call.failed .status, .call.timed_out .status { color: #dc2626; }
  .call.cancelled .status { opacity: .5; }
  .job-entry.running .status { color: #00491e; }
  .tool { grid-area: tool; min-width: 0; display: flex; gap: 5px; align-items: baseline; white-space: nowrap; overflow: hidden; }
  .tool-name { flex: 0 0 auto; font-weight: 700; }
  .scope { min-width: 0; flex: 1 1 0; overflow: hidden; direction: rtl; text-align: left; text-overflow: ellipsis; font: 10.5px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .48; }
  .job-entry .tool-name { font-weight: 700; }
  .detail { grid-area: detail; min-width: 0; margin-top: 1px; font: 10.5px/1.25 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .58; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-height: 1.25em; }
  .submitted, .elapsed { justify-self: end; white-space: nowrap; font-size: 10.5px; opacity: .52; font-variant-numeric: tabular-nums; }
  .submitted { grid-area: submitted; }
  .elapsed { grid-area: elapsed; }
  .row-chevron { grid-area: chevron; width: 14px; text-align: center; opacity: .46; transition: transform .14s ease; }
  .call:not(.expanded) .row-chevron { transform: rotate(-90deg); }
  .detail-panel { margin: 1px 0 5px 21px; padding: 5px 7px 6px; border-left: 2px solid color-mix(in srgb, #00491e 28%, transparent); border-radius: 0 6px 6px 0; background: color-mix(in srgb, CanvasText 3%, transparent); }
  .detail-block + .detail-block { margin-top: 5px; }
  .detail-label { display: block; margin-bottom: 2px; color: color-mix(in srgb, #00491e 82%, CanvasText); font-size: 10px; font-weight: 700; letter-spacing: .035em; text-transform: uppercase; }
  .detail-value { margin: 0; min-width: 0; overflow: visible; }
  .rich-value { font-size: 10.5px; line-height: 1.4; }
  .rich-structure { display: grid; gap: 3px; min-width: 0; }
  .rich-field { display: grid; grid-template-columns: minmax(44px, 20%) minmax(0, 1fr); gap: 6px; align-items: start; min-width: 0; padding: 2px 0; border-bottom: 1px solid color-mix(in srgb, CanvasText 5%, transparent); }
  .rich-field:last-child { border-bottom: 0; }
  .rich-field-name { min-width: 0; padding-top: 1px; color: color-mix(in srgb, #00491e 72%, CanvasText); font: 600 10px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
  .rich-field-value { min-width: 0; }
  .rich-field-block { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .rich-field-block > .rich-field-value { grid-column: 1; }
  .rich-array > .rich-field { grid-template-columns: 20px minmax(0, 1fr); gap: 4px; }
  .rich-array > .rich-field > .rich-field-name { padding-right: 2px; color: color-mix(in srgb, CanvasText 45%, transparent); text-align: right; }
  .rich-string, .rich-text { color: color-mix(in srgb, CanvasText 88%, transparent); }
  .rich-string { overflow-wrap: anywhere; }
  .rich-text { white-space: pre-wrap; overflow-wrap: anywhere; font: 10.5px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  .rich-inline-code { display: inline-block; max-width: 100%; padding: 1px 4px; border-radius: 4px; background: color-mix(in srgb, CanvasText 6%, transparent); font: 10.3px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
  .rich-scalar { font: 10.3px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .rich-boolean { color: #7c3aed; font-weight: 600; }
  .rich-number { color: #b45309; }
  .rich-null { opacity: .46; font-style: italic; }
  .rich-nested { min-width: 0; }
  .rich-nested > summary { width: max-content; max-width: 100%; cursor: pointer; color: color-mix(in srgb, CanvasText 64%, transparent); font: 10px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; user-select: none; }
  .rich-nested[open] > summary { margin-bottom: 3px; }
  .rich-nested > .rich-structure { margin-left: 2px; padding-left: 5px; border-left: 1px solid color-mix(in srgb, CanvasText 10%, transparent); }
  .rich-nested > .rich-array { margin-left: 0; padding-left: 0; border-left: 0; }
  .rich-code { min-width: 0; overflow: hidden; border: 1px solid color-mix(in srgb, CanvasText 11%, transparent); border-radius: 5px; background: color-mix(in srgb, CanvasText 3%, transparent); }
  .rich-code-toolbar { display: flex; align-items: center; justify-content: space-between; min-height: 24px; padding: 3px 5px 3px 7px; border-bottom: 1px solid color-mix(in srgb, CanvasText 8%, transparent); color: color-mix(in srgb, CanvasText 50%, transparent); font: 9px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; text-transform: uppercase; letter-spacing: .045em; }
  .rich-copy { border: 0; border-radius: 4px; padding: 2px 5px; background: transparent; color: inherit; font: inherit; text-transform: none; letter-spacing: 0; cursor: pointer; }
  .rich-copy:hover { background: color-mix(in srgb, CanvasText 6%, transparent); color: CanvasText; }
  .rich-code-scroll { display: grid; grid-template-columns: 34px minmax(0, 1fr); align-items: start; min-width: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10.5px; line-height: 1.45; tab-size: 2; }
  .rich-code-numbers, .rich-code-content { margin: 0; padding-top: 6px; padding-bottom: 6px; font: inherit; line-height: inherit; white-space: pre; tab-size: inherit; }
  .rich-code-numbers { min-width: 34px; padding-left: 4px; padding-right: 7px; border-right: 1px solid color-mix(in srgb, CanvasText 7%, transparent); background: color-mix(in srgb, Canvas 96%, CanvasText); color: color-mix(in srgb, CanvasText 34%, transparent); text-align: right; user-select: none; }
  .rich-code-content-scroll { width: 100%; min-width: 0; overflow-x: auto; overflow-y: hidden; touch-action: pan-y pinch-zoom; }
  .rich-code-content { display: block; width: max-content; min-width: 100%; padding-left: 8px; padding-right: 8px; color: color-mix(in srgb, CanvasText 90%, transparent); }
  .rich-code-diff .rich-code-content { color: color-mix(in srgb, CanvasText 84%, transparent); }
  .detail-media { margin-top: 5px; }
  .detail-media-preview { display: block; max-width: 100%; max-height: 420px; border-radius: 5px; object-fit: contain; }
  .detail-media-audio { width: min(100%, 420px); height: 32px; }
  .detail-media-file { display: inline-flex; align-items: center; min-height: 28px; padding: 4px 7px; border: 1px solid color-mix(in srgb, CanvasText 12%, transparent); border-radius: 5px; color: inherit; font-size: 10.5px; text-decoration: none; }
  .detail-media-note { margin-top: 3px; font-size: 10px; opacity: .58; }
  .job-meta { font-size: 10.5px; opacity: .68; font-variant-numeric: tabular-nums; }
  .job-output-note { margin-bottom: 3px; font-size: 10px; opacity: .56; }
  .job-output-scroll { max-height: 180px; overflow: auto; overscroll-behavior: contain; border: 1px solid color-mix(in srgb, CanvasText 10%, transparent); border-radius: 5px; background: color-mix(in srgb, CanvasText 3%, transparent); }
  .job-output { margin: 0; min-height: 2.4em; padding: 6px 7px; white-space: pre-wrap; overflow-wrap: anywhere; font: 10.5px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .detail-loading { font-size: 11px; opacity: .58; }
  .other-jobs { width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) 14px; gap: 6px; align-items: center; margin-top: 3px; padding: 4px 0 2px 21px; border: 0; border-top: 1px solid color-mix(in srgb, CanvasText 8%, transparent); background: transparent; color: inherit; text-align: left; cursor: pointer; font-size: 11px; opacity: .58; }
  .other-jobs:hover { opacity: .78; }
  .empty { padding: 5px 0 2px; opacity: .58; }
  @keyframes pulse { 50% { opacity: .28; } }
  @media (prefers-color-scheme: dark) { .logo { color: #70c990; } .header-status.running, .call.running .status, .job-entry.running .status { color: #70c990; } }
  @media (prefers-reduced-motion: reduce) { .call.running .status { animation: none; } .chevron, .other-jobs-chevron, .row-chevron { transition: none; } }
  @media (max-width: 520px) {
    body { font-size: 12px; }
    .header { min-height: 42px; grid-template-columns: minmax(0, 1fr) auto 12px; gap: 5px; padding: 6px 7px; }
    .header-detail { font-size: 10.5px; }
    .summary { font-size: 11px; }
    .body { max-height: var(--activity-body-height, 202px); }
    .body-scroll { padding: 3px 7px 6px; }
    .row-header { grid-template-columns: 15px minmax(0, 1fr) auto 12px; column-gap: 5px; padding: 3px 0; }
    .detail { font-size: 10.5px; }
    .detail-panel { margin-left: 20px; }
  }
</style>
<script>
(() => {
  const root = document.getElementById("serena-activity");
  if (!root || root.dataset.initialized === "true") return;
  root.dataset.initialized = "true";

  const header = document.getElementById("activity-header");
  const body = document.getElementById("activity-body");
  const bodyScroll = body.querySelector(".body-scroll");
  const resizeHandle = document.getElementById("activity-resize-handle");
  const headerTool = document.getElementById("activity-header-tool");
  const headerScope = document.getElementById("activity-header-scope");
  const headerDetail = document.getElementById("activity-header-detail");
  const headerStats = document.getElementById("activity-header-stats");
  const headerSubmitted = document.getElementById("activity-header-submitted");
  const headerElapsed = document.getElementById("activity-header-elapsed");
  const headerStatus = document.getElementById("activity-header-status");
  const calls = document.getElementById("activity-calls");
  const empty = document.getElementById("activity-empty");
  const logo = document.getElementById("activity-logo");
  const otherJobsButton = document.getElementById("activity-other-jobs");
  const otherJobsLabel = document.getElementById("activity-other-jobs-label");
  const backgroundJobsList = document.getElementById("activity-background-jobs");
  const rowsByKey = new Map();
  const expandedRows = new Set();
  let state = window.openai?.toolOutput ?? null;
  let initialViewResolved = false;
  let preferSummaryCollapsedHeader = false;
  let otherJobsExpanded = false;
  let timer = null;
  let clockTimer = null;
  let clockDelay = null;
  let jobDetailTimer = null;
  let lastBodyScrollAt = -Infinity;
  let deferredRenderState = null;
  let deferredRenderTimer = null;
  const scrollIdleDelay = 140;
  const resizeMin = 72;
  const resizePanelMax = 720;
  const initialActivityWindowSeconds = 5 * 60;

  function resizeMaxBodyHeight() {
    const chromeHeight = root.getBoundingClientRect().height - body.getBoundingClientRect().height;
    return Math.max(resizeMin, Math.floor(resizePanelMax - chromeHeight));
  }

  function setBodyHeight(height) {
    const resizeMax = resizeMaxBodyHeight();
    const bounded = Math.max(resizeMin, Math.min(resizeMax, Math.round(height)));
    resizeHandle.setAttribute("aria-valuemax", String(resizeMax));
    root.classList.add("resized");
    root.style.setProperty("--activity-body-height", `${bounded}px`);
    resizeHandle.setAttribute("aria-valuenow", String(bounded));
    window.openai?.notifyIntrinsicHeight?.();
  }

  function resetBodyHeight() {
    root.classList.remove("resized");
    root.style.removeProperty("--activity-body-height");
    resizeHandle.removeAttribute("aria-valuenow");
    window.openai?.notifyIntrinsicHeight?.();
  }

  function scheduleDeferredRender() {
    if (!deferredRenderState) return;
    if (deferredRenderTimer !== null) clearTimeout(deferredRenderTimer);
    const delay = Math.max(0, scrollIdleDelay - (performance.now() - lastBodyScrollAt));
    deferredRenderTimer = setTimeout(() => {
      deferredRenderTimer = null;
      const pending = deferredRenderState;
      deferredRenderState = null;
      if (pending) render(pending);
    }, delay);
  }

  bodyScroll.addEventListener("scroll", () => {
    lastBodyScrollAt = performance.now();
    scheduleDeferredRender();
  }, { passive: true });

  resizeHandle.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = body.getBoundingClientRect().height;
    root.classList.add("resizing");
    resizeHandle.setPointerCapture(event.pointerId);

    const move = moveEvent => setBodyHeight(startHeight + moveEvent.clientY - startY);
    const finish = finishEvent => {
      root.classList.remove("resizing");
      resizeHandle.removeEventListener("pointermove", move);
      resizeHandle.removeEventListener("pointerup", finish);
      resizeHandle.removeEventListener("pointercancel", finish);
      if (resizeHandle.hasPointerCapture(finishEvent.pointerId)) resizeHandle.releasePointerCapture(finishEvent.pointerId);
    };
    resizeHandle.addEventListener("pointermove", move);
    resizeHandle.addEventListener("pointerup", finish);
    resizeHandle.addEventListener("pointercancel", finish);
  });
  resizeHandle.addEventListener("dblclick", resetBodyHeight);
  resizeHandle.addEventListener("keydown", event => {
    if (event.key === "Home") {
      event.preventDefault();
      setBodyHeight(resizeMin);
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      setBodyHeight(resizeMaxBodyHeight());
      return;
    }
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const current = body.getBoundingClientRect().height;
    setBodyHeight(current + (event.key === "ArrowDown" ? 20 : -20));
  });

  function setCollapsed(collapsed, summaryHeader = false) {
    root.classList.toggle("collapsed", collapsed);
    root.classList.toggle("summary-collapsed", collapsed && summaryHeader);
    header.setAttribute("aria-expanded", String(!collapsed));
    body.hidden = collapsed;
    syncJobDetailTimer();
    window.openai?.notifyIntrinsicHeight?.();
  }

  async function hydrateFullActivity() {
    if (!state?.summary_only || !state?.run_id || !window.openai?.callTool) return false;
    try {
      const result = await window.openai.callTool("get_activity", { run_id: state.run_id, full: true });
      const next = result?.structuredContent ?? result?.structured_content ?? result;
      if (!next?.run_id) return false;
      render(next);
      return true;
    } catch (_) {
      // Keep the compact retained state if historical expansion cannot be refreshed.
      return false;
    }
  }

  header.addEventListener("click", async () => {
    initialViewResolved = true;
    const expanding = root.classList.contains("collapsed");
    setCollapsed(!root.classList.contains("collapsed"), preferSummaryCollapsedHeader);
    if (expanding && await hydrateFullActivity()) return;
    if (state?.run_id) render(state);
  });
  otherJobsButton.addEventListener("click", event => {
    event.stopPropagation();
    otherJobsExpanded = !otherJobsExpanded;
    if (state?.run_id) render(state);
  });

  function elapsed(entry, nowSeconds) {
    const liveLagSeconds = entry.kind === "job" ? 1.5 : 0.5;
    const end = entry.finished_at ?? Math.max(entry.started_at, nowSeconds - liveLagSeconds);
    const seconds = Math.max(0, end - entry.started_at);
    if (seconds < 10) return `${seconds.toFixed(1)}s`;
    if (seconds < 120) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  }

  function submittedClock(timestamp) {
    if (!Number.isFinite(timestamp)) return "";
    return new Date(timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function statusIcon(status) {
    if (status === "running") return "●";
    if (status === "failed" || status === "timed_out") return "!";
    if (status === "cancelled") return "\u00d7";
    if (status === "queued" || status === "waiting") return "○";
    return "✓";
  }

  function countLabel(count, singular) {
    return `${count} ${singular}${count === 1 ? "" : "s"}`;
  }

  function currentTurnJobs(next) {
    return (next.jobs || []).filter(job => Boolean(job.current_turn));
  }

  function otherRunningJobs(next) {
    return (next.jobs || []).filter(job => !job.current_turn && job.status === "running");
  }

  function callEntry(call) {
    return { ...call, key: `call:${call.call_id}`, kind: "call" };
  }

  function jobEntry(job) {
    return {
      ...job,
      key: `job:${job.job_id}`,
      kind: "job",
      tool_name: "start_job",
      scope: job.project || "",
      detail: job.label || "background job",
    };
  }

  function visibleCalls(next) {
    const currentJobIds = new Set(currentTurnJobs(next).map(job => job.job_id));
    return (next.calls || [])
      .filter(call => !(call.tool_name === "start_job" && call.job_id && currentJobIds.has(call.job_id)))
      .map(callEntry);
  }

  function headerEntry(next) {
    return primaryEntries(next)[0] || null;
  }

  function primaryEntries(next) {
    const entries = [...visibleCalls(next), ...currentTurnJobs(next).map(jobEntry)];
    const running = entries.filter(entry => entry.status === "running").sort((a, b) => b.started_at - a.started_at);
    const terminal = entries.filter(entry => entry.status !== "running").sort((a, b) => b.started_at - a.started_at);
    return [...running, ...terminal];
  }

  function displayedEntries(next) {
    return primaryEntries(next);
  }

  function displayedBackgroundJobs(next) {
    if (!otherJobsExpanded) return [];
    return otherRunningJobs(next).map(jobEntry).sort((a, b) => b.started_at - a.started_at);
  }

  function resetToolMedia(refs) {
    if (!refs.mediaBlock) return;
    refs.mediaBlock.hidden = true;
    refs.mediaImage.hidden = true;
    refs.mediaImage.removeAttribute("src");
    refs.mediaAudio.pause();
    refs.mediaAudio.hidden = true;
    refs.mediaAudio.removeAttribute("src");
    refs.mediaFile.hidden = true;
    refs.mediaFile.removeAttribute("href");
    refs.mediaFile.textContent = "";
    refs.mediaNote.textContent = "";
  }

  function mediaContentBlocks(result) {
    if (Array.isArray(result?.content)) return result.content;
    if (Array.isArray(result?.structuredContent?.content)) return result.structuredContent.content;
    if (Array.isArray(result?.structured_content?.content)) return result.structured_content.content;
    return [];
  }

  async function loadToolMedia(row, media) {
    const refs = row._activityRefs;
    if (!media || !refs.mediaBlock) {
      resetToolMedia(refs);
      return;
    }

    resetToolMedia(refs);
    refs.mediaBlock.hidden = false;
    refs.mediaNote.textContent = "Loading media...";
    const directUrl = typeof media.url === "string" ? media.url : "";
    if (directUrl) {
      if (media.type === "image") {
        refs.mediaImage.hidden = false;
        refs.mediaImage.src = directUrl;
      } else if (media.type === "audio") {
        refs.mediaAudio.hidden = false;
        refs.mediaAudio.src = directUrl;
      } else {
        refs.mediaFile.hidden = false;
        refs.mediaFile.href = directUrl;
        refs.mediaFile.textContent = media.name || "Open file";
      }
      refs.mediaNote.textContent = media.mime_type || "";
      return;
    }

    try {
      const result = await window.openai.callTool("get_activity_media", { run_id: state.run_id, call_id: row.dataset.callId });
      const blocks = mediaContentBlocks(result);
      const image = blocks.find(block => block?.type === "image" && block.data);
      const audio = blocks.find(block => block?.type === "audio" && block.data);
      const file = blocks.find(block => block?.type === "resource_link");
      if (image) {
        refs.mediaImage.hidden = false;
        refs.mediaImage.src = `data:${image.mimeType || media.mime_type || "image/png"};base64,${image.data}`;
      } else if (audio) {
        refs.mediaAudio.hidden = false;
        refs.mediaAudio.src = `data:${audio.mimeType || media.mime_type || "audio/mpeg"};base64,${audio.data}`;
      } else if (file) {
        refs.mediaFile.hidden = false;
        refs.mediaFile.textContent = file.name || media.name || "File result";
        if (typeof file.uri === "string" && /^https?:/.test(file.uri)) refs.mediaFile.href = file.uri;
      } else {
        throw new Error("No media content returned");
      }
      refs.mediaNote.textContent = media.mime_type || "";
    } catch (_) {
      refs.mediaFile.hidden = false;
      refs.mediaFile.textContent = media.name || "Media result";
      refs.mediaNote.textContent = "Preview unavailable.";
    }
  }

  const richCodeKeys = new Set(["body", "code", "command", "needle", "output", "regex", "repl", "script", "source", "stderr", "stdout", "substring_pattern"]);
  const richPathKeys = new Set(["cwd", "path", "project", "relative_path", "remote", "branch", "file_mask", "paths_include_glob"]);

  function parsedJsonValue(value) {
    if (typeof value !== "string") return { parsed: false, value };
    const text = value.trim();
    if (!text || !["{", "["].includes(text[0])) return { parsed: false, value };
    try {
      return { parsed: true, value: JSON.parse(text) };
    } catch (_) {
      return { parsed: false, value };
    }
  }

  function looksLikeDiff(text) {
    return /^diff --git /m.test(text) || (/^@@ .* @@/m.test(text) && (/^\+/m.test(text) || /^-/m.test(text)));
  }

  function codeLanguage(text, key) {
    if (looksLikeDiff(text)) return "diff";
    if (["output", "stderr", "stdout"].includes(key)) return "output";
    if (key === "command" || /^#!.*\b(?:ba|z|fi)?sh\b/m.test(text)) return "shell";
    if (/^\s*(?:def|class|from|import|async def)\b/m.test(text)) return "python";
    if (/^\s*(?:const|let|var|function|interface|type|export|import)\b/m.test(text) || /=>/.test(text)) return "javascript";
    if (/^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER)\b/im.test(text)) return "sql";
    return "code";
  }

  function looksLikeCode(text, key) {
    if (looksLikeDiff(text)) return true;
    if (richCodeKeys.has(key)) return text.includes("\n") || text.length > 52 || key === "command" || key === "regex";
    if (!text.includes("\n")) return false;
    return /^\s*(?:def|class|from|import|const|let|var|function|if|for|while|return|#include)\b/m.test(text)
      || /[{};]\s*$/m.test(text)
      || /=>/.test(text);
  }

  function copyButton(text) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "rich-copy";
    button.textContent = "Copy";
    button.addEventListener("click", async event => {
      event.stopPropagation();
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
        window.setTimeout(() => { button.textContent = "Copy"; }, 1200);
      } catch (_) {
        button.textContent = "Copy unavailable";
        window.setTimeout(() => { button.textContent = "Copy"; }, 1200);
      }
    });
    return button;
  }

  function enableHorizontalTouchDrag(scroller) {
    let pointerId = null;
    let startX = 0;
    let startY = 0;
    let startScrollLeft = 0;
    let horizontal = false;
    let decided = false;

    scroller.addEventListener("pointerdown", event => {
      if (event.pointerType !== "touch" || scroller.scrollWidth <= scroller.clientWidth) return;
      pointerId = event.pointerId;
      startX = event.clientX;
      startY = event.clientY;
      startScrollLeft = scroller.scrollLeft;
      horizontal = false;
      decided = false;
    });

    scroller.addEventListener("pointermove", event => {
      if (event.pointerId !== pointerId) return;
      const dx = event.clientX - startX;
      const dy = event.clientY - startY;
      if (!decided) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) < 5) return;
        horizontal = Math.abs(dx) > Math.abs(dy) * 1.15;
        decided = true;
        if (horizontal && scroller.setPointerCapture) scroller.setPointerCapture(pointerId);
      }
      if (horizontal) scroller.scrollLeft = startScrollLeft - dx;
    });

    const finish = event => {
      if (event.pointerId !== pointerId) return;
      if (scroller.hasPointerCapture?.(pointerId)) scroller.releasePointerCapture(pointerId);
      pointerId = null;
    };
    scroller.addEventListener("pointerup", finish);
    scroller.addEventListener("pointercancel", finish);
  }

  function appendCodeBlock(container, text, key = "") {
    const language = codeLanguage(text, key);
    const block = document.createElement("div");
    block.className = `rich-code rich-code-${language}`;
    const toolbar = document.createElement("div");
    toolbar.className = "rich-code-toolbar";
    const label = document.createElement("span");
    label.textContent = language;
    toolbar.append(label, copyButton(text));
    const scroll = document.createElement("div");
    scroll.className = "rich-code-scroll";
    const lineCount = text.split("\n").length;
    const numbers = document.createElement("pre");
    numbers.className = "rich-code-numbers";
    numbers.textContent = Array.from({ length: lineCount }, (_, index) => String(index + 1)).join("\n");
    const content = document.createElement("pre");
    content.className = "rich-code-content";
    content.textContent = text || " ";
    const contentScroll = document.createElement("div");
    contentScroll.className = "rich-code-content-scroll";
    contentScroll.append(content);
    enableHorizontalTouchDrag(contentScroll);
    scroll.append(numbers, contentScroll);
    block.append(toolbar, scroll);
    container.append(block);
  }

  function appendScalarValue(container, value, key = "") {
    if (value === null || value === undefined) {
      const scalar = document.createElement("span");
      scalar.className = "rich-scalar rich-null";
      scalar.textContent = "null";
      container.append(scalar);
      return;
    }
    if (typeof value === "boolean" || typeof value === "number") {
      const scalar = document.createElement("span");
      scalar.className = `rich-scalar rich-${typeof value}`;
      scalar.textContent = String(value);
      container.append(scalar);
      return;
    }

    const text = String(value);
    const parsed = parsedJsonValue(text);
    if (parsed.parsed) {
      appendStructuredValue(container, parsed.value, 0, key);
      return;
    }
    if (looksLikeCode(text, key)) {
      appendCodeBlock(container, text, key);
      return;
    }
    if (richPathKeys.has(key) || key.endsWith("_path") || key.endsWith("_id") || key === "name_path" || key === "name_path_pattern") {
      const code = document.createElement("code");
      code.className = "rich-inline-code";
      code.textContent = text;
      container.append(code);
      return;
    }
    if (text.includes("\n") || text.length > 180) {
      const prose = document.createElement("div");
      prose.className = "rich-text";
      prose.textContent = text;
      container.append(prose);
      return;
    }
    const scalar = document.createElement("span");
    scalar.className = "rich-string";
    scalar.textContent = text;
    container.append(scalar);
  }

  function appendStructuredValue(container, value, depth = 0, key = "") {
    if (value === null || typeof value !== "object") {
      appendScalarValue(container, value, key);
      return;
    }

    const entries = Array.isArray(value) ? value.map((item, index) => [String(index), item]) : Object.entries(value);
    if (depth > 0) {
      const details = document.createElement("details");
      details.className = "rich-nested";
      details.open = depth === 1 && entries.length <= 5;
      const summary = document.createElement("summary");
      summary.textContent = Array.isArray(value)
        ? `${entries.length} item${entries.length === 1 ? "" : "s"}`
        : `${entries.length} field${entries.length === 1 ? "" : "s"}`;
      const inner = document.createElement("div");
      inner.className = `rich-structure${Array.isArray(value) ? " rich-array" : ""}`;
      for (const [childKey, childValue] of entries) appendStructuredRow(inner, childKey, childValue, depth + 1);
      details.append(summary, inner);
      container.append(details);
      return;
    }

    const structure = document.createElement("div");
    structure.className = `rich-structure${Array.isArray(value) ? " rich-array" : ""}`;
    for (const [childKey, childValue] of entries) appendStructuredRow(structure, childKey, childValue, depth + 1);
    container.append(structure);
  }

  function appendStructuredRow(container, key, value, depth) {
    const row = document.createElement("div");
    row.className = "rich-field";
    if (typeof value === "string" && looksLikeCode(value, key)) row.classList.add("rich-field-block");
    const name = document.createElement("div");
    name.className = "rich-field-name";
    name.textContent = key;
    const content = document.createElement("div");
    content.className = "rich-field-value";
    if (value !== null && typeof value === "object") appendStructuredValue(content, value, depth, key);
    else appendScalarValue(content, value, key);
    row.append(name, content);
    container.append(row);
  }

  function renderRichValue(container, rawValue, structuredValue = undefined) {
    container.replaceChildren();
    if (structuredValue !== undefined && structuredValue !== null) {
      appendStructuredValue(container, structuredValue);
      return;
    }
    const parsed = parsedJsonValue(rawValue);
    if (parsed.parsed) appendStructuredValue(container, parsed.value);
    else appendScalarValue(container, rawValue ?? "");
  }

  async function loadToolDetail(row) {
    const refs = row._activityRefs;
    const callId = row.dataset.callId;
    if (!state?.run_id || !callId || !window.openai?.callTool || !refs.panel) return;

    refs.loading.hidden = false;
    refs.loading.textContent = "Loading details...";
    refs.content.hidden = true;
    try {
      const result = await window.openai.callTool("get_activity_detail", { run_id: state.run_id, call_id: callId });
      const detail = result?.structuredContent ?? result?.structured_content ?? result;
      if (!detail?.call_id || detail.call_id !== callId) throw new Error("Mismatched activity detail");
      renderRichValue(refs.arguments, detail.arguments || "{}", detail.structured_arguments);
      const hasMedia = Boolean(detail.media);
      refs.result.parentElement.hidden = hasMedia;
      if (hasMedia) refs.result.replaceChildren();
      else renderRichValue(
        refs.result,
        detail.result ?? (detail.status === "running" ? "Tool is still running." : "No result returned."),
      );
      await loadToolMedia(row, detail.media);
      refs.loading.hidden = true;
      refs.content.hidden = false;
      row.dataset.detailStatus = detail.status || "";
    } catch (_) {
      refs.loading.hidden = false;
      refs.loading.textContent = "Details unavailable.";
      refs.content.hidden = true;
    }
    window.openai?.notifyIntrinsicHeight?.();
  }

  function formatBytes(value) {
    if (value === null || value === undefined) return null;
    if (value < 1024) return `${Math.round(value)} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
    if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
  }

  function formatRuntimeSeconds(value) {
    if (value === null || value === undefined) return null;
    if (value < 10) return `${value.toFixed(1)}s`;
    if (value < 120) return `${Math.round(value)}s`;
    if (value < 3600) return `${Math.floor(value / 60)}m ${String(Math.round(value % 60)).padStart(2, "0")}s`;
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  }

  function jobDetailMeta(detail) {
    const parts = [];
    const runtime = formatRuntimeSeconds(detail.elapsed_seconds);
    if (runtime) parts.push(`${runtime} ${detail.status === "running" ? "elapsed" : "runtime"}`);
    if (detail.status === "running") {
      const sinceOutput = formatRuntimeSeconds(detail.seconds_since_last_output);
      parts.push(sinceOutput ? `${sinceOutput} since output` : "no output yet");
      const memory = formatBytes(detail.memory_bytes);
      if (memory) parts.push(memory);
      const cpu = formatRuntimeSeconds(detail.cpu_seconds);
      if (cpu) parts.push(`${cpu} CPU`);
      if (detail.process_count !== null && detail.process_count !== undefined) parts.push(`${detail.process_count} proc`);
      const timeout = formatRuntimeSeconds(detail.timeout_seconds);
      if (timeout) parts.push(`limit ${timeout}`);
    } else if (detail.return_code !== null && detail.return_code !== undefined) {
      parts.push(`exit ${detail.return_code}`);
    }
    if (detail.project) parts.push(detail.project);
    return parts.join(" · ");
  }

  async function loadJobDetail(row) {
    const refs = row._activityRefs;
    const jobId = row.dataset.jobId;
    if (!state?.run_id || !jobId || !window.openai?.callTool || !refs.panel || row.dataset.jobDetailLoading === "true") return;

    row.dataset.jobDetailLoading = "true";
    if (!row.dataset.jobDetailLoaded) {
      refs.loading.hidden = false;
      refs.loading.textContent = "Loading job output...";
      refs.content.hidden = true;
    }
    const scroller = refs.jobOutputScroll;
    const follow = !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 32;
    try {
      const result = await window.openai.callTool("get_activity_job_detail", { run_id: state.run_id, job_id: jobId });
      const detail = result?.structuredContent ?? result?.structured_content ?? result;
      if (!detail?.job_id || detail.job_id !== jobId) throw new Error("Mismatched activity job detail");
      refs.jobMeta.textContent = jobDetailMeta(detail);
      refs.jobOutput.textContent = detail.output || "";
      if (detail.output) {
        refs.jobOutputNote.textContent = detail.earlier_output_omitted || detail.has_earlier_output
          ? "Latest retained output · earlier output available"
          : detail.status === "running" ? "Live output" : "Final output";
      } else {
        refs.jobOutputNote.textContent = detail.status === "running" ? "Waiting for output..." : "No output recorded";
      }
      refs.loading.hidden = true;
      refs.content.hidden = false;
      row.dataset.jobDetailLoaded = "true";
      row.dataset.detailStatus = detail.status || "";
      if (follow && scroller) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
    } catch (_) {
      refs.loading.hidden = false;
      refs.loading.textContent = "Job details unavailable.";
      if (!row.dataset.jobDetailLoaded) refs.content.hidden = true;
    } finally {
      row.dataset.jobDetailLoading = "false";
    }
    window.openai?.notifyIntrinsicHeight?.();
  }

  function syncJobDetailTimer() {
    const hasExpandedRunningJob = !root.classList.contains("collapsed") && [...rowsByKey.values()].some(row => {
      const entry = row._activityEntry;
      return entry?.kind === "job" && entry.status === "running" && expandedRows.has(entry.key);
    });
    if (!hasExpandedRunningJob) {
      if (jobDetailTimer !== null) clearInterval(jobDetailTimer);
      jobDetailTimer = null;
      return;
    }
    if (jobDetailTimer !== null) return;
    jobDetailTimer = setInterval(() => {
      for (const row of rowsByKey.values()) {
        const entry = row._activityEntry;
        if (entry?.kind === "job" && entry.status === "running" && expandedRows.has(entry.key)) void loadJobDetail(row);
      }
    }, 1000);
  }

  function createRow(entry) {
    const isJob = entry.kind === "job";
    const row = document.createElement("li");
    row.dataset.scrollKey = entry.key;

    const rowHeader = document.createElement("button");
    rowHeader.className = "row-header";
    rowHeader.type = "button";
    rowHeader.setAttribute("aria-expanded", "false");
    const status = document.createElement("span");
    status.className = "status";
    const tool = document.createElement("span");
    tool.className = "tool";
    const toolName = document.createElement("span");
    toolName.className = "tool-name";
    const scope = document.createElement("span");
    scope.className = "scope";
    tool.append(toolName, scope);
    const detail = document.createElement("span");
    detail.className = "detail";
    const submittedNode = document.createElement("span");
    submittedNode.className = "submitted";
    const elapsedNode = document.createElement("span");
    elapsedNode.className = "elapsed";
    const chevron = document.createElement("span");
    chevron.className = "row-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = "⌄";
    rowHeader.append(status, tool, detail, submittedNode, elapsedNode, chevron);
    row.append(rowHeader);

    const panel = document.createElement("div");
    panel.className = `detail-panel${isJob ? " job-detail-panel" : ""}`;
    panel.hidden = true;
    const loading = document.createElement("div");
    loading.className = "detail-loading";
    loading.textContent = isJob ? "Loading job output..." : "Loading details...";
    const content = document.createElement("div");
    content.hidden = true;
    let argumentsValue = null;
    let resultValue = null;
    let mediaBlock = null;
    let mediaImage = null;
    let mediaAudio = null;
    let mediaFile = null;
    let mediaNote = null;
    let jobMetaValue = null;
    let jobOutputNote = null;
    let jobOutputScroll = null;
    let jobOutputValue = null;

    if (isJob) {
      const metaBlock = document.createElement("div");
      metaBlock.className = "detail-block";
      const metaLabel = document.createElement("span");
      metaLabel.className = "detail-label";
      metaLabel.textContent = "Job";
      jobMetaValue = document.createElement("div");
      jobMetaValue.className = "job-meta";
      metaBlock.append(metaLabel, jobMetaValue);

      const outputBlock = document.createElement("div");
      outputBlock.className = "detail-block";
      const outputLabel = document.createElement("span");
      outputLabel.className = "detail-label";
      outputLabel.textContent = "Output";
      jobOutputNote = document.createElement("div");
      jobOutputNote.className = "job-output-note";
      jobOutputScroll = document.createElement("div");
      jobOutputScroll.className = "job-output-scroll";
      jobOutputValue = document.createElement("pre");
      jobOutputValue.className = "job-output";
      jobOutputScroll.append(jobOutputValue);
      outputBlock.append(outputLabel, jobOutputNote, jobOutputScroll);
      content.append(metaBlock, outputBlock);
    } else {
      const argumentsBlock = document.createElement("div");
      argumentsBlock.className = "detail-block";
      const argumentsLabel = document.createElement("span");
      argumentsLabel.className = "detail-label";
      argumentsLabel.textContent = "Parameters";
      argumentsValue = document.createElement("div");
      argumentsValue.className = "detail-value rich-value";
      argumentsBlock.append(argumentsLabel, argumentsValue);
      const resultBlock = document.createElement("div");
      resultBlock.className = "detail-block";
      const resultLabel = document.createElement("span");
      resultLabel.className = "detail-label";
      resultLabel.textContent = "Result";
      resultValue = document.createElement("div");
      resultValue.className = "detail-value rich-value";
      resultBlock.append(resultLabel, resultValue);

      mediaBlock = document.createElement("div");
      mediaBlock.className = "detail-block detail-media";
      mediaBlock.hidden = true;
      const mediaLabel = document.createElement("span");
      mediaLabel.className = "detail-label";
      mediaLabel.textContent = "Media";
      mediaImage = document.createElement("img");
      mediaImage.className = "detail-media-preview";
      mediaImage.alt = "Serena tool media result";
      mediaImage.hidden = true;
      mediaAudio = document.createElement("audio");
      mediaAudio.className = "detail-media-audio";
      mediaAudio.controls = true;
      mediaAudio.preload = "metadata";
      mediaAudio.hidden = true;
      mediaFile = document.createElement("a");
      mediaFile.className = "detail-media-file";
      mediaFile.target = "_blank";
      mediaFile.rel = "noopener";
      mediaFile.hidden = true;
      mediaNote = document.createElement("div");
      mediaNote.className = "detail-media-note";
      mediaBlock.append(mediaLabel, mediaImage, mediaAudio, mediaFile, mediaNote);

      content.append(argumentsBlock, resultBlock, mediaBlock);
    }

    panel.append(loading, content);
    row.append(panel);
    rowHeader.addEventListener("click", () => {
      const key = row.dataset.scrollKey;
      if (!key) return;
      const expanded = !expandedRows.has(key);
      if (expanded) expandedRows.add(key);
      else expandedRows.delete(key);
      row.classList.toggle("expanded", expanded);
      rowHeader.setAttribute("aria-expanded", String(expanded));
      panel.hidden = !expanded;
      if (expanded) {
        if (isJob) void loadJobDetail(row);
        else void loadToolDetail(row);
      }
      syncJobDetailTimer();
      window.openai?.notifyIntrinsicHeight?.();
    });

    row._activityRefs = {
      header: rowHeader,
      status,
      tool,
      toolName,
      scope,
      detail,
      submitted: submittedNode,
      elapsed: elapsedNode,
      chevron,
      panel,
      loading,
      content,
      arguments: argumentsValue,
      result: resultValue,
      mediaBlock,
      mediaImage,
      mediaAudio,
      mediaFile,
      mediaNote,
      jobMeta: jobMetaValue,
      jobOutputNote,
      jobOutputScroll,
      jobOutput: jobOutputValue,
    };
    rowsByKey.set(entry.key, row);
    return row;
  }

  function updateRow(row, entry, now) {
    const refs = row._activityRefs;
    const isJob = entry.kind === "job";
    const previousDetailStatus = row.dataset.detailStatus;
    row.dataset.callId = entry.call_id || "";
    row.dataset.jobId = entry.job_id || "";
    row.dataset.entryKind = entry.kind;
    row._activityEntry = entry;
    const renderSignature = [
      entry.status, entry.tool_name, entry.scope || "", entry.detail || "",
      entry.submitted_at ?? "", entry.started_at ?? "", entry.finished_at ?? "",
      expandedRows.has(entry.key) ? "1" : "0",
    ].join("\\u001f");
    if (row._activityRenderSignature === renderSignature) return;
    row._activityRenderSignature = renderSignature;
    row.className = `call ${entry.status}${isJob ? " job-entry" : ""}`;
    refs.status.textContent = statusIcon(entry.status);
    refs.toolName.textContent = entry.tool_name;
    refs.toolName.title = entry.tool_name;
    const scopeText = entry.scope || "";
    refs.scope.textContent = scopeText;
    refs.scope.title = scopeText;
    refs.scope.hidden = !scopeText;
    const detailText = entry.detail || "";
    refs.detail.textContent = detailText;
    refs.detail.title = detailText;
    refs.submitted.textContent = submittedClock(entry.submitted_at ?? entry.started_at);
    refs.elapsed.textContent = elapsed(entry, now);

    const expanded = expandedRows.has(entry.key);
    row.classList.toggle("expanded", expanded);
    refs.header.setAttribute("aria-expanded", String(expanded));
    refs.panel.hidden = !expanded;
    if (expanded && previousDetailStatus && previousDetailStatus !== entry.status) {
      if (isJob) void loadJobDetail(row);
      else void loadToolDetail(row);
    }
  }

  function reconcileInto(container, entries, now) {
    entries.forEach((entry, index) => {
      const row = rowsByKey.get(entry.key) || createRow(entry);
      updateRow(row, entry, now);
      const current = container.children[index] || null;
      if (current !== row) container.insertBefore(row, current);
    });
  }

  function reconcileRows(primary, background, now) {
    const retained = new Set([...primary, ...background].map(entry => entry.key));
    const oldScrollTop = bodyScroll.scrollTop;
    reconcileInto(calls, primary, now);
    reconcileInto(backgroundJobsList, background, now);
    for (const [key, row] of rowsByKey) {
      if (retained.has(key)) continue;
      row.remove();
      rowsByKey.delete(key);
      expandedRows.delete(key);
    }
    if (oldScrollTop > 0) bodyScroll.scrollTop = oldScrollTop;
  }

  function refreshDurations() {
    if (!state?.run_id) return;
    const now = Date.now() / 1000;
    const activeHeaderEntry = headerEntry(state);
    if (activeHeaderEntry) headerElapsed.textContent = elapsed(activeHeaderEntry, now);
    for (const row of rowsByKey.values()) {
      const entry = row._activityEntry;
      if (entry?.status === "running") row._activityRefs.elapsed.textContent = elapsed(entry, now);
    }
  }

  function syncClockTimer() {
    const hasRunningTool = (state?.calls || []).some(call => call.status === "running");
    const hasRunningJob = (state?.jobs || []).some(job => job.status === "running");
    const nextDelay = hasRunningTool ? 100 : hasRunningJob ? 1000 : null;
    if (nextDelay === clockDelay) return;
    if (clockTimer !== null) clearInterval(clockTimer);
    clockTimer = null;
    clockDelay = nextDelay;
    if (nextDelay !== null) clockTimer = setInterval(refreshDurations, nextDelay);
  }

  function render(next) {
    if (!next || !next.run_id) return;
    state = next;
    if (performance.now() - lastBodyScrollAt < scrollIdleDelay) {
      deferredRenderState = next;
      scheduleDeferredRender();
      return;
    }
    deferredRenderState = null;
    applyInitialCollapsedPolicy(next);
    const now = Date.now() / 1000;
    const activeHeaderEntry = headerEntry(next);
    const backgroundJobs = otherRunningJobs(next);

    headerTool.textContent = activeHeaderEntry?.tool_name || "Waiting for activity";
    const headerScopeText = activeHeaderEntry?.scope || "";
    headerScope.textContent = headerScopeText;
    headerScope.title = headerScopeText;
    headerScope.hidden = !headerScopeText;
    const headerDetailText = activeHeaderEntry?.detail || "";
    headerDetail.textContent = headerDetailText;
    headerDetail.title = headerDetailText;
    headerSubmitted.textContent = activeHeaderEntry ? submittedClock(activeHeaderEntry.submitted_at ?? activeHeaderEntry.started_at) : "";
    headerElapsed.textContent = activeHeaderEntry ? elapsed(activeHeaderEntry, now) : "";

    const toolCount = Number.isFinite(next.tool_count) ? next.tool_count : (next.calls || []).length;
    const jobCount = Number.isFinite(next.job_count) ? next.job_count : (next.jobs || []).length;
    root.classList.toggle("empty-state", toolCount + jobCount === 0);
    const projectName = next.project_name || "no project";
    headerStats.textContent = `${countLabel(toolCount, "tool")} · ${countLabel(jobCount, "job")} · ${projectName}`;
    headerStats.title = headerStats.textContent;

    const runningCount = (next.calls || []).filter(call => call.status === "running").length
      + (next.jobs || []).filter(job => job.status === "running").length;
    const issueCount = (next.calls || []).filter(call => call.status === "failed" || call.status === "timed_out").length
      + (next.jobs || []).filter(job => job.status === "failed" || job.status === "timed_out").length;
    headerStatus.textContent = runningCount > 0
      ? `${runningCount} running`
      : issueCount > 0 ? `${issueCount} failed` : toolCount + jobCount > 0 ? "Complete" : "Idle";
    headerStatus.classList.toggle("running", runningCount > 0);
    headerStatus.classList.toggle("failed", runningCount === 0 && issueCount > 0);

    if (backgroundJobs.length === 0) otherJobsExpanded = false;
    otherJobsButton.hidden = backgroundJobs.length === 0;
    otherJobsButton.setAttribute("aria-expanded", String(otherJobsExpanded));
    otherJobsLabel.textContent = backgroundJobs.length === 1 ? "1 other job running" : `${backgroundJobs.length} other jobs running`;

    const primary = primaryEntries(next);
    empty.hidden = primary.length > 0 || backgroundJobs.length > 0;
    if (root.classList.contains("collapsed")) {
      reconcileRows([], [], now);
    } else {
      reconcileRows(displayedEntries(next), displayedBackgroundJobs(next), now);
    }
    syncClockTimer();
    syncJobDetailTimer();
    window.openai?.notifyIntrinsicHeight?.();
  }

  function hasRunningPanelActivity(next) {
    const hasRunningTool = (next?.calls || []).some(call => call.status === "running");
    const hasRunningJob = (next?.jobs || []).some(job => job.status === "running");
    return hasRunningTool || hasRunningJob;
  }

  function latestPanelActivityTimestamp(next) {
    const timestamps = [
      ...(next?.calls || []).map(call => call.finished_at ?? call.started_at ?? call.submitted_at),
      ...(next?.jobs || []).map(job => job.finished_at ?? job.started_at),
    ].map(Number).filter(Number.isFinite);
    return timestamps.length > 0 ? Math.max(...timestamps) : null;
  }

  function applyInitialCollapsedPolicy(next) {
    if (initialViewResolved) return;

    const itemCount = (next?.calls || []).length + (next?.jobs || []).length;
    if (itemCount === 0) {
      if (!root.classList.contains("collapsed")) setCollapsed(true);
      return;
    }

    const latestActivity = latestPanelActivityTimestamp(next);
    const recentlyActive = hasRunningPanelActivity(next)
      || (latestActivity !== null && Date.now() / 1000 - latestActivity <= initialActivityWindowSeconds);
    preferSummaryCollapsedHeader = !recentlyActive;
    setCollapsed(!recentlyActive, preferSummaryCollapsedHeader);
    initialViewResolved = true;
  }

  function retire() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (clockTimer !== null) clearInterval(clockTimer);
    clockTimer = null;
    clockDelay = null;
    if (jobDetailTimer !== null) clearInterval(jobDetailTimer);
    jobDetailTimer = null;
    root.classList.add("retired");
    window.openai?.notifyIntrinsicHeight?.();
  }

  async function poll() {
    if (!state?.run_id || !window.openai?.callTool) {
      timer = setTimeout(poll, 250);
      return;
    }
    try {
      const result = await window.openai.callTool("get_activity", { run_id: state.run_id });
      const next = result?.structuredContent ?? result?.structured_content ?? result;
      if (next?.run_id) render(next);
      if (next?.superseded && !hasRunningPanelActivity(next)) {
        retire();
        return;
      }
    } catch (_) {
      // Keep the last useful state; transient bridge/server failures are non-fatal.
    }
    const hasRunningTool = (state?.calls || []).some(call => call.status === "running");
    const hasRunningJob = (state?.jobs || []).some(job => job.status === "running");
    const delay = hasRunningTool ? 500 : hasRunningJob ? 3000 : 5000;
    timer = setTimeout(poll, delay);
  }

  function acceptGlobals(event) {
    const next = event?.detail?.globals?.toolOutput;
    if (!next?.run_id) return;
    if (state?.run_id && next.run_id !== state.run_id) return;
    render(next);
    if (next.superseded && !hasRunningPanelActivity(next)) retire();
  }
  window.addEventListener("openai:set_globals", acceptGlobals, { passive: true });

  if (state?.run_id) {
    render(state);
    if (!root.classList.contains("collapsed")) void hydrateFullActivity();
  }
  poll();
})();
</script>
""".strip()
