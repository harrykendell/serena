const API_PREFIX = "/dashboard/api";
const ACTIVE_POLL_INTERVAL_MS = 500;
const IDLE_POLL_INTERVAL_MS = 5000;
const HIDDEN_POLL_INTERVAL_MS = 60000;

const DASHBOARD_LOAD_ID = Date.now().toString(36);
const DASHBOARD_ASSET_VERSION = document.documentElement.dataset.assetVersion || "";
let dashboardBootstrapState = null;
try {
  const bootstrapNode = document.getElementById("dashboard-bootstrap");
  dashboardBootstrapState = bootstrapNode?.textContent ? JSON.parse(bootstrapNode.textContent) : null;
} catch (_) {
  dashboardBootstrapState = null;
}

const sessionWidgetLoaders = new Map();
let refreshInFlight = false;
let refreshTimer = null;
let initialActivityStateLoaded = false;
let initialActivityViewSelected = false;
let latestPanelActivity = false;
let latestJobs = { jobs: [], running_jobs: 0, max_concurrent_jobs: 0 };
let notificationTarget = (() => {
  const params = new URLSearchParams(window.location.search);
  const panelId = params.get("panel");
  const jobId = params.get("job");
  return panelId && jobId ? { panelId, jobId } : null;
})();

function byId(id) {
  return document.getElementById(id);
}

function setText(id, value, fallback = "—") {
  const elem = byId(id);
  if (elem) setNodeText(elem, value || value === 0 ? String(value) : fallback);
}

function setNodeText(node, value) {
  const text = String(value ?? "");
  if (node.textContent !== text) node.textContent = text;
}

function makeElement(tag, className, text) {
  const elem = document.createElement(tag);
  if (className) elem.className = className;
  if (text !== undefined) elem.textContent = text;
  return elem;
}

function dashboardAssetUrl(name) {
  return DASHBOARD_ASSET_VERSION ? `${name}?v=${encodeURIComponent(DASHBOARD_ASSET_VERSION)}` : name;
}

function clearAndAppend(parent, children) {
  parent.replaceChildren(...children);
}

function reconcileKeyed(container, items, keyFor, createNode, updateNode) {
  const existing = new Map();
  Array.from(container.children).forEach((child) => {
    if (child.dataset.itemKey) existing.set(child.dataset.itemKey, child);
  });

  const wanted = new Set();
  let cursor = container.firstElementChild;
  items.forEach((item) => {
    const key = String(keyFor(item));
    wanted.add(key);
    let node = existing.get(key);
    if (!node) {
      node = createNode(item);
      node.dataset.itemKey = key;
    }
    updateNode(node, item);

    if (node !== cursor) container.insertBefore(node, cursor);
    cursor = node.nextElementSibling;
  });

  Array.from(container.children).forEach((child) => {
    if (!child.dataset.itemKey || !wanted.has(child.dataset.itemKey)) child.remove();
  });
}

function activateActivityView(name) {
  const selected = name === "orchestrator" ? "orchestrator" : "serena";
  byId("activity-columns").dataset.activeView = selected;
  document.querySelectorAll("[data-activity-view-tab]").forEach((button) => {
    const active = button.dataset.activityViewTab === selected;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
}

function isTabbedActivityMode() {
  return byId("activity-columns")?.getBoundingClientRect().width <= 1011;
}

function setupActivityViewTabs() {
  const buttons = Array.from(document.querySelectorAll("[data-activity-view-tab]"));
  buttons.forEach((button, index) => {
    button.addEventListener("click", () => activateActivityView(button.dataset.activityViewTab));
    button.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const offset = event.key === "ArrowRight" ? 1 : -1;
      const target = buttons[(index + offset + buttons.length) % buttons.length];
      target.focus();
      activateActivityView(target.dataset.activityViewTab);
    });
  });
  activateActivityView("serena");
}

