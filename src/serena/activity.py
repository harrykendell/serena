from mcp.server.fastmcp import FastMCP

from serena.execution_store import ActivityPanelRun, ExecutionStore

ACTIVITY_RESOURCE_URI = "ui://serena/activity-v31.html"
_ACTIVITY_RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"


class ActivityRunManager:
    """Owns only activity-run creation, supersession and execution association."""

    def __init__(self, execution_store: ExecutionStore) -> None:
        self._execution_store = execution_store

    @property
    def execution_store(self) -> ExecutionStore:
        """Returns the canonical execution store used for run grouping."""
        return self._execution_store

    def start_run(self, session_id: str, project_name: str) -> ActivityPanelRun:
        """Starts one activity run without creating or completing executions."""
        return self._execution_store.start_activity_run(session_id, project_name)

    def update_project(self, session_id: str, project_name: str) -> None:
        """Updates project attribution for the current run."""
        self._execution_store.update_activity_run_project(session_id, project_name)

    def associate_execution(self, session_id: str, execution_id: str, *, project_name: str = "") -> None:
        """Associates an already-created execution with the current run, if any."""
        self._execution_store.append_execution_to_current_run(session_id, execution_id, project_name=project_name)


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
        <strong id="activity-header-title">Serena</strong>
        <span id="activity-header-stats" class="header-stats"><span id="activity-header-stats-base" class="header-stats-base">0 tools · 0 jobs</span><span id="activity-header-git" class="header-git"></span></span>
      </span>
    </span>
    <span class="header-meta">
      <span class="header-times">
        <span id="activity-header-submitted" class="header-submitted"></span>
        <span id="activity-header-elapsed" class="summary"></span>
      </span>
      <span id="activity-header-duration" class="header-duration"></span>
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
  .header-detail, .header-stats { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10.5px; line-height: 1.2; }
  .header-detail, .header-stats-base { opacity: .58; }
  .header-detail { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .header-git { margin-left: 5px; font-variant-numeric: tabular-nums; }
  .git-additions { color: #1a7f37; }
  .git-deletions { margin-left: 4px; color: #cf222e; }
  .git-ahead { margin-left: 4px; color: CanvasText; }
  .header-meta { justify-self: end; min-width: 0; }
  .header-times { display: grid; gap: 1px; justify-items: end; }
  .header-submitted, .summary, .header-duration { white-space: nowrap; font-size: 11px; font-variant-numeric: tabular-nums; }
  .header-submitted { opacity: .52; }
  .summary { opacity: .66; }
  .header-duration { opacity: .58; }
  .activity.collapsed .header-overview, .activity.collapsed .header-duration { display: none; }
  .activity:not(.collapsed) .header-tool, .activity:not(.collapsed) .header-times { display: none; }
  .activity.collapsed.empty-state .header-tool, .activity.collapsed.empty-state .header-times,
  .activity.collapsed.summary-collapsed .header-tool, .activity.collapsed.summary-collapsed .header-times { display: none; }
  .activity.collapsed.empty-state .header-overview, .activity.collapsed.summary-collapsed .header-overview { display: grid; }
  .activity.collapsed.empty-state .header-duration, .activity.collapsed.summary-collapsed .header-duration { display: inline; }
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
  .result-retained { display: flex; flex-wrap: wrap; gap: 3px 5px; align-items: baseline; margin: 0 0 5px; padding: 4px 6px; border: 1px solid color-mix(in srgb, #b45309 24%, transparent); border-radius: 5px; background: color-mix(in srgb, #b45309 7%, transparent); font-size: 10px; line-height: 1.35; }
  .result-retained code { min-width: 0; font: 10px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
  .result-retained .rich-copy { margin-left: auto; }
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
  .rich-code-lines { min-width: 0; padding: 6px 0; font: 10.5px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; tab-size: 2; }
  .rich-code-line { display: grid; grid-template-columns: 34px minmax(0, 1fr); align-items: start; min-width: 0; }
  .rich-code-line-number { min-width: 34px; padding: 0 7px 0 4px; border-right: 1px solid color-mix(in srgb, CanvasText 7%, transparent); background: color-mix(in srgb, Canvas 96%, CanvasText); color: color-mix(in srgb, CanvasText 34%, transparent); text-align: right; user-select: none; }
  .rich-code-line-content { min-width: 0; padding: 0 8px; color: color-mix(in srgb, CanvasText 90%, transparent); font: inherit; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
  .rich-code-diff .rich-code-line-content { color: color-mix(in srgb, CanvasText 84%, transparent); }
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
  @media (prefers-color-scheme: dark) { .logo { color: #70c990; } .call.running .status, .job-entry.running .status { color: #70c990; } .git-additions { color: #3fb950; } .git-deletions { color: #f85149; } }
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
  const headerTitle = document.getElementById("activity-header-title");
  const headerStats = document.getElementById("activity-header-stats");
  const headerStatsBase = document.getElementById("activity-header-stats-base");
  const headerGit = document.getElementById("activity-header-git");
  const headerSubmitted = document.getElementById("activity-header-submitted");
  const headerElapsed = document.getElementById("activity-header-elapsed");
  const headerDuration = document.getElementById("activity-header-duration");
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
    if (expanding && state?.summary_only && await hydrateFullActivity()) {
      setCollapsed(false);
      if (state?.run_id) render(state);
      return;
    }

    setCollapsed(!root.classList.contains("collapsed"), preferSummaryCollapsedHeader);
    if (state?.run_id) render(state);
  });
  otherJobsButton.addEventListener("click", event => {
    event.stopPropagation();
    otherJobsExpanded = !otherJobsExpanded;
    if (state?.run_id) render(state);
  });

  function formatDuration(seconds) {
    const boundedSeconds = Math.max(0, seconds);
    if (boundedSeconds < 10) return `${boundedSeconds.toFixed(1)}s`;

    const roundedSeconds = Math.round(boundedSeconds);
    if (roundedSeconds < 120) return `${roundedSeconds}s`;
    if (roundedSeconds < 3600) {
      const minutes = Math.floor(roundedSeconds / 60);
      return `${minutes}m ${String(roundedSeconds % 60).padStart(2, "0")}s`;
    }

    const roundedMinutes = Math.round(boundedSeconds / 60);
    const hours = Math.floor(roundedMinutes / 60);
    return `${hours}h ${String(roundedMinutes % 60).padStart(2, "0")}m`;
  }

  function elapsed(entry, nowSeconds) {
    const liveLagSeconds = entry.kind === "job" ? 1.5 : 0.5;
    const end = entry.finished_at ?? Math.max(entry.started_at, nowSeconds - liveLagSeconds);
    return formatDuration(end - entry.started_at);
  }

  function submissionSpan(next) {
    if (Number.isFinite(next?.submission_span_seconds)) return formatDuration(next.submission_span_seconds);

    const submitted = (next?.calls || [])
      .map(call => Number(call.submitted_at ?? call.started_at))
      .filter(Number.isFinite);
    if (submitted.length === 0) return "";
    return formatDuration(Math.max(...submitted) - Math.min(...submitted));
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
    if (["output", "stderr", "stdout"].includes(key)) return true;
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

  function appendCodeBlock(container, text, key = "") {
    const language = codeLanguage(text, key);
    const block = document.createElement("div");
    block.className = `rich-code rich-code-${language}`;
    const toolbar = document.createElement("div");
    toolbar.className = "rich-code-toolbar";
    const label = document.createElement("span");
    label.textContent = language;
    toolbar.append(label, copyButton(text));

    const lines = document.createElement("div");
    lines.className = "rich-code-lines";
    for (const [index, line] of text.split("\n").entries()) {
      const row = document.createElement("div");
      row.className = "rich-code-line";
      const number = document.createElement("span");
      number.className = "rich-code-line-number";
      number.textContent = String(index + 1);
      const content = document.createElement("code");
      content.className = "rich-code-line-content";
      content.textContent = line || " ";
      row.append(number, content);
      lines.append(row);
    }
    block.append(toolbar, lines);
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
    appendScalarValue(container, rawValue ?? "");
  }

  function updateRetainedResultNotice(container, structuredValue) {
    container.replaceChildren();
    const metadata = structuredValue && typeof structuredValue === "object" && !Array.isArray(structuredValue)
      && structuredValue.truncated === true && typeof structuredValue.output_id === "string" && structuredValue.output_id
      ? structuredValue
      : null;
    container.hidden = !metadata;
    if (!metadata) return;

    const label = document.createElement("span");
    const size = typeof metadata.total_chars === "number" ? ` · ${metadata.total_chars.toLocaleString()} chars` : "";
    label.textContent = `Centrally truncated${size} · full result retained as`;
    const outputId = document.createElement("code");
    outputId.textContent = metadata.output_id;
    const copy = copyButton(metadata.output_id);
    copy.textContent = "Copy ID";
    container.append(label, outputId, copy);
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
      if (hasMedia) {
        refs.resultNotice.hidden = true;
        refs.resultNotice.replaceChildren();
        refs.result.replaceChildren();
      } else {
        updateRetainedResultNotice(refs.resultNotice, detail.structured_result);
        renderRichValue(
          refs.result,
          detail.result ?? (detail.status === "running" ? "Tool is still running." : "No result returned."),
          detail.structured_result,
        );
      }
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
    let resultNotice = null;
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
      resultNotice = document.createElement("div");
      resultNotice.className = "result-retained";
      resultNotice.hidden = true;
      resultValue = document.createElement("div");
      resultValue.className = "detail-value rich-value";
      resultBlock.append(resultLabel, resultNotice, resultValue);

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
      resultNotice,
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
    const sessionTitle = next.session_title || "Serena";
    headerTitle.textContent = sessionTitle;
    headerTitle.title = sessionTitle;
    const projectName = next.project_name || "no project";
    const statsBase = `${countLabel(toolCount, "tool")} · ${countLabel(jobCount, "job")} · ${projectName}`;
    headerStatsBase.textContent = statsBase;
    const gitAdditions = Number.isFinite(next.git_additions) ? next.git_additions : 0;
    const gitDeletions = Number.isFinite(next.git_deletions) ? next.git_deletions : 0;
    const gitAheadCommits = Number.isFinite(next.git_ahead_commits) ? next.git_ahead_commits : 0;
    const hasGitMetrics = gitAdditions > 0 || gitDeletions > 0 || gitAheadCommits > 0;
    headerGit.replaceChildren();
    if (hasGitMetrics) {
      const separator = document.createTextNode(" · ");
      headerGit.append(separator);
      if (gitAdditions > 0 || gitDeletions > 0) {
        const additions = document.createElement("span");
        additions.className = "git-additions";
        additions.textContent = `+${gitAdditions}`;
        const deletions = document.createElement("span");
        deletions.className = "git-deletions";
        deletions.textContent = `-${gitDeletions}`;
        headerGit.append(additions, deletions);
      }
      if (gitAheadCommits > 0) {
        const ahead = document.createElement("span");
        ahead.className = "git-ahead";
        ahead.textContent = `(+${gitAheadCommits})`;
        headerGit.append(ahead);
      }
    }
    const gitTitle = [
      gitAdditions > 0 || gitDeletions > 0 ? `+${gitAdditions} -${gitDeletions}` : "",
      gitAheadCommits > 0 ? `(+${gitAheadCommits} commits ahead)` : "",
    ].filter(Boolean).join(" ");
    headerStats.title = `${statsBase}${gitTitle ? ` · ${gitTitle}` : ""}`;

    headerDuration.textContent = submissionSpan(next);
    headerDuration.title = headerDuration.textContent ? "Time between first and latest submitted tool" : "";

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

  async function focusJob(jobId) {
    if (!jobId) return;
    initialViewResolved = true;
    if (state?.summary_only) await hydrateFullActivity();
    if (root.classList.contains("collapsed")) setCollapsed(false);
    if (state?.run_id) render(state);

    const key = `job:${jobId}`;
    if (!(state?.jobs || []).some(job => job.job_id === jobId)) return;
    expandedRows.add(key);
    if (state?.run_id) render(state);

    const row = rowsByKey.get(key);
    if (!row) return;
    void loadJobDetail(row);
    requestAnimationFrame(() => row.scrollIntoView({ block: "center", behavior: "smooth" }));
  }

  function acceptGlobals(event) {
    const next = event?.detail?.globals?.toolOutput;
    if (!next?.run_id) return;
    if (state?.run_id && next.run_id !== state.run_id) return;
    render(next);
    if (next.superseded && !hasRunningPanelActivity(next)) retire();
  }
  window.addEventListener("openai:set_globals", acceptGlobals, { passive: true });
  window.addEventListener("serena:focus-job", event => {
    void focusJob(String(event?.detail?.job_id || ""));
  });

  if (state?.run_id) {
    render(state);
    if (!root.classList.contains("collapsed")) void hydrateFullActivity();
  }
  poll();
})();
</script>
""".strip()
