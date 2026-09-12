#!/usr/bin/env node

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { access, rm, writeFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

const root = new URL("../", import.meta.url);
const read = relative => readFile(new URL(relative, root), "utf8");
const [indexHtml, dashboardCss, activityCss, activityJs, inlineHostJs, dashboardJs, serviceWorkerJs] = await Promise.all([
  read("src/serena/resources/kendell_dashboard/index.html"),
  read("src/serena/resources/kendell_dashboard/styles.css"),
  read("src/serena/resources/activity/activity-panel.css"),
  read("src/serena/resources/activity/activity-panel.js"),
  read("src/serena/resources/activity/inline-host.js"),
  read("src/serena/resources/kendell_dashboard/dashboard.js"),
  read("src/serena/resources/kendell_dashboard/service-worker.js"),
]);

async function chromeBinary() {
  const candidates = [process.env.CHROME_BIN, "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"].filter(Boolean);
  for (const candidate of candidates) {
    try {
      await access(candidate);
      return candidate;
    } catch (_) {
      // Try the next conventional path.
    }
  }
  throw new Error("Chrome/Chromium is required for the activity UI smoke check");
}

function call(index, active = false) {
  return {
    call_id: `call-${index}`,
    tool_name: "read_file",
    detail: `file-${index}.txt`,
    scope: "src/serena",
    project_name: "serena",
    started_at: 1000 + index,
    finished_at: active ? null : 1000.25 + index,
    status: active ? "running" : "completed",
  };
}

function session(index, active = false) {
  const latest = call(index, active);
  return {
    panel_id: `panel-${index}`,
    display_name: `Session ${index}`,
    started_at: 1000 + index,
    active,
    tool_count: 1,
    job_count: 0,
    submission_span_seconds: 0,
    git_additions: 0,
    git_deletions: 0,
    git_ahead_commits: 0,
    latest_activity: {
      label: latest.tool_name,
      detail: latest.detail,
      scope: latest.scope,
      status: latest.status,
      started_at: latest.started_at,
      finished_at: latest.finished_at,
    },
  };
}

function overview(count, { active = true, orchestratorPanels = [] } = {}) {
  const sessions = Array.from({ length: count }, (_, index) => session(index, active && index === 0));
  return {
    status: "success",
    session: {
      status: "success",
      runtime_policy: "ChatGPT",
      serena_version: "smoke",
      active_tools: [],
    },
    jobs: { status: "success", jobs: [], running_jobs: 0, max_concurrent_jobs: 12 },
    serena: { status: "success", panels: sessions },
    orchestrator: { status: "success", panels: orchestratorPanels },
  };
}

function selected(expanded = false) {
  const latest = call(0, true);
  return {
    panel_id: "panel-0",
    session_id: "session-0",
    run_id: null,
    project_name: "serena",
    session_title: "Session 0",
    started_at: 1000,
    updated_at: 1001,
    superseded: false,
    tool_count: 1,
    job_count: 0,
    submission_span_seconds: 0,
    latest_activity: {
      label: latest.tool_name,
      detail: latest.detail,
      scope: latest.scope,
      status: latest.status,
      started_at: latest.started_at,
      finished_at: latest.finished_at,
    },
    git_additions: 0,
    git_deletions: 0,
    git_ahead_commits: 0,
    calls: [latest],
    jobs: [],
    dashboard_jobs: { status: "success", jobs: [], running_jobs: 0, max_concurrent_jobs: 12 },
    expanded_call: expanded
      ? {
          call_id: "call-0",
          tool_name: "read_file",
          status: "running",
          arguments: { relative_path: "file-0.txt" },
          structured_result: null,
          result: null,
          error: null,
          media: null,
        }
      : null,
    expanded_job: null,
  };
}


function selectedMany(count) {
  const calls = Array.from({ length: count }, (_, index) => call(index, false));
  const latest = calls.at(-1);
  return {
    panel_id: "panel-large",
    session_id: "session-large",
    run_id: null,
    project_name: "serena",
    session_title: "Large session",
    started_at: 1000,
    updated_at: 1000 + count,
    superseded: false,
    tool_count: count,
    job_count: 0,
    submission_span_seconds: count > 1 ? count - 1 : 0,
    latest_activity: latest
      ? {
          label: latest.tool_name,
          detail: latest.detail,
          scope: latest.scope,
          status: latest.status,
          started_at: latest.started_at,
          finished_at: latest.finished_at,
        }
      : null,
    git_additions: 0,
    git_deletions: 0,
    git_ahead_commits: 0,
    calls,
    jobs: [],
    expanded_call: null,
    expanded_job: null,
  };
}

function selectedJob(expanded = false) {
  const snapshot = selected(false);
  snapshot.tool_count = 1;
  snapshot.job_count = 1;
  snapshot.calls = [call(0, false)];
  snapshot.jobs = [
    {
      job_id: "job-0",
      label: "Smoke background job",
      project: "serena",
      command: "python smoke_job.py --steps 20",
      status: "completed",
      started_at: 1000,
      finished_at: 1001,
      current_turn: true,
    },
  ];
  snapshot.expanded_job = expanded
    ? {
        job_id: "job-0",
        label: "Smoke background job",
        project: "serena",
        cwd: "/tmp/serena",
        command: "python smoke_job.py --steps 20",
        status: "completed",
        status_message: "Job completed successfully.",
        return_code: 0,
        timeout_seconds: null,
        elapsed_seconds: 1,
        seconds_since_last_output: 0,
        memory_bytes: 1024,
        cpu_seconds: 0.1,
        process_count: 1,
        output: "job output",
        output_truncated: false,
        earlier_output_omitted: false,
        has_earlier_output: false,
        cursor_reset: false,
      }
    : null;
  return snapshot;
}

function selectedRunningJob(output) {
  const snapshot = selectedJob(true);
  snapshot.active = true;
  snapshot.updated_at = 1000;
  snapshot.jobs[0] = {
    ...snapshot.jobs[0],
    status: "running",
    finished_at: null,
  };
  snapshot.expanded_job = {
    ...snapshot.expanded_job,
    status: "running",
    status_message: null,
    return_code: null,
    elapsed_seconds: 2,
    output,
  };
  return snapshot;
}

function orchestratorPanel() {
  return {
    panel_id: "orchestrator-0",
    display_name: "Smoke orchestration",
    started_at: 1000,
    updated_at: 1001,
    active: true,
    delegate_count: 1,
    active_count: 1,
  };
}

function selectedOrchestrator(expanded = false) {
  const panel = orchestratorPanel();
  return {
    ...panel,
    delegates: [
      {
        delegate_id: "delegate-0",
        kind: "explore",
        project_name: "serena",
        provider_policy: "chat",
        active_provider: null,
        state: "WAITING_FOR_CHAT",
        created_at: "2026-09-11T18:00:00Z",
        claim_deadline: null,
        claimed_at: null,
        started_at: null,
        finished_at: null,
        result_available: false,
        message: null,
      },
    ],
    expanded_delegate: expanded
      ? {
          delegate_id: "delegate-0",
          project_name: "serena",
          kind: "explore",
          goal: "Verify the direct Orchestrator dashboard path.",
          state: "WAITING_FOR_CHAT",
          provider_policy: "chat",
          active_provider: null,
          error: null,
          provider_metadata: { launch_mode: "manual" },
          audit: [],
        }
      : null,
  };
}

function fetchMockScript(state) {
  return `
    window.__smokeFetches = [];
    window.fetch = async input => {
      const url = new URL(String(input), location.href);
      const path = url.pathname + url.search;
      window.__smokeFetches.push(path);
      if (url.pathname === "/dashboard/api/state" && window.__holdNextStateFetch) {
        window.__holdNextStateFetch = false;
        await new Promise(resolve => { window.__releaseStateFetch = resolve; });
      }
      let payload;
      if (url.pathname === "/dashboard/api/state") payload = window.__overviewPayload ? structuredClone(window.__overviewPayload) : ${JSON.stringify(state)};
      else if (url.pathname === "/dashboard/api/serena/sessions/panel-0") {
        if (window.__selectedPayload) payload = structuredClone(window.__selectedPayload);
        else if (url.searchParams.get("expanded") === "job-0") payload = ${JSON.stringify(selectedJob(true))};
        else payload = url.searchParams.get("expanded") === "call-0" ? ${JSON.stringify(selected(true))} : ${JSON.stringify(selected(false))};
      } else if (url.pathname === "/dashboard/api/serena/sessions/panel-1") {
        payload = ${JSON.stringify({ ...selected(false), panel_id: "panel-1", session_id: "session-1", session_title: "Session 1" })};
      } else if (url.pathname === "/dashboard/api/orchestrator/sessions/orchestrator-0") {
        payload = url.searchParams.get("expanded") === "delegate-0" ? ${JSON.stringify(selectedOrchestrator(true))} : ${JSON.stringify(selectedOrchestrator(false))};
      } else if (url.pathname === "/dashboard/api/push/config") payload = { public_key: "BA" };
      else if (url.pathname === "/dashboard/api/push/subscribe") payload = { status: "success" };
      else throw new Error("Unexpected smoke fetch: " + path);
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json", ETag: '"smoke"' } });
    };
  `;
}

function prepareHtml(state, scenarioScript, { preludeScript = "" } = {}) {
  let html = indexHtml;
  html = html.replace('<link rel="stylesheet" href="styles.css">', `<style>${dashboardCss}</style><style>${activityCss}</style>`);
  html = html.replace('<script src="dashboard.js" defer data-cfasync="false"></script>', "");
  html = html.replace("</head>", `<script>${activityJs}</script><script>${fetchMockScript(state)}</script><script>${preludeScript}</script></head>`);
  html = html.replace("</body>", `<div id="smoke-marker" hidden>SMOKE_PENDING</div><script>${dashboardJs}</script><script>${scenarioScript}</script></body>`);
  return html;
}


function prepareInlineHtml(state, scenarioScript, { forceHidden = false, pollState = state } = {}) {
  const hiddenScript = forceHidden
    ? 'Object.defineProperty(document, "hidden", { configurable: true, get: () => true });'
    : "";
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>${activityCss}</style>
<script>${activityJs}</script>
<script>
${hiddenScript}
window.openai = {
  toolOutput: ${JSON.stringify(state)},
  callTool: async () => ({ structuredContent: ${JSON.stringify(pollState)} }),
  notifyIntrinsicHeight: () => { window.__heightNotifications = (window.__heightNotifications || 0) + 1; },
};
</script>
</head>
<body>
<div id="serena-activity-root"></div>
<div id="smoke-marker" hidden>SMOKE_PENDING</div>
<script>${inlineHostJs}</script>
<script>${scenarioScript}</script>
</body>
</html>`;
}

async function runChrome(html, virtualTimeMs = null, { initialPath = "/" } = {}) {
  const chrome = await chromeBinary();
  const file = join(tmpdir(), `serena-ui-smoke-${process.pid}-${Math.random().toString(16).slice(2)}.html`);
  await writeFile(file, html, "utf8");
  const server = createServer(async (request, response) => {
    const requestUrl = new URL(request.url || "/", "http://127.0.0.1");
    if (requestUrl.pathname === "/") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end(await readFile(file));
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  try {
    const args = [
      "--headless=new",
      "--disable-gpu",
      "--no-sandbox",
      "--disable-dev-shm-usage",
    ];
    if (Number.isFinite(virtualTimeMs)) args.push(`--virtual-time-budget=${virtualTimeMs}`);
    args.push("--dump-dom", `http://127.0.0.1:${port}${initialPath}`);
    const child = spawn(chrome, args);
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", chunk => { stdout += chunk; });
    child.stderr.on("data", chunk => { stderr += chunk; });
    const code = await new Promise(resolve => child.on("close", resolve));
    if (code !== 0) throw new Error(`Chrome exited ${code}: ${stderr.slice(-2000)}`);
    return stdout;
  } finally {
    await new Promise(resolve => server.close(resolve));
    await rm(file, { force: true });
  }
}

