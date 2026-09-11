#!/usr/bin/env node

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { access, rm, writeFile } from "node:fs/promises";

const root = new URL("../", import.meta.url);
const read = relative => readFile(new URL(relative, root), "utf8");
const [indexHtml, dashboardCss, activityCss, activityJs, dashboardJs] = await Promise.all([
  read("src/serena/resources/kendell_dashboard/index.html"),
  read("src/serena/resources/kendell_dashboard/styles.css"),
  read("src/serena/resources/activity/activity-panel.css"),
  read("src/serena/resources/activity/activity-panel.js"),
  read("src/serena/resources/kendell_dashboard/dashboard.js"),
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

function overview(count) {
  const sessions = Array.from({ length: count }, (_, index) => session(index, index === 0));
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
    orchestrator: { status: "success", panels: [] },
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

function fetchMockScript(state) {
  return `
    window.__smokeFetches = [];
    window.fetch = async input => {
      const url = new URL(String(input), location.href);
      const path = url.pathname + url.search;
      window.__smokeFetches.push(path);
      let payload;
      if (url.pathname === "/dashboard/api/state") payload = ${JSON.stringify(state)};
      else if (url.pathname === "/dashboard/api/serena/sessions/panel-0") payload = ${JSON.stringify(selected(false))};
      else throw new Error("Unexpected smoke fetch: " + path);
      if (url.searchParams.get("expanded") === "call-0") payload = ${JSON.stringify(selected(true))};
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json", ETag: '"smoke"' } });
    };
  `;
}

function prepareHtml(state, scenarioScript) {
  let html = indexHtml;
  html = html.replace('<link rel="stylesheet" href="styles.css">', `<style>${dashboardCss}</style><style>${activityCss}</style>`);
  html = html.replace('<script src="dashboard.js" defer data-cfasync="false"></script>', "");
  html = html.replace("</head>", `<script>${activityJs}</script><script id="dashboard-bootstrap" type="application/json">${JSON.stringify(state).replaceAll("<", "\\u003c")}</script><script>${fetchMockScript(state)}</script></head>`);
  html = html.replace("</body>", `<div id="smoke-marker" hidden>SMOKE_PENDING</div><script>${dashboardJs}</script><script>${scenarioScript}</script></body>`);
  return html;
}

async function runChrome(html, virtualTimeMs) {
  const chrome = await chromeBinary();
  const file = join(tmpdir(), `serena-ui-smoke-${process.pid}-${Math.random().toString(16).slice(2)}.html`);
  await writeFile(file, html, "utf8");
  const server = createServer(async (request, response) => {
    if (request.url === "/") {
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
    const child = spawn(chrome, [
      "--headless=new",
      "--disable-gpu",
      "--no-sandbox",
      "--disable-dev-shm-usage",
      `--virtual-time-budget=${virtualTimeMs}`,
      "--dump-dom",
      `http://127.0.0.1:${port}/`,
    ]);
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

await overviewRequestScenario(10);
await overviewRequestScenario(1000);
await selectedSessionScenario();
console.log("Activity UI browser smoke checks passed");