async function getJson(path) {
  const response = await fetch(`${API_PREFIX}${path}`, {
    cache: "no-cache",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const data = await response.json();
  if (data?.status === "error") throw new Error(data.message || "Serena API error");
  return data;
}

function decodeBase64Url(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const binary = window.atob(base64);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function setNotificationButtonState(button, state) {
  button.dataset.state = state;
  const enabled = state === "enabled";
  button.setAttribute("aria-pressed", enabled ? "true" : "false");
  const label = enabled
    ? "Job notifications enabled"
    : state === "denied"
      ? "Job notifications blocked by browser settings"
      : "Enable job notifications";
  button.setAttribute("aria-label", label);
  button.title = label;
}

async function setupPushNotifications() {
  const button = byId("notification-button");
  if (!button) return;
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    button.hidden = true;
    return;
  }

  try {
    const registration = await navigator.serviceWorker.register(dashboardAssetUrl("service-worker.js"), { scope: "/dashboard/" });
    const existing = await registration.pushManager.getSubscription();
    setNotificationButtonState(button, existing ? "enabled" : Notification.permission === "denied" ? "denied" : "available");

    button.addEventListener("click", async () => {
      if (button.disabled || button.getAttribute("aria-pressed") === "true") return;
      button.disabled = true;
      try {
        const permission = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
        if (permission !== "granted") {
          setNotificationButtonState(button, permission === "denied" ? "denied" : "available");
          return;
        }

        const config = await getJson("/push/config");
        let subscription = await registration.pushManager.getSubscription();
        if (!subscription) {
          subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: decodeBase64Url(config.public_key),
          });
        }

        const response = await fetch(`${API_PREFIX}/push/subscribe`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(subscription.toJSON()),
        });
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        setNotificationButtonState(button, "enabled");
      } catch (error) {
        console.error("Could not enable job notifications", error);
        setNotificationButtonState(button, "available");
      } finally {
        button.disabled = false;
      }
    });
  } catch (error) {
    console.error("Web Push setup failed", error);
    button.hidden = true;
  }
}

let latestResources = { tools: [], memories: [] };
let memoryRequestGeneration = 0;

async function openMemory(memoryName) {
  const dialog = byId("memory-dialog");
  const title = byId("memory-dialog-title");
  const content = byId("memory-dialog-content");
  const generation = ++memoryRequestGeneration;

  title.textContent = memoryName;
  content.textContent = "Loading…";
  if (!dialog.open) dialog.showModal();

  try {
    const memory = await getJson(`/memory?name=${encodeURIComponent(memoryName)}`);
    if (generation !== memoryRequestGeneration) return;
    title.textContent = memory.memory_name || memoryName;
    content.textContent = memory.content || "";
    content.scrollTop = 0;
  } catch (error) {
    if (generation !== memoryRequestGeneration) return;
    content.textContent = `Could not load memory: ${error.message}`;
  }
}

function openResourceDialog(kind) {
  const dialog = byId("resource-dialog");
  const title = byId("resource-dialog-title");
  const content = byId("resource-dialog-content");
  const values = latestResources[kind] || [];
  const isMemories = kind === "memories";

  title.textContent = isMemories ? "Memories" : "Active tools";
  const children = values.map((value) => {
    if (!isMemories) return makeElement("span", "chip", value);

    const button = makeElement("button", "chip memory-chip", value);
    button.type = "button";
    button.addEventListener("click", () => {
      dialog.close();
      openMemory(value);
    });
    return button;
  });
  if (!children.length) children.push(makeElement("span", "empty", isMemories ? "No memories available" : "No active tools"));
  clearAndAppend(content, children);
  if (!dialog.open) dialog.showModal();
}