async function initialOverviewLoadScenario() {
  const state = overview(10);
  const scenario = `
    setTimeout(() => {
      const panels = document.querySelectorAll("#serena-widgets .serena-activity-panel").length;
      const fetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      const pass = panels === 10 && fetches.length === 1;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS initial-overview"
        : "SMOKE_FAIL initial-overview panels=" + panels + " fetches=" + JSON.stringify(window.__smokeFetches);
    }, 100);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 200);
  if (!dom.includes("SMOKE_PASS initial-overview")) throw new Error("Initial overview load smoke failed");
}

async function sessionDaySeparatorsScenario() {
  const state = overview(3, { active: false });
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  state.serena.panels[0].started_at = today.getTime() / 1000;
  state.serena.panels[1].started_at = today.getTime() / 1000 + 60;
  state.serena.panels[2].started_at = yesterday.getTime() / 1000;

  const scenario = `
    setTimeout(() => {
      const separators = [...document.querySelectorAll("#serena-widgets .session-day-separator")];
      const pass = separators.length === 1
        && Boolean(separators[0]?.textContent?.trim())
        && separators[0].nextElementSibling?.dataset.panelId === "panel-2";
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS session-day-separators"
        : "SMOKE_FAIL session-day-separators count=" + separators.length + " labels=" + separators.map(separator => separator.textContent).join("|");
    }, 100);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 200);
  if (!dom.includes("SMOKE_PASS session-day-separators")) throw new Error("Session day separator smoke failed");
}

async function durableJobCommandScenario() {
  const snapshot = selected(false);
  snapshot.calls = [{
    call_id: "start-job-call",
    tool_name: "start_job",
    detail: "Smoke background job",
    scope: "serena",
    project_name: "serena",
    started_at: 1000,
    finished_at: 1000.1,
    status: "completed",
    job_id: "job-0",
    job_label: "Smoke background job",
  }];
  snapshot.jobs = [{
    job_id: "job-0",
    label: "Smoke background job",
    project: "serena",
    command: "python smoke_job.py --steps 20",
    status: "running",
    started_at: 1000,
    finished_at: null,
    current_turn: true,
    panel_id: snapshot.panel_id,
  }];
  snapshot.tool_count = 1;
  snapshot.job_count = 1;

  const scenario = `
    const testRoot = document.createElement("div");
    document.body.append(testRoot);
    const panel = new window.SerenaActivity.ActivityPanel(testRoot, { initialCollapsed: false });
    panel.render(${JSON.stringify(snapshot)});
    const rows = [...testRoot.querySelectorAll(".activity-row")];
    const callRow = rows.find(row => row.dataset.kind === "call");
    const jobRow = rows.find(row => row.dataset.kind === "job");
    const jobButton = jobRow?.querySelector(".activity-row-button");
    const jobAccent = jobButton ? getComputedStyle(jobButton, "::before") : null;
    const jobDetail = jobRow?.querySelector(".activity-row-detail");
    const pass = rows.length === 2
      && callRow?.dataset.entryId === "start-job-call"
      && jobRow?.dataset.entryId === "job-0"
      && jobDetail?.textContent === "python smoke_job.py --steps 20"
      && jobAccent?.backgroundImage?.includes("linear-gradient")
      && testRoot.textContent.includes("start_job");
    document.getElementById("smoke-marker").textContent = pass
      ? "SMOKE_PASS durable-job-command"
      : "SMOKE_FAIL durable-job-command rows=" + rows.length + " text=" + testRoot.textContent;
  `;
  const dom = await runChrome(prepareInlineHtml(snapshot, scenario), 200);
  if (!dom.includes("SMOKE_PASS durable-job-command")) throw new Error("Durable-job command smoke failed");
}

