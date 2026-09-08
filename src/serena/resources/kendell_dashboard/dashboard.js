const API_PREFIX = "/dashboard/api";
const ACTIVE_POLL_INTERVAL_MS = 500;
const IDLE_POLL_INTERVAL_MS = 5000;
const HIDDEN_POLL_INTERVAL_MS = 60000;
const SCROLL_IDLE_MS = 350;
const DASHBOARD_LOAD_ID = Date.now().toString(36);
const DASHBOARD_ASSET_VERSION = document.documentElement.dataset.assetVersion || "";
let dashboardBootstrapState = null;
try {
  const bootstrapNode = document.getElementById("dashboard-bootstrap");
  dashboardBootstrapState = bootstrapNode?.textContent ? JSON.parse(bootstrapNode.textContent) : null;
} catch (_) {
  dashboardBootstrapState = null;
}

const outputCache = new Map();
const outputRequests = new Set();
const executionOutputCache = new Map();
const executionOutputRequests = new Set();
const jobCpuSamples = new Map();
const scrollerStates = new WeakMap();
const sessionWidgetLoaders = new Map();
const expandedExecutionKeys = new Set();
const executionRenderState = {
  lastScrollAt: 0,
  pending: null,
  flushTimer: null,
  snapshot: null,
};
let refreshInFlight = false;
let refreshTimer = null;
let initialActivityStateLoaded = false;
let initialActivityViewSelected = false;
let latestPanelActivity = false;

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

function markExecutionScrollActivity() {
  executionRenderState.lastScrollAt = performance.now();
  schedulePendingExecutions();
}

function schedulePendingExecutions() {
  if (!executionRenderState.pending) return;

  window.clearTimeout(executionRenderState.flushTimer);
  const elapsed = performance.now() - executionRenderState.lastScrollAt;
  const delay = Math.max(0, SCROLL_IDLE_MS - elapsed);
  executionRenderState.flushTimer = window.setTimeout(() => {
    const idleFor = performance.now() - executionRenderState.lastScrollAt;
    if (idleFor < SCROLL_IDLE_MS) {
      schedulePendingExecutions();
      return;
    }

    const pending = executionRenderState.pending;
    if (!pending) return;
    executionRenderState.pending = null;
    renderExecutionsNow(pending.data, pending.snapshot);
  }, delay);
}

function setupExecutionRenderStability() {
  const list = byId("executions-list");
  const markListActivity = () => markExecutionScrollActivity();
  ["touchstart", "touchmove", "touchend", "wheel"].forEach((eventName) => {
    list.addEventListener(eventName, markListActivity, { passive: true });
  });
  list.addEventListener("scroll", markListActivity, { capture: true, passive: true });
  window.addEventListener("scroll", () => {
    if (!byId("tools-panel").hidden) markExecutionScrollActivity();
  }, { passive: true });
}

function captureExecutionViewportAnchor(list) {
  if (byId("tools-panel").hidden) return null;

  const tabs = document.querySelector(".tabs");
  const viewportTop = Math.max(0, tabs?.getBoundingClientRect().bottom || 0);
  const listRect = list.getBoundingClientRect();
  if (listRect.top >= viewportTop || listRect.bottom <= viewportTop) return null;

  for (const child of list.children) {
    const rect = child.getBoundingClientRect();
    if (rect.bottom > viewportTop) return { node: child, top: rect.top };
  }
  return null;
}

function restoreExecutionViewportAnchor(anchor) {
  if (!anchor?.node?.isConnected) return;
  const delta = anchor.node.getBoundingClientRect().top - anchor.top;
  if (Math.abs(delta) > 0.5) window.scrollBy(0, delta);
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
  const imageLink = makeElement("a", "media-preview-image-link");
  imageLink.target = "_blank";
  imageLink.rel = "noopener";
  const image = makeElement("img", "media-preview-image");
  image.alt = "Serena tool media result";
  imageLink.appendChild(image);
  const audio = makeElement("audio", "media-preview-audio");
  audio.controls = true;
  audio.preload = "metadata";
  const file = makeElement("a", "media-preview-file");
  file.target = "_blank";
  file.rel = "noopener";
  const note = makeElement("div", "media-preview-note", "Open the tool call to load preview.");
  imageLink.hidden = true;
  audio.hidden = true;
  file.hidden = true;
  preview.append(imageLink, audio, file, note);
  section.append(heading, preview);
  return {
    section,
    imageLink,
    image,
    audio,
    file,
    note,
    taskId: null,
    mediaType: null,
    fileName: null,
    mimeType: null,
    loadedTaskId: null,
  };
}