function setupResourceDialog() {
  const dialog = byId("resource-dialog");
  byId("tools-button").addEventListener("click", () => openResourceDialog("tools"));
  byId("memories-button").addEventListener("click", () => openResourceDialog("memories"));
  byId("resource-dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

function renderRunningJobs() {
  const jobs = (latestJobs.jobs || []).filter((job) => job.status === "running");
  const content = byId("jobs-dialog-content");
  setText("jobs-dialog-title", `Running jobs (${jobs.length}/${latestJobs.max_concurrent_jobs || 0})`);

  if (jobs.length === 0) {
    clearAndAppend(content, [makeElement("div", "jobs-empty", "No running jobs.")]);
    return;
  }

  const rows = jobs.map((job) => {
    const row = makeElement("div", "running-job");
    row.append(makeElement("div", "running-job-title", job.label || "Background job"));

    const details = [];
    if (job.project) details.push(job.project);
    if (Number.isFinite(Number(job.elapsed_seconds))) details.push(formatDuration(Number(job.elapsed_seconds)));
    if (Number(job.process_count) > 0) details.push(`${job.process_count} process${Number(job.process_count) === 1 ? "" : "es"}`);
    row.append(makeElement("div", "running-job-detail", details.join(" · ") || job.job_id));
    return row;
  });
  clearAndAppend(content, rows);
}

function setupJobsDialog() {
  const dialog = byId("jobs-dialog");
  byId("jobs-button").addEventListener("click", () => {
    renderRunningJobs();
    dialog.showModal();
  });
  byId("jobs-dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

function setupMemoryDialog() {
  const dialog = byId("memory-dialog");
  byId("memory-dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

function renderOverview(session, jobs) {
  setText("languages", (session.languages || []).join(" · "), "None");
  setText("runtime-policy", session.runtime_policy, "ChatGPT");
  setText("version", session.serena_version, "—");

  const activeTools = session.active_tools || [];
  const memories = session.available_memories || [];
  latestResources = { tools: activeTools, memories };
  latestJobs = jobs || { jobs: [], running_jobs: 0, max_concurrent_jobs: 0 };

  setText("tool-count", activeTools.length);
  setText("memories-count", memories.length);
  setText("running-jobs-count", latestJobs.running_jobs || 0);
  setText("max-jobs-count", latestJobs.max_concurrent_jobs || 0);
  byId("tools-button").setAttribute("aria-label", `${activeTools.length} active tools`);
  byId("memories-button").setAttribute("aria-label", `${memories.length} memories`);
  byId("jobs-button").setAttribute(
    "aria-label",
    `${latestJobs.running_jobs || 0} of ${latestJobs.max_concurrent_jobs || 0} Serena jobs running`,
  );

  if (byId("jobs-dialog").open) renderRunningJobs();
}

function formatEpochClock(timestamp) {
  if (!Number.isFinite(timestamp)) return "—";
  return new Date(timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatEpochDate(timestamp) {
  if (!Number.isFinite(timestamp)) return "—";
  return new Date(timestamp * 1000).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
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

function setConnection(state, label) {
  const connection = byId("connection-state");
  connection.dataset.state = state;
  setText("connection-label", label);
}

function updateSessionWidgetHeading(entry, panel) {
  let heading = entry.querySelector(".activity-widget-heading");
  if (!panel.display_name) {
    heading?.remove();
    return;
  }

  if (!heading) {
    heading = makeElement("div", "activity-widget-heading");
    heading.append(
      makeElement("span", "activity-widget-title"),
      makeElement("span", "activity-widget-date"),
    );
    entry.prepend(heading);
  }

  heading.querySelector(".activity-widget-title").textContent = panel.display_name;
  const date = heading.querySelector(".activity-widget-date");
  const dateText = formatEpochDate(panel.started_at);
  date.textContent = dateText;
  date.hidden = dateText === "—";
}

class SessionWidgetLoader {
  constructor(kind) {
    this.sourceUrl = `/dashboard/widget/${kind}?load=${DASHBOARD_LOAD_ID}`;
    this.documentUrlPromise = null;
    this.mounts = new WeakMap();
    this.queue = [];
    this.loadingFrame = null;
    this.loadingTimeout = null;
    this.observer = "IntersectionObserver" in window
      ? new IntersectionObserver(entries => {
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            this.observer.unobserve(entry.target);
            this.deferMount(entry.target);
          }
        }, { rootMargin: "800px 0px" })
      : null;
  }

  prepare(shell, mountFrame, active) {
    this.mounts.set(shell, mountFrame);
    if (active || !this.observer) {
      this.deferMount(shell, active);
      return;
    }
    this.observer.observe(shell);
  }

  prioritize(shell) {
    this.observer?.unobserve(shell);
    this.deferMount(shell, true);
  }

  deferMount(shell, priority = false) {
    if (shell.dataset.widgetMounted === "true" || shell.dataset.widgetMountPending === "true") return;
    shell.dataset.widgetMountPending = "true";
    requestAnimationFrame(() => {
      window.setTimeout(() => {
        delete shell.dataset.widgetMountPending;
        this.mount(shell, priority);
      }, 0);
    });
  }

  mount(shell, priority = false) {
    if (!shell.isConnected || shell.dataset.widgetMounted === "true") return;
    const mountFrame = this.mounts.get(shell);
    if (!mountFrame) return;
    shell.dataset.widgetMounted = "true";
    const frame = mountFrame();
    this.enqueue(frame, priority);
  }

  enqueue(frame, priority = false) {
    if (frame.dataset.widgetStarted === "true" || frame.dataset.widgetQueued === "true") return;
    frame.dataset.widgetQueued = "true";
    if (priority) this.queue.unshift(frame);
    else this.queue.push(frame);
    this.drain();
  }

  complete(frame) {
    if (this.loadingFrame !== frame) return;
    window.clearTimeout(this.loadingTimeout);
    this.loadingFrame = null;
    this.loadingTimeout = null;
    this.drain();
  }

  documentUrl() {
    if (!this.documentUrlPromise) {
      this.documentUrlPromise = fetch(this.sourceUrl, { cache: "force-cache" })
        .then(response => {
          if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
          return response.text();
        })
        .then(html => URL.createObjectURL(new Blob([html], { type: "text/html" })))
        .catch(() => this.sourceUrl);
    }
    return this.documentUrlPromise;
  }

  drain() {
    if (this.loadingFrame) return;

    let frame = this.queue.shift();
    while (frame && !frame.isConnected) frame = this.queue.shift();
    if (!frame) return;

    delete frame.dataset.widgetQueued;
    frame.dataset.widgetStarted = "true";
    this.loadingFrame = frame;
    this.documentUrl().then(url => {
      if (!frame.isConnected) {
        this.complete(frame);
        return;
      }
      frame.src = url;
      this.loadingTimeout = window.setTimeout(() => {
        if (this.loadingFrame !== frame) return;
        this.loadingFrame = null;
        this.loadingTimeout = null;
        this.drain();
      }, 5000);
    });
  }
}

function sessionWidgetLoader(kind) {
  let loader = sessionWidgetLoaders.get(kind);
  if (!loader) {
    loader = new SessionWidgetLoader(kind);
    sessionWidgetLoaders.set(kind, loader);
  }
  return loader;
}

function sessionWidgetBootstrap(panel, kind) {
  const toolOutput = kind === "serena"
    ? panel.initial_state
    : {
        run_id: panel.panel_id,
        started_at: panel.started_at || 0,
        superseded: false,
        delegates: panel.delegates || [],
      };
  return {
    panel_id: panel.panel_id,
    active: Boolean(panel.active),
    revision: panel.revision || "",
    tool_output: toolOutput || null,
  };
}

function sessionPreviewStatus(status) {
  if (status === "running") return "●";
  if (status === "failed" || status === "timed_out" || status === "FAILED") return "!";
  if (status === "cancelled") return "×";
  if (status === "queued" || status === "waiting") return "○";
  return "✓";
}

function sessionPreviewElapsed(entry, now) {
  const started = Number(entry.started_at ?? entry.submitted_at);
  if (!Number.isFinite(started)) return "";
  const finished = Number(entry.finished_at);
  const end = Number.isFinite(finished) ? finished : now;
  return formatDuration(Math.max(0, end - started));
}

function sessionPreviewSubmissionSpan(state) {
  if (Number.isFinite(state?.submission_span_seconds)) return formatDuration(state.submission_span_seconds);

  const submitted = (state?.calls || [])
    .map(call => Number(call.submitted_at ?? call.started_at))
    .filter(Number.isFinite);
  if (submitted.length === 0) return "";
  return formatDuration(Math.max(...submitted) - Math.min(...submitted));
}

function serenaPreviewEntries(state) {
  if (!state) return [];
  const currentJobIds = new Set((state.jobs || []).filter(job => job.current_turn !== false).map(job => job.job_id));
  const calls = (state.calls || [])
    .filter(call => !(call.tool_name === "start_job" && call.job_id && currentJobIds.has(call.job_id)))
    .map(call => ({ ...call, kind: "tool" }));
  const jobs = (state.jobs || []).filter(job => job.current_turn !== false).map(job => ({
    kind: "job",
    job_id: job.job_id,
    tool_name: "start_job",
    scope: job.project || "",
    detail: job.label || "background job",
    status: job.status || "completed",
    submitted_at: job.started_at,
    started_at: job.started_at,
    finished_at: job.finished_at,
  }));
  const entries = [...calls, ...jobs];
  const running = entries.filter(entry => entry.status === "running").sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  const terminal = entries.filter(entry => entry.status !== "running").sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  return [...running, ...terminal];
}

function serenaPreviewExpanded(state) {
  if (typeof state?.initial_expanded === "boolean") return state.initial_expanded;
  const entries = [...(state?.calls || []), ...(state?.jobs || [])];
  if (!entries.length) return false;
  if (entries.some(entry => entry.status === "running")) return true;
  const timestamps = entries
    .map(entry => Number(entry.finished_at ?? entry.started_at ?? entry.submitted_at))
    .filter(Number.isFinite);
  const latest = timestamps.length ? Math.max(...timestamps) : null;
  return latest !== null && Date.now() / 1000 - latest <= 5 * 60;
}

function orchestratorPreviewEntries(panel) {
  const delegates = panel.delegates || [];
  return delegates.map(delegate => ({
    tool_name: delegate.kind || "delegate",
    scope: delegate.project_name || delegate.active_provider || "",
    detail: delegate.active_provider || delegate.provider_policy || "",
    status: String(delegate.state || "completed").toLowerCase(),
    started_at: Date.parse(delegate.started_at || delegate.created_at || "") / 1000,
    finished_at: delegate.finished_at ? Date.parse(delegate.finished_at) / 1000 : null,
  })).sort((a, b) => {
    const ar = a.status === "running" || a.status === "claimed";
    const br = b.status === "running" || b.status === "claimed";
    if (ar !== br) return ar ? -1 : 1;
    return (b.started_at || 0) - (a.started_at || 0);
  });
}

function sessionPreviewModel(panel, kind) {
  if (kind === "serena") {
    const state = panel.initial_state || null;
    const toolCount = Number.isFinite(state?.tool_count) ? state.tool_count : (state?.calls || []).length;
    const jobCount = Number.isFinite(state?.job_count) ? state.job_count : (state?.jobs || []).length;
    return {
      label: "Serena",
      icon: dashboardAssetUrl("serena-logo.svg"),
      stats: `${toolCount} tool${toolCount === 1 ? "" : "s"} · ${jobCount} job${jobCount === 1 ? "" : "s"} · ${state?.project_name || panel.project_name || "no project"}`,
      status: sessionPreviewSubmissionSpan(state),
      statusClass: "",
      expanded: serenaPreviewExpanded(state),
      entries: serenaPreviewEntries(state),
    };
  }

  const delegates = panel.delegates || [];
  const entries = orchestratorPreviewEntries(panel);
  const running = entries.filter(entry => ["running", "claimed", "pending"].includes(entry.status)).length;
  const failed = entries.filter(entry => entry.status === "failed").length;
  return {
    label: "Orchestrator",
    icon: dashboardAssetUrl("orchestrator-logo.svg"),
    stats: `${delegates.length} delegate${delegates.length === 1 ? "" : "s"}`,
    status: running ? `${running} running` : failed ? `${failed} failed` : delegates.length ? "Complete" : "Idle",
    statusClass: running ? "running" : failed ? "failed" : "",
    expanded: Boolean(panel.active) || Date.now() / 1000 - Number(panel.updated_at || 0) <= 5 * 60,
    entries,
  };
}

function createSessionWidgetPreview(panel, kind) {
  const model = sessionPreviewModel(panel, kind);
  const preview = makeElement("div", `activity-widget-preview ${model.expanded ? "expanded" : "collapsed"}`);
  const header = makeElement("div", "activity-widget-preview-header");
  const title = makeElement("div", "activity-widget-preview-title");
  const icon = document.createElement("img");
  icon.className = "activity-widget-preview-logo";
  icon.src = model.icon;
  icon.alt = "";
  const overview = makeElement("div", "activity-widget-preview-overview");
  overview.append(makeElement("strong", "", model.label), makeElement("span", "activity-widget-preview-stats", model.stats));
  title.append(icon, overview);
  const meta = makeElement("span", `activity-widget-preview-status ${model.statusClass}`, model.status);
  header.append(title, meta, makeElement("span", `activity-widget-preview-chevron ${model.expanded ? "" : "collapsed"}`, "⌄"));
  preview.append(header);

  if (model.expanded) {
    const body = makeElement("div", "activity-widget-preview-body");
    const now = Date.now() / 1000;
    for (const entry of model.entries.slice(0, 6)) {
      const row = makeElement("div", `activity-widget-preview-row ${entry.status || "completed"}`);
      row.append(
        makeElement("span", "activity-widget-preview-row-status", sessionPreviewStatus(entry.status)),
        makeElement("strong", "activity-widget-preview-row-tool", entry.tool_name || "activity"),
        makeElement("span", "activity-widget-preview-row-scope", entry.scope || ""),
        makeElement("span", "activity-widget-preview-row-submitted", formatEpochClock(Number(entry.submitted_at ?? entry.started_at))),
        makeElement("span", "activity-widget-preview-row-detail", entry.detail || ""),
        makeElement("span", "activity-widget-preview-row-elapsed", sessionPreviewElapsed(entry, now)),
      );
      body.append(row);
    }
    preview.append(body);
  }
  return preview;
}

function renderSessionWidgets(containerId, countId, panels, kind) {
  const container = byId(containerId);
  const loader = sessionWidgetLoader(kind);
  const orderedPanels = [...panels].sort((left, right) => {
    const leftStarted = Number(left.started_at) || 0;
    const rightStarted = Number(right.started_at) || 0;
    if (leftStarted !== rightStarted) return rightStarted - leftStarted;
    return String(right.panel_id || "").localeCompare(String(left.panel_id || ""));
  });
  setText(countId, orderedPanels.length, "0");
  if (!orderedPanels.length) {
    const label = kind === "serena" ? "No Serena session activity recorded yet." : "No orchestration activity recorded yet.";
    if (!container.querySelector(".empty-card")) clearAndAppend(container, [makeElement("div", "empty-card", label)]);
    return;
  }

  reconcileKeyed(
    container,
    orderedPanels,
    panel => panel.panel_id,
    panel => {
      const entry = makeElement("div", "activity-widget-entry");
      updateSessionWidgetHeading(entry, panel);

      const shell = makeElement("div", `activity-widget-shell ${panel.active ? "active-session" : "retained-session"}`);
      const preview = createSessionWidgetPreview(panel, kind);
      shell.append(preview);
      entry.append(shell);

      const mountFrame = () => {
        const existing = shell.querySelector(".activity-widget-frame");
        if (existing) return existing;
        const frame = document.createElement("iframe");
        frame.className = "activity-widget-frame";
        frame.loading = "eager";
        frame.title = kind === "serena" ? "Serena session activity" : "Orchestrator activity";
        frame.addEventListener("load", () => {
          if (frame.dataset.widgetStarted !== "true") return;
          frame.dataset.loaded = "true";
          const previewHeight = Math.ceil(preview.getBoundingClientRect().height);
          if (previewHeight > 0) frame.style.height = `${previewHeight}px`;
          frame.contentWindow?.postMessage(
            { type: "serena-dashboard-bootstrap", bootstrap: sessionWidgetBootstrap(panel, kind) },
            location.origin,
          );
          loader.complete(frame);
        });
        frame.addEventListener("error", () => {
          if (frame.dataset.widgetStarted !== "true") return;
          frame.dataset.loaded = "true";
          loader.complete(frame);
        });
        shell.append(frame);
        return frame;
      };

      loader.prepare(shell, mountFrame, Boolean(panel.active));
      return entry;
    },
    (entry, panel) => {
      const shell = entry.querySelector(".activity-widget-shell");
      if (shell) {
        shell.classList.toggle("active-session", Boolean(panel.active));
        shell.classList.toggle("retained-session", !panel.active);
      }
      const frame = entry.querySelector(".activity-widget-frame");
      if (frame?.dataset.loaded === "true" && frame.contentWindow) {
        frame.contentWindow.postMessage(
          {
            type: "serena-dashboard-panel",
            panel_id: panel.panel_id,
            active: Boolean(panel.active),
            revision: panel.revision || "",
          },
          location.origin,
        );
      } else if (shell && panel.active) {
        loader.prioritize(shell);
      }

      updateSessionWidgetHeading(entry, panel);
    },
  );
}


function consumeNotificationTarget() {
  if (!notificationTarget) return;
  const url = new URL(window.location.href);
  url.searchParams.delete("panel");
  url.searchParams.delete("job");
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  notificationTarget = null;
}

function sendNotificationJobFocus(frame, panelId) {
  if (!notificationTarget || notificationTarget.panelId !== panelId || !frame?.contentWindow) return;
  const target = notificationTarget;
  frame.contentWindow.postMessage(
    {
      type: "serena-dashboard-focus-job",
      panel_id: panelId,
      job_id: target.jobId,
    },
    location.origin,
  );
  consumeNotificationTarget();
}

function focusNotificationJob(panels) {
  if (!notificationTarget) return;
  if (!(panels || []).some(panel => panel.panel_id === notificationTarget.panelId)) return;

  activateActivityView("serena");
  const entry = Array.from(byId("serena-widgets").children)
    .find(candidate => candidate.dataset.itemKey === notificationTarget.panelId);
  if (!entry) return;

  const shell = entry.querySelector(".activity-widget-shell");
  if (shell) sessionWidgetLoader("serena").prioritize(shell);
  requestAnimationFrame(() => entry.scrollIntoView({ block: "center" }));

  const frame = entry.querySelector(".activity-widget-frame");
  if (frame?.classList.contains("ready")) sendNotificationJobFocus(frame, notificationTarget.panelId);
}

window.addEventListener("message", event => {
  if (event.origin !== location.origin) return;
  const frame = Array.from(document.querySelectorAll(".activity-widget-frame")).find(candidate => candidate.contentWindow === event.source);
  if (!frame) return;
  if (event.data?.type === "serena-dashboard-widget-ready") {
    const preview = frame.parentElement?.querySelector(".activity-widget-preview");
    if (preview) preview.hidden = true;
    requestAnimationFrame(() => {
      frame.classList.add("ready");
      sendNotificationJobFocus(frame, String(event.data.panel_id || ""));
    });
    return;
  }
  if (event.data?.type !== "serena-activity-height") return;
  const height = Math.max(42, Math.min(720, Number(event.data.height) || 42));
  frame.style.height = `${height}px`;
});

function nextRefreshDelay() {
  if (document.hidden) return HIDDEN_POLL_INTERVAL_MS;
  return latestPanelActivity ? ACTIVE_POLL_INTERVAL_MS : IDLE_POLL_INTERVAL_MS;
}

function scheduleRefresh(delay = nextRefreshDelay()) {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(refresh, delay);
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;

  let session;
  let jobs;
  let serena;
  let orchestrator;
  try {
    let state;
    if (!initialActivityStateLoaded && dashboardBootstrapState) {
      state = dashboardBootstrapState;
      dashboardBootstrapState = null;
    } else {
      state = await getJson(initialActivityStateLoaded ? "/state" : "/state?include_state=1");
    }
    session = state.session;
    jobs = state.jobs;
    serena = state.serena;
    orchestrator = state.orchestrator;
  } catch (error) {
    console.error("Dashboard data refresh failed", error);
    setConnection("error", "Disconnected");
    refreshInFlight = false;
    scheduleRefresh();
    return;
  }

  setConnection("live", "Live");
  setText("last-update", new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
  try {
    renderOverview(session, jobs);
    renderSessionWidgets("serena-widgets", "serena-panel-count", serena.panels || [], "serena");
    renderSessionWidgets("orchestrator-widgets", "orchestrator-panel-count", orchestrator.panels || [], "orchestrator");
    latestPanelActivity = [...(serena.panels || []), ...(orchestrator.panels || [])].some(panel => panel.active);
    initialActivityStateLoaded = true;

    if (!initialActivityViewSelected) {
      const orchestratorActive = (orchestrator.panels || []).some(panel => panel.active);
      if (notificationTarget) activateActivityView("serena");
      else if (isTabbedActivityMode() && orchestratorActive) activateActivityView("orchestrator");
      initialActivityViewSelected = true;
    }
    focusNotificationJob(serena.panels || []);
  } catch (error) {
    console.error("Dashboard render failed", error);
    setConnection("error", "UI error");
  } finally {
    refreshInFlight = false;
    scheduleRefresh();
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refresh();
  } else {
    scheduleRefresh();
  }
});
window.addEventListener("focus", () => refresh(), { passive: true });

setupResourceDialog();
setupJobsDialog();
setupMemoryDialog();
setupActivityViewTabs();
setupPushNotifications();
refresh();