async function overviewAnimationContinuityScenario() {
  const state = overview(2);
  const updated = structuredClone(state);
  updated.serena.panels[0].tool_count = 2;
  updated.serena.panels[0].latest_activity = {
    ...updated.serena.panels[0].latest_activity,
    detail: "updated activity",
    started_at: updated.serena.panels[0].latest_activity.started_at + 0.5,
  };

  const scenario = `
    let beforeRoot = null;
    let beforeHeader = null;
    let beforeAnimation = null;
    setTimeout(() => {
      beforeRoot = document.querySelector('#serena-widgets [data-panel-id="panel-0"]');
      beforeHeader = beforeRoot?.querySelector(".activity-header");
      beforeAnimation = beforeRoot?.querySelector(".activity-logo-mark")?.getAnimations()[0];
      beforeHeader?.focus();
      window.__overviewPayload = ${JSON.stringify(updated)};
      window.dispatchEvent(new Event("focus"));
    }, 80);
    setTimeout(() => {
      const afterRoot = document.querySelector('#serena-widgets [data-panel-id="panel-0"]');
      const afterAnimation = afterRoot?.querySelector(".activity-logo-mark")?.getAnimations()[0];
      const pass = beforeRoot === afterRoot
        && beforeAnimation
        && afterAnimation === beforeAnimation
        && document.activeElement === beforeHeader;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS overview-animation-continuity"
        : "SMOKE_FAIL overview-animation-continuity root=" + (beforeRoot === afterRoot)
          + " animation=" + (afterAnimation === beforeAnimation)
          + " focus=" + (document.activeElement === beforeHeader);
    }, 220);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 350);
  if (!dom.includes("SMOKE_PASS overview-animation-continuity")) throw new Error("Overview animation continuity smoke failed");
}

async function runningIconAnimationScenario() {
  const overviewState = overview(2);
  const overviewScenario = `
    setTimeout(() => {
      const panels = [...document.querySelectorAll("#serena-widgets .serena-activity-panel")];
      const activeMark = panels[0]?.querySelector(".activity-logo-mark");
      const idleMark = panels[1]?.querySelector(".activity-logo-mark");
      const activeAnimation = activeMark ? getComputedStyle(activeMark).animationName : "";
      const idleAnimation = idleMark ? getComputedStyle(idleMark).animationName : "";
      const pass = panels[0]?.classList.contains("activity-running")
        && !panels[1]?.classList.contains("activity-running")
        && activeAnimation === "serena-logo-quarter-turn"
        && idleAnimation === "none";
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS running-icon-overview"
        : "SMOKE_FAIL running-icon-overview active=" + activeAnimation + " idle=" + idleAnimation;
    }, 100);
  `;
  const overviewDom = await runChrome(prepareHtml(overviewState, overviewScenario), 200);
  if (!overviewDom.includes("SMOKE_PASS running-icon-overview")) throw new Error("Running overview icon animation smoke failed");

  const selectedState = selected(false);
  selectedState.calls = [call(0, false)];
  selectedState.jobs = [{
    job_id: "job-running",
    label: "Running smoke job",
    project: "serena",
    status: "running",
    started_at: 1001,
    finished_at: null,
    current_turn: false,
    panel_id: "panel-0",
  }];
  const selectedPrelude = `window.__selectedPayload = ${JSON.stringify(selectedState)};`;
  const selectedScenario = `
    setTimeout(() => {
      const panel = document.querySelector("#serena-widgets .serena-activity-panel");
      const mark = panel?.querySelector(".activity-logo-mark");
      const status = panel?.querySelector('.activity-row[data-status="running"] .activity-status');
      const elapsed = panel?.querySelector('.activity-row[data-status="running"] .activity-row-elapsed');
      const animation = mark ? getComputedStyle(mark).animationName : "";
      const statusAnimation = status ? getComputedStyle(status).animationName : "";
      const elapsedBefore = elapsed?.textContent || "";
      setTimeout(() => {
        const elapsedAfter = elapsed?.textContent || "";
        const pass = panel?.classList.contains("activity-running")
          && animation === "serena-logo-quarter-turn"
          && statusAnimation === "activity-running-pulse"
          && elapsedBefore !== elapsedAfter;
        document.getElementById("smoke-marker").textContent = pass
          ? "SMOKE_PASS running-icon-job"
          : "SMOKE_FAIL running-icon-job animation=" + animation
            + " status=" + statusAnimation + " elapsed=" + elapsedBefore + "/" + elapsedAfter;
      }, 300);
    }, 100);
  `;
  const selectedDom = await runChrome(
    prepareHtml(overview(1, { active: false }), selectedScenario, { preludeScript: selectedPrelude }),
    500,
    { initialPath: "/?panel=panel-0" },
  );
  if (!selectedDom.includes("SMOKE_PASS running-icon-job")) throw new Error("Running job icon animation smoke failed");
}


async function inlineHiddenTimerScenario() {
  const state = selected(false);
  state.run_id = "inline-run";
  state.started_at = Date.now() / 1000;
  state.updated_at = state.started_at;
  state.submission_span_seconds = 12.3;
  state.calls = [call(0, true)];
  state.calls[0].started_at = state.started_at;
  state.latest_activity = {
    ...state.latest_activity,
    started_at: state.started_at,
    finished_at: null,
    status: "running",
  };
  const scenario = `
    const initialElapsed = document.querySelector(".activity-row-elapsed")?.textContent || "";
    const initialHeaderDuration = document.querySelector(".activity-header-duration")?.textContent || "";
    setTimeout(() => {
      const elapsed = document.querySelector(".activity-row-elapsed")?.textContent || "";
      const headerDuration = document.querySelector(".activity-header-duration")?.textContent || "";
      const pass = elapsed !== ""
        && elapsed !== initialElapsed
        && !elapsed.includes(".")
        && initialHeaderDuration === "12s"
        && headerDuration === initialHeaderDuration;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS inline-hidden-live-timer"
        : "SMOKE_FAIL inline-hidden-live-timer elapsed=" + initialElapsed + "/" + elapsed
          + " header=" + initialHeaderDuration + "/" + headerDuration
          + " hidden=" + document.hidden;
    }, 1200);
  `;
  const dom = await runChrome(prepareInlineHtml(state, scenario, { forceHidden: true }), 1400);
  if (!dom.includes("SMOKE_PASS inline-hidden-live-timer")) throw new Error("Inline hidden-frame live timer smoke failed");
}

async function inlineStaleGlobalsAnimationScenario() {
  const initial = selected(false);
  initial.run_id = "inline-stale-globals";
  initial.active = false;
  initial.calls = [];
  initial.jobs = [];
  initial.tool_count = 0;
  initial.job_count = 0;
  initial.latest_activity = null;
  initial.updated_at = initial.started_at;

  const live = selected(false);
  live.run_id = initial.run_id;
  live.active = true;
  live.calls = [call(0, true), call(1, true)];
  live.tool_count = 2;
  live.latest_activity = {
    label: live.calls[1].tool_name,
    detail: live.calls[1].detail,
    scope: live.calls[1].scope,
    status: live.calls[1].status,
    started_at: live.calls[1].started_at,
    finished_at: null,
  };
  live.updated_at = live.calls[1].started_at;

  const scenario = `
    setTimeout(() => {
      const panel = document.querySelector(".serena-activity-panel");
      const header = panel?.querySelector(".activity-header");
      if (panel && !panel.classList.contains("collapsed")) header?.click();
      const mark = panel?.querySelector(".activity-logo-mark");
      const beforeAnimation = mark?.getAnimations()[0];
      const before = beforeAnimation?.currentTime ?? -1;
      window.dispatchEvent(new CustomEvent("openai:set_globals", {
        detail: { globals: { toolOutput: ${JSON.stringify(initial)} } },
      }));
      setTimeout(() => {
        const afterAnimation = mark?.getAnimations()[0];
        const after = afterAnimation?.currentTime ?? -1;
        const pass = panel?.classList.contains("collapsed")
          && panel?.classList.contains("activity-running")
          && beforeAnimation
          && afterAnimation === beforeAnimation;
        document.getElementById("smoke-marker").textContent = pass
          ? "SMOKE_PASS inline-stale-globals-animation"
          : "SMOKE_FAIL inline-stale-globals-animation before=" + before + " after=" + after
            + " running=" + panel?.classList.contains("activity-running")
            + " collapsed=" + panel?.classList.contains("collapsed");
      }, 50);
    }, 350);
  `;
  const dom = await runChrome(prepareInlineHtml(initial, scenario, { pollState: live }), 500);
  const marker = dom.match(/<div id="smoke-marker"[^>]*>.*?<\/div>/s)?.[0] || "missing marker";
  if (!marker.includes("SMOKE_PASS inline-stale-globals-animation")) {
    throw new Error(`Inline stale globals animation continuity smoke failed: ${marker}`);
  }
}

async function inlineFreshGlobalsRecoveryScenario() {
  const initial = selected(false);
  initial.run_id = "inline-fresh-globals";
  initial.calls = [call(0, false)];
  initial.tool_count = 1;
  initial.updated_at = initial.calls[0].finished_at;

  const fresh = structuredClone(initial);
  fresh.calls.push(call(1, false));
  fresh.tool_count = 2;
  fresh.latest_activity = {
    label: fresh.calls[1].tool_name,
    detail: fresh.calls[1].detail,
    scope: fresh.calls[1].scope,
    status: fresh.calls[1].status,
    started_at: fresh.calls[1].started_at,
    finished_at: fresh.calls[1].finished_at,
  };
  fresh.updated_at = fresh.calls[1].finished_at;

  const scenario = `
    setTimeout(() => {
      window.dispatchEvent(new CustomEvent("openai:set_globals", {
        detail: { globals: { toolOutput: ${JSON.stringify(fresh)} } },
      }));
      setTimeout(() => {
        const rows = document.querySelectorAll(".activity-row");
        document.getElementById("smoke-marker").textContent = rows.length === 2
          ? "SMOKE_PASS inline-fresh-globals-recovery"
          : "SMOKE_FAIL inline-fresh-globals-recovery rows=" + rows.length;
      }, 50);
    }, 50);
  `;
  const dom = await runChrome(prepareInlineHtml(initial, scenario, { pollState: initial }), 200);
  if (!dom.includes("SMOKE_PASS inline-fresh-globals-recovery")) {
    throw new Error("Inline fresh globals recovery smoke failed");
  }
}

async function expandedLiveAnimationContinuityScenario() {
  const live = selected(false);
  live.run_id = "expanded-animation";
  live.active = true;
  live.calls = [call(0, true)];
  live.jobs = [{
    job_id: "job-running",
    label: "Background smoke job",
    project: "serena",
    status: "running",
    started_at: live.started_at,
    finished_at: null,
    current_turn: false,
    panel_id: live.panel_id,
  }];
  live.updated_at = live.calls[0].started_at;

  const scenario = `
    const testRoot = document.createElement("div");
    document.body.append(testRoot);
    const testPanel = new window.SerenaActivity.ActivityPanel(testRoot, { initialCollapsed: false });
    const first = ${JSON.stringify(live)};
    testPanel.render(first);
    const firstMarkAnimation = testRoot.querySelector(".activity-logo-mark")?.getAnimations()[0];
    const firstStatus = testRoot.querySelector('.activity-row[data-status="running"] .activity-status')?.textContent;
    const firstStatusAnimation = testRoot.querySelector('.activity-row[data-status="running"] .activity-status')?.getAnimations()[0];
    const second = structuredClone(first);
    second.updated_at += 0.5;
    testPanel.render(second);
    const secondMarkAnimation = testRoot.querySelector(".activity-logo-mark")?.getAnimations()[0];
    const secondStatus = testRoot.querySelector('.activity-row[data-status="running"] .activity-status')?.textContent;
    const secondStatusAnimation = testRoot.querySelector('.activity-row[data-status="running"] .activity-status')?.getAnimations()[0];
    const finished = structuredClone(second);
    finished.updated_at += 0.5;
    finished.calls[0].status = "success";
    finished.calls[0].finished_at = finished.updated_at;
    testPanel.render(finished);
    const finishedStatus = testRoot.querySelector('.activity-row[data-status="success"] .activity-status')?.textContent;
    const pass = firstMarkAnimation
      && firstStatus === "…"
      && secondMarkAnimation === firstMarkAnimation
      && secondStatus === "…"
      && !firstStatusAnimation
      && !secondStatusAnimation
      && finishedStatus === "✓";
    document.getElementById("smoke-marker").textContent = pass
      ? "SMOKE_PASS expanded-live-animation-continuity"
      : "SMOKE_FAIL expanded-live-animation-continuity mark=" + (secondMarkAnimation === firstMarkAnimation)
        + " status=" + firstStatus + "/" + secondStatus
        + " animated=" + Boolean(firstStatusAnimation || secondStatusAnimation)
        + " finished=" + finishedStatus;
  `;
  const dom = await runChrome(prepareInlineHtml(live, scenario), 200);
  const marker = dom.match(/<div id="smoke-marker"[^>]*>.*?<\/div>/s)?.[0] || "missing marker";
  if (!marker.includes("SMOKE_PASS expanded-live-animation-continuity")) {
    throw new Error(`Expanded live animation continuity smoke failed: ${marker}`);
  }
}

async function inlineExpandedHeightNotificationScenario() {
  const live = selected(false);
  live.run_id = "expanded-height";
  live.active = true;
  live.calls = [call(0, true)];
  live.updated_at = live.calls[0].started_at;

  const scenario = `
    setTimeout(() => {
      const panel = document.querySelector(".serena-activity-panel");
      const baseline = window.__heightNotifications || 0;
      setTimeout(() => {
        const after = window.__heightNotifications || 0;
        const pass = panel && !panel.classList.contains("collapsed") && baseline > 0 && after === baseline;
        document.getElementById("smoke-marker").textContent = pass
          ? "SMOKE_PASS expanded-height-notification"
          : "SMOKE_FAIL expanded-height-notification baseline=" + baseline + " after=" + after;
      }, 1100);
    }, 100);
  `;
  const dom = await runChrome(prepareInlineHtml(live, scenario, { pollState: live }), 1300);
  const marker = dom.match(/<div id="smoke-marker"[^>]*>.*?<\/div>/s)?.[0] || "missing marker";
  if (!marker.includes("SMOKE_PASS expanded-height-notification")) {
    throw new Error(`Expanded height notification smoke failed: ${marker}`);
  }
}

async function overviewRequestScenario(count) {
  const state = overview(count);
  const scenario = `
    setTimeout(() => {
      const fetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      document.getElementById("smoke-marker").textContent = fetches.length === 2
        ? "SMOKE_PASS overview-${count}"
        : "SMOKE_FAIL overview-${count} requests=" + JSON.stringify(window.__smokeFetches);
    }, 2150);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 2300);
  if (!dom.includes(`SMOKE_PASS overview-${count}`)) throw new Error(`Overview ${count} smoke failed`);
}

