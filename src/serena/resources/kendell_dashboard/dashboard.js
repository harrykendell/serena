const API_PREFIX = "/dashboard/api";
const POLL_INTERVAL_MS = 1500;
const SCROLL_IDLE_MS = 350;

const outputCache = new Map();
const outputRequests = new Set();
const jobCpuSamples = new Map();
const scrollerStates = new WeakMap();
let refreshInFlight = false;

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

function clearAndAppend(parent, children) {
  parent.replaceChildren(...children);
}

function decodeOutputEscapes(value) {
  return String(value ?? "")
    .replace(/\\+r\\+n/g, "\n")
    .replace(/\\+n/g, "\n")
    .replace(/\\+r/g, "\n")
    .replace(/\\+t/g, "\t");
}

function normaliseOutputText(value) {
  const raw = String(value ?? "");
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return Object.entries(parsed)
        .filter(([, fieldValue]) => fieldValue !== "" && fieldValue !== null && fieldValue !== undefined)
        .map(([key, fieldValue]) => {
          const rendered = typeof fieldValue === "string"
            ? decodeOutputEscapes(fieldValue)
            : JSON.stringify(fieldValue, null, 2);
          return `${key}:\n${rendered}`;
        })
        .join("\n\n");
    }
  } catch (_error) {
    // non-JSON tool output remains useful as plain text
  }
  return decodeOutputEscapes(raw);
}

function isNearBottom(scroller) {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 24;
}

function getScrollerState(scroller, defaultToBottom = false) {
  let state = scrollerStates.get(scroller);
  if (state) return state;

  state = {
    followTail: defaultToBottom,
    interacting: false,
    lastScrollAt: 0,
    pending: null,
    flushTimer: null,
  };
  scrollerStates.set(scroller, state);

  const beginInteraction = () => {
    state.interacting = true;
  };
  const endInteraction = () => {
    state.interacting = false;
    schedulePendingOutput(scroller, state);
  };

  scroller.addEventListener("pointerdown", beginInteraction, { passive: true });
  scroller.addEventListener("pointerup", endInteraction, { passive: true });
  scroller.addEventListener("pointercancel", endInteraction, { passive: true });
  scroller.addEventListener("touchstart", beginInteraction, { passive: true });
  scroller.addEventListener("touchend", endInteraction, { passive: true });
  scroller.addEventListener("touchcancel", endInteraction, { passive: true });
  scroller.addEventListener("wheel", () => {
    state.interacting = true;
    window.clearTimeout(state.wheelTimer);
    state.wheelTimer = window.setTimeout(endInteraction, SCROLL_IDLE_MS);
  }, { passive: true });
  scroller.addEventListener("scroll", () => {
    state.lastScrollAt = performance.now();
    state.followTail = isNearBottom(scroller);
    if (state.followTail) schedulePendingOutput(scroller, state);
  }, { passive: true });

  return state;
}

function applyScrollableText(scroller, text, state, defaultToBottom) {
  const followTail = state.followTail ?? defaultToBottom;
  const previousScrollTop = scroller.scrollTop;
  if (scroller.textContent === text) return;

  scroller.textContent = text;
  if (followTail) {
    scroller.scrollTop = scroller.scrollHeight;
  } else {
    scroller.scrollTop = Math.min(previousScrollTop, Math.max(0, scroller.scrollHeight - scroller.clientHeight));
  }
}

function schedulePendingOutput(scroller, state) {
  if (!state.pending || state.interacting) return;
  if (state.pending.live && !state.followTail) return;

  window.clearTimeout(state.flushTimer);
  const elapsed = performance.now() - state.lastScrollAt;
  const delay = Math.max(0, SCROLL_IDLE_MS - elapsed);
  state.flushTimer = window.setTimeout(() => {
    if (!state.pending || state.interacting) return;
    const idleFor = performance.now() - state.lastScrollAt;
    if (idleFor < SCROLL_IDLE_MS) {
      schedulePendingOutput(scroller, state);
      return;
    }
    state.followTail = isNearBottom(scroller);
    if (state.pending.live && !state.followTail) return;
    const pending = state.pending;
    state.pending = null;
    applyScrollableText(scroller, pending.text, state, pending.defaultToBottom);
  }, delay);
}

