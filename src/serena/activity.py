import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

ACTIVITY_RESOURCE_URI = "ui://serena/activity-v1.html"
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

    def as_dict(self) -> dict[str, Any]:
        """Serializes the call for the activity widget."""
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
        }


@dataclass
class ActivityRun:
    """One model-driven Serena activity panel within a client session."""

    run_id: str
    session_id: str
    project_name: str
    started_at: float
    calls: list[ActivityCall] = field(default_factory=list)
    superseded: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serializes the run for the activity widget."""
        return {
            "run_id": self.run_id,
            "project_name": self.project_name,
            "started_at": self.started_at,
            "superseded": self.superseded,
            "calls": [call.as_dict() for call in self.calls],
        }


class ActivityTracker:
    """Tracks short-lived Serena tool activity per ChatGPT conversation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, ActivityRun] = OrderedDict()
        self._current_run_by_session: dict[str, str] = {}

    def start_run(self, session_id: str, project_name: str) -> dict[str, Any]:
        """Starts a new activity run for ``session_id`` and returns its snapshot."""
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
            self._prune_runs()
            return run.as_dict()

    def start_tool(self, session_id: str, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Records a tool invocation when a run is active for ``session_id``."""
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
            )
            run.calls.append(call)
            if len(run.calls) > _MAX_CALLS_PER_RUN:
                del run.calls[: len(run.calls) - _MAX_CALLS_PER_RUN]
            return call.call_id

    def finish_tool(self, call_id: str | None, succeeded: bool) -> None:
        """Marks the tracked call terminal when ``call_id`` belongs to a live run."""
        if call_id is None:
            return

        with self._lock:
            for run in reversed(self._runs.values()):
                for call in reversed(run.calls):
                    if call.call_id == call_id:
                        call.status = "completed" if succeeded else "failed"
                        call.finished_at = time.time()
                        return

    def get_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        """Returns one run only when it belongs to the requesting client session."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")
            return run.as_dict()

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
        description="Compact live view of Serena tool calls in the current ChatGPT turn.",
        mime_type=_ACTIVITY_RESOURCE_MIME_TYPE,
        meta={
            "ui": {"prefersBorder": True},
            "openai/widgetDescription": "Shows Serena tool calls while they run and collapses when idle.",
        },
    )
    def activity_resource() -> str:
        return _activity_widget_html()