async function overviewScrollPreservationScenario() {
  const state = overview(1000);
  const scenario = `
    setTimeout(() => {
      window.scrollTo(0, 1200);
      window.__smokeScrollBefore = window.scrollY;
    }, 100);
    setTimeout(() => {
      const before = Number(window.__smokeScrollBefore || 0);
      const after = window.scrollY;
      const fetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      const pass = before > 0 && Math.abs(after - before) <= 1 && fetches.length === 2;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS overview-scroll"
        : "SMOKE_FAIL overview-scroll before=" + before + " after=" + after + " requests=" + JSON.stringify(window.__smokeFetches);
    }, 2150);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 2300);
  if (!dom.includes("SMOKE_PASS overview-scroll")) throw new Error("Overview scroll-preservation smoke failed");
}

async function previewOpenScrollPreservationScenario() {
  const state = overview(1000);
  const scenario = `
    setTimeout(() => {
      window.scrollTo(0, 120);
      window.__smokeScrollBefore = window.scrollY;
      const header = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header');
      header?.focus({ preventScroll: true });
      header?.click();
    }, 100);
    setTimeout(() => {
      const before = Number(window.__smokeScrollBefore || 0);
      const after = window.scrollY;
      const header = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header');
      const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0"));
      const pass = before > 0
        && Math.abs(after - before) <= 1
        && document.activeElement === header
        && sessionFetches.length === 1
        && Boolean(document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-row'));
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS preview-open-scroll"
        : "SMOKE_FAIL preview-open-scroll before=" + before + " after=" + after + " requests=" + JSON.stringify(window.__smokeFetches);
    }, 250);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 350);
  if (!dom.includes("SMOKE_PASS preview-open-scroll")) throw new Error("Preview-open scroll-preservation smoke failed");
}

async function previewCloseScrollPreservationScenario() {
  const state = overview(1000);
  const scenario = `
    setTimeout(() => {
      window.scrollTo(0, 120);
      document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header')?.click();
    }, 100);
    setTimeout(() => {
      window.__smokeScrollBeforeClose = window.scrollY;
      document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header')?.click();
    }, 250);
    setTimeout(() => {
      const before = Number(window.__smokeScrollBeforeClose || 0);
      const after = window.scrollY;
      const header = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header');
      const body = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-body');
      const pass = before > 0
        && Math.abs(after - before) <= 1
        && !location.search.includes("panel=")
        && header?.getAttribute("aria-expanded") === "false"
        && body === null;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS preview-close-scroll"
        : "SMOKE_FAIL preview-close-scroll before=" + before + " after=" + after + " search=" + location.search;
    }, 400);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 500);
  if (!dom.includes("SMOKE_PASS preview-close-scroll")) throw new Error("Preview-close scroll-preservation smoke failed");
}

async function previewSwitchScrollPreservationScenario() {
  const state = overview(1000);
  const scenario = `
    setTimeout(() => {
      window.scrollTo(0, 120);
      document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header')?.click();
    }, 100);
    setTimeout(() => {
      window.__smokeScrollBeforeSwitch = window.scrollY;
      document.querySelector('#serena-widgets [data-panel-id="panel-1"] .activity-header')?.click();
    }, 250);
    setTimeout(() => {
      const before = Number(window.__smokeScrollBeforeSwitch || 0);
      const after = window.scrollY;
      const firstHeader = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header');
      const firstBody = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-body');
      const secondHeader = document.querySelector('#serena-widgets [data-panel-id="panel-1"] .activity-header');
      const secondBody = document.querySelector('#serena-widgets [data-panel-id="panel-1"] .activity-body');
      const secondFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-1"));
      const pass = before > 0
        && Math.abs(after - before) <= 1
        && location.search.includes("panel=panel-1")
        && firstHeader?.getAttribute("aria-expanded") === "false"
        && firstBody === null
        && secondHeader?.getAttribute("aria-expanded") === "true"
        && Boolean(secondBody)
        && secondFetches.length === 1;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS preview-switch-scroll"
        : "SMOKE_FAIL preview-switch-scroll before=" + before + " after=" + after + " search=" + location.search + " requests=" + JSON.stringify(window.__smokeFetches);
    }, 400);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 500);
  if (!dom.includes("SMOKE_PASS preview-switch-scroll")) throw new Error("Preview-switch scroll-preservation smoke failed");
}

async function overviewRenderMeasurement(count) {
  const state = overview(count, { active: false });
  const scenario = `
    const started = performance.now();
    renderOverview(${JSON.stringify(state)});
    const elapsed = performance.now() - started;
    document.getElementById("smoke-marker").textContent = "SMOKE_METRIC overview-render-${count}=" + elapsed.toFixed(3);
  `;
  const dom = await runChrome(prepareHtml(state, scenario));
  const match = dom.match(new RegExp(`SMOKE_METRIC overview-render-${count}=([0-9.]+)`));
  if (!match) throw new Error(`Overview ${count} render measurement failed`);
  const elapsedMs = Number(match[1]);
  console.log(`Overview ${count} DOM rebuild: ${elapsedMs.toFixed(3)} ms`);
  return elapsedMs;
}


async function selectedSessionRenderMeasurement(count) {
  const state = overview(0, { active: false });
  const snapshot = selectedMany(count);
  const targetId = `call-${Math.floor(count / 2)}`;
  const scenario = `
    const root = document.createElement("div");
    document.body.append(root);
    const panel = new window.SerenaActivity.ActivityPanel(root, { initialCollapsed: false });
    const snapshot = ${JSON.stringify(snapshot)};
    const initialStarted = performance.now();
    panel.render(snapshot);
    const initialMs = performance.now() - initialStarted;
    panel.list.scrollTop = Math.floor(panel.list.scrollHeight / 2);
    const scrollBefore = panel.list.scrollTop;
    const expandStarted = performance.now();
    panel.setExpandedEntryId(${JSON.stringify(targetId)});
    const expandMs = performance.now() - expandStarted;
    const scrollAfterExpand = panel.list.scrollTop;
    const detailed = {
      ...snapshot,
      expanded_call: {
        call_id: ${JSON.stringify(targetId)},
        tool_name: "read_file",
        status: "completed",
        arguments: { relative_path: "large.txt" },
        structured_result: { ok: true },
        result: "{\\"ok\\":true}",
        error: null,
        media: null,
      },
    };
    const rerenderStarted = performance.now();
    panel.render(detailed);
    const rerenderMs = performance.now() - rerenderStarted;
    const detailLoaded = Boolean(root.querySelector(".activity-detail-parameters"))
      && Boolean(root.querySelector(".activity-detail-result"))
      && !root.querySelector(".activity-structure");
    const collapseStarted = performance.now();
    panel.setExpandedEntryId(null);
    const collapseMs = performance.now() - collapseStarted;
    const rowCount = panel.list.querySelectorAll(".activity-row").length;
    const preservedScroll = Math.abs(scrollAfterExpand - scrollBefore) <= 1;
    document.getElementById("smoke-marker").textContent = rowCount === ${count} && detailLoaded && preservedScroll
      ? "SMOKE_METRIC selected-${count}=" + [initialMs, rerenderMs, expandMs, collapseMs].map(value => value.toFixed(3)).join(",")
      : "SMOKE_FAIL selected-${count} rows=" + rowCount + " detail=" + detailLoaded + " scroll=" + scrollBefore + "/" + scrollAfterExpand;
  `;
  const dom = await runChrome(prepareHtml(state, scenario));
  const match = dom.match(new RegExp(`SMOKE_METRIC selected-${count}=([0-9.]+),([0-9.]+),([0-9.]+),([0-9.]+)`));
  if (!match) throw new Error(`Selected session ${count} render measurement failed`);
  const [initialMs, rerenderMs, expandMs, collapseMs] = match.slice(1).map(Number);
  console.log(
    `Selected ${count} calls: initial ${initialMs.toFixed(3)} ms; unchanged rerender ${rerenderMs.toFixed(3)} ms; expand ${expandMs.toFixed(3)} ms; collapse ${collapseMs.toFixed(3)} ms`
  );
  return { initialMs, rerenderMs, expandMs, collapseMs };
}

