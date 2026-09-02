"""ChatGPT activity panel for Orchestrator delegates."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from orchestrator.delegates import (
    DelegateError,
    DelegateState,
    DelegateStatusResponse,
    DelegateStore,
)

ACTIVITY_RESOURCE_URI = "ui://orchestrator/activity-v1.html"
_ACTIVITY_RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"
_MAX_RUNS = 32
_MAX_DELEGATES_PER_RUN = 50
_ACTIVE_STATES = {
    DelegateState.WAITING_FOR_CHAT,
    DelegateState.QUEUED,
    DelegateState.RUNNING_CHAT,
    DelegateState.RUNNING_CODEX,
}
_LOGO_SVG = (Path(__file__).parent / "resources" / "orchestrator-logo.svg").read_text(encoding="utf-8")


@dataclass
class ActivityRun:
    """Represents one Orchestrator panel owned by a ChatGPT session."""

    run_id: str
    session_id: str
    started_at: float
    delegate_ids: list[str] = field(default_factory=list)
    superseded: bool = False


class OrchestratorActivityTracker:
    """Tracks delegate visibility for independently owned ChatGPT activity panels."""

    def __init__(self, delegate_store: DelegateStore) -> None:
        self._delegate_store = delegate_store
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, ActivityRun] = OrderedDict()
        self._current_run_by_session: dict[str, str] = {}

    def start_run(self, session_id: str) -> dict[str, Any]:
        """Starts a panel and supersedes only the previous panel in this session."""
        active = self._delegate_store.list_visible(session_id, active_only=True, limit=_MAX_DELEGATES_PER_RUN)

        with self._lock:
            # retire ownership only within the same ChatGPT conversation
            previous_run_id = self._current_run_by_session.get(session_id)
            previous_run = self._runs.get(previous_run_id) if previous_run_id is not None else None
            if previous_run is not None:
                previous_run.superseded = True

            run = ActivityRun(
                run_id=uuid.uuid4().hex,
                session_id=session_id,
                started_at=time.time(),
                delegate_ids=[item.delegate_id for item in active],
            )
            self._runs[run.run_id] = run
            self._current_run_by_session[session_id] = run.run_id
            self._prune_runs()

        return self.get_run(session_id, run.run_id)

    def note_delegate(self, session_id: str, delegate_id: str) -> None:
        """Associates one successfully touched delegate with the current session panel."""
        with self._lock:
            run_id = self._current_run_by_session.get(session_id)
            run = self._runs.get(run_id) if run_id is not None else None
            if run is None or delegate_id in run.delegate_ids:
                return
            run.delegate_ids.append(delegate_id)
            if len(run.delegate_ids) > _MAX_DELEGATES_PER_RUN:
                del run.delegate_ids[: len(run.delegate_ids) - _MAX_DELEGATES_PER_RUN]

    def get_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        """Returns session-owned delegate activity without absorbing another panel's work."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")
            superseded = run.superseded

        # current panels discover all active delegates visible to their own session
        if not superseded:
            active = self._delegate_store.list_visible(session_id, active_only=True, limit=_MAX_DELEGATES_PER_RUN)
            with self._lock:
                run = self._runs.get(run_id)
                if run is None or run.session_id != session_id:
                    raise ValueError("Activity run is not available in this session")
                for item in active:
                    if item.delegate_id not in run.delegate_ids:
                        run.delegate_ids.append(item.delegate_id)
                if len(run.delegate_ids) > _MAX_DELEGATES_PER_RUN:
                    del run.delegate_ids[: len(run.delegate_ids) - _MAX_DELEGATES_PER_RUN]

        # superseded panels retain only delegates they already owned
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("Activity run is not available in this session")
            delegate_ids = list(run.delegate_ids)
            started_at = run.started_at
            superseded = run.superseded

        delegates: list[DelegateStatusResponse] = []
        for delegate_id in delegate_ids:
            try:
                delegates.append(self._delegate_store.status(delegate_id, session_id))
            except DelegateError:
                continue
        delegates.sort(
            key=lambda item: (
                item.state not in _ACTIVE_STATES,
                -item.created_at.timestamp(),
            )
        )

        return {
            "run_id": run_id,
            "started_at": started_at,
            "superseded": superseded,
            "delegates": [item.model_dump(mode="json") for item in delegates],
        }

    def _prune_runs(self) -> None:
        """Bounds retained panel state while preserving current-run bookkeeping."""
        while len(self._runs) > _MAX_RUNS:
            run_id, run = self._runs.popitem(last=False)
            if self._current_run_by_session.get(run.session_id) == run_id:
                self._current_run_by_session.pop(run.session_id, None)


