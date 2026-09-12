"use strict";

const API_PREFIX = "/dashboard/api";
const ACTIVE_POLL_INTERVAL_MS = 2000;
const IDLE_POLL_INTERVAL_MS = 10000;
const HIDDEN_POLL_INTERVAL_MS = 60000;

const DASHBOARD_ASSET_VERSION = document.documentElement.dataset.assetVersion || "";

let currentRoute = initialRoute();
let activeOverviewView = currentRoute.kind === "orchestrator" ? "orchestrator" : "serena";
let currentDocument = null;
let currentEtag = null;
let routeGeneration = 0;
let refreshInFlight = false;
let refreshRequested = false;
let refreshTimer = null;
let clockTimer = null;
let latestOverview = null;
let latestOverviewEtag = null;
let latestTools = [];
let latestJobs = { jobs: [], running_jobs: 0, max_concurrent_jobs: 0 };
let visibleActivityPanels = [];
let serenaOverviewPanels = new Map();
let visibleElapsedNodes = [];
let selectedSerenaPanel = null;
let selectedSerenaRoot = null;
let pendingNotificationTarget = currentRoute.notificationTarget;
let changeStream = null;
let changeStreamConnected = false;

function byId(id) {
  return document.getElementById(id);
}

function initialRoute() {
  const params = new URLSearchParams(window.location.search);
  const panelId = params.get("panel");
  const orchestratorId = params.get("orchestrator");
  const jobId = params.get("job");
  if (panelId) {
    return {
      kind: "serena",
      panelId,
      expandedEntryId: jobId || null,
      notificationTarget: jobId ? { panelId, jobId } : null,
    };
  }
  if (orchestratorId) {
    return { kind: "orchestrator", panelId: orchestratorId, expandedEntryId: null, notificationTarget: null };
  }
  return { kind: "overview", notificationTarget: null };
}

function routePath(route = currentRoute) {
  if (route.kind === "serena") {
    const base = `/serena/sessions/${encodeURIComponent(route.panelId)}`;
    return route.expandedEntryId ? `${base}?expanded=${encodeURIComponent(route.expandedEntryId)}` : base;
  }
  if (route.kind === "orchestrator") {
    const base = `/orchestrator/sessions/${encodeURIComponent(route.panelId)}`;
    return route.expandedEntryId ? `${base}?expanded=${encodeURIComponent(route.expandedEntryId)}` : base;
  }
  return "/state";
}

function dashboardAssetUrl(name) {
  return `/dashboard/${name}${DASHBOARD_ASSET_VERSION ? `?v=${encodeURIComponent(DASHBOARD_ASSET_VERSION)}` : ""}`;
}

function setText(id, value, fallback = "—") {
  const node = byId(id);
  if (node) node.textContent = value === null || value === undefined || value === "" ? fallback : String(value);
}

function setConnection(state, label) {
  const node = byId("connection-state");
  if (!node) return;
  node.dataset.state = state;
  setText("connection-label", label, "Connected");
  if (state === "connected") {
    setText("last-update", new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }), "");
  }
}

function isSerenaActive(documentState) {
  return [...(documentState?.calls || []), ...(documentState?.jobs || [])]
    .some(item => ["starting", "running"].includes(String(item?.status || "").toLowerCase()));
}

function isOrchestratorActive(documentState) {
  const activeStates = new Set(["WAITING_FOR_CHAT", "QUEUED", "RUNNING_CHAT", "RUNNING_CODEX"]);
  return Boolean(documentState?.active) || (documentState?.delegates || []).some(item => activeStates.has(String(item?.state || "")));
}

function documentHasActiveWork(documentState = currentDocument) {
  if (!documentState) return false;
  if (currentRoute.kind === "serena") return isSerenaActive(documentState);
  if (currentRoute.kind === "orchestrator") return isOrchestratorActive(documentState);
  const serenaActive = (documentState?.serena?.panels || []).some(panel => panel.active);
  const orchestratorActive = (documentState?.orchestrator?.panels || []).some(panel => panel.active);
  return serenaActive || orchestratorActive || Number(documentState?.jobs?.running_jobs || 0) > 0;
}