function loadExecutionMedia(row) {
  const media = row._dashboardRefs?.media;
  if (!row.open || !media || !media.taskId || !media.mediaType || media.loadedTaskId === media.taskId) return;

  const url = `${API_PREFIX}/executions/${encodeURIComponent(media.taskId)}/media`;
  media.note.textContent = "Loading preview…";
  media.file.hidden = true;
  media.file.removeAttribute("href");
  if (media.mediaType === "image") {
    media.audio.pause();
    media.audio.removeAttribute("src");
    media.audio.hidden = true;
    media.imageLink.hidden = false;
    media.imageLink.href = url;
    media.image.hidden = false;
    media.image.onload = () => { media.note.textContent = ""; };
    media.image.onerror = () => { media.note.textContent = "Could not load media preview."; };
    media.image.src = url;
  } else if (media.mediaType === "audio") {
    media.image.removeAttribute("src");
    media.imageLink.removeAttribute("href");
    media.imageLink.hidden = true;
    media.audio.hidden = false;
    media.audio.onloadedmetadata = () => { media.note.textContent = ""; };
    media.audio.onerror = () => { media.note.textContent = "Could not load media preview."; };
    media.audio.src = url;
  } else if (media.mediaType === "file") {
    media.image.removeAttribute("src");
    media.imageLink.removeAttribute("href");
    media.imageLink.hidden = true;
    media.audio.pause();
    media.audio.removeAttribute("src");
    media.audio.hidden = true;
    media.file.hidden = false;
    media.file.href = url;
    const name = media.fileName || "Open file";
    media.file.textContent = media.mimeType ? `${name} · ${media.mimeType}` : name;
    media.note.textContent = "";
  }
  media.loadedTaskId = media.taskId;
}

async function loadExecutionOutput(row, force = false) {
  const stream = row._dashboardRefs?.stream;
  const taskId = row.dataset.taskId;
  const outputId = row.dataset.streamOutputId;
  if (!row.open || !stream || !taskId || !outputId || executionOutputRequests.has(taskId)) return;

  const cached = executionOutputCache.get(taskId);
  if (cached !== undefined && !force) {
    updateScrollableText(stream.output, cached, { defaultToBottom: true, live: true });
    return;
  }
  if (cached === undefined) updateScrollableText(stream.output, "Loading…", { defaultToBottom: true, live: true });

  executionOutputRequests.add(taskId);
  try {
    const data = await getJson(`/executions/${encodeURIComponent(taskId)}/output`);
    if (data.output_id !== outputId) {
      throw new Error("Execution output changed identity");
    }
    const text = data.output || "No output captured yet.";
    executionOutputCache.set(taskId, text);
    updateScrollableText(stream.output, text, { defaultToBottom: true, live: true });
  } catch (error) {
    updateScrollableText(stream.output, `Could not load live output: ${error.message}`, { defaultToBottom: true });
  } finally {
    executionOutputRequests.delete(taskId);
  }
}

function createExecutionRow() {
  const details = makeElement("details", "execution-entry");
  const summary = makeElement("summary", "execution-summary");
  const title = makeElement("div", "activity-title execution-title mono");
  const metaRow = makeElement("div", "execution-meta-row");
  const detail = makeElement("span", "activity-subtitle execution-detail");
  const badge = makeElement("span", "status-badge");
  metaRow.append(detail, badge);
  const submitted = createActivityTimeItem("execution-submitted");
  const elapsed = createActivityTimeItem("execution-elapsed");
  summary.append(title, submitted.item, metaRow, elapsed.item);

  const body = makeElement("div", "execution-body");
  const parameters = createOutputSection("Parameters", "execution-parameters");
  const stream = createOutputSection("Live output", "execution-stream");
  const result = createOutputSection("Result", "execution-result");
  const error = createOutputSection("Error", "execution-error");
  const media = createMediaSection();
  body.append(parameters.section, stream.section, result.section, error.section, media.section);
  details.append(summary, body);

  details._dashboardRefs = {
    title,
    detail,
    badge,
    submitted: submitted.valueNode,
    elapsed: elapsed.valueNode,
    body,
    parameters,
    stream,
    result,
    error,
    media,
  };
  summary.addEventListener("click", (event) => {
    event.preventDefault();
    const key = details.dataset.itemKey;
    const nextOpen = !details.open;
    details.open = nextOpen;
    if (key) {
      if (nextOpen) expandedExecutionKeys.add(key);
      else expandedExecutionKeys.delete(key);
    }
    if (nextOpen) {
      loadExecutionOutput(details, true);
      loadExecutionMedia(details);
    }
  });
  return details;
}

