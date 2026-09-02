import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

from mcp.server.fastmcp import FastMCP

from serena.jobs import JobManager, JobRecord, JobSnapshot, JobStatus
from serena.session import get_mcp_session_id  # noqa: F401 - compatibility re-export

ACTIVITY_RESOURCE_URI = "ui://serena/activity-v17.html"
_ACTIVITY_RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"
_MAX_RUNS = 32
_MAX_CALLS_PER_RUN = 100
_DETAIL_KEYS = (
    "command",
    "project",
    "relative_path",
    "memory_name",
    "name_path_pattern",
    "job_id",
    "message",
)


@dataclass
class ActivityCall:
    """One tool invocation displayed in the ChatGPT activity panel."""

    call_id: str
    tool_name: str
    detail: str
    started_at: float
    project_name: str = ""
    arguments: str = field(default="{}", repr=False)
    finished_at: float | None = None
    status: str = "running"
    result: str | None = field(default=None, repr=False)
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
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        if self.job_label is not None:
            payload["job_label"] = self.job_label
        return payload


@dataclass
class ActivityRun:
    """One model-driven Serena activity panel within a client session."""

    run_id: str
    session_id: str
    project_name: str
    started_at: float
    calls: list[ActivityCall] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
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


class ActivityJobSource(Protocol):
    """Provides retained durable-job metadata and output for the activity panel."""

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        """Returns running jobs followed by recent terminal jobs."""
        ...

    def get_job(self, job_id: str) -> JobSnapshot:
        """Returns one job snapshot with bounded retained output."""
        ...


class ActivityTracker:
    """Tracks Serena tool activity and lightweight durable-job state per ChatGPT conversation."""

    _JOB_LIST_LIMIT = 100
    _JOB_CACHE_SECONDS = 2.0

    def __init__(self, job_source: ActivityJobSource | None = None) -> None:
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, ActivityRun] = OrderedDict()
        self._current_run_by_session: dict[str, str] = {}
        self._job_source = job_source or JobManager()
        self._job_cache_at = 0.0
        self._job_cache: list[JobRecord] = []

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

            run = ActivityRun(
                run_id=uuid.uuid4().hex,
                session_id=session_id,
                project_name=project_name,
                started_at=time.time(),
                calls=continuing_calls,
            )
            self._runs[run.run_id] = run
            self._current_run_by_session[session_id] = run.run_id
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

    def start_tool(self, session_id: str, tool_name: str, arguments: dict[str, Any], project_name: str = "") -> str | None:
        """Records one tool invocation when an activity run is active for ``session_id``."""
        with self._lock:
            run_id = self._current_run_by_session.get(session_id)
            run = self._runs.get(run_id) if run_id is not None else None
            if run is None:
                return None

            call = ActivityCall(
                call_id=uuid.uuid4().hex,
                tool_name=tool_name,
                detail=self._summarize_arguments(arguments),
                started_at=time.time(),
                project_name=project_name or run.project_name,
                arguments=self._serialize_value(arguments),
            )
            run.calls.append(call)
            if len(run.calls) > _MAX_CALLS_PER_RUN:
                del run.calls[: len(run.calls) - _MAX_CALLS_PER_RUN]
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
            serialized_result = self._serialize_value(result) if result is not None else None
            for _, call in owners:
                call.status = status
                call.finished_at = finished_at
                call.result = serialized_result
                if project_name is not None:
                    call.project_name = project_name

            # associate a newly submitted durable job with every panel that retained the call
            first_call = owners[0][1]
            if not succeeded or first_call.tool_name != "start_job":
                return

            job_id, label = self._extract_job_identity(result)
            if job_id is None:
                return

            for run, call in owners:
                call.job_id = job_id
                call.job_label = label
                if label is not None:
                    call.detail = label
                if job_id not in run.job_ids:
                    run.job_ids.append(job_id)
            self._job_cache_at = 0.0

    def get_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        """Returns one session-owned run enriched with the jobs relevant to that panel."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")
            payload = run.as_dict()
            current_job_ids = set(run.job_ids)
            superseded = run.superseded

        records = self._list_jobs_safely()
        visible_records = [
            record for record in records if record.job_id in current_job_ids or (not superseded and record.status is JobStatus.RUNNING)
        ]
        visible_records.sort(key=lambda record: record.created_at, reverse=True)
        payload["jobs"] = [self._job_payload(record) | {"current_turn": record.job_id in current_job_ids} for record in visible_records]
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
                    }
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
    def _summarize_arguments(arguments: dict[str, Any]) -> str:
        """Builds a bounded, low-noise detail string from safe display arguments."""
        for key in _DETAIL_KEYS:
            value = arguments.get(key)
            if isinstance(value, str | int | float | bool):
                detail = " ".join(str(value).split())
                if len(detail) > 180:
                    detail = detail[:177] + "..."
                return detail
        return ""


def register_activity_resource(mcp: FastMCP) -> None:
    """Registers the compact ChatGPT activity-panel resource."""

    @mcp.resource(
        ACTIVITY_RESOURCE_URI,
        name="Serena activity",
        description="Compact live view of Serena tool calls with lightweight durable-job visibility.",
        mime_type=_ACTIVITY_RESOURCE_MIME_TYPE,
        meta={
            "ui": {"prefersBorder": True},
            "openai/widgetDescription": "Shows Serena tool calls, current-turn jobs, and a compact indicator for other running jobs.",
        },
    )
    def activity_resource() -> str:
        return _activity_widget_html()


def _activity_widget_html() -> str:
    """Returns the self-contained activity widget HTML."""
    return r"""