def register_activity_resource(mcp: FastMCP) -> None:
    """Registers the Orchestrator-only ChatGPT delegate panel resource."""

    @mcp.resource(
        ACTIVITY_RESOURCE_URI,
        name="Orchestrator activity",
        description="Compact live view of delegates visible to the current ChatGPT session.",
        mime_type=_ACTIVITY_RESOURCE_MIME_TYPE,
        meta={
            "ui": {"prefersBorder": True},
            "openai/widgetDescription": "Shows Orchestrator delegates, lifecycle state, and private on-demand detail.",
        },
    )
    def activity_resource() -> str:
        return activity_widget_html()


def activity_widget_html() -> str:
    """Returns the self-contained Orchestrator delegate widget."""
    return r"""
<div id="orchestrator-activity" class="activity">
  <button id="activity-header" class="header" type="button" aria-expanded="true">
    <span class="title">
      <span id="activity-logo" class="logo" aria-hidden="true">__ORCHESTRATOR_LOGO__</span>
      <span class="header-overview">
        <strong>Orchestrator</strong>
        <span id="activity-count" class="count">0 agents</span>
      </span>
    </span>
    <span id="activity-header-status" class="header-status">Idle</span>
    <span id="activity-chevron" class="chevron" aria-hidden="true">⌄</span>
  </button>
  <div id="activity-body" class="body" aria-live="polite">
    <div id="activity-empty" class="empty">Waiting for activity...</div>
    <ol id="activity-delegates" class="delegates"></ol>
  </div>
</div>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 12px/1.35 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: CanvasText; background: transparent; }
  button { font: inherit; color: inherit; }
  .activity { width: 100%; min-width: 0; }
  .header { width: 100%; min-height: 42px; display: grid; grid-template-columns: minmax(0, 1fr) auto 12px; gap: 5px; align-items: center; padding: 6px 7px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
  .title { min-width: 0; display: flex; gap: 7px; align-items: center; white-space: nowrap; overflow: hidden; }
  .logo { width: 21px; height: 21px; flex: 0 0 auto; color: #00491e; opacity: .82; }
  .logo svg { display: block; width: 100%; height: 100%; }
  .header-overview { min-width: 0; display: grid; gap: 1px; overflow: hidden; }
  .header-overview strong, .count { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .count { font-size: 10.5px; line-height: 1.2; opacity: .58; }
  .header-status { justify-self: end; white-space: nowrap; font-size: 11px; font-variant-numeric: tabular-nums; opacity: .58; }
  .header-status.running { color: #00491e; opacity: 1; font-weight: 650; }
  .header-status.failed { color: #dc2626; opacity: 1; font-weight: 650; }
  .chevron { width: 14px; text-align: center; transition: transform .14s ease; opacity: .58; }
  .activity.collapsed .chevron { transform: rotate(-90deg); }
  .activity.collapsed .body { display: none; }
  .body { max-height: 202px; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent); padding: 3px 7px 6px; }
  .empty { padding: 5px 0 2px; opacity: .58; }
  .delegates { list-style: none; margin: 0; padding: 0; }
  .row { min-width: 0; }
  .row-header { width: 100%; display: grid; grid-template-columns: 15px minmax(0, 1fr) auto; grid-template-areas: "status label submitted" "status meta elapsed"; column-gap: 5px; row-gap: 0; align-items: start; min-height: 0; padding: 3px 0; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
  .status { grid-area: status; align-self: center; width: 15px; text-align: center; opacity: .78; font-size: larger;}
  .row.running .status { color: #00491e; animation: pulse 1.1s ease-in-out infinite; }
  .row.completed .status { color: #16a34a; }
  .row.failed .status, .row.timed_out .status { color: #dc2626; }
  .row.cancelled .status { opacity: .5; }
  .label { grid-area: label; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 700; }
  .submitted, .elapsed { justify-self: end; white-space: nowrap; font-size: 10.5px; opacity: .52; font-variant-numeric: tabular-nums; }
  .submitted { grid-area: submitted; }
  .elapsed { grid-area: elapsed; }
  .meta { grid-area: meta; min-width: 0; margin-top: 1px; font-size: 10.5px; line-height: 1.25; opacity: .58; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .detail { margin: 1px 0 5px 20px; padding: 5px 7px 6px; border-left: 2px solid color-mix(in srgb, #00491e 28%, transparent); border-radius: 0 6px 6px 0; background: color-mix(in srgb, CanvasText 3%, transparent); font-size: 10.5px; }
  .detail[hidden] { display: none; }
  .detail-grid { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 3px 8px; }
  .detail-key { color: color-mix(in srgb, #00491e 82%, CanvasText); opacity: .78; font-weight: 700; }
  .goal { margin-top: 5px; white-space: pre-wrap; }
  .audit { margin-top: 5px; max-height: 90px; overflow: auto; white-space: pre-wrap; font: 10.5px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; opacity: .72; }
  .row-actions { margin: 1px 0 5px 20px; display: flex; align-items: stretch; gap: 6px; }
  .row-actions[hidden] { display: none; }
  .copy-action, .fallback-action { border: 1px solid color-mix(in srgb, CanvasText 14%, transparent); background: transparent; border-radius: 5px; padding: 4px 7px; cursor: pointer; font-size: 10.5px; }
  .copy-action { flex: 0 0 auto; }
  .fallback-action { position: relative; flex: 1 1 auto; min-width: 145px; overflow: hidden; text-align: left; }
  .fallback-action[hidden] { display: none; }
  .fallback-action:disabled { cursor: default; opacity: .65; }
  .fallback-progress { position: absolute; inset: 0 auto 0 0; width: 100%; background: color-mix(in srgb, #00491e 9%, transparent); pointer-events: none; }
  .fallback-label { position: relative; z-index: 1; }
  @keyframes pulse { 50% { opacity: .28; } }
  @media (prefers-color-scheme: dark) { .logo { color: #70c990; } .header-status.running, .row.running .status { color: #70c990; } }
  @media (prefers-reduced-motion: reduce) { .row.running .status { animation: none; } .chevron { transition: none; } }
</style>
<script>
(() => {
  const root = document.getElementById("orchestrator-activity");
  if (!root || root.dataset.initialized === "true") return;
  root.dataset.initialized = "true";

  const header = document.getElementById("activity-header");
  const body = document.getElementById("activity-body");
  const logo = document.getElementById("activity-logo");
  const count = document.getElementById("activity-count");
  const headerStatus = document.getElementById("activity-header-status");
  const list = document.getElementById("activity-delegates");
  const empty = document.getElementById("activity-empty");
  const rows = new Map();
  const expanded = new Set();
  let state = window.openai?.toolOutput ?? null;
  let timer = null;

  const activeStates = new Set(["WAITING_FOR_CHAT", "QUEUED", "RUNNING_CHAT", "RUNNING_CODEX"]);

  header.addEventListener("click", () => {
    const collapsed = !root.classList.contains("collapsed");
    root.classList.toggle("collapsed", collapsed);
    header.setAttribute("aria-expanded", String(!collapsed));
    body.hidden = collapsed;
    window.openai?.notifyIntrinsicHeight?.();
  });

  function elapsed(item) {
    const start = Date.parse(item.started_at || item.created_at || "");
    if (!Number.isFinite(start)) return "";
    const end = item.finished_at ? Date.parse(item.finished_at) : Date.now();
    const seconds = Math.max(0, Math.floor((end - start) / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }

  function submittedClock(timestamp) {
    const value = Date.parse(timestamp || "");
    if (!Number.isFinite(value)) return "";
    return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function label(item) {
    const provider = item.active_provider || item.provider_policy || "chat";
    return `${provider === "chat" ? "ChatGPT" : "Codex"} · ${item.kind}`;
  }

  function meta(item) {
    const stateLabel = String(item.state || "").toLowerCase().replaceAll("_", " ");
    return `${item.project_name || "project"} · ${stateLabel}`;
  }

  function statusClass(state) {
    if (state === "RUNNING_CHAT" || state === "RUNNING_CODEX") return "running";
    if (state === "COMPLETED") return "completed";
    if (state === "FAILED") return "failed";
    if (state === "TIMED_OUT") return "timed_out";
    if (state === "CANCELLED") return "cancelled";
    return "waiting";
  }

  function statusIcon(state) {
    if (state === "RUNNING_CHAT" || state === "RUNNING_CODEX") return "●";
    if (state === "FAILED" || state === "TIMED_OUT") return "!";
    if (state === "CANCELLED") return "\u00d7";
    if (state === "WAITING_FOR_CHAT" || state === "QUEUED") return "○";
    return "✓";
  }

  function launchPrompt(item) {
    return `@Orchestrator claim delegate ${item.delegate_id} and complete it independently.`;
  }

  async function copyLaunchPrompt(item, button) {
    const prompt = launchPrompt(item);
    try {
      await navigator.clipboard.writeText(prompt);
      button.textContent = "Copied";
    } catch (_) {
      button.textContent = prompt;
    }
  }

  function fallbackTiming(item) {
    const created = Date.parse(item.created_at || "");
    const deadline = Date.parse(item.claim_deadline || "");
    if (!Number.isFinite(created) || !Number.isFinite(deadline) || deadline <= created) return null;
    const totalMs = deadline - created;
    const remainingMs = Math.max(0, deadline - Date.now());
    return { remainingMs, fraction: Math.min(1, remainingMs / totalMs) };
  }

  function formatRemaining(milliseconds) {
    const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }

  async function useCodexNow(item, button) {
    if (!window.openai?.callTool || item.state !== "WAITING_FOR_CHAT") return;
    button.disabled = true;
    const labelNode = button.querySelector(".fallback-label");
    if (labelNode) labelNode.textContent = "Starting Codex...";
    try {
      const result = await window.openai.callTool("delegate_reroute", { delegate_id: item.delegate_id, provider_policy: "codex" });
      const nextItem = result?.structuredContent ?? result?.structured_content ?? result;
      if (nextItem?.delegate_id !== item.delegate_id) throw new Error("Mismatched delegate reroute");
    } catch (_) {
      button.disabled = false;
      if (labelNode) labelNode.textContent = "Use Codex now";
    }
  }

  function updateRowActions(item, refs) {
    const waiting = item.state === "WAITING_FOR_CHAT";
    refs.actions.hidden = !waiting;
    if (!waiting) return;

    refs.copyAction.textContent = "Copy launch prompt";
    refs.fallbackAction.hidden = item.provider_policy !== "auto" || !item.claim_deadline;
    if (refs.fallbackAction.hidden) return;

    const timing = fallbackTiming(item);
    refs.fallbackAction.disabled = false;
    refs.fallbackProgress.style.width = `${(timing?.fraction ?? 0) * 100}%`;
    refs.fallbackLabel.textContent = timing
      ? `Use Codex now · auto in ${formatRemaining(timing.remainingMs)}`
      : "Use Codex now";
  }

  async function loadDetail(item, detail) {
    detail.textContent = "Loading detail...";
    try {
      const result = await window.openai.callTool("get_orchestrator_delegate_detail", { delegate_id: item.delegate_id });
      const value = result?.structuredContent ?? result?.structured_content ?? result;
      if (!value?.delegate_id || value.delegate_id !== item.delegate_id) throw new Error("Mismatched delegate detail");
      detail.textContent = "";
      const grid = document.createElement("div");
      grid.className = "detail-grid";
      const metadata = value.provider_metadata || {};
      const usage = metadata.usage || {};
      const pairs = [
        ["ID", value.delegate_id],
        ["Provider", value.active_provider || value.provider_policy || "—"],
        ["State", String(value.state || "").toLowerCase().replaceAll("_", " ")],
      ];
      if (metadata.model) pairs.push(["Model", metadata.model]);
      if (metadata.reasoning_effort) pairs.push(["Reasoning", metadata.reasoning_effort]);
      if (metadata.worktree) pairs.push(["Worktree", metadata.worktree]);
      const usageParts = [];
      if (Number.isFinite(usage.input_tokens)) usageParts.push(`in ${usage.input_tokens}`);
      if (Number.isFinite(usage.cached_input_tokens)) usageParts.push(`cached ${usage.cached_input_tokens}`);
      if (Number.isFinite(usage.output_tokens)) usageParts.push(`out ${usage.output_tokens}`);
      if (Number.isFinite(usage.reasoning_output_tokens)) usageParts.push(`reasoning ${usage.reasoning_output_tokens}`);
      if (usageParts.length) pairs.push(["Tokens", usageParts.join(" · ")]);
      for (const [key, val] of pairs) {
        const k = document.createElement("span"); k.className = "detail-key"; k.textContent = key;
        const v = document.createElement("span"); v.textContent = val;
        grid.append(k, v);
      }
      detail.appendChild(grid);
      if (metadata.warning) {
        const warning = document.createElement("div"); warning.className = "goal"; warning.textContent = metadata.warning; detail.appendChild(warning);
      }
      const goal = document.createElement("div"); goal.className = "goal"; goal.textContent = value.goal || ""; detail.appendChild(goal);
      const audit = document.createElement("div"); audit.className = "audit";
      audit.textContent = (value.audit || []).map(event => `${event.event} · ${event.actor} · ${String(event.state).toLowerCase()}`).join("\n");
      if (audit.textContent) detail.appendChild(audit);
    } catch (_) {
      detail.textContent = "Delegate detail unavailable.";
    }
    window.openai?.notifyIntrinsicHeight?.();
  }

  function render(next) {
    state = next;
    const delegates = next?.delegates || [];
    root.classList.toggle("empty-state", delegates.length === 0);
    count.textContent = `${delegates.length} ${delegates.length === 1 ? "agent" : "agents"}`;
    const activeCount = delegates.filter(item => activeStates.has(item.state)).length;
    const failedCount = delegates.filter(item => item.state === "FAILED").length;
    headerStatus.textContent = activeCount > 0 ? `${activeCount} running` : failedCount > 0 ? `${failedCount} failed` : delegates.length > 0 ? "Complete" : "Idle";
    headerStatus.classList.toggle("running", activeCount > 0);
    headerStatus.classList.toggle("failed", activeCount === 0 && failedCount > 0);
    empty.hidden = delegates.length !== 0;

    const seen = new Set();
    for (const item of delegates) {
      seen.add(item.delegate_id);
      let row = rows.get(item.delegate_id);
      if (!row) {
        row = document.createElement("li"); row.className = "row";
        const button = document.createElement("button"); button.className = "row-header"; button.type = "button";
        const status = document.createElement("span"); status.className = "status";
        const labelNode = document.createElement("span"); labelNode.className = "label";
        const submittedNode = document.createElement("span"); submittedNode.className = "submitted";
        const elapsedNode = document.createElement("span"); elapsedNode.className = "elapsed";
        const metaNode = document.createElement("span"); metaNode.className = "meta";
        const actions = document.createElement("div"); actions.className = "row-actions"; actions.hidden = true;
        const copyAction = document.createElement("button"); copyAction.className = "copy-action"; copyAction.type = "button";
        const fallbackAction = document.createElement("button"); fallbackAction.className = "fallback-action"; fallbackAction.type = "button"; fallbackAction.hidden = true;
        const fallbackProgress = document.createElement("span"); fallbackProgress.className = "fallback-progress";
        const fallbackLabel = document.createElement("span"); fallbackLabel.className = "fallback-label";
        fallbackAction.append(fallbackProgress, fallbackLabel); actions.append(copyAction, fallbackAction);
        const detail = document.createElement("div"); detail.className = "detail"; detail.hidden = true;
        button.append(status, labelNode, submittedNode, elapsedNode, metaNode); row.append(button, actions, detail);
        row._refs = { status, labelNode, submittedNode, elapsedNode, metaNode, actions, copyAction, fallbackAction, fallbackProgress, fallbackLabel, detail };
        copyAction.addEventListener("click", event => { event.stopPropagation(); copyLaunchPrompt(row._item, copyAction); });
        fallbackAction.addEventListener("click", event => { event.stopPropagation(); useCodexNow(row._item, fallbackAction); });
        button.addEventListener("click", () => {
          const isOpen = expanded.has(item.delegate_id);
          if (isOpen) { expanded.delete(item.delegate_id); detail.hidden = true; }
          else { expanded.add(item.delegate_id); detail.hidden = false; loadDetail(row._item, detail); }
          window.openai?.notifyIntrinsicHeight?.();
        });
        rows.set(item.delegate_id, row);
      }
      row._item = item;
      row.className = `row ${statusClass(item.state)}`;
      row._refs.status.textContent = statusIcon(item.state);
      row._refs.labelNode.textContent = label(item);
      row._refs.submittedNode.textContent = submittedClock(item.created_at);
      row._refs.elapsedNode.textContent = elapsed(item);
      row._refs.metaNode.textContent = meta(item);
      updateRowActions(item, row._refs);
      list.appendChild(row);
    }
    for (const [id, row] of rows) {
      if (!seen.has(id)) { row.remove(); rows.delete(id); expanded.delete(id); }
    }
    window.openai?.notifyIntrinsicHeight?.();
  }

  function hasActive(next) {
    return (next?.delegates || []).some(item => activeStates.has(item.state));
  }

  function retire() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    root.classList.add("retired");
  }

  async function poll() {
    if (!state?.run_id || !window.openai?.callTool) { timer = setTimeout(poll, 250); return; }
    try {
      const result = await window.openai.callTool("get_orchestrator_activity", { run_id: state.run_id });
      const next = result?.structuredContent ?? result?.structured_content ?? result;
      if (next?.run_id) render(next);
      if (next?.superseded && !hasActive(next)) { retire(); return; }
    } catch (_) {
      // Retain the last useful state across transient bridge/server errors.
    }
    timer = setTimeout(poll, hasActive(state) ? 1000 : 5000);
  }

  function acceptGlobals(event) {
    const next = event?.detail?.globals?.toolOutput;
    if (!next?.run_id) return;
    if (state?.run_id && next.run_id !== state.run_id) return;
    render(next);
    if (next.superseded && !hasActive(next)) retire();
  }
  window.addEventListener("openai:set_globals", acceptGlobals, { passive: true });

  if (state?.run_id) render(state);
  poll();
})();
</script>
""".replace("__ORCHESTRATOR_LOGO__", _LOGO_SVG).strip()