async function directSelectedSessionLoadScenario() {
  const state = overview(3);
  const scenario = `
    setTimeout(() => {
      const rows = document.querySelectorAll("#serena-widgets .activity-row").length;
      const title = document.getElementById("serena-activity-title")?.textContent || "";
      const summary = document.querySelector("#serena-widgets [data-panel-id=\"panel-0\"] .activity-summary")?.textContent || "";
      const panels = document.querySelectorAll("#serena-widgets [data-panel-id]").length;
      const runtime = document.getElementById("runtime-policy")?.textContent || "";
      const version = document.getElementById("version")?.textContent || "";
      const tools = document.getElementById("tool-count")?.textContent || "";
      const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0"));
      const overviewFetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      const pass = rows === 1
        && title === "Serena"
        && summary === "1 tool · 0 jobs"
        && panels === 3
        && runtime === "ChatGPT"
        && version === "smoke"
        && tools === "0"
        && sessionFetches.length === 1
        && overviewFetches.length === 1;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS direct-selected-load"
        : "SMOKE_FAIL direct-selected-load rows=" + rows + " title=" + title + " summary=" + summary + " panels=" + panels + " metadata=" + [runtime, version, tools].join("|") + " fetches=" + JSON.stringify(window.__smokeFetches);
    }, 100);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 200, { initialPath: "/?panel=panel-0" });
  if (!dom.includes("SMOKE_PASS direct-selected-load")) throw new Error("Direct selected-session load smoke failed");
}


async function selectedSessionLiveRefreshScenario() {
  const state = overview(1, { active: false });
  const initial = selected(false);
  const completed = call(0, false);
  initial.calls = [completed];
  initial.latest_activity = {
    label: completed.tool_name,
    detail: completed.detail,
    scope: completed.scope,
    status: completed.status,
    started_at: completed.started_at,
    finished_at: completed.finished_at,
  };

  const updated = structuredClone(initial);
  const liveCall = call(1, true);
  updated.tool_count = 2;
  updated.updated_at = 1002;
  updated.calls = [completed, liveCall];
  updated.latest_activity = {
    label: liveCall.tool_name,
    detail: liveCall.detail,
    scope: liveCall.scope,
    status: liveCall.status,
    started_at: liveCall.started_at,
    finished_at: liveCall.finished_at,
  };
  updated.dashboard_jobs = {
    status: "success",
    jobs: [
      {
        job_id: "job-live",
        label: "Live background job",
        project: "serena",
        status: "running",
        started_at: 1002,
        finished_at: null,
        current_turn: false,
        panel_id: "panel-0",
      },
    ],
    running_jobs: 1,
    max_concurrent_jobs: 12,
  };

  const prelude = `
    window.__selectedPayload = ${JSON.stringify(initial)};
    window.__eventSourceUrl = null;
    window.__dashboardEventSource = null;
    class SmokeEventSource {
      constructor(url) {
        this.listeners = new Map();
        window.__eventSourceUrl = url;
        window.__dashboardEventSource = this;
        setTimeout(() => this.emit("open"), 20);
      }
      addEventListener(name, callback) {
        if (!this.listeners.has(name)) this.listeners.set(name, []);
        this.listeners.get(name).push(callback);
      }
      emit(name) {
        for (const callback of this.listeners.get(name) || []) callback({ type: name });
      }
    }
    window.EventSource = SmokeEventSource;
  `;
  const scenario = `
    setTimeout(() => {
      window.__selectedPayload = ${JSON.stringify(updated)};
      window.__dashboardEventSource?.emit("invalidate");
    }, 120);
    setTimeout(() => {
      document.getElementById("jobs-button")?.click();
      const rows = document.querySelectorAll('#serena-widgets [data-panel-id="panel-0"] .activity-row').length;
      const runningJobs = document.getElementById("running-jobs-count")?.textContent || "";
      const jobLabel = document.querySelector("#jobs-dialog-content .resource-row strong")?.textContent || "";
      const overviewFetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0"));
      const pass = rows === 2
        && runningJobs === "1"
        && jobLabel === "Live background job"
        && window.__eventSourceUrl === "/dashboard/api/events"
        && overviewFetches.length === 1
        && sessionFetches.length >= 2
        && sessionFetches.length <= 3;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS selected-live-refresh"
        : "SMOKE_FAIL selected-live-refresh rows=" + rows + " jobs=" + runningJobs + "/" + jobLabel + " stream=" + window.__eventSourceUrl + " fetches=" + JSON.stringify(window.__smokeFetches);
    }, 500);
  `;
  const dom = await runChrome(prepareHtml(state, scenario, { preludeScript: prelude }), 650, { initialPath: "/?panel=panel-0" });
  if (!dom.includes("SMOKE_PASS selected-live-refresh")) throw new Error("Selected-session live refresh smoke failed");
}

async function expandedRunningJobLiveOutputScenario() {
  const state = overview(1, { active: true });
  const initial = selectedRunningJob("line 1");
  const updated = selectedRunningJob("line 1\nline 2");
  const prelude = `
    window.__selectedPayload = ${JSON.stringify(initial)};
    window.__eventSourceUrl = null;
    class SmokeEventSource {
      constructor(url) {
        this.listeners = new Map();
        window.__eventSourceUrl = url;
        setTimeout(() => this.emit("open"), 20);
      }
      addEventListener(name, callback) {
        if (!this.listeners.has(name)) this.listeners.set(name, []);
        this.listeners.get(name).push(callback);
      }
      emit(name) {
        for (const callback of this.listeners.get(name) || []) callback({ type: name });
      }
    }
    window.EventSource = SmokeEventSource;
  `;
  const scenario = `
    setTimeout(() => { window.__selectedPayload = ${JSON.stringify(updated)}; }, 200);
    setTimeout(() => {
      const panel = document.querySelector('#serena-widgets [data-panel-id="panel-0"]');
      const output = panel?.querySelector(".activity-job-output")?.textContent || "";
      const command = panel?.querySelector(".activity-job-command")?.textContent || "";
      const metadata = panel?.querySelector(".activity-job-metadata")?.textContent || "";
      const fetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0?expanded=job-0"));
      const pass = output === "line 1\\nline 2"
        && command === "python smoke_job.py --steps 20"
        && metadata.includes("Elapsed")
        && metadata.includes("2s")
        && metadata.includes("Memory")
        && metadata.includes("1.00 KiB")
        && metadata.includes("CPU time")
        && metadata.includes("0.1s")
        && fetches.length >= 2
        && window.__eventSourceUrl === "/dashboard/api/events";
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS expanded-running-job-live-output"
        : "SMOKE_FAIL expanded-running-job-live-output output=" + JSON.stringify(output) + " fetches=" + JSON.stringify(window.__smokeFetches);
    }, 2250);
  `;
  const dom = await runChrome(
    prepareHtml(state, scenario, { preludeScript: prelude }),
    2400,
    { initialPath: "/?panel=panel-0&job=job-0" },
  );
  if (!dom.includes("SMOKE_PASS expanded-running-job-live-output")) {
    throw new Error("Expanded running-job live-output smoke failed");
  }
}

async function jobOutputScrollStabilityScenario() {
  const initialOutput = Array.from({ length: 500 }, (_, index) => `initial line ${index}`).join("\n");
  const updatedOutput = `${initialOutput}\n${Array.from({ length: 20 }, (_, index) => `new line ${index}`).join("\n")}`;
  const initial = selectedRunningJob(initialOutput);
  const updated = selectedRunningJob(updatedOutput);
  const scenario = `
    const root = document.getElementById("serena-activity-root");
    const panel = new window.SerenaActivity.ActivityPanel(root, {
      initialCollapsed: false,
      expandedEntryId: "job-0",
    });
    panel.render(${JSON.stringify(initial)});
    setTimeout(() => {
      const output = document.querySelector(".activity-job-output");
      const rect = output.getBoundingClientRect();
      window.scrollTo(0, Math.max(0, window.scrollY + rect.bottom - window.innerHeight - 40));
      window.__smokeScrollBefore = window.scrollY;
      panel.render(${JSON.stringify(updated)});
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const before = Number(window.__smokeScrollBefore || 0);
        const after = window.scrollY;
        const text = document.querySelector(".activity-job-output")?.textContent || "";
        const pass = before > 0 && Math.abs(after - before) <= 1 && text.endsWith("new line 19");
        document.getElementById("smoke-marker").textContent = pass
          ? "SMOKE_PASS job-output-scroll-stability"
          : "SMOKE_FAIL job-output-scroll-stability before=" + before + " after=" + after + " tail=" + JSON.stringify(text.slice(-40));
      }));
    }, 50);
  `;
  const html = `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>${activityCss}</style>