function updateExecutionSection(sectionRefs, value) {
  const visible = Boolean(value);
  const hidden = !visible;
  if (sectionRefs.section.hidden !== hidden) sectionRefs.section.hidden = hidden;
  if (!visible) return;

  const text = normaliseOutputText(value);
  if (sectionRefs.output.textContent !== text) sectionRefs.output.textContent = text;
}

function updateExecutionRow(row, execution) {
  const refs = row._dashboardRefs;
  const key = String(execution.task_id);
  const shouldOpen = expandedExecutionKeys.has(key);
  if (row.open !== shouldOpen) row.open = shouldOpen;

  const snapshot = JSON.stringify([
    execution.name,
    execution.status,
    execution.project || null,
    execution.detail || null,
    execution.submitted_at ?? null,
    execution.elapsed_seconds ?? null,
    execution.parameters,
    execution.result,
    execution.error,
    execution.stream_output_id || null,
    execution.stream_output_chars ?? null,
    execution.media?.type || null,
    execution.media?.name || null,
    execution.media?.mime_type || null,
  ]);
  if (row._dashboardSnapshot === snapshot) return;
  row._dashboardSnapshot = snapshot;

  const executionTitle = executionDisplayName(execution.name);
  const executionDetailParts = [execution.detail].filter(Boolean);
  if (execution.project && execution.project !== execution.detail) executionDetailParts.push(execution.project);
  const executionDetail = executionDetailParts.join(" · ");
  setNodeText(refs.title, executionTitle);
  setNodeText(refs.detail, executionDetail || "—");
  updateStatusBadge(refs.badge, execution.status);
  setNodeText(refs.submitted, formatEpochClock(execution.submitted_at));
  setNodeText(refs.elapsed, formatDuration(execution.elapsed_seconds));
  updateExecutionSection(refs.parameters, execution.parameters);

  const taskId = String(execution.task_id);
  const streamOutputId = execution.stream_output_id || "";
  row.dataset.taskId = taskId;
  if (streamOutputId) {
    if (row.dataset.streamOutputId !== streamOutputId) executionOutputCache.delete(taskId);
    row.dataset.streamOutputId = streamOutputId;
    refs.stream.section.hidden = false;
    if (!refs.stream.output.textContent) refs.stream.output.textContent = "Open the tool call to load output.";
  } else {
    delete row.dataset.streamOutputId;
    refs.stream.section.hidden = true;
    executionOutputCache.delete(taskId);
  }

  const mediaType = execution.media?.type || null;
  refs.media.section.hidden = !mediaType;
  if (mediaType) {
    refs.media.taskId = execution.task_id;
    refs.media.mediaType = mediaType;
    refs.media.fileName = execution.media?.name || null;
    refs.media.mimeType = execution.media?.mime_type || null;
    if (refs.media.loadedTaskId !== execution.task_id) {
      refs.media.note.textContent = "Open the tool call to load preview.";
      refs.media.image.removeAttribute("src");
      refs.media.imageLink.removeAttribute("href");
      refs.media.audio.pause();
      refs.media.audio.removeAttribute("src");
      refs.media.file.removeAttribute("href");
      refs.media.file.textContent = "";
      refs.media.imageLink.hidden = true;
      refs.media.audio.hidden = true;
      refs.media.file.hidden = true;
    }
  } else {
    refs.media.taskId = null;
    refs.media.mediaType = null;
    refs.media.fileName = null;
    refs.media.mimeType = null;
    refs.media.loadedTaskId = null;
  }

  updateExecutionSection(refs.result, mediaType || streamOutputId ? null : execution.result);
  updateExecutionSection(refs.error, execution.error);
  refs.body.hidden =
    !execution.parameters && !streamOutputId && !execution.result && !execution.error && !mediaType;
  if (streamOutputId && row.open) loadExecutionOutput(row, true);
  if (mediaType && row.open) loadExecutionMedia(row);
}