<div id="serena-activity" class="activity">
  <button id="activity-header" class="header" type="button" aria-expanded="true">
    <span class="title">
      <span id="activity-logo" class="logo" aria-hidden="true">
        <svg viewBox="0 0 256 256" focusable="false">
          <rect x="24" y="24" width="208" height="208" rx="48" fill="#ffffff" stroke="#00491e" stroke-width="12"/>
          <path d="M104 76 64 128l40 52M152 76l40 52-40 52" fill="none" stroke="#00491e" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M116 128h24" fill="none" stroke="#00491e" stroke-width="18" stroke-linecap="round"/>
          <circle cx="128" cy="128" r="9" fill="#00491e"/>
        </svg>
      </span>
      <span class="header-tool">
        <strong id="activity-header-tool">Waiting for activity</strong>
        <span id="activity-header-detail" class="header-detail"></span>
      </span>
    </span>
    <span id="activity-header-elapsed" class="summary"></span>
    <span id="activity-chevron" class="chevron" aria-hidden="true">⌄</span>
  </button>
  <div id="activity-body" class="body" aria-live="polite">
    <div id="activity-empty" class="empty">Waiting for Serena commands...</div>
    <ol id="activity-calls" class="calls"></ol>
    <button id="activity-other-jobs" class="other-jobs" type="button" aria-expanded="false" hidden>
      <span id="activity-other-jobs-label"></span><span class="other-jobs-chevron" aria-hidden="true">⌄</span>
    </button>
    <ol id="activity-background-jobs" class="calls background-jobs"></ol>
  </div>