function hasExpandedRunningJob() {
  if (currentRoute.kind !== "serena" || !currentRoute.expandedEntryId) return false;
  return (currentDocument?.jobs || []).some(
    job => job.job_id === currentRoute.expandedEntryId && ["starting", "running"].includes(job.status),
  );
}

function nextRefreshDelay() {
  if (document.hidden) return HIDDEN_POLL_INTERVAL_MS;
  if (hasExpandedRunningJob()) return ACTIVE_POLL_INTERVAL_MS;
  if (changeStreamConnected) return HIDDEN_POLL_INTERVAL_MS;
  if (currentRoute.kind !== "overview") return ACTIVE_POLL_INTERVAL_MS;
  return documentHasActiveWork() ? ACTIVE_POLL_INTERVAL_MS : IDLE_POLL_INTERVAL_MS;
}

function scheduleRefresh(delay = nextRefreshDelay()) {
  if (refreshTimer !== null) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    void refresh();
  }, delay);
}

function setupChangeStream() {
  if (!("EventSource" in window)) return;
  const source = new EventSource(`${API_PREFIX}/events`);
  changeStream = source;
  source.addEventListener("open", () => {
    changeStreamConnected = true;
    scheduleRefresh(0);
  });
  source.addEventListener("invalidate", () => {
    scheduleRefresh(0);
  });
  source.addEventListener("error", () => {
    changeStreamConnected = false;
    scheduleRefresh();
  });
}

class DashboardHttpError extends Error {
  constructor(status, statusText) {
    super(`${status} ${statusText}`);
    this.name = "DashboardHttpError";
    this.status = status;
  }
}

function recoverRouteError(error) {
  if (!(error instanceof DashboardHttpError) || currentRoute.kind === "overview") return false;

  if (error.status === 400 && currentRoute.expandedEntryId) {
    pendingNotificationTarget = null;
    navigate({ ...currentRoute, expandedEntryId: null, notificationTarget: null }, { replace: true });
    return true;
  }

  if (error.status === 404) {
    pendingNotificationTarget = null;
    navigate({ kind: "overview", notificationTarget: null }, { replace: true });
    return true;
  }

  return false;
}

async function fetchCurrentDocument(path, etag) {
  const headers = { Accept: "application/json" };
  if (etag) headers["If-None-Match"] = etag;
  const response = await fetch(`${API_PREFIX}${path}`, { cache: etag ? "no-cache" : "no-store", headers });
  if (response.status === 304) {
    if (!etag) throw new Error("304 response without a reusable dashboard document");
    return { unchanged: true, etag };
  }
  if (!response.ok) throw new DashboardHttpError(response.status, response.statusText);
  const data = await response.json();
  if (data?.status === "error") throw new Error(data.message || "Serena API error");
  return { unchanged: false, etag: response.headers.get("ETag"), data };
}

async function refresh() {
  if (refreshInFlight) {
    refreshRequested = true;
    return;
  }
  if (refreshTimer !== null) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }

  refreshInFlight = true;
  const generation = routeGeneration;
  const path = routePath();
  const requestEtag = hasExpandedRunningJob() ? null : currentEtag;
  const overviewRequest = currentRoute.kind !== "overview" && latestOverview === null
    ? fetchCurrentDocument("/state", null).catch(error => {
        console.warn("Dashboard overview context fetch failed", error);
        return null;
      })
    : Promise.resolve(null);
  try {
    const [response, overviewResponse] = await Promise.all([
      fetchCurrentDocument(path, requestEtag),
      overviewRequest,
    ]);
    if (generation !== routeGeneration) return;
    if (overviewResponse && !overviewResponse.unchanged) {
      latestOverview = overviewResponse.data;
      latestOverviewEtag = overviewResponse.etag || null;
      renderOverviewMetadata(latestOverview?.session || {}, latestOverview?.jobs || {});
    }
    currentEtag = response.etag || null;
    if (!response.unchanged) {
      const preserveViewport = currentDocument !== null;
      currentDocument = response.data;
      if (currentRoute.kind === "overview") {
        latestOverview = response.data;
        latestOverviewEtag = currentEtag;
      } else if (currentRoute.kind === "serena" && response.data?.dashboard_jobs) {
        renderOverviewMetadata(latestOverview?.session || {}, response.data.dashboard_jobs);
      }
      renderCurrentDocument({ preserveViewport });
    }
    setConnection("connected", "Connected");
  } catch (error) {
    if (generation === routeGeneration) {
      if (recoverRouteError(error)) {
        setConnection("connected", "Connected");
      } else {
        console.error("Dashboard refresh failed", error);
        setConnection("error", "Disconnected");
      }
    }
  } finally {
    refreshInFlight = false;
    if (generation !== routeGeneration || refreshRequested) {
      refreshRequested = false;
      void refresh();
    } else {
      scheduleRefresh();
    }
  }
}