function updateScrollableText(scroller, value, { defaultToBottom = false, live = false } = {}) {
  const text = normaliseOutputText(value);
  const state = getScrollerState(scroller, defaultToBottom);
  state.followTail = isNearBottom(scroller);
  if (scroller.textContent === text && state.pending === null) return;

  const recentlyScrolled = performance.now() - state.lastScrollAt < SCROLL_IDLE_MS;
  if (state.interacting || recentlyScrolled || (live && !state.followTail)) {
    state.pending = { text, live, defaultToBottom };
    schedulePendingOutput(scroller, state);
    return;
  }

  state.pending = null;
  applyScrollableText(scroller, text, state, defaultToBottom);
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

function activateTab(name, updateHash = true) {
  const selected = name === "jobs" ? "jobs" : "tools";
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === selected;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    const active = panel.dataset.panel === selected;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  if (updateHash) history.replaceState(null, "", `#${selected}`);
}

function setupTabs() {
  const buttons = Array.from(document.querySelectorAll("[data-tab]"));
  buttons.forEach((button, index) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const offset = event.key === "ArrowRight" ? 1 : -1;
      const target = buttons[(index + offset + buttons.length) % buttons.length];
      target.focus();
      activateTab(target.dataset.tab);
    });
  });
  window.addEventListener("hashchange", () => activateTab(location.hash === "#jobs" ? "jobs" : "tools", false));
  activateTab(location.hash === "#jobs" ? "jobs" : "tools", false);
}