</div>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body { margin: 0; font: 12px/1.35 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: CanvasText; background: transparent; }
  button { font: inherit; }
  .activity { width: 100%; min-width: 0; }
  .header { width: 100%; min-height: 42px; display: grid; grid-template-columns: minmax(0, 1fr) auto 12px; gap: 5px; align-items: center; padding: 6px 7px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
  .title { min-width: 0; display: flex; gap: 7px; align-items: center; white-space: nowrap; overflow: hidden; }
  .logo { width: 21px; height: 21px; flex: 0 0 auto; color: #00491e; opacity: .82; transform-origin: center; }
  .logo svg { display: block; width: 100%; height: 100%; }
  .logo.running { animation: logo-work .85s ease-in-out infinite alternate; opacity: 1; }
  .header-tool { min-width: 0; display: grid; gap: 1px; overflow: hidden; }
  #activity-header-tool { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .header-detail { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: 10.5px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .58; }
  .summary { white-space: nowrap; font-size: 11px; opacity: .66; font-variant-numeric: tabular-nums; }
  .activity:not(.collapsed) .header-detail, .activity:not(.collapsed) .summary { display: none; }
  .chevron, .other-jobs-chevron { width: 14px; text-align: center; transition: transform .14s ease; opacity: .58; }
  .activity.collapsed .chevron, .other-jobs[aria-expanded="false"] .other-jobs-chevron { transform: rotate(-90deg); }
  .body { max-height: 202px; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent); padding: 3px 7px 6px; }
  .activity.collapsed .body { display: none; }
  .calls { list-style: none; padding: 0; margin: 0; }
  .call { min-width: 0; }
  .row-header { width: 100%; display: grid; grid-template-columns: 15px minmax(0, 1fr) auto 12px; grid-template-areas: "status tool submitted chevron" ". detail elapsed chevron"; column-gap: 5px; row-gap: 0; align-items: start; min-height: 0; padding: 3px 0; border: 0; background: transparent; color: inherit; text-align: left; }
  button.row-header { cursor: pointer; }
  .job-entry { margin: 1px 0; border-radius: 6px; background: color-mix(in srgb, #00491e 5%, transparent); }
  .job-entry .row-header { padding-left: 4px; padding-right: 4px; }
  .status { grid-area: status; width: 15px; text-align: center; opacity: .78; }
  .call.running .status { color: #d97706; animation: pulse 1.1s ease-in-out infinite; }
  .call.completed .status { color: #16a34a; }
  .call.failed .status, .call.timed_out .status { color: #dc2626; }
  .call.cancelled .status { opacity: .5; }
  .job-entry.running .status { color: #00491e; }
  .tool { grid-area: tool; min-width: 0; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .job-entry .tool { font-weight: 700; }
  .detail { grid-area: detail; min-width: 0; margin-top: 1px; font: 10.5px/1.25 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .58; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-height: 1.25em; }
  .submitted, .elapsed { justify-self: end; white-space: nowrap; font-size: 10.5px; opacity: .52; font-variant-numeric: tabular-nums; }
  .submitted { grid-area: submitted; }
  .elapsed { grid-area: elapsed; }
  .row-chevron { grid-area: chevron; width: 14px; text-align: center; opacity: .46; transition: transform .14s ease; }
  .call:not(.expanded) .row-chevron { transform: rotate(-90deg); }
  .detail-panel { margin: 1px 0 5px 21px; padding: 5px 7px 6px; border-left: 2px solid color-mix(in srgb, #00491e 28%, transparent); border-radius: 0 6px 6px 0; background: color-mix(in srgb, CanvasText 3%, transparent); }
  .detail-block + .detail-block { margin-top: 5px; }
  .detail-label { display: block; margin-bottom: 2px; color: color-mix(in srgb, #00491e 82%, CanvasText); font-size: 10px; font-weight: 700; letter-spacing: .035em; text-transform: uppercase; }
  .detail-value { margin: 0; max-height: 9em; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: 10.5px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .78; }
  .job-meta { font-size: 10.5px; opacity: .68; font-variant-numeric: tabular-nums; }
  .job-output-note { margin-bottom: 3px; font-size: 10px; opacity: .56; }
  .job-output-scroll { max-height: 180px; overflow: auto; overscroll-behavior: contain; border: 1px solid color-mix(in srgb, CanvasText 10%, transparent); border-radius: 5px; background: color-mix(in srgb, CanvasText 3%, transparent); }
  .job-output { margin: 0; min-height: 2.4em; padding: 6px 7px; white-space: pre-wrap; overflow-wrap: anywhere; font: 10.5px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .detail-loading { font-size: 11px; opacity: .58; }
  .other-jobs { width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) 14px; gap: 6px; align-items: center; margin-top: 3px; padding: 4px 0 2px 21px; border: 0; border-top: 1px solid color-mix(in srgb, CanvasText 8%, transparent); background: transparent; color: inherit; text-align: left; cursor: pointer; font-size: 11px; opacity: .58; }
  .other-jobs:hover { opacity: .78; }
  .empty { padding: 5px 0 2px; opacity: .58; }
  @keyframes pulse { 50% { opacity: .28; } }
  @keyframes logo-work { from { transform: scale(.92); opacity: .62; } to { transform: scale(1.05); opacity: 1; } }
  @media (prefers-color-scheme: dark) { .logo { color: #70c990; } .job-entry.running .status { color: #70c990; } }
  @media (prefers-reduced-motion: reduce) { .call.running .status, .logo.running { animation: none; } .chevron, .other-jobs-chevron, .row-chevron { transition: none; } }
  @media (max-width: 520px) {
    body { font-size: 12px; }
    .header { min-height: 42px; grid-template-columns: minmax(0, 1fr) auto 12px; gap: 5px; padding: 6px 7px; }
    .header-detail { font-size: 10.5px; }
    .summary { font-size: 11px; }
    .body { max-height: 202px; padding: 3px 7px 6px; }
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
  const headerTool = document.getElementById("activity-header-tool");
  const headerDetail = document.getElementById("activity-header-detail");
  const headerElapsed = document.getElementById("activity-header-elapsed");
  const calls = document.getElementById("activity-calls");
  const empty = document.getElementById("activity-empty");
  const logo = document.getElementById("activity-logo");
  const otherJobsButton = document.getElementById("activity-other-jobs");
  const otherJobsLabel = document.getElementById("activity-other-jobs-label");
  const backgroundJobsList = document.getElementById("activity-background-jobs");
  const rowsByKey = new Map();
  const expandedRows = new Set();
  let state = window.openai?.toolOutput ?? null;
  let otherJobsExpanded = false;
  let timer = null;
  let clockTimer = null;
  let clockDelay = null;
  let jobDetailTimer = null;

  function setCollapsed(collapsed) {
    root.classList.toggle("collapsed", collapsed);
    header.setAttribute("aria-expanded", String(!collapsed));
    body.hidden = collapsed;
    syncJobDetailTimer();
    window.openai?.notifyIntrinsicHeight?.();
  }

  header.addEventListener("click", () => {
    setCollapsed(!root.classList.contains("collapsed"));
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
      detail: job.label || "background job",
    };
  }

  function visibleCalls(next) {
    const currentJobIds = new Set(currentTurnJobs(next).map(job => job.job_id));
    return (next.calls || [])
      .filter(call => !(call.tool_name === "start_job" && call.job_id && currentJobIds.has(call.job_id)))
      .map(callEntry);
  }

  function headerCall(next) {
    const toolCalls = next.calls || [];
    const running = toolCalls.filter(call => call.status === "running").sort((a, b) => b.started_at - a.started_at);
    if (running.length > 0) return running[0];
    return [...toolCalls].sort((a, b) => b.started_at - a.started_at)[0] || null;
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
      refs.arguments.textContent = detail.arguments || "{}";
      refs.result.textContent = detail.result ?? (detail.status === "running" ? "Tool is still running." : "No result returned.");
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
      argumentsValue = document.createElement("pre");
      argumentsValue.className = "detail-value";
      argumentsBlock.append(argumentsLabel, argumentsValue);
      const resultBlock = document.createElement("div");
      resultBlock.className = "detail-block";
      const resultLabel = document.createElement("span");
      resultLabel.className = "detail-label";
      resultLabel.textContent = "Result";
      resultValue = document.createElement("pre");
      resultValue.className = "detail-value";
      resultBlock.append(resultLabel, resultValue);
      content.append(argumentsBlock, resultBlock);
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
      detail,
      submitted: submittedNode,
      elapsed: elapsedNode,
      chevron,
      panel,
      loading,
      content,
      arguments: argumentsValue,
      result: resultValue,
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
    row.className = `call ${entry.status}${isJob ? " job-entry" : ""}`;
    refs.status.textContent = statusIcon(entry.status);
    refs.tool.textContent = entry.tool_name;
    refs.tool.title = entry.tool_name;
    const detailText = entry.detail || "";
    refs.detail.textContent = detailText;
    refs.detail.title = detailText;
    refs.submitted.textContent = submittedClock(entry.started_at);
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
    const oldScrollTop = body.scrollTop;
    reconcileInto(calls, primary, now);
    reconcileInto(backgroundJobsList, background, now);
    for (const [key, row] of rowsByKey) {
      if (retained.has(key)) continue;
      row.remove();
      rowsByKey.delete(key);
      expandedRows.delete(key);
    }
    if (oldScrollTop > 0) body.scrollTop = oldScrollTop;
  }

  function refreshDurations() {
    if (!state?.run_id) return;
    const now = Date.now() / 1000;
    const activeHeaderCall = headerCall(state);
    if (activeHeaderCall) headerElapsed.textContent = elapsed(activeHeaderCall, now);
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
    const now = Date.now() / 1000;
    const activeHeaderCall = headerCall(next);
    const backgroundJobs = otherRunningJobs(next);

    headerTool.textContent = activeHeaderCall?.tool_name || "Waiting for activity";
    const headerDetailText = activeHeaderCall?.detail || "";
    headerDetail.textContent = headerDetailText;
    headerDetail.title = headerDetailText;
    headerElapsed.textContent = activeHeaderCall ? elapsed(activeHeaderCall, now) : "";
    logo.classList.toggle("running", activeHeaderCall?.status === "running");

    if (backgroundJobs.length === 0) otherJobsExpanded = false;
    otherJobsButton.hidden = backgroundJobs.length === 0;
    otherJobsButton.setAttribute("aria-expanded", String(otherJobsExpanded));
    otherJobsLabel.textContent = backgroundJobs.length === 1 ? "1 other job running" : `${backgroundJobs.length} other jobs running`;

    const primary = primaryEntries(next);
    empty.hidden = primary.length > 0 || backgroundJobs.length > 0;
    reconcileRows(displayedEntries(next), displayedBackgroundJobs(next), now);
    syncClockTimer();
    syncJobDetailTimer();
    window.openai?.notifyIntrinsicHeight?.();
  }

  function hasRunningPanelActivity(next) {
    const hasRunningTool = (next?.calls || []).some(call => call.status === "running");
    const hasRunningJob = currentTurnJobs(next || {}).some(job => job.status === "running");
    return hasRunningTool || hasRunningJob;
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

  if (state?.run_id) render(state);
  poll();
})();
</script>
""".strip()
