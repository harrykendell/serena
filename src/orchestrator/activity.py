"""ChatGPT activity panel for Orchestrator delegates."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP

from orchestrator.delegates import DelegateError, DelegateState, DelegateStatusResponse, DelegateStore

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
        delegates.sort(key=lambda item: (item.state not in _ACTIVE_STATES, -item.created_at.timestamp()))

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
        return _activity_widget_html()


def _activity_widget_html() -> str:
    """Returns the self-contained Orchestrator delegate widget."""
    return r"""
<div id="orchestrator-activity" class="activity">
  <button id="activity-header" class="header" type="button" aria-expanded="true">
    <span class="title"><strong>Orchestrator</strong><span id="activity-count" class="count">0 agents</span></span>
    <span id="activity-chevron" class="chevron" aria-hidden="true">⌄</span>
  </button>
  <div id="activity-body" class="body" aria-live="polite">
    <div id="activity-empty" class="empty">No delegates visible in this session.</div>
    <ol id="activity-delegates" class="delegates"></ol>
  </div>
</div>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.35 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  button { font: inherit; color: inherit; }
  .activity { width: 100%; min-width: 0; }
  .header { width: 100%; border: 0; background: transparent; display: flex; align-items: center; justify-content: space-between; padding: 8px 10px; cursor: pointer; }
  .title { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
  .count { opacity: .65; font-size: 12px; }
  .chevron { transition: transform .15s ease; }
  .activity.collapsed .chevron { transform: rotate(-90deg); }
  .activity.collapsed .body { display: none; }
  .body { max-height: 260px; overflow: auto; padding: 0 8px 8px; }
  .empty { padding: 8px 2px; opacity: .65; }
  .delegates { list-style: none; margin: 0; padding: 0; }
  .row { border-top: 1px solid color-mix(in srgb, currentColor 14%, transparent); }
  .row-header { width: 100%; border: 0; background: transparent; display: grid; grid-template-columns: 10px minmax(0, 1fr) auto; grid-template-areas: "dot label elapsed" ". meta meta"; gap: 1px 7px; padding: 7px 2px; text-align: left; cursor: pointer; }
  .dot { grid-area: dot; width: 8px; height: 8px; border-radius: 50%; margin-top: 4px; background: currentColor; opacity: .35; }
  .row.active .dot { opacity: .9; }
  .label { grid-area: label; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
  .elapsed { grid-area: elapsed; opacity: .6; font-size: 11px; }
  .meta { grid-area: meta; opacity: .68; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .detail { margin: 0 2px 8px 17px; padding: 7px 8px; border-radius: 6px; background: color-mix(in srgb, currentColor 6%, transparent); font-size: 11px; }
  .detail[hidden] { display: none; }
  .detail-grid { display: grid; grid-template-columns: auto minmax(0,1fr); gap: 3px 8px; }
  .detail-key { opacity: .6; }
  .goal { margin-top: 6px; white-space: pre-wrap; }
  .audit { margin-top: 6px; max-height: 90px; overflow: auto; white-space: pre-wrap; opacity: .78; }
  .copy { margin-top: 7px; border: 1px solid color-mix(in srgb, currentColor 20%, transparent); background: transparent; border-radius: 5px; padding: 3px 6px; cursor: pointer; }
  .retired { opacity: .7; }
</style>
<script>
(() => {
  const root = document.getElementById("orchestrator-activity");
  if (!root || root.dataset.initialized === "true") return;
  root.dataset.initialized = "true";

  const header = document.getElementById("activity-header");
  const body = document.getElementById("activity-body");
  const count = document.getElementById("activity-count");
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

  function label(item) {
    const provider = item.active_provider || item.provider_policy || "chat";
    return `${provider === "chat" ? "ChatGPT" : "Codex"} · ${item.kind}`;
  }

  function meta(item) {
    const stateLabel = String(item.state || "").toLowerCase().replaceAll("_", " ");
    return `${item.project_name || "project"} · ${stateLabel}`;
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
      if (item.state === "WAITING_FOR_CHAT") {
        const copy = document.createElement("button"); copy.className = "copy"; copy.type = "button"; copy.textContent = "Copy launch prompt";
        copy.addEventListener("click", async event => {
          event.stopPropagation();
          const prompt = `@Orchestrator claim delegate ${item.delegate_id} and complete it independently.`;
          try { await navigator.clipboard.writeText(prompt); copy.textContent = "Copied"; } catch (_) { copy.textContent = prompt; }
        });
        detail.appendChild(copy);
      }
    } catch (_) {
      detail.textContent = "Delegate detail unavailable.";
    }
    window.openai?.notifyIntrinsicHeight?.();
  }

  function render(next) {
    state = next;
    const delegates = next?.delegates || [];
    count.textContent = `${delegates.length} ${delegates.length === 1 ? "agent" : "agents"}`;
    empty.hidden = delegates.length !== 0;

    const seen = new Set();
    for (const item of delegates) {
      seen.add(item.delegate_id);
      let row = rows.get(item.delegate_id);
      if (!row) {
        row = document.createElement("li"); row.className = "row";
        const button = document.createElement("button"); button.className = "row-header"; button.type = "button";
        const dot = document.createElement("span"); dot.className = "dot";
        const labelNode = document.createElement("span"); labelNode.className = "label";
        const elapsedNode = document.createElement("span"); elapsedNode.className = "elapsed";
        const metaNode = document.createElement("span"); metaNode.className = "meta";
        const detail = document.createElement("div"); detail.className = "detail"; detail.hidden = true;
        button.append(dot, labelNode, elapsedNode, metaNode); row.append(button, detail);
        row._refs = { labelNode, elapsedNode, metaNode, detail };
        button.addEventListener("click", () => {
          const isOpen = expanded.has(item.delegate_id);
          if (isOpen) { expanded.delete(item.delegate_id); detail.hidden = true; }
          else { expanded.add(item.delegate_id); detail.hidden = false; loadDetail(row._item, detail); }
          window.openai?.notifyIntrinsicHeight?.();
        });
        rows.set(item.delegate_id, row);
      }
      row._item = item;
      row.classList.toggle("active", activeStates.has(item.state));
      row._refs.labelNode.textContent = label(item);
      row._refs.elapsedNode.textContent = elapsed(item);
      row._refs.metaNode.textContent = meta(item);
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
""".strip()