function renderExecutionsNow(data, snapshot) {
  setText("execution-running", data.running || 0);
  setText("execution-queued", data.queued || 0);
  setText("execution-done", data.done || 0);

  const list = byId("executions-list");
  const executions = data.executions || [];
  const viewportAnchor = captureExecutionViewportAnchor(list);
  setText("tools-tab-count", executions.length);
  if (!executions.length) {
    if (!list.querySelector(".empty-card")) clearAndAppend(list, [makeElement("div", "empty-card", "No tool executions recorded yet.")]);
    executionRenderState.snapshot = snapshot;
    return;
  }

  reconcileKeyed(list, executions, (execution) => execution.task_id, createExecutionRow, updateExecutionRow);
  Array.from(list.children).forEach((row) => {
    const key = row.dataset.itemKey;
    if (!key) return;
    const shouldOpen = expandedExecutionKeys.has(key);
    if (row.open !== shouldOpen) row.open = shouldOpen;
    if (shouldOpen) {
      loadExecutionOutput(row, true);
      loadExecutionMedia(row);
    }
  });
  restoreExecutionViewportAnchor(viewportAnchor);
  executionRenderState.snapshot = snapshot;
}

function renderExecutions(data) {
  const snapshot = JSON.stringify([
    data.running || 0,
    data.queued || 0,
    data.done || 0,
    data.executions || [],
  ]);
  if (snapshot === executionRenderState.snapshot || snapshot === executionRenderState.pending?.snapshot) return;

  const recentlyScrolled = performance.now() - executionRenderState.lastScrollAt < SCROLL_IDLE_MS;
  if (recentlyScrolled) {
    executionRenderState.pending = { data, snapshot };
    schedulePendingExecutions();
    return;
  }

  executionRenderState.pending = null;
  renderExecutionsNow(data, snapshot);
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

function createActivityTimeItem(className) {
  const item = makeElement("div", `activity-time-item ${className}`);
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

  const submitted = createActivityTimeItem("job-submitted");
  const elapsed = createActivityTimeItem("job-elapsed");
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
    const running = [...(state?.calls || []), ...(state?.jobs || [])].filter(entry => entry.status === "running").length;
    const failed = [...(state?.calls || []), ...(state?.jobs || [])].filter(entry => entry.status === "failed" || entry.status === "timed_out").length;
    const toolCount = Number.isFinite(state?.tool_count) ? state.tool_count : (state?.calls || []).length;
    const jobCount = Number.isFinite(state?.job_count) ? state.job_count : (state?.jobs || []).length;
    return {
      label: "Serena",
      icon: dashboardAssetUrl("serena-logo.svg"),
      stats: `${toolCount} tool${toolCount === 1 ? "" : "s"} · ${jobCount} job${jobCount === 1 ? "" : "s"} · ${state?.project_name || panel.project_name || "no project"}`,
      status: running ? `${running} running` : failed ? `${failed} failed` : toolCount + jobCount ? "Complete" : "Idle",
      statusClass: running ? "running" : failed ? "failed" : "",
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

window.addEventListener("message", event => {
  if (event.origin !== location.origin) return;
  const frame = Array.from(document.querySelectorAll(".activity-widget-frame")).find(candidate => candidate.contentWindow === event.source);
  if (!frame) return;
  if (event.data?.type === "serena-dashboard-widget-ready") {
    const preview = frame.parentElement?.querySelector(".activity-widget-preview");
    if (preview) preview.hidden = true;
    requestAnimationFrame(() => frame.classList.add("ready"));
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
    renderOverview(session);
    renderSessionWidgets("serena-widgets", "serena-panel-count", serena.panels || [], "serena");
    renderSessionWidgets("orchestrator-widgets", "orchestrator-panel-count", orchestrator.panels || [], "orchestrator");
    latestPanelActivity = [...(serena.panels || []), ...(orchestrator.panels || [])].some(panel => panel.active);
    initialActivityStateLoaded = true;

    if (!initialActivityViewSelected) {
      const orchestratorActive = (orchestrator.panels || []).some(panel => panel.active);
      if (isTabbedActivityMode() && orchestratorActive) activateActivityView("orchestrator");
      initialActivityViewSelected = true;
    }
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
setupMemoryDialog();
setupActivityViewTabs();
refresh();
