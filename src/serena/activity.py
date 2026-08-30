import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

from mcp.server.fastmcp import Context, FastMCP

from serena.jobs import JobManager, JobRecord, JobStatus

ACTIVITY_RESOURCE_URI = "ui://serena/activity-v2.html"
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


def get_mcp_session_id(context: Context | None) -> str:
    """Returns the host conversation identifier, falling back to the MCP session object."""
    if context is None:
        return "global"

    try:
        meta = context.request_context.meta
        if meta is not None and meta.model_extra is not None:
            openai_session = meta.model_extra.get("openai/session")
            if isinstance(openai_session, str) and openai_session:
                return openai_session
    except (AttributeError, ValueError):
        pass

    try:
        return f"{id(context.session):x}"
    except (AttributeError, ValueError):
        return "global"


@dataclass
class ActivityCall:
    """One tool invocation displayed in the ChatGPT activity panel."""

    call_id: str
    tool_name: str
    detail: str
    started_at: float
    finished_at: float | None = None
    status: str = "running"
    job_id: str | None = None
    job_label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serializes the call for the activity widget."""
        payload: dict[str, Any] = {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "detail": self.detail,
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
        """Serializes the run-local state for the activity widget."""
        return {
            "run_id": self.run_id,
            "project_name": self.project_name,
            "started_at": self.started_at,
            "superseded": self.superseded,
            "calls": [call.as_dict() for call in self.calls],
        }


class ActivityJobSource(Protocol):
    """Provides retained durable-job metadata and activity ownership persistence."""

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        """Returns running jobs followed by recent terminal jobs."""
        ...

    def set_activity_owner(self, job_id: str, owner_token: str) -> JobRecord:
        """Persists the opaque activity owner token for one durable job."""
        ...


@dataclass(frozen=True)
class _ActiveToolInvocation:
    """One currently executing Serena tool, including calls outside an activity panel."""

    session_id: str
    run_id: str | None
    call: ActivityCall


class ActivityTracker:
    """Tracks Serena activity per ChatGPT conversation and reconciles durable jobs."""

    _MAX_JOBS_PER_SESSION = 100
    _JOB_LIST_LIMIT = 100

    def __init__(self, job_source: ActivityJobSource | None = None) -> None:
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, ActivityRun] = OrderedDict()
        self._current_run_by_session: dict[str, str] = {}
        self._active_tools: dict[str, _ActiveToolInvocation] = {}
        self._job_ids_by_session: dict[str, OrderedDict[str, None]] = {}
        self._job_source = job_source or JobManager()

    def start_run(self, session_id: str, project_name: str) -> dict[str, Any]:
        """Starts a new activity run with run-local history.

        Durable jobs are reconciled in :meth:`get_run`; only jobs running while this
        panel is active are backfilled into its history. Any tool already running in
        this chat is attached immediately so the new panel does not miss it.
        """
        with self._lock:
            previous_run_id = self._current_run_by_session.get(session_id)
            if previous_run_id is not None and previous_run_id in self._runs:
                self._runs[previous_run_id].superseded = True

            run = ActivityRun(
                run_id=uuid.uuid4().hex,
                session_id=session_id,
                project_name=project_name,
                started_at=time.time(),
            )
            self._runs[run.run_id] = run
            self._current_run_by_session[session_id] = run.run_id

            for call_id, invocation in list(self._active_tools.items()):
                if invocation.session_id != session_id:
                    continue
                run.calls.append(invocation.call)
                self._active_tools[call_id] = _ActiveToolInvocation(
                    session_id=invocation.session_id,
                    run_id=run.run_id,
                    call=invocation.call,
                )

            if len(run.calls) > _MAX_CALLS_PER_RUN:
                del run.calls[: len(run.calls) - _MAX_CALLS_PER_RUN]
            self._prune_runs()
            run_id = run.run_id

        return self.get_run(session_id, run_id)

    def start_tool(self, session_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """Records one tool invocation, including calls made outside an open activity panel."""
        call = ActivityCall(
            call_id=uuid.uuid4().hex,
            tool_name=tool_name,
            detail=self._summarize_arguments(arguments),
            started_at=time.time(),
        )

        with self._lock:
            run_id = self._current_run_by_session.get(session_id)
            run = self._runs.get(run_id) if run_id is not None else None

            # retain a specific durable-job identifier so a successful status/cancel call can reclaim it
            job_id = arguments.get("job_id")
            if tool_name in {"job_status", "cancel_job"} and isinstance(job_id, str) and job_id:
                call.job_id = job_id

            if run is not None:
                run.calls.append(call)
                if len(run.calls) > _MAX_CALLS_PER_RUN:
                    del run.calls[: len(run.calls) - _MAX_CALLS_PER_RUN]

            self._active_tools[call.call_id] = _ActiveToolInvocation(
                session_id=session_id,
                run_id=run.run_id if run is not None else None,
                call=call,
            )
        return call.call_id

    def finish_tool(self, call_id: str | None, succeeded: bool, result: object | None = None) -> None:
        """Marks a tool terminal and associates jobs dispatched by ``start_job`` with their chat."""
        if call_id is None:
            return

        job_to_claim: str | None = None
        owner_session: str | None = None
        with self._lock:
            invocation = self._active_tools.pop(call_id, None)
            if invocation is None:
                return

            call = invocation.call
            call.status = "completed" if succeeded else "failed"
            call.finished_at = time.time()

            if succeeded and call.tool_name == "start_job":
                job_id, label = self._extract_job_identity(result)
                if job_id is not None:
                    call.job_id = job_id
                    call.job_label = label
                    job_to_claim = job_id

            if job_to_claim is not None:
                run = self._runs.get(invocation.run_id) if invocation.run_id is not None else None
                self._remember_job_locked(invocation.session_id, job_to_claim, run)
                owner_session = invocation.session_id

        if job_to_claim is not None and owner_session is not None:
            self._claim_job_safely(job_to_claim, self._owner_token(owner_session))

    def get_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        """Returns one session-owned run enriched with durable jobs and global busy state."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")

            payload = run.as_dict()
            known_job_ids = set(self._job_ids_by_session.get(session_id, OrderedDict()).keys())
            run_job_ids = set(run.job_ids)
            active_this_chat = sum(invocation.session_id == session_id for invocation in self._active_tools.values())
            active_elsewhere = len(self._active_tools) - active_this_chat

        records = self._list_jobs_safely()
        owner_token = self._owner_token(session_id)
        persisted_running_ids = {
            record.job_id for record in records if record.activity_owner_token == owner_token and record.status is JobStatus.RUNNING
        }

        newly_running_ids = persisted_running_ids - run_job_ids
        if newly_running_ids:
            with self._lock:
                current_run = self._runs.get(run_id)
                for job_id in newly_running_ids:
                    self._remember_job_locked(session_id, job_id, current_run)
                run_job_ids = set(current_run.job_ids) if current_run is not None else run_job_ids | newly_running_ids

        visible_job_ids = run_job_ids
        jobs = [self._job_payload(record) for record in records if record.job_id in visible_job_ids]
        jobs.sort(key=lambda job: float(job["started_at"]))

        running_job_ids = {record.job_id for record in records if record.status is JobStatus.RUNNING}
        local_running_jobs = len(running_job_ids & (known_job_ids | persisted_running_ids))
        other_running_jobs = len(running_job_ids - (known_job_ids | persisted_running_ids))

        payload["jobs"] = jobs
        payload["busy"] = {
            "this_chat": bool(active_this_chat or local_running_jobs),
            "elsewhere": bool(active_elsewhere or other_running_jobs),
            "active_tools": active_this_chat,
            "running_jobs": local_running_jobs,
            "other_active_tools": active_elsewhere,
            "other_running_jobs": other_running_jobs,
        }
        return payload

    @staticmethod
    def _owner_token(session_id: str) -> str:
        """Returns a non-reversible token used to persist job ownership across Serena restarts."""
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _claim_job_safely(self, job_id: str, owner_token: str) -> None:
        """Persists job ownership without allowing activity bookkeeping to fail a successful tool call."""
        try:
            self._job_source.set_activity_owner(job_id, owner_token)
        except (OSError, RuntimeError, ValueError):
            pass

    def _remember_job_locked(self, session_id: str, job_id: str, run: ActivityRun | None) -> None:
        """Associates one durable job with a chat and its current panel, preserving recency."""
        jobs = self._job_ids_by_session.setdefault(session_id, OrderedDict())
        jobs[job_id] = None
        jobs.move_to_end(job_id)
        while len(jobs) > self._MAX_JOBS_PER_SESSION:
            jobs.popitem(last=False)

        if run is not None and job_id not in run.job_ids:
            run.job_ids.append(job_id)
            if len(run.job_ids) > self._MAX_JOBS_PER_SESSION:
                del run.job_ids[: len(run.job_ids) - self._MAX_JOBS_PER_SESSION]

    def _list_jobs_safely(self) -> list[JobRecord]:
        """Returns retained jobs without making activity polling fail when the job backend is unavailable."""
        try:
            return self._job_source.list_jobs(limit=self._JOB_LIST_LIMIT)
        except (OSError, RuntimeError, ValueError):
            return []

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
        """Extracts a durable job identifier from the normal ``start_job`` result shape."""
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

    def _prune_runs(self) -> None:
        """Bounds retained activity state while preserving current runs."""
        while len(self._runs) > _MAX_RUNS:
            run_id, run = self._runs.popitem(last=False)
            if self._current_run_by_session.get(run.session_id) == run_id:
                self._current_run_by_session.pop(run.session_id, None)

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
        description="Compact live view of Serena tool calls and durable jobs associated with the current ChatGPT conversation.",
        mime_type=_ACTIVITY_RESOURCE_MIME_TYPE,
        meta={
            "ui": {"prefersBorder": True},
            "openai/widgetDescription": "Shows Serena tool calls, durable jobs, and whether Serena is busy elsewhere.",
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
          <rect x="24" y="24" width="208" height="208" rx="48" fill="none" stroke="currentColor" stroke-width="12"/>
          <path d="M104 76 64 128l40 52M152 76l40 52-40 52" fill="none" stroke="currentColor" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M116 128h24" fill="none" stroke="currentColor" stroke-width="18" stroke-linecap="round"/>
          <circle cx="128" cy="128" r="9" fill="currentColor"/>
        </svg>
      </span>
      <strong id="activity-latest">Waiting for activity</strong>
    </span>
    <span id="activity-summary" class="summary"></span>
    <span id="activity-chevron" class="chevron" aria-hidden="true">⌄</span>
  </button>
  <div id="activity-body" class="body" aria-live="polite">
    <div id="activity-empty" class="empty">Waiting for commands...</div>
    <ol id="activity-calls" class="calls"></ol>
  </div>
</div>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.35 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: CanvasText; background: transparent; }
  .activity { width: 100%; min-width: 0; }
  .header { width: 100%; min-height: 36px; display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px; align-items: center; padding: 7px 9px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
  .title { min-width: 0; display: flex; gap: 7px; align-items: center; white-space: nowrap; overflow: hidden; }
  #activity-latest { min-width: 0; overflow: hidden; text-overflow: ellipsis; font-weight: 620; }
  .logo { width: 20px; height: 20px; flex: 0 0 auto; opacity: .72; transform-origin: center; }
  .logo svg { display: block; width: 100%; height: 100%; }
  .logo.running { animation: logo-work .85s ease-in-out infinite alternate; opacity: 1; }
  .summary { min-width: 0; max-width: 48vw; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 12px; opacity: .68; font-variant-numeric: tabular-nums; }
  .chevron { width: 14px; text-align: center; transition: transform .14s ease; opacity: .58; }
  .activity.collapsed .chevron { transform: rotate(-90deg); }
  .body { border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent); padding: 4px 9px 7px; }
  .activity.collapsed .body { display: none; }
  .calls { list-style: none; padding: 0; margin: 0; }
  .call { display: grid; grid-template-columns: 15px minmax(110px, auto) minmax(0, 1fr) auto; grid-template-areas: "status tool detail elapsed"; gap: 6px; align-items: baseline; min-height: 24px; padding: 3px 0; }
  .status { grid-area: status; width: 15px; text-align: center; opacity: .74; }
  .call.running .status { animation: pulse 1.1s ease-in-out infinite; }
  .tool { grid-area: tool; min-width: 0; font-weight: 590; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .detail { grid-area: detail; min-width: 0; font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .72; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .elapsed { grid-area: elapsed; white-space: nowrap; font-size: 11px; opacity: .56; font-variant-numeric: tabular-nums; }
  .empty { padding: 5px 0 2px; opacity: .58; }
  @keyframes pulse { 50% { opacity: .28; } }
  @keyframes logo-work { from { transform: scale(.90); opacity: .58; } to { transform: scale(1.04); opacity: 1; } }
  @media (prefers-reduced-motion: reduce) { .call.running .status, .logo.running { animation: none; } .chevron { transition: none; } }
  @media (max-width: 520px) {
    body { font-size: 12px; }
    .header { min-height: 34px; grid-template-columns: minmax(0, 1fr) minmax(0, auto) 12px; gap: 5px; padding: 6px 7px; }
    .summary { max-width: 45vw; font-size: 11px; }
    .body { padding: 2px 7px 5px; }
    .call { grid-template-columns: 15px minmax(0, 1fr) auto; grid-template-areas: "status tool elapsed" ". detail detail"; column-gap: 5px; row-gap: 0; min-height: 0; padding: 2px 0; align-items: start; }
    .tool { line-height: 1.2; }
    .detail { margin-top: 1px; font-size: 11px; line-height: 1.2; min-height: 1.2em; }
    .elapsed { line-height: 1.2; align-self: start; }
    .empty { padding: 4px 0 2px; }
  }
</style>
<script>
(() => {
  const root = document.getElementById("serena-activity");
  if (!root || root.dataset.initialized === "true") return;
  root.dataset.initialized = "true";

  const header = document.getElementById("activity-header");
  const body = document.getElementById("activity-body");
  const latest = document.getElementById("activity-latest");
  const summary = document.getElementById("activity-summary");
  const calls = document.getElementById("activity-calls");
  const empty = document.getElementById("activity-empty");
  const logo = document.getElementById("activity-logo");
  let state = window.openai?.toolOutput ?? null;
  let lastChange = Date.now();
  let lastSignature = "";
  let timer = null;

  function setCollapsed(collapsed) {
    root.classList.toggle("collapsed", collapsed);
    header.setAttribute("aria-expanded", String(!collapsed));
    body.hidden = collapsed;
    window.openai?.notifyIntrinsicHeight?.();
  }

  header.addEventListener("click", () => {
    setCollapsed(!root.classList.contains("collapsed"));
  });

  function elapsed(entry, nowSeconds) {
    const end = entry.finished_at ?? nowSeconds;
    const seconds = Math.max(0, end - entry.started_at);
    if (seconds < 10) return `${seconds.toFixed(1)}s`;
    if (seconds < 120) return `${Math.round(seconds)}s`;
    const minutes = Math.floor(seconds / 60);
    const remainder = Math.round(seconds % 60);
    return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  }

  function statusIcon(status) {
    if (status === "running") return "●";
    if (status === "failed" || status === "timed_out") return "!";
    if (status === "cancelled") return "x";
    return "✓";
  }

  function callEntry(call) {
    return { ...call, key: `call:${call.call_id}`, kind: "call" };
  }

  function jobEntry(job) {
    return {
      key: `job:${job.job_id}`,
      kind: "job",
      tool_name: job.label || "background job",
      detail: job.project ? `job · ${job.project}` : "background job",
      started_at: job.started_at,
      finished_at: job.finished_at,
      status: job.status,
    };
  }

  function allEntries(next) {
    const jobs = next.jobs || [];
    const durableIds = new Set(jobs.map(job => job.job_id));
    return [
      ...jobs.map(jobEntry),
      ...(next.calls || [])
        .filter(call => !(call.tool_name === "start_job" && call.job_id && durableIds.has(call.job_id)))
        .map(callEntry),
    ];
  }

  function visibleEntries(next) {
    const entries = allEntries(next);
    const running = entries.filter(entry => entry.status === "running").sort((a, b) => b.started_at - a.started_at);
    const terminal = entries.filter(entry => entry.status !== "running").sort((a, b) => b.started_at - a.started_at);
    const runningVisible = running.slice(0, 8);
    const terminalSlots = Math.max(0, 8 - runningVisible.length);
    return [...runningVisible, ...terminal.slice(0, terminalSlots)];
  }

  function latestEntry(next) {
    const entries = allEntries(next);
    if (!entries.length) return null;
    return entries.reduce((latestEntry, entry) => entry.started_at >= latestEntry.started_at ? entry : latestEntry);
  }

  function render(next) {
    if (!next || !next.run_id) return;
    state = next;
    const jobs = next.jobs || [];
    const busy = next.busy || {};
    const signature = JSON.stringify([
      (next.calls || []).map(call => [call.call_id, call.status, call.finished_at, call.job_id]),
      jobs.map(job => [job.job_id, job.status, job.finished_at]),
      busy,
    ]);
    if (signature !== lastSignature) {
      lastSignature = signature;
      lastChange = Date.now();
    }

    calls.replaceChildren();
    const now = Date.now() / 1000;
    const runningJobs = jobs.filter(job => job.status === "running");
    const runningCalls = (next.calls || []).filter(call => call.status === "running");
    const localRunningCount = Math.max(runningJobs.length, Number(busy.running_jobs || 0))
      + Math.max(runningCalls.length, Number(busy.active_tools || 0));
    const otherRunningCount = Number(busy.other_active_tools || 0) + Number(busy.other_running_jobs || 0);
    const totalRunningCount = localRunningCount + otherRunningCount;
    const busyThisChat = Boolean(busy.this_chat || localRunningCount);
    const busyElsewhere = Boolean(busy.elsewhere || otherRunningCount);
    const newest = latestEntry(next);

    logo.classList.toggle("running", totalRunningCount > 0);
    root.classList.toggle("busy-elsewhere", !busyThisChat && busyElsewhere);

    if (newest) {
      latest.textContent = newest.tool_name;
      const extraRunning = Math.max(0, totalRunningCount - (newest.status === "running" ? 1 : 0));
      const parts = [elapsed(newest, now)];
      if (extraRunning > 0) parts.push(`+${extraRunning}`);
      summary.textContent = parts.join(" · ");
    } else if (otherRunningCount > 0) {
      latest.textContent = "Busy elsewhere";
      summary.textContent = `+${otherRunningCount}`;
    } else {
      latest.textContent = "Waiting for activity";
      summary.textContent = "";
    }

    const entries = visibleEntries(next);
    empty.hidden = entries.length > 0;
    entries.forEach(entry => {
      const row = document.createElement("li");
      row.className = `call ${entry.status}`;
      row.innerHTML = `<span class="status"></span><span class="tool"></span><span class="detail"></span><span class="elapsed"></span>`;
      row.querySelector(".status").textContent = statusIcon(entry.status);
      row.querySelector(".tool").textContent = entry.tool_name;
      row.querySelector(".detail").textContent = entry.detail || "";
      row.querySelector(".elapsed").textContent = elapsed(entry, now);
      calls.appendChild(row);
    });

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
      if (next?.superseded) return;
    } catch (_) {
      // Keep the last useful state; transient bridge/server failures are non-fatal.
    }
    const busy = state?.busy || {};
    const hasActivity = Boolean(busy.this_chat || busy.elsewhere || state?.calls?.some(call => call.status === "running"));
    const idleFor = Date.now() - lastChange;
    const delay = hasActivity || idleFor < 5000 ? 500 : idleFor < 120000 ? 2000 : 10000;
    timer = setTimeout(poll, delay);
  }

  function acceptGlobals(event) {
    const next = event?.detail?.globals?.toolOutput;
    if (next?.run_id) render(next);
  }
  window.addEventListener("openai:set_globals", acceptGlobals, { passive: true });

  if (state?.run_id) render(state);
  poll();
})();
</script>
""".strip()