function navigate(route, { replace = false, preserveOverviewDom = false } = {}) {
  const openingPromotedSerenaPanel = Boolean(
    route.kind === "serena"
    && selectedSerenaPanel
    && selectedSerenaRoot?.dataset.panelId === route.panelId
  );

  currentRoute = { ...route, notificationTarget: null };
  routeGeneration += 1;
  currentDocument = null;
  currentEtag = currentRoute.kind === "overview" ? latestOverviewEtag : null;

  const url = new URL(window.location.href);
  url.search = "";
  if (currentRoute.kind === "serena") url.searchParams.set("panel", currentRoute.panelId);
  if (currentRoute.kind === "orchestrator") url.searchParams.set("orchestrator", currentRoute.panelId);
  if (replace) history.replaceState(null, "", url);
  else history.pushState(null, "", url);

  if (currentRoute.kind === "serena") activeOverviewView = "serena";
  if (currentRoute.kind === "orchestrator") activeOverviewView = "orchestrator";
  applyActivityView();

  if (currentRoute.kind === "overview" && latestOverview) {
    currentDocument = latestOverview;
    if (!preserveOverviewDom) renderCurrentDocument();
  } else if (!openingPromotedSerenaPanel) {
    renderLoadingRoute();
  }
  void refresh();
}

function returnToOverview() {
  const panelId = currentRoute.kind === "serena" ? currentRoute.panelId : null;
  const summary = latestOverview?.serena?.panels?.find(panel => panel.panel_id === panelId) || null;
  if (panelId && summary && selectedSerenaPanel && selectedSerenaRoot?.dataset.panelId === panelId) {
    selectedSerenaPanel.demote();
    selectedSerenaPanel.render(activitySummarySnapshot(summary));
    selectedSerenaPanel = null;
    selectedSerenaRoot = null;
    navigate({ kind: "overview" }, { preserveOverviewDom: true });
    return;
  }
  navigate({ kind: "overview" });
}

function renderLoadingRoute() {
  visibleActivityPanels = [];
  visibleElapsedNodes = [];
  selectedSerenaPanel = null;
  selectedSerenaRoot = null;
  const targetIds = currentRoute.kind === "overview"
    ? ["serena-widgets", "orchestrator-widgets"]
    : [currentRoute.kind === "orchestrator" ? "orchestrator-widgets" : "serena-widgets"];
  for (const targetId of targetIds) {
    const target = byId(targetId);
    if (!target) continue;
    target.replaceChildren();
    const loading = document.createElement("div");
    loading.className = "empty-card";
    loading.textContent = "Loading…";
    target.append(loading);
  }
}

function restoreViewport(viewport) {
  if (!viewport) return;
  const generation = routeGeneration;
  const restore = () => {
    if (generation === routeGeneration) window.scrollTo(viewport.x, viewport.y);
  };
  restore();
  requestAnimationFrame(() => {
    restore();
    requestAnimationFrame(restore);
  });
}

function renderCurrentDocument({ preserveViewport = false } = {}) {
  const viewport = preserveViewport ? { x: window.scrollX, y: window.scrollY } : null;
  visibleElapsedNodes = [];
  if (currentRoute.kind === "serena") renderSelectedSerena(currentDocument);
  else if (currentRoute.kind === "orchestrator") renderSelectedOrchestrator(currentDocument);
  else renderOverview(currentDocument);
  restoreViewport(viewport);
  startClock();
}

function renderOverview(state) {
  latestOverview = state || {};
  renderOverviewMetadata(state?.session || {}, state?.jobs || {});
  renderSerenaOverview(state?.serena?.panels || []);
  renderOrchestratorOverview(state?.orchestrator?.panels || []);
  applyActivityView();
}