<script>${activityJs}</script>
</head>
<body>
<div id="serena-activity-root"></div>
<div id="smoke-marker" hidden>SMOKE_PENDING</div>
<script>${scenario}</script>
</body>
</html>`;
  const dom = await runChrome(html, 250);
  if (!dom.includes("SMOKE_PASS job-output-scroll-stability")) {
    throw new Error("Job-output scroll-stability smoke failed");
  }
}


async function structuredValueReadabilityScenario() {
  const snapshot = selected(true);
  snapshot.calls[0].status = "completed";
  snapshot.calls[0].finished_at = 1001;
  snapshot.expanded_call.status = "completed";
  snapshot.expanded_call.structured_result = {
    files: ["README.md"],
    result: [{ name_path: "durableJobDeduplicationScenario", relative_path: "scripts/smoke_activity_ui.mjs" }],
    lines: "first line\nsecond line",
  };

  const scenario = `
    const testRoot = document.createElement("div");
    document.body.append(testRoot);
    const panel = new window.SerenaActivity.ActivityPanel(testRoot, {
      initialCollapsed: false,
      expandedEntryId: "call-0",
    });
    panel.render(${JSON.stringify(snapshot)});
    const result = testRoot.querySelector(".activity-detail-result");
    const visibleLeaf = [...result.querySelectorAll("dt, dd")].find(node => node.textContent.includes("durableJobDeduplicationScenario"));
    const pre = testRoot.querySelector(".activity-pre-wrap > .activity-pre");
    const style = pre ? getComputedStyle(pre) : null;
    const pass = result?.innerText.includes("README.md")
      && result.innerText.includes("durableJobDeduplicationScenario")
      && visibleLeaf?.getClientRects().length > 0
      && style?.boxSizing === "border-box"
      && parseFloat(style.paddingRight) >= 50;
    document.getElementById("smoke-marker").textContent = pass
      ? "SMOKE_PASS structured-value-disclosure"
      : "SMOKE_FAIL structured-value-disclosure text=" + result?.innerText
        + " visible=" + Boolean(visibleLeaf?.getClientRects().length)
        + " padding=" + style?.paddingRight + " box=" + style?.boxSizing;
  `;
  const dom = await runChrome(prepareInlineHtml(snapshot, scenario), 200);
  if (!dom.includes("SMOKE_PASS structured-value-disclosure")) throw new Error("Structured-value disclosure smoke failed");
}

async function selectedRowIdentityScenario() {
  const snapshot = selected(true);
  snapshot.calls = [
    { ...call(0, false), status: "completed" },
    { ...call(1, true), status: "running" },
  ];
  snapshot.tool_count = 2;
  snapshot.expanded_call.status = "completed";
  snapshot.expanded_call.structured_result = { ok: true, detail: "stable detail" };

  const scenario = `
    const testRoot = document.createElement("div");
    document.body.append(testRoot);
    const panel = new window.SerenaActivity.ActivityPanel(testRoot, {
      initialCollapsed: false,
      expandedEntryId: "call-0",
    });
    const first = ${JSON.stringify(snapshot)};
    panel.render(first);
    const stableRow = testRoot.querySelector('[data-entry-id="call-0"]');
    const stableDetail = stableRow?.querySelector(".activity-detail");
    const changingButton = testRoot.querySelector('[data-entry-id="call-1"] .activity-row-button');
    changingButton?.focus();

    const second = structuredClone(first);
    second.calls[1].status = "completed";
    second.calls[1].finished_at = second.calls[1].started_at + 1;
    panel.render(second);

    const nextStableRow = testRoot.querySelector('[data-entry-id="call-0"]');
    const nextStableDetail = nextStableRow?.querySelector(".activity-detail");
    const nextChangingButton = testRoot.querySelector('[data-entry-id="call-1"] .activity-row-button');
    const pass = stableRow === nextStableRow
      && stableDetail === nextStableDetail
      && document.activeElement === nextChangingButton;
    document.getElementById("smoke-marker").textContent = pass
      ? "SMOKE_PASS selected-row-identity"
      : "SMOKE_FAIL selected-row-identity row=" + (stableRow === nextStableRow)
        + " detail=" + (stableDetail === nextStableDetail)
        + " focus=" + (document.activeElement === nextChangingButton);
  `;
  const dom = await runChrome(prepareInlineHtml(snapshot, scenario), 200);
  if (!dom.includes("SMOKE_PASS selected-row-identity")) throw new Error("Selected row identity smoke failed");
}

async function mediaLifetimeAndRetryScenario() {
  const snapshot = selected(true);
  snapshot.calls[0].status = "completed";
  snapshot.calls[0].finished_at = 1001;
  snapshot.expanded_call.status = "completed";
  snapshot.expanded_call.media = {
    media_type: "file",
    name: "result.txt",
    mime_type: "text/plain",
  };

  const scenario = `
    const testRoot = document.createElement("div");
    document.body.append(testRoot);
    let attempts = 0;
    let disposals = 0;
    const panel = new window.SerenaActivity.ActivityPanel(testRoot, {
      initialCollapsed: false,
      expandedEntryId: "call-0",
      loadMedia: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("transient media failure");
        return { type: "file", src: "#media-result", name: "result.txt", dispose: () => { disposals += 1; } };
      },
    });
    const state = ${JSON.stringify(snapshot)};
    panel.render(state);
    setTimeout(() => {
      panel.render(structuredClone(state));
      setTimeout(() => {
        const link = testRoot.querySelector(".activity-file");
        panel.render(structuredClone(state));
        setTimeout(() => {
          const cached = attempts === 2 && testRoot.querySelector(".activity-file")?.getAttribute("href") === "#media-result";
          panel.setExpandedEntryId(null);
          panel.destroy();
          const pass = link && cached && disposals === 1;
          document.getElementById("smoke-marker").textContent = pass
            ? "SMOKE_PASS media-lifetime-retry"
            : "SMOKE_FAIL media-lifetime-retry attempts=" + attempts + " disposals=" + disposals + " cached=" + cached;
        }, 20);
      }, 20);
    }, 20);
  `;
  const dom = await runChrome(prepareInlineHtml(snapshot, scenario), 150);
  if (!dom.includes("SMOKE_PASS media-lifetime-retry")) throw new Error("Media lifetime/retry smoke failed");
}

async function activitySummaryFlashScenario() {
  const state = overview(0, { active: false });
  const initial = selected(false);
  initial.run_id = null;
  const initialSummary = {
    ...initial,
    calls: [],
    jobs: [],
  };

  const toolSummary = structuredClone(initialSummary);
  toolSummary.tool_count = 2;
  toolSummary.updated_at = 1002;
  toolSummary.latest_activity = {
    label: "search_for_pattern",
    detail: "ActivityPanel",
    scope: "src/serena",
    status: "running",
    started_at: 1002,
    finished_at: null,
  };

  const hydratedTool = structuredClone(toolSummary);
  hydratedTool.calls = [
    ...initial.calls,
    {
      call_id: "call-flash",
      tool_name: "search_for_pattern",
      detail: "ActivityPanel",
      scope: "src/serena",
      project_name: "serena",
      status: "running",
      started_at: 1002,
      finished_at: null,
    },
  ];

  const withJob = structuredClone(hydratedTool);
  withJob.job_count = 1;
  withJob.updated_at = 1003;
  withJob.jobs = [
    {
      job_id: "job-flash",
      label: "Smoke background job",
      project: "serena",
      status: "running",
      started_at: 1003,
      finished_at: null,
      current_turn: false,
    },
  ];
  withJob.latest_activity = {
    label: "Smoke background job",
    detail: "serena",
    scope: "serena",
    status: "running",
    started_at: 1003,
    finished_at: null,
  };

  const scenario = `
    const root = document.createElement("div");
    document.body.append(root);
    const initialSummary = ${JSON.stringify(initialSummary)};
    const toolSummary = ${JSON.stringify(toolSummary)};
    const hydratedTool = ${JSON.stringify(hydratedTool)};
    const withJob = ${JSON.stringify(withJob)};

    const baseline = new window.SerenaActivity.ActivityPanel(root, { initialCollapsed: true, summaryMode: true });
    baseline.render(initialSummary);
    baseline.destroy();

    const panel = new window.SerenaActivity.ActivityPanel(root, {
      initialCollapsed: true,
      summaryMode: true,
      previousSnapshot: initialSummary,
    });
    panel.render(toolSummary);
    const toolFlashActive = panel.summaryStack.classList.contains("is-activity-flash");
    const toolFlashText = panel.summaryFlashNode.textContent || "";
    const toolSteadyText = panel.summaryNode.textContent || "";

    setTimeout(() => {
      const toolFadedBack = !panel.summaryStack.classList.contains("is-activity-flash");
      panel.promote();
      panel.render(hydratedTool);
      const hydrationDidNotFlash = !panel.summaryStack.classList.contains("is-activity-flash");

      panel.render(withJob);
      const jobFlashActive = panel.summaryStack.classList.contains("is-activity-flash");
      const jobFlashText = panel.summaryFlashNode.textContent || "";
      const jobSteadyText = panel.summaryNode.textContent || "";

      setTimeout(() => {
        const jobFadedBack = !panel.summaryStack.classList.contains("is-activity-flash")
          && getComputedStyle(panel.summaryNode).opacity === "1"
          && getComputedStyle(panel.summaryFlashNode).opacity === "0";
        const pass = toolFlashActive
          && toolFlashText === "search_for_pattern · ActivityPanel"
          && toolSteadyText === "2 tools · 0 jobs"
          && toolFadedBack
          && hydrationDidNotFlash
          && jobFlashActive
          && jobFlashText === "Smoke background job · serena"
          && jobSteadyText === "2 tools · 1 job"
          && jobFadedBack;
        document.getElementById("smoke-marker").textContent = pass
          ? "SMOKE_PASS activity-summary-flash"
          : "SMOKE_FAIL activity-summary-flash tool=" + [toolFlashActive, toolFlashText, toolSteadyText, toolFadedBack].join("|")
            + " hydration=" + hydrationDidNotFlash
            + " job=" + [jobFlashActive, jobFlashText, jobSteadyText, jobFadedBack].join("|");
      }, 1250);
    }, 1250);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 2700);
  if (!dom.includes("SMOKE_PASS activity-summary-flash")) throw new Error("Activity summary flash smoke failed");
}

