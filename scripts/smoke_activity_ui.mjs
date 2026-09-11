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
const [indexHtml, dashboardCss, activityCss, activityJs, dashboardJs, serviceWorkerJs] = await Promise.all([
  read("src/serena/resources/kendell_dashboard/index.html"),
  read("src/serena/resources/kendell_dashboard/styles.css"),
  read("src/serena/resources/activity/activity-panel.css"),
  read("src/serena/resources/activity/activity-panel.js"),
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
      languages: ["python"],
      runtime_policy: "ChatGPT",
      serena_version: "smoke",
      active_tools: [],
      total_tools: 0,
      available_memories: [],
    },
    jobs: { status: "success", jobs: [], running_jobs: 0, max_concurrent_jobs: 12 },
    serena: { status: "success", panels: sessions },
    orchestrator: { status: "success", panels: orchestratorPanels },
  };
}

function selected(expanded = false) {
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
    git_additions: 0,
    git_deletions: 0,
    git_ahead_commits: 0,
    calls: [call(0, true)],
    jobs: [],
    expanded_call: expanded
      ? {
          call_id: "call-0",
          tool_name: "read_file",
          status: "running",
          structured_arguments: { relative_path: "file-0.txt" },
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

function orchestratorPanel() {
  return {
    panel_id: "orchestrator-0",
    display_name: "Smoke orchestration",
    started_at: 1000,
    updated_at: 1001,
    active: true,
    delegates: [
      {
        delegate_id: "delegate-0",
        kind: "chat",
        project_name: "serena",
        provider_policy: "chatgpt",
        active_provider: null,
        state: "WAITING_FOR_CHAT",
        created_at: "2026-09-11T18:00:00Z",
        started_at: null,
        finished_at: null,
      },
    ],
  };
}

function selectedOrchestrator(expanded = false) {
  const panel = orchestratorPanel();
  return {
    ...panel,
    expanded_delegate: expanded
      ? {
          delegate_id: "delegate-0",
          goal: "Verify the direct Orchestrator dashboard path.",
          state: "WAITING_FOR_CHAT",
          provider_policy: "chatgpt",
          active_provider: null,
          error: null,
          provider_metadata: { launch_mode: "manual" },
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
      if (url.pathname === "/dashboard/api/state") payload = ${JSON.stringify(state)};
      else if (url.pathname === "/dashboard/api/serena/sessions/panel-0") {
        if (url.searchParams.get("expanded") === "job-0") payload = ${JSON.stringify(selectedJob(true))};
        else payload = url.searchParams.get("expanded") === "call-0" ? ${JSON.stringify(selected(true))} : ${JSON.stringify(selected(false))};
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
  html = html.replace("</head>", `<script>${activityJs}</script><script id="dashboard-bootstrap" type="application/json">${JSON.stringify(state).replaceAll("<", "\\u003c")}</script><script>${fetchMockScript(state)}</script><script>${preludeScript}</script></head>`);
  html = html.replace("</body>", `<div id="smoke-marker" hidden>SMOKE_PENDING</div><script>${dashboardJs}</script><script>${scenarioScript}</script></body>`);
  return html;
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

async function overviewRequestScenario(count) {
  const state = overview(count);
  const scenario = `
    setTimeout(() => {
      const fetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
      document.getElementById("smoke-marker").textContent = fetches.length === 1
        ? "SMOKE_PASS overview-${count}"
        : "SMOKE_FAIL overview-${count} requests=" + JSON.stringify(window.__smokeFetches);
    }, 2150);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 2300);
  if (!dom.includes(`SMOKE_PASS overview-${count}`)) throw new Error(`Overview ${count} smoke failed`);
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

async function selectedSessionScenario() {
  const state = overview(1);
  const scenario = `
    setTimeout(() => {
      document.querySelector("#serena-widgets .activity-header")?.click();
      setTimeout(() => {
        const row = document.querySelector("#serena-widgets .activity-row-button");
        const elapsed = document.querySelector("#serena-widgets .activity-row-elapsed");
        const elapsedBefore = elapsed?.textContent || "";
        row?.click();
        setTimeout(() => {
          const detailLoaded = Boolean(document.querySelector("#serena-widgets .activity-detail-section"));
          const elapsedAfter = document.querySelector("#serena-widgets .activity-row-elapsed")?.textContent || "";
          const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0"));
          const pass = detailLoaded
            && sessionFetches.length === 2
            && sessionFetches[1].includes("expanded=call-0")
            && elapsedBefore !== elapsedAfter;
          document.getElementById("smoke-marker").textContent = pass
            ? "SMOKE_PASS selected-session"
            : "SMOKE_FAIL selected-session detail=" + detailLoaded + " elapsed=" + elapsedBefore + "/" + elapsedAfter + " fetches=" + JSON.stringify(window.__smokeFetches);
        }, 1100);
      }, 100);
    }, 50);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 1500);
  if (!dom.includes("SMOKE_PASS selected-session")) throw new Error("Selected-session smoke failed");
}

async function returnToOverviewScenario() {
  const state = overview(1);
  const scenario = `
    setTimeout(() => {
      document.querySelector("#serena-widgets .activity-header")?.click();
      setTimeout(() => {
        document.querySelector("#serena-widgets .activity-back-button")?.click();
        setTimeout(() => {
          const stateFetches = window.__smokeFetches.filter(path => path === "/dashboard/api/state");
          const sessionFetches = window.__smokeFetches.filter(path => path.startsWith("/dashboard/api/serena/sessions/panel-0"));
          const pass = stateFetches.length === 1
            && sessionFetches.length === 1
            && location.search === ""
            && document.querySelectorAll("#serena-widgets .dashboard-activity-card").length === 1;
          document.getElementById("smoke-marker").textContent = pass
            ? "SMOKE_PASS return-overview"
            : "SMOKE_FAIL return-overview search=" + location.search + " fetches=" + JSON.stringify(window.__smokeFetches);
        }, 100);
      }, 100);
    }, 50);
  `;
  const dom = await runChrome(prepareHtml(state, scenario), 450);
  if (!dom.includes("SMOKE_PASS return-overview")) throw new Error("Return-to-overview smoke failed");
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
      const pass = sessionFetches.length === 2
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
        document.getElementById("smoke-marker").textContent = fetches.length === 2
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
      document.getElementById("smoke-marker").textContent = fetches.length === 1
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
      document.getElementById("smoke-marker").textContent = fetches.length === 1
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

await overviewRequestScenario(10);
await overviewRequestScenario(1000);
await overviewRenderMeasurement(1000);
await selectedSessionScenario();
await returnToOverviewScenario();
await notificationDeepLinkScenario();
await notificationOptInScenario();
await orchestratorScenario();
await refreshCoalescingScenario();
await idleOverviewRequestScenario();
await hiddenOverviewRequestScenario();
await serviceWorkerScenario();
console.log("Activity UI browser smoke checks passed");