function renderOverviewMetadata(session, jobs) {
  setText("runtime-policy", session.runtime_policy, "ChatGPT");
  setText("version", session.serena_version, "—");
  latestTools = session.active_tools || [];
  latestJobs = jobs || { jobs: [], running_jobs: 0, max_concurrent_jobs: 0 };
  setText("tool-count", latestTools.length);
  setText("running-jobs-count", latestJobs.running_jobs || 0);
  setText("max-jobs-count", latestJobs.max_concurrent_jobs || 0);
  byId("tools-button")?.setAttribute("aria-label", `${latestTools.length} active tools`);
  byId("jobs-button")?.setAttribute("aria-label", `${latestJobs.running_jobs || 0} of ${latestJobs.max_concurrent_jobs || 0} Serena jobs running`);
  if (byId("jobs-dialog")?.open) renderRunningJobs();
}

function activitySummarySnapshot(panel) {
  return {
    ...panel,
    run_id: null,
    session_title: panel.display_name || panel.session_title || "Serena",
  };
}

function openSerenaSummaryPanel(panel, root, panelId) {
  if (selectedSerenaPanel && selectedSerenaPanel !== panel) {
    const previousPanelId = selectedSerenaRoot?.dataset.panelId || null;
    const previousSummary = latestOverview?.serena?.panels?.find(item => item.panel_id === previousPanelId) || null;
    selectedSerenaPanel.demote();
    if (previousSummary) selectedSerenaPanel.render(activitySummarySnapshot(previousSummary));
  }

  selectedSerenaPanel = panel;
  selectedSerenaRoot = root;
  navigate({ kind: "serena", panelId, expandedEntryId: null });
}

function sessionDay(timestampSeconds) {
  const seconds = Number(timestampSeconds);
  if (!Number.isFinite(seconds)) return null;

  const date = new Date(seconds * 1000);
  if (!Number.isFinite(date.getTime())) return null;

  return {
    date,
    key: `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`,
  };
}

function sessionDayLabel(date) {
  const day = date.getDate();
  const mod100 = day % 100;
  const suffix = mod100 >= 11 && mod100 <= 13
    ? "th"
    : ({ 1: "st", 2: "nd", 3: "rd" }[day % 10] || "th");
  const weekday = date.toLocaleDateString("en-GB", { weekday: "long" });
  const month = date.toLocaleDateString("en-GB", { month: "long" });
  const year = date.getFullYear();
  const yearSuffix = year === new Date().getFullYear() ? "" : ` ${year}`;

  return `${weekday} ${day}${suffix} ${month}${yearSuffix}`;
}

function renderSerenaOverview(panels) {
  const container = byId("serena-widgets");
  if (!container) return;
  visibleActivityPanels = [];
  selectedSerenaPanel = null;
  selectedSerenaRoot = null;

  if (!panels.length) {
    for (const { panel } of serenaOverviewPanels.values()) panel.destroy();
    serenaOverviewPanels = new Map();
    container.replaceChildren(emptyCard("No Serena session activity recorded yet."));
    return;
  }

  const nextPanels = new Map();
  const desiredNodes = [];
  let previousDayKey = null;
  for (const summary of panels) {
    const day = sessionDay(summary.started_at);
    if (day && previousDayKey !== null && day.key !== previousDayKey) {
      const separator = document.createElement("div");
      separator.className = "session-day-separator";
      separator.textContent = sessionDayLabel(day.date);
      desiredNodes.push(separator);
    }
    if (day) previousDayKey = day.key;

    const snapshot = activitySummarySnapshot(summary);
    const existing = serenaOverviewPanels.get(summary.panel_id);
    let root = existing?.root || null;
    let panel = existing?.panel || null;
    if (!root || !panel) {
      root = document.createElement("div");
      root.dataset.panelId = summary.panel_id;
      panel = new window.SerenaActivity.ActivityPanel(root, {
        initialCollapsed: true,
        summaryMode: true,
        onOpen: () => openSerenaSummaryPanel(panel, root, summary.panel_id),
      });
    } else {
      panel.demote();
    }
    panel.render(snapshot);
    nextPanels.set(summary.panel_id, { root, panel });
    desiredNodes.push(root);
  }

  for (const [panelId, entry] of serenaOverviewPanels) {
    if (!nextPanels.has(panelId)) entry.panel.destroy();
  }
  serenaOverviewPanels = nextPanels;

  let cursor = container.firstChild;
  for (const node of desiredNodes) {
    if (node === cursor) {
      cursor = cursor.nextSibling;
      continue;
    }
    container.insertBefore(node, cursor);
  }
  while (cursor) {
    const next = cursor.nextSibling;
    cursor.remove();
    cursor = next;
  }
}