async function selectedSessionScenario() {
  const state = overview(3);
  const scenario = `
    setTimeout(() => {
      document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header')?.click();
      setTimeout(() => {
        const row = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-row-button');
        const elapsed = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-row-elapsed');
        const elapsedBefore = elapsed?.textContent || "";
        row?.click();
        setTimeout(() => {
          const detailLoaded = Boolean(document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-detail-section'));
          const elapsedAfter = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-row-elapsed')?.textContent || "";
          const panelCount = document.querySelectorAll("#serena-widgets [data-panel-id]").length;
          const siblingsVisible = Boolean(document.querySelector('#serena-widgets [data-panel-id="panel-1"]'))
            && Boolean(document.querySelector('#serena-widgets [data-panel-id="panel-2"]'));
          const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0"));
          const pass = detailLoaded
            && panelCount === 3
            && siblingsVisible
            && sessionFetches.length === 2
            && sessionFetches[1].includes("expanded=call-0")
            && elapsedBefore !== elapsedAfter;
          document.getElementById("smoke-marker").textContent = pass
            ? "SMOKE_PASS selected-session"
            : "SMOKE_FAIL selected-session detail=" + detailLoaded + " panels=" + panelCount + " siblings=" + siblingsVisible + " elapsed=" + elapsedBefore + "/" + elapsedAfter + " fetches=" + JSON.stringify(window.__smokeFetches);
        }, 1100);
      }, 100);
    }, 50);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 1500);
  if (!dom.includes("SMOKE_PASS selected-session")) throw new Error("Selected-session smoke failed");
}

async function returnToOverviewScenario() {
  const state = overview(3);
  const scenario = `
    setTimeout(() => {
      document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header')?.click();
      setTimeout(() => {
        document.querySelector('#serena-widgets [data-panel-id="panel-0"] .activity-header')?.click();
        setTimeout(() => {
          const stateFetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
          const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0"));
          const panels = document.querySelectorAll("#serena-widgets [data-panel-id]").length;
          const rows = document.querySelectorAll("#serena-widgets .activity-row").length;
          const pass = stateFetches.length === 2
            && sessionFetches.length === 1
            && location.search === ""
            && panels === 3
            && rows === 0;
          document.getElementById("smoke-marker").textContent = pass
            ? "SMOKE_PASS return-overview"
            : "SMOKE_FAIL return-overview search=" + location.search + " panels=" + panels + " rows=" + rows + " fetches=" + JSON.stringify(window.__smokeFetches);
        }, 100);
      }, 100);
    }, 50);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 450);
  if (!dom.includes("SMOKE_PASS return-overview")) throw new Error("Return-to-overview smoke failed");
}

async function staleRouteRecoveryScenario() {
  const state = overview(2);
  const staleJobPrelude = `
    const baseFetch = window.fetch;
    window.fetch = async input => {
      const url = new URL(String(input), location.href);
      if (url.pathname === "/dashboard/api/serena/sessions/panel-0" && url.searchParams.get("expanded") === "missing-job") {
        return new Response("", { status: 400, statusText: "Bad Request" });
      }
      return baseFetch(input);
    };
  `;
  const staleJobScenario = `
    setTimeout(() => {
      const selectedPanel = document.querySelector('#serena-widgets [data-panel-id="panel-0"] .serena-activity-panel');
      const connection = document.getElementById("connection-state")?.dataset.state;
      const pass = location.search === "?panel=panel-0" && selectedPanel && connection === "connected";
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS stale-job-route-recovery"
        : "SMOKE_FAIL stale-job-route-recovery search=" + location.search + " connection=" + connection;
    }, 350);
  `;
  const staleJobDom = await runChrome(
    prepareHtml(state, staleJobScenario, { preludeScript: staleJobPrelude }),
    500,
    { initialPath: "/?panel=panel-0&job=missing-job" },
  );
  if (!staleJobDom.includes("SMOKE_PASS stale-job-route-recovery")) throw new Error("Stale job route recovery smoke failed");

  const stalePanelPrelude = `
    const baseFetch = window.fetch;
    window.fetch = async input => {
      const url = new URL(String(input), location.href);
      if (url.pathname === "/dashboard/api/serena/sessions/missing-panel") {
        return new Response("", { status: 404, statusText: "Not Found" });
      }
      return baseFetch(input);
    };
  `;
  const stalePanelScenario = `
    setTimeout(() => {
      const panels = document.querySelectorAll("#serena-widgets .serena-activity-panel").length;
      const connection = document.getElementById("connection-state")?.dataset.state;
      const pass = location.search === "" && panels === 2 && connection === "connected";
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS stale-panel-route-recovery"
        : "SMOKE_FAIL stale-panel-route-recovery search=" + location.search + " panels=" + panels + " connection=" + connection;
    }, 350);
  `;
  const stalePanelDom = await runChrome(
    prepareHtml(state, stalePanelScenario, { preludeScript: stalePanelPrelude }),
    500,
    { initialPath: "/?panel=missing-panel" },
  );
  if (!stalePanelDom.includes("SMOKE_PASS stale-panel-route-recovery")) throw new Error("Stale panel route recovery smoke failed");
}

async function notificationDeepLinkScenario() {
  const state = overview(1, { active: false });
  const scenario = `
    setTimeout(() => {
      const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0"));
      const row = document.querySelector('#serena-widgets [data-entry-id="job-0"]');
      const detailLoaded = Boolean(document.querySelector("#serena-widgets .activity-detail-section"));
      const columns = document.getElementById("activity-columns");
      const serenaVisible = getComputedStyle(document.querySelector(".activity-section")).display !== "none";
      const orchestratorHidden = getComputedStyle(document.querySelector(".orchestrator-section")).display === "none";
      const pass = sessionFetches.length >= 2
        && sessionFetches.every(path => path.endsWith("?expanded=job-0"))
        && location.search === "?panel=panel-0"
        && Boolean(row)
        && detailLoaded
        && columns?.dataset.activeView === "serena"
        && serenaVisible
        && orchestratorHidden;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS notification-deep-link"
        : "SMOKE_FAIL notification-deep-link search=" + location.search + " detail=" + detailLoaded + " fetches=" + JSON.stringify(window.__smokeFetches);
    }, 10150);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 10300, { initialPath: "/?panel=panel-0&job=job-0" });
  if (!dom.includes("SMOKE_PASS notification-deep-link")) throw new Error("Notification deep-link smoke failed");
}

async function secondaryInteractionScenario() {
  const state = overview(1, { orchestratorPanels: [orchestratorPanel()] });
  state.jobs = {
    status: "success",
    jobs: [{
      job_id: "orphan-job",
      label: "Unowned smoke job",
      project: "serena",
      status: "running",
      panel_id: null,
    }],
    running_jobs: 1,
    max_concurrent_jobs: 12,
  };

  const scenario = `
    setTimeout(() => {
      document.getElementById("jobs-button")?.click();
      const orphan = document.querySelector("#jobs-dialog-content .resource-row");
      const serenaTab = document.querySelector('[data-activity-view-tab="serena"]');
      serenaTab?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
      const orchestratorTab = document.querySelector('[data-activity-view-tab="orchestrator"]');
      const activeView = document.getElementById("activity-columns")?.dataset.activeView;
      const pass = orphan?.tagName === "DIV"
        && !orphan.classList.contains("resource-row-button")
        && orchestratorTab?.getAttribute("aria-selected") === "true"
        && activeView === "orchestrator";
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS secondary-interactions"
        : "SMOKE_FAIL secondary-interactions orphan=" + orphan?.tagName
          + " selected=" + orchestratorTab?.getAttribute("aria-selected") + " view=" + activeView;
    }, 120);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 250);
  if (!dom.includes("SMOKE_PASS secondary-interactions")) throw new Error("Secondary interaction smoke failed");
}