async function getJson(path) {
  const response = await fetch(`${API_PREFIX}${path}`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const data = await response.json();
  if (data?.status === "error") throw new Error(data.message || "Serena API error");
  return data;
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

function setupMemoryDialog() {
  const dialog = byId("memory-dialog");
  byId("memory-dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

function renderOverview(session) {
  const project = session.active_project || {};
  setText("project-name", project.name, "No active project");
  setText("project-path", project.path, "—");
  setText("languages", (session.languages || []).join(" · "), "None");
  setText("context-name", session.context, "—");
  setText("version", session.serena_version, "—");

  const activeTools = session.active_tools || [];
  const memories = session.available_memories || [];
  latestResources = { tools: activeTools, memories };

  setText("tool-count", activeTools.length);
  setText("memories-count", memories.length);
  byId("tools-button").setAttribute("aria-label", `${activeTools.length} active tools`);
  byId("memories-button").setAttribute("aria-label", `${memories.length} memories`);
}

function executionDisplayName(name) {
  const raw = String(name || "execution").replace(/^Task-\d+:/, "").replace(/^BackgroundTask:/, "");
  if (!raw.endsWith("Tool")) return raw;
  return raw
    .slice(0, -4)
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase();
}

function updateStatusBadge(badge, status) {
  badge.className = `status-badge ${status}`;
  setNodeText(badge, status);
}

function createOutputSection(label, className) {
  const section = makeElement("section", `execution-section ${className}`);
  const heading = makeElement("div", "execution-section-label", label);
  const output = makeElement("pre", "scroll-output rich-output");
  section.append(heading, output);
  return { section, output };
}

function createMediaSection() {
  const section = makeElement("section", "execution-section execution-media");
  const heading = makeElement("div", "execution-section-label", "Preview");
  const preview = makeElement("div", "media-preview");
  const image = makeElement("img", "media-preview-image");
  image.alt = "Serena tool media result";
  const audio = makeElement("audio", "media-preview-audio");
  audio.controls = true;
  audio.preload = "metadata";
  const note = makeElement("div", "media-preview-note", "Open the tool call to load preview.");
  image.hidden = true;
  audio.hidden = true;
  preview.append(image, audio, note);
  section.append(heading, preview);
  return { section, image, audio, note, taskId: null, mediaType: null, loadedTaskId: null };
}

function loadExecutionMedia(row) {
  const media = row._dashboardRefs?.media;
  if (!row.open || !media || !media.taskId || !media.mediaType || media.loadedTaskId === media.taskId) return;

  const url = `${API_PREFIX}/executions/${encodeURIComponent(media.taskId)}/media`;
  media.note.textContent = "Loading preview…";
  if (media.mediaType === "image") {
    media.audio.pause();
    media.audio.removeAttribute("src");
    media.audio.hidden = true;
    media.image.hidden = false;
    media.image.onload = () => { media.note.textContent = ""; };
    media.image.onerror = () => { media.note.textContent = "Could not load media preview."; };
    media.image.src = url;
  } else if (media.mediaType === "audio") {
    media.image.removeAttribute("src");
    media.image.hidden = true;
    media.audio.hidden = false;
    media.audio.onloadedmetadata = () => { media.note.textContent = ""; };
    media.audio.onerror = () => { media.note.textContent = "Could not load media preview."; };
    media.audio.src = url;
  }
  media.loadedTaskId = media.taskId;
}

function createExecutionRow() {
  const details = makeElement("details", "execution-entry");
  const summary = makeElement("summary", "execution-summary");
  const title = makeElement("span", "activity-title mono");
  const badge = makeElement("span", "status-badge");
  summary.append(title, badge);

  const body = makeElement("div", "execution-body");
  const parameters = createOutputSection("Parameters", "execution-parameters");
  const result = createOutputSection("Result", "execution-result");
  const error = createOutputSection("Error", "execution-error");
  const media = createMediaSection();
  body.append(parameters.section, result.section, error.section, media.section);
  details.append(summary, body);

  details._dashboardRefs = { title, badge, body, parameters, result, error, media };
  details.addEventListener("toggle", () => loadExecutionMedia(details));
  return details;
}

function updateExecutionSection(sectionRefs, value, { live = false, defaultToBottom = false } = {}) {
  const visible = Boolean(value);
  sectionRefs.section.hidden = !visible;
  if (visible) updateScrollableText(sectionRefs.output, value, { live, defaultToBottom });
}

function updateExecutionRow(row, execution) {
  const refs = row._dashboardRefs;
  setNodeText(refs.title, executionDisplayName(execution.name));
  updateStatusBadge(refs.badge, execution.status);
  updateExecutionSection(refs.parameters, execution.parameters);

  const mediaType = execution.media?.type || null;
  refs.media.section.hidden = !mediaType;
  if (mediaType) {
    refs.media.taskId = execution.task_id;
    refs.media.mediaType = mediaType;
    if (refs.media.loadedTaskId !== execution.task_id) {
      refs.media.note.textContent = "Open the tool call to load preview.";
      refs.media.image.removeAttribute("src");
      refs.media.audio.pause();
      refs.media.audio.removeAttribute("src");
      refs.media.image.hidden = true;
      refs.media.audio.hidden = true;
    }
  } else {
    refs.media.taskId = null;
    refs.media.mediaType = null;
    refs.media.loadedTaskId = null;
  }

  updateExecutionSection(refs.result, mediaType ? null : execution.result, { defaultToBottom: true });
  updateExecutionSection(refs.error, execution.error, { defaultToBottom: true });
  refs.body.hidden = !execution.parameters && !execution.result && !execution.error && !mediaType;
  if (mediaType && row.open) loadExecutionMedia(row);
}

function renderExecutions(data) {
  setText("execution-running", data.running || 0);
  setText("execution-queued", data.queued || 0);
  setText("execution-done", data.done || 0);

  const list = byId("executions-list");
  const executions = data.executions || [];
  setText("tools-tab-count", executions.length);
  if (!executions.length) {
    if (!list.querySelector(".empty-card")) clearAndAppend(list, [makeElement("div", "empty-card", "No tool executions recorded yet.")]);
    return;
  }

  reconcileKeyed(list, executions, (execution) => execution.task_id, createExecutionRow, updateExecutionRow);
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${secs}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return null;
  const value = Math.max(0, Number(bytes));
  if (!Number.isFinite(value)) return null;
  const mebibytes = value / (1024 ** 2);
  if (mebibytes < 1024) return `${Math.round(mebibytes)} MB`;
  return `${(mebibytes / 1024).toFixed(1)} GB`;
}

function formatJobCpuPercent(job) {
  if (job.status !== "running" || job.cpu_seconds === null || job.cpu_seconds === undefined) {
    jobCpuSamples.delete(job.job_id);
    return null;
  }

  const cpuSeconds = Number(job.cpu_seconds);
  if (!Number.isFinite(cpuSeconds)) return null;
  const sampledAt = performance.now() / 1000;
  const previous = jobCpuSamples.get(job.job_id);
  let cpuPercent = job.elapsed_seconds > 0 ? (cpuSeconds / job.elapsed_seconds) * 100 : null;

  if (previous && cpuSeconds >= previous.cpuSeconds && sampledAt > previous.sampledAt) {
    cpuPercent = ((cpuSeconds - previous.cpuSeconds) / (sampledAt - previous.sampledAt)) * 100;
  }
  jobCpuSamples.set(job.job_id, { cpuSeconds, sampledAt });

  return cpuPercent === null || !Number.isFinite(cpuPercent) ? null : Math.max(0, Math.round(cpuPercent));
}

function formatJobRuntime(job) {
  const parts = [formatDuration(job.elapsed_seconds)];
  if (job.status !== "running") return parts[0];

  const cpuPercent = formatJobCpuPercent(job);
  const memory = formatBytes(job.memory_bytes);
  if (cpuPercent !== null) parts.push(`CPU ${cpuPercent}%`);
  if (memory !== null) parts.push(`RAM ${memory}`);
  return parts.join(" · ");
}

function formatClock(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function loadJobOutput(row, force = false) {
  const refs = row._dashboardRefs;
  const jobId = row.dataset.jobId;
  if (!jobId || outputRequests.has(jobId)) return;

  const cached = outputCache.get(jobId);
  if (cached !== undefined && !force) {
    updateScrollableText(refs.output, cached, { defaultToBottom: true, live: true });
    return;
  }
  if (cached === undefined) updateScrollableText(refs.output, "Loading…", { defaultToBottom: true, live: true });

  outputRequests.add(jobId);
  try {
    const data = await getJson(`/jobs/${encodeURIComponent(jobId)}/output`);
    const text = data.output || "No output captured.";
    outputCache.set(jobId, text);
    updateScrollableText(refs.output, text, { defaultToBottom: true, live: true });

    const notes = [];
    if (data.earlier_output_omitted || data.has_earlier_output) notes.push("Earlier output exists");
    if (data.output_truncated) notes.push("Output truncated");
    setNodeText(refs.note, notes.join(" · "));
  } catch (error) {
    updateScrollableText(refs.output, `Could not load output: ${error.message}`, { defaultToBottom: true });
  } finally {
    outputRequests.delete(jobId);
  }
}

function createJobTimeItem(className) {
  const item = makeElement("div", `job-time-item ${className}`);
  const valueNode = makeElement("span", "job-meta-value");
  item.append(valueNode);
  return { item, valueNode };
}

function createJobRow() {
  const details = makeElement("details", "job-entry");
  const summary = makeElement("summary", "job-summary");

  const title = makeElement("div", "activity-title job-title");
  const projectRow = makeElement("div", "job-project-row");
  const project = makeElement("span", "activity-subtitle job-project");
  const badge = makeElement("span", "status-badge");
  projectRow.append(project, badge);

  const submitted = createJobTimeItem("job-submitted");
  const elapsed = createJobTimeItem("job-elapsed");
  summary.append(title, submitted.item, projectRow, elapsed.item);

  const outputBody = makeElement("div", "job-output-body");
  const output = makeElement("pre", "scroll-output rich-output");
  const note = makeElement("div", "output-note");
  outputBody.append(output, note);
  details.append(summary, outputBody);

  getScrollerState(output, true);
  details.addEventListener("toggle", () => {
    if (details.open) loadJobOutput(details, true);
  });

  details._dashboardRefs = { title, project, badge, submitted: submitted.valueNode, elapsed: elapsed.valueNode, output, note };
  return details;
}

function updateJobRow(row, job) {
  const refs = row._dashboardRefs;
  const previousStatus = row.dataset.jobStatus;
  row.dataset.jobId = job.job_id;
  row.dataset.jobStatus = job.status;

  setNodeText(refs.title, job.label || job.job_id);
  setNodeText(refs.project, job.project || "—");
  updateStatusBadge(refs.badge, job.status);
  setNodeText(refs.submitted, formatClock(job.created_at));
  setNodeText(refs.elapsed, formatJobRuntime(job));

  if (!row.open) return;
  const needsFinalRefresh = previousStatus === "running" && job.status !== "running";
  if (job.status === "running" || needsFinalRefresh || !outputCache.has(job.job_id)) {
    loadJobOutput(row, true);
  }
}

function renderJobs(data) {
  setText("jobs-running", data.running_jobs || 0);
  setText("jobs-done", data.terminal_jobs || 0);
  const persistence = data.persistence || {};
  const persistenceText = persistence.survives_serena_restart
    ? "Jobs survive Serena restarts"
    : "Jobs are tied to this Serena process";
  setText("jobs-note", `${persistenceText} · ${data.running_jobs || 0}/${data.max_concurrent_jobs || 0} slots in use`);

  const list = byId("jobs-list");
  const jobs = data.jobs || [];
  setText("jobs-tab-count", jobs.length);
  if (!jobs.length) {
    if (!list.querySelector(".empty-card")) clearAndAppend(list, [makeElement("div", "empty-card", "No jobs recorded.")]);
    return;
  }

  reconcileKeyed(list, jobs, (job) => job.job_id, createJobRow, updateJobRow);
}

function setConnection(state, label) {
  const connection = byId("connection-state");
  connection.dataset.state = state;
  setText("connection-label", label);
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;

  let session;
  let executions;
  let jobs;
  try {
    [session, executions, jobs] = await Promise.all([
      getJson("/session"),
      getJson("/executions"),
      getJson("/jobs"),
    ]);
  } catch (error) {
    console.error("Dashboard data refresh failed", error);
    setConnection("error", "Disconnected");
    refreshInFlight = false;
    return;
  }

  setConnection("live", "Live");
  setText("last-update", new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
  try {
    renderOverview(session);
    renderExecutions(executions);
    renderJobs(jobs);
  } catch (error) {
    console.error("Dashboard render failed", error);
    setConnection("error", "UI error");
  } finally {
    refreshInFlight = false;
  }
}

setupTabs();
setupResourceDialog();
setupMemoryDialog();
refresh();
setInterval(refresh, POLL_INTERVAL_MS);