function selectedBackButton(label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "activity-back-button";
  button.textContent = `‹ ${label}`;
  button.addEventListener("click", returnToOverview);
  return button;
}

function selectedSerenaPanelOptions() {
  return {
    onCollapse: returnToOverview,
    onExpandedChange: entryId => {
      currentRoute = { ...currentRoute, expandedEntryId: entryId || null };
      routeGeneration += 1;
      currentEtag = null;
      void refresh();
    },
    loadMedia: loadDashboardMedia,
  };
}

function renderSelectedSerena(snapshot) {
  const container = byId("serena-widgets");
  if (!container || !snapshot) return;
  const summaries = latestOverview?.serena?.panels || [];
  applyActivityView("serena");

  if (!selectedSerenaRoot || !selectedSerenaPanel || selectedSerenaRoot.dataset.panelId !== currentRoute.panelId) {
    if (selectedSerenaPanel && selectedSerenaRoot?.dataset.panelId !== currentRoute.panelId) {
      const previousPanelId = selectedSerenaRoot?.dataset.panelId || null;
      const previousSummary = summaries.find(item => item.panel_id === previousPanelId) || null;
      selectedSerenaPanel.demote();
      if (previousSummary) selectedSerenaPanel.render(activitySummarySnapshot(previousSummary));
    }

    let entry = serenaOverviewPanels.get(currentRoute.panelId) || null;
    if (!entry && summaries.length) {
      renderSerenaOverview(summaries);
      entry = serenaOverviewPanels.get(currentRoute.panelId) || null;
    }

    if (entry) {
      selectedSerenaRoot = entry.root;
      selectedSerenaPanel = entry.panel;
    } else {
      selectedSerenaRoot = document.createElement("div");
      selectedSerenaRoot.dataset.panelId = currentRoute.panelId;
      container.append(selectedSerenaRoot);
      selectedSerenaPanel = new window.SerenaActivity.ActivityPanel(selectedSerenaRoot, {
        initialCollapsed: false,
        expandedEntryId: currentRoute.expandedEntryId,
        ...selectedSerenaPanelOptions(),
      });
      serenaOverviewPanels.set(currentRoute.panelId, { root: selectedSerenaRoot, panel: selectedSerenaPanel });
    }
  }

  selectedSerenaPanel.promote(selectedSerenaPanelOptions());
  selectedSerenaPanel.setExpandedEntryId(currentRoute.expandedEntryId);
  selectedSerenaPanel.render(snapshot);
  visibleActivityPanels = [selectedSerenaPanel];
  consumeNotificationTarget(snapshot);
}