async function notificationOptInScenario() {
  const state = overview(1);
  const prelude = `
    window.__pushSubscription = null;
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        register: async () => ({
          pushManager: {
            getSubscription: async () => window.__pushSubscription,
            subscribe: async options => {
              window.__pushSubscribeOptions = options;
              window.__pushSubscription = {
                toJSON: () => ({
                  endpoint: "https://push.example.invalid/subscription",
                  keys: { p256dh: "public-key", auth: "auth-secret" },
                }),
              };
              return window.__pushSubscription;
            },
          },
        }),
      },
    });
    window.PushManager = function PushManager() {};
    Object.defineProperty(window, "Notification", {
      configurable: true,
      value: {
        permission: "default",
        requestPermission: async () => {
          window.Notification.permission = "granted";
          return "granted";
        },
      },
    });
  `;
  const scenario = `
    setTimeout(() => {
      const button = document.getElementById("notification-button");
      button?.click();
      setTimeout(() => {
        const pushFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/push/"));
        const pass = button?.getAttribute("aria-pressed") === "true"
          && button?.dataset.state === "enabled"
          && pushFetches.length === 2
          && pushFetches[0] === "/dashboard/api/push/config"
          && pushFetches[1] === "/dashboard/api/push/subscribe"
          && window.__pushSubscribeOptions?.userVisibleOnly === true;
        document.getElementById("smoke-marker").textContent = pass
          ? "SMOKE_PASS notification-opt-in"
          : "SMOKE_FAIL notification-opt-in state=" + button?.dataset.state + " fetches=" + JSON.stringify(window.__smokeFetches);
      }, 100);
    }, 50);
  `;
  const dom = await runChrome(prepareHtml(state, scenario, { preludeScript: prelude }), 300);
  if (!dom.includes("SMOKE_PASS notification-opt-in")) throw new Error("Notification opt-in smoke failed");
}

async function orchestratorScenario() {
  const state = overview(0, { orchestratorPanels: [orchestratorPanel()] });
  const prelude = `
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async text => { window.__copiedPrompt = text; } },
    });
  `;
  const scenario = `
    setTimeout(() => {
      document.querySelector('[data-activity-view-tab="orchestrator"]')?.click();
      const selectedView = document.getElementById("activity-columns")?.dataset.activeView === "orchestrator";
      document.querySelector("#orchestrator-widgets .orchestrator-session-card")?.click();
      setTimeout(() => {
        document.querySelector("#orchestrator-widgets .orchestrator-delegate-main")?.click();
        setTimeout(() => {
          document.querySelector("#orchestrator-widgets .orchestrator-copy-prompt")?.click();
          setTimeout(() => {
            const detailLoaded = Boolean(document.querySelector("#orchestrator-widgets .orchestrator-delegate-detail section"));
            const fetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/orchestrator/sessions/orchestrator-0"));
            const pass = selectedView
              && detailLoaded
              && fetches.length === 2
              && fetches[0] === "/dashboard/api/orchestrator/sessions/orchestrator-0"
              && fetches[1].endsWith("?expanded=delegate-0")
              && window.__copiedPrompt === "@Orchestrator claim delegate delegate-0 and complete it independently.";
            document.getElementById("smoke-marker").textContent = pass
              ? "SMOKE_PASS orchestrator"
              : "SMOKE_FAIL orchestrator detail=" + detailLoaded + " copied=" + window.__copiedPrompt + " fetches=" + JSON.stringify(window.__smokeFetches);
          }, 50);
        }, 100);
      }, 100);
    }, 50);
  `;
  const dom = await runChrome(prepareHtml(state, scenario, { preludeScript: prelude }), 500);
  if (!dom.includes("SMOKE_PASS orchestrator")) throw new Error("Orchestrator smoke failed");
}

async function directOrchestratorLoadScenario() {
  const state = overview(0, { orchestratorPanels: [orchestratorPanel()] });
  const scenario = `
    setTimeout(() => {
      const delegates = document.querySelectorAll("#orchestrator-widgets .orchestrator-delegate").length;
      const runtime = document.getElementById("runtime-policy")?.textContent || "";
      const version = document.getElementById("version")?.textContent || "";
      const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/orchestrator/sessions/orchestrator-0"));
      const overviewFetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      const pass = delegates === 1
        && runtime === "ChatGPT"
        && version === "smoke"
        && sessionFetches.length === 1
        && overviewFetches.length === 1;
      document.getElementById("smoke-marker").textContent = pass
        ? "SMOKE_PASS direct-orchestrator-load"
        : "SMOKE_FAIL direct-orchestrator-load delegates=" + delegates + " metadata=" + [runtime, version].join("|") + " fetches=" + JSON.stringify(window.__smokeFetches);
    }, 100);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 200, { initialPath: "/?orchestrator=orchestrator-0" });
  if (!dom.includes("SMOKE_PASS direct-orchestrator-load")) throw new Error("Direct Orchestrator load smoke failed");
}

async function refreshCoalescingScenario() {
  const state = overview(1);
  const scenario = `
    setTimeout(() => {
      window.__holdNextStateFetch = true;
      window.dispatchEvent(new Event("focus"));
      setTimeout(() => {
        window.dispatchEvent(new Event("focus"));
        window.dispatchEvent(new Event("focus"));
        window.dispatchEvent(new Event("focus"));
        setTimeout(() => window.__releaseStateFetch?.(), 25);
      }, 25);
      setTimeout(() => {
        const fetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
        document.getElementById("smoke-marker").textContent = fetches.length === 3
          ? "SMOKE_PASS refresh-coalescing"
          : "SMOKE_FAIL refresh-coalescing requests=" + JSON.stringify(window.__smokeFetches);
      }, 250);
    }, 50);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 400);
  if (!dom.includes("SMOKE_PASS refresh-coalescing")) throw new Error("Refresh coalescing smoke failed");
}

async function idleOverviewRequestScenario() {
  const state = overview(10, { active: false });
  const scenario = `
    setTimeout(() => {
      const fetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      document.getElementById("smoke-marker").textContent = fetches.length === 2
        ? "SMOKE_PASS idle-overview"
        : "SMOKE_FAIL idle-overview requests=" + JSON.stringify(window.__smokeFetches);
    }, 10150);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 10300);
  if (!dom.includes("SMOKE_PASS idle-overview")) throw new Error("Idle overview smoke failed");
}

async function hiddenOverviewRequestScenario() {
  const state = overview(10);
  const prelude = `
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
  `;
  const scenario = `
    setTimeout(() => {
      const fetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      document.getElementById("smoke-marker").textContent = fetches.length === 2
        ? "SMOKE_PASS hidden-overview"
        : "SMOKE_FAIL hidden-overview requests=" + JSON.stringify(window.__smokeFetches);
    }, 60150);
  `;
  const dom = await runChrome(prepareHtml(state, scenario, { preludeScript: prelude }), 60300);
  if (!dom.includes("SMOKE_PASS hidden-overview")) throw new Error("Hidden overview smoke failed");
}

async function serviceWorkerScenario() {
  const listeners = {};
  const shown = [];
  let navigated = null;
  let focused = false;
  const client = {
    url: "https://mcp.kendell.uk/dashboard/",
    navigate: async url => {
      navigated = url;
      return client;
    },
    focus: async () => {
      focused = true;
      return client;
    },
  };
  const clientsApi = {
    matchAll: async () => [client],
    openWindow: async url => {
      navigated = url;
      return client;
    },
  };
  const serviceWorkerSelf = {
    location: { origin: "https://mcp.kendell.uk" },
    registration: {
      showNotification: async (title, options) => {
        shown.push({ title, options });
      },
    },
    addEventListener: (name, callback) => {
      listeners[name] = callback;
    },
  };
  runInNewContext(serviceWorkerJs, { self: serviceWorkerSelf, clients: clientsApi, URL });

  const pushWaits = [];
  listeners.push({
    data: {
      json: () => ({
        title: "Smoke background job",
        body: "Completed · serena · 1s",
        tag: "serena-job-job-0",
        url: "/dashboard/job/job-0",
      }),
    },
    waitUntil: promise => pushWaits.push(promise),
  });
  await Promise.all(pushWaits);

  const clickWaits = [];
  let closed = false;
  listeners.notificationclick({
    notification: {
      data: { url: "/dashboard/job/job-0" },
      close: () => { closed = true; },
    },
    waitUntil: promise => clickWaits.push(promise),
  });
  await Promise.all(clickWaits);

  const notification = shown[0];
  if (
    shown.length !== 1
    || notification.title !== "Smoke background job"
    || notification.options.body !== "Completed · serena · 1s"
    || notification.options.data.url !== "/dashboard/job/job-0"
    || navigated !== "https://mcp.kendell.uk/dashboard/job/job-0"
    || !closed
    || !focused
  ) {
    throw new Error("Service-worker notification smoke failed");
  }
}

await initialOverviewLoadScenario();
await sessionDaySeparatorsScenario();
await durableJobCommandScenario();
await overviewAnimationContinuityScenario();
await runningIconAnimationScenario();
await inlineHiddenTimerScenario();
await inlineStaleGlobalsAnimationScenario();
await inlineFreshGlobalsRecoveryScenario();
await expandedLiveAnimationContinuityScenario();
await inlineExpandedHeightNotificationScenario();
await overviewRequestScenario(10);
await overviewRequestScenario(1000);
await overviewScrollPreservationScenario();
await previewOpenScrollPreservationScenario();
await previewCloseScrollPreservationScenario();
await previewSwitchScrollPreservationScenario();
await overviewRenderMeasurement(1000);
await selectedSessionRenderMeasurement(2048);
await directSelectedSessionLoadScenario();
await selectedSessionLiveRefreshScenario();
await expandedRunningJobLiveOutputScenario();
await jobOutputScrollStabilityScenario();
await structuredValueReadabilityScenario();
await selectedRowIdentityScenario();
await mediaLifetimeAndRetryScenario();
await activitySummaryFlashScenario();
await selectedSessionScenario();
await returnToOverviewScenario();
await staleRouteRecoveryScenario();
await notificationDeepLinkScenario();
await secondaryInteractionScenario();
await notificationOptInScenario();
await orchestratorScenario();
await directOrchestratorLoadScenario();
await refreshCoalescingScenario();
await idleOverviewRequestScenario();
await hiddenOverviewRequestScenario();
await serviceWorkerScenario();
console.log("Activity UI browser smoke checks passed");