def _activity_widget_html() -> str:
    """Returns the self-contained activity widget HTML."""
    return r"""
<div id="serena-activity" class="activity">
  <button id="activity-header" class="header" type="button" aria-expanded="true">
    <span class="title"><span id="activity-dot" class="dot"></span><strong>Serena</strong><span id="activity-project"></span></span>
    <span id="activity-summary" class="summary">Starting...</span>
    <span id="activity-chevron" class="chevron" aria-hidden="true">⌄</span>
  </button>
  <div id="activity-body" class="body" aria-live="polite">
    <div id="activity-empty" class="empty">Waiting for Serena commands...</div>
    <ol id="activity-calls" class="calls"></ol>
  </div>
</div>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.35 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: CanvasText; background: transparent; }
  .activity { width: 100%; min-width: 0; }
  .header { width: 100%; min-height: 36px; display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 9px; align-items: center; padding: 7px 9px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
  .title { min-width: 0; display: flex; gap: 5px; align-items: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #activity-project { overflow: hidden; text-overflow: ellipsis; opacity: .66; }
  #activity-project:not(:empty)::before { content: "· "; }
  .summary { white-space: nowrap; font-size: 12px; opacity: .66; }
  .chevron { width: 14px; text-align: center; transition: transform .14s ease; opacity: .58; }
  .activity.collapsed .chevron { transform: rotate(-90deg); }
  .body { border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent); padding: 4px 9px 7px; }
  .activity.collapsed .body { display: none; }
  .calls { list-style: none; padding: 0; margin: 0; }
  .call { display: grid; grid-template-columns: 15px minmax(110px, auto) minmax(0, 1fr) auto; gap: 6px; align-items: baseline; min-height: 24px; padding: 3px 0; }
  .status { width: 15px; text-align: center; opacity: .74; }
  .call.running .status { animation: pulse 1.1s ease-in-out infinite; }
  .tool { font-weight: 590; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .detail { min-width: 0; font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .72; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .elapsed { white-space: nowrap; font-size: 11px; opacity: .56; font-variant-numeric: tabular-nums; }
  .empty { padding: 5px 0 2px; opacity: .58; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; opacity: .42; flex: 0 0 auto; }
  .dot.running { animation: pulse 1.1s ease-in-out infinite; opacity: .8; }
  @keyframes pulse { 50% { opacity: .28; } }
  @media (prefers-reduced-motion: reduce) { .call.running .status, .dot.running { animation: none; } .chevron { transition: none; } }
  @media (max-width: 420px) { .call { grid-template-columns: 15px minmax(0, 1fr) auto; } .detail { grid-column: 2 / 4; margin-top: -2px; } }
</style>
<script>
(() => {
  const root = document.getElementById("serena-activity");
  if (!root || root.dataset.initialized === "true") return;
  root.dataset.initialized = "true";

  const header = document.getElementById("activity-header");
  const body = document.getElementById("activity-body");
  const project = document.getElementById("activity-project");
  const summary = document.getElementById("activity-summary");
  const calls = document.getElementById("activity-calls");
  const empty = document.getElementById("activity-empty");
  const dot = document.getElementById("activity-dot");
  let state = window.openai?.toolOutput ?? null;
  let lastChange = Date.now();
  let lastSignature = "";
  let collapsedByIdle = false;
  let timer = null;

  function setCollapsed(collapsed) {
    root.classList.toggle("collapsed", collapsed);
    header.setAttribute("aria-expanded", String(!collapsed));
    body.hidden = collapsed;
    window.openai?.notifyIntrinsicHeight?.();
  }

  header.addEventListener("click", () => {
    collapsedByIdle = false;
    setCollapsed(!root.classList.contains("collapsed"));
  });

  function elapsed(call, nowSeconds) {
    const end = call.finished_at ?? nowSeconds;
    const seconds = Math.max(0, end - call.started_at);
    return seconds < 10 ? `${seconds.toFixed(1)}s` : `${Math.round(seconds)}s`;
  }

  function render(next) {
    if (!next || !next.run_id) return;
    state = next;
    const signature = JSON.stringify(next.calls.map(call => [call.call_id, call.status, call.finished_at]));
    if (signature !== lastSignature) {
      lastSignature = signature;
      lastChange = Date.now();
      if (next.calls.some(call => call.status === "running")) {
        collapsedByIdle = false;
        setCollapsed(false);
      }
    }

    project.textContent = next.project_name || "";
    calls.replaceChildren();
    const now = Date.now() / 1000;
    const running = next.calls.filter(call => call.status === "running").length;
    const failed = next.calls.filter(call => call.status === "failed").length;
    const completed = next.calls.filter(call => call.status === "completed").length;
    dot.classList.toggle("running", running > 0);

    if (running) summary.textContent = `${running} running · ${completed} done`;
    else if (failed) summary.textContent = `${completed} done · ${failed} failed`;
    else summary.textContent = `${completed} command${completed === 1 ? "" : "s"}`;

    empty.hidden = next.calls.length > 0;
    next.calls.slice(-8).forEach(call => {
      const row = document.createElement("li");
      row.className = `call ${call.status}`;
      const icon = call.status === "running" ? "●" : call.status === "failed" ? "!" : "✓";
      row.innerHTML = `<span class="status"></span><span class="tool"></span><span class="detail"></span><span class="elapsed"></span>`;
      row.querySelector(".status").textContent = icon;
      row.querySelector(".tool").textContent = call.tool_name;
      row.querySelector(".detail").textContent = call.detail || "";
      row.querySelector(".elapsed").textContent = elapsed(call, now);
      calls.appendChild(row);
    });

    if (!running && next.calls.length > 0 && Date.now() - lastChange > 1800 && !collapsedByIdle) {
      collapsedByIdle = true;
      setCollapsed(true);
    }
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
    const hasRunning = state?.calls?.some(call => call.status === "running");
    const idleFor = Date.now() - lastChange;
    const delay = hasRunning || idleFor < 5000 ? 500 : idleFor < 120000 ? 2000 : 10000;
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