async function loadDashboardMedia(callId, media) {
  const response = await fetch(`${API_PREFIX}/serena/sessions/${encodeURIComponent(currentRoute.panelId)}/media/${encodeURIComponent(callId)}`, {
    cache: "force-cache",
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  return {
    type: media?.media_type || (blob.type.startsWith("image/") ? "image" : blob.type.startsWith("audio/") ? "audio" : "file"),
    src: url,
    name: media?.name,
    dispose: () => URL.revokeObjectURL(url),
  };
}

function setOrchestratorSectionDetail(selected, title = "") {
  const titleNode = byId("orchestrator-title");
  const noteNode = titleNode?.parentElement?.querySelector(".section-note");
  if (!titleNode || !noteNode) return;
  titleNode.textContent = selected ? title : "Orchestrator";
  noteNode.textContent = selected ? "Selected retained orchestration" : "Retained orchestrations";
}

function emptyCard(message) {
  const node = document.createElement("div");
  node.className = "empty-card";
  node.textContent = message;
  return node;
}

function delegateTime(value) {
  const parsed = Date.parse(value || "");
  if (!Number.isFinite(parsed)) return "";
  return new Date(parsed).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function delegateIsActive(delegate) {
  return ["WAITING_FOR_CHAT", "QUEUED", "RUNNING_CHAT", "RUNNING_CODEX"].includes(String(delegate?.state || ""));
}

function delegateStatusLabel(state) {
  return String(state || "").toLowerCase().replaceAll("_", " ");
}

function renderOrchestratorOverview(panels) {
  const container = byId("orchestrator-widgets");
  if (!container) return;
  container.replaceChildren();
  setText("orchestrator-panel-count", panels.length, "0");
  setOrchestratorSectionDetail(false);
  if (!panels.length) {
    container.append(emptyCard("No orchestration activity recorded yet."));
    return;
  }

  for (const panel of panels) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "orchestrator-session-card";
    const copy = document.createElement("span");
    copy.className = "orchestrator-session-copy";
    const title = document.createElement("strong");
    title.textContent = panel.display_name || "Orchestrator";
    const summary = document.createElement("span");
    const delegateCount = Number(panel.delegate_count || 0);
    const activeCount = Number(panel.active_count || 0);
    summary.textContent = `${delegateCount} ${delegateCount === 1 ? "delegate" : "delegates"}${activeCount ? ` · ${activeCount} active` : ""}`;
    copy.append(title, summary);
    const chevron = document.createElement("span");
    chevron.textContent = "›";
    chevron.className = "orchestrator-chevron";
    button.append(copy, chevron);
    button.addEventListener("click", () => navigate({ kind: "orchestrator", panelId: panel.panel_id, expandedEntryId: null }));
    container.append(button);
  }
}

function renderSelectedOrchestrator(documentState) {
  const container = byId("orchestrator-widgets");
  if (!container || !documentState) return;
  container.replaceChildren();
  visibleActivityPanels = [];
  setOrchestratorSectionDetail(true, documentState.display_name || "Orchestrator");
  setText("orchestrator-panel-count", "1", "1");
  applyActivityView("orchestrator");
  container.append(selectedBackButton("All orchestrations"));

  const list = document.createElement("div");
  list.className = "orchestrator-delegate-list";
  const delegates = documentState.delegates || [];
  if (!delegates.length) list.append(emptyCard("No delegates in this orchestration."));

  for (const delegate of delegates) {
    const row = document.createElement("div");
    row.className = "orchestrator-delegate-row";
    row.dataset.active = delegateIsActive(delegate) ? "true" : "false";
    const main = document.createElement("button");
    main.type = "button";
    main.className = "orchestrator-delegate-main";
    main.setAttribute("aria-expanded", String(currentRoute.expandedEntryId === delegate.delegate_id));

    const status = document.createElement("span");
    status.className = "orchestrator-status";
    const copy = document.createElement("span");
    copy.className = "orchestrator-delegate-copy";
    const heading = document.createElement("span");
    heading.className = "orchestrator-delegate-heading";
    const name = document.createElement("strong");
    name.textContent = delegate.kind || "delegate";
    const project = document.createElement("span");
    project.textContent = delegate.project_name || "";
    heading.append(name, project);
    const detail = document.createElement("span");
    detail.textContent = delegate.active_provider || delegate.provider_policy || delegateStatusLabel(delegate.state);
    copy.append(heading, detail);
    const timing = document.createElement("span");
    timing.className = "orchestrator-delegate-time";
    const submitted = document.createElement("span");
    submitted.textContent = delegateTime(delegate.started_at || delegate.created_at);
    const elapsed = document.createElement("span");
    const startedAt = Date.parse(delegate.started_at || delegate.created_at || "") / 1000;
    const finishedAt = Date.parse(delegate.finished_at || "") / 1000;
    if (Number.isFinite(startedAt)) {
      if (delegateIsActive(delegate)) visibleElapsedNodes.push({ node: elapsed, startedAt });
      else if (Number.isFinite(finishedAt)) elapsed.textContent = window.SerenaActivity.formatDuration(finishedAt - startedAt);
    }
    timing.append(submitted, elapsed);
    const chevron = document.createElement("span");
    chevron.textContent = currentRoute.expandedEntryId === delegate.delegate_id ? "⌄" : "›";
    main.append(status, copy, timing, chevron);
    main.addEventListener("click", () => {
      const next = currentRoute.expandedEntryId === delegate.delegate_id ? null : delegate.delegate_id;
      currentRoute = { ...currentRoute, expandedEntryId: next };
      routeGeneration += 1;
      currentEtag = null;
      renderSelectedOrchestrator({ ...currentDocument, expanded_delegate: null });
      void refresh();
    });
    row.append(main);

    if (String(delegate.state || "") === "WAITING_FOR_CHAT") {
      const action = document.createElement("button");
      action.type = "button";
      action.className = "orchestrator-copy-prompt";
      action.textContent = "Copy launch prompt";
      action.addEventListener("click", async () => {
        const prompt = `@Orchestrator claim delegate ${delegate.delegate_id} and complete it independently.`;
        try {
          await navigator.clipboard.writeText(prompt);
          action.textContent = "Copied";
        } catch (_) {
          action.textContent = prompt;
        }
      });
      row.append(action);
    }

    if (currentRoute.expandedEntryId === delegate.delegate_id) {
      row.append(renderDelegateDetail(documentState.expanded_delegate, delegate.delegate_id));
    }
    list.append(row);
  }
  container.append(list);
}

function renderDelegateDetail(detail, expectedId) {
  const container = document.createElement("div");
  container.className = "orchestrator-delegate-detail";
  if (!detail || detail.delegate_id !== expectedId) {
    container.textContent = "Loading…";
    return container;
  }
  const fields = [
    ["Goal", detail.goal],
    ["State", delegateStatusLabel(detail.state)],
    ["Provider", detail.active_provider || detail.provider_policy],
    ["Error", detail.error],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");
  for (const [label, value] of fields) {
    const section = document.createElement("section");
    const heading = document.createElement("h4");
    heading.textContent = label;
    const content = document.createElement(typeof value === "string" && value.includes("\n") ? "pre" : "div");
    content.textContent = String(value);
    section.append(heading, content);
    container.append(section);
  }
  if (detail.provider_metadata && Object.keys(detail.provider_metadata).length) {
    const section = document.createElement("section");
    const heading = document.createElement("h4");
    heading.textContent = "Provider metadata";
    section.append(heading, window.SerenaActivity.renderValue(detail.provider_metadata));
    container.append(section);
  }
  return container;
}

function applyActivityView(forceView = null) {
  if (forceView) activeOverviewView = forceView;
  const columns = byId("activity-columns");
  if (columns) {
    columns.dataset.activeView = activeOverviewView;
    columns.dataset.route = currentRoute.kind;
  }
  for (const button of document.querySelectorAll("[data-activity-view-tab]")) {
    const active = button.dataset.activityViewTab === activeOverviewView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  }
}

function setupActivityViewTabs() {
  const buttons = Array.from(document.querySelectorAll("[data-activity-view-tab]"));
  for (const [index, button] of buttons.entries()) {
    button.addEventListener("click", () => {
      const view = button.dataset.activityViewTab;
      if (view !== "serena" && view !== "orchestrator") return;
      if (currentRoute.kind !== "overview") returnToOverview();
      activeOverviewView = view;
      applyActivityView();
    });
    button.addEventListener("keydown", event => {
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = buttons.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      buttons[nextIndex]?.focus();
      buttons[nextIndex]?.click();
    });
  }
}

function consumeNotificationTarget(snapshot) {
  if (!pendingNotificationTarget || currentRoute.kind !== "serena") return;
  if (snapshot?.panel_id !== pendingNotificationTarget.panelId) return;
  const targetId = pendingNotificationTarget.jobId;
  const found = (snapshot?.jobs || []).some(job => job.job_id === targetId);
  if (!found) return;
  pendingNotificationTarget = null;
  const url = new URL(window.location.href);
  url.search = "";
  url.searchParams.set("panel", currentRoute.panelId);
  history.replaceState(null, "", url);
  requestAnimationFrame(() => {
    const row = selectedSerenaRoot?.querySelector(`[data-entry-id="${CSS.escape(targetId)}"]`);
    row?.scrollIntoView({ block: "center", behavior: "smooth" });
  });
}

function renderRunningJobs() {
  const container = byId("jobs-dialog-content");
  if (!container) return;
  container.replaceChildren();
  const jobs = latestJobs.jobs || [];
  if (!jobs.length) {
    container.append(emptyCard("No Serena jobs are currently running."));
    return;
  }
  for (const job of jobs) {
    const row = document.createElement(job.panel_id ? "button" : "div");
    if (job.panel_id) row.type = "button";
    row.className = job.panel_id ? "resource-row resource-row-button" : "resource-row";
    const label = document.createElement("strong");
    label.textContent = job.label || job.job_id;
    const meta = document.createElement("span");
    meta.textContent = job.project || job.status || "running";
    row.append(label, meta);
    if (job.panel_id) {
      row.addEventListener("click", () => {
        byId("jobs-dialog")?.close();
        navigate({ kind: "serena", panelId: job.panel_id, expandedEntryId: job.job_id });
      });
    }
    container.append(row);
  }
}

function setupJobsDialog() {
  const dialog = byId("jobs-dialog");
  byId("jobs-button")?.addEventListener("click", () => {
    renderRunningJobs();
    dialog?.showModal();
  });
  byId("jobs-dialog-close")?.addEventListener("click", () => dialog?.close());
  dialog?.addEventListener("click", event => {
    if (event.target === dialog) dialog.close();
  });
}

function openResourceDialog() {
  const dialog = byId("resource-dialog");
  const container = byId("resource-dialog-content");
  if (!dialog || !container) return;
  container.replaceChildren();
  if (!latestTools.length) container.append(emptyCard("No active tools."));
  for (const toolName of latestTools) {
    const row = document.createElement("div");
    row.className = "resource-row";
    row.textContent = toolName;
    container.append(row);
  }
  dialog.showModal();
}

function setupResourceDialog() {
  const dialog = byId("resource-dialog");
  byId("tools-button")?.addEventListener("click", openResourceDialog);
  byId("resource-dialog-close")?.addEventListener("click", () => dialog?.close());
  dialog?.addEventListener("click", event => {
    if (event.target === dialog) dialog.close();
  });
}

function decodeBase64Url(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const binary = atob((value + padding).replaceAll("-", "+").replaceAll("_", "/"));
  return Uint8Array.from(binary, character => character.charCodeAt(0));
}

function setNotificationButtonState(button, state) {
  button.dataset.state = state;
  const enabled = state === "enabled";
  button.setAttribute("aria-pressed", String(enabled));
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
        const configResponse = await fetch(`${API_PREFIX}/push/config`, { cache: "no-cache" });
        if (!configResponse.ok) throw new Error(`${configResponse.status} ${configResponse.statusText}`);
        const config = await configResponse.json();
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

function tickVisibleActivity() {
  const now = Date.now() / 1000;
  for (const panel of visibleActivityPanels) panel.tick(now);
  for (const item of visibleElapsedNodes) item.node.textContent = window.SerenaActivity.formatLiveDuration(now - item.startedAt);
}

function startClock() {
  const needsClock = !document.hidden && (
    visibleElapsedNodes.length > 0
    || visibleActivityPanels.some(panel => panel.hasLiveActivity())
  );
  if (needsClock && clockTimer === null) {
    clockTimer = setInterval(tickVisibleActivity, 1000);
  } else if (!needsClock && clockTimer !== null) {
    clearInterval(clockTimer);
    clockTimer = null;
  }
}

function handleVisibilityChange() {
  startClock();
  scheduleRefresh(document.hidden ? HIDDEN_POLL_INTERVAL_MS : 0);
}

function handlePopState() {
  const route = initialRoute();
  currentRoute = route;
  pendingNotificationTarget = route.notificationTarget;
  routeGeneration += 1;
  currentDocument = route.kind === "overview" ? latestOverview : null;
  currentEtag = null;
  if (route.kind === "serena") activeOverviewView = "serena";
  applyActivityView();
  if (currentDocument) {
    renderCurrentDocument();
  } else if (route.kind === "serena" && latestOverview) {
    renderSerenaOverview(latestOverview?.serena?.panels || []);
    applyActivityView("serena");
  } else {
    renderLoadingRoute();
  }
  void refresh();
}

setupActivityViewTabs();
setupJobsDialog();
setupResourceDialog();
void setupPushNotifications();
applyActivityView();
startClock();

document.addEventListener("visibilitychange", handleVisibilityChange, { passive: true });
window.addEventListener("focus", () => scheduleRefresh(0), { passive: true });
window.addEventListener("popstate", handlePopState, { passive: true });

renderLoadingRoute();
void refresh();
setupChangeStream();
