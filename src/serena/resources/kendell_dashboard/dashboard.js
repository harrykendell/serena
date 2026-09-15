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
let jobsDialogPanel = null;
let jobsDialogRoot = null;
let jobsDialogExpandedJob = null;
let jobsDialogDetailGeneration = 0;
let visibleActivityPanels = [];
let serenaOverviewPanels = new Map();
let serenaOverviewGroupNodes = new Map();
const serenaOverviewGroupExpansion = new Map([["active", true]]);
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
  const resolvedLabel = label || "Connected";
  node.dataset.state = state;
  setText("connection-label", resolvedLabel, "Connected");
  const menuButton = byId("serena-options-button");
  if (menuButton) {
    menuButton.dataset.state = state;
    menuButton.dataset.tone = state === "error" ? "bad" : "";
    const menuLabel = `${resolvedLabel}. Open Serena status and options`;
    menuButton.setAttribute("aria-label", menuLabel);
    menuButton.title = menuLabel;
  }
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
    loading.className = "kd-surface empty-card";
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
  renderSerenaOverview(
    state?.serena?.panels || [],
    state?.orchestrator?.panels || []
  );
  applyActivityView("serena");
}

function renderOverviewMetadata(session, jobs) {
  setText("access-identity", session.access_identity, "Cloudflare Access");
  const signOut = byId("access-sign-out");
  if (signOut) signOut.title = session.access_identity ? `Sign out ${session.access_identity}` : "Sign out of Cloudflare Access";
  setText("runtime-policy", session.runtime_policy, "ChatGPT");
  setText("version", session.serena_version, "—");
  latestTools = session.active_tools || [];
  latestJobs = jobs || { jobs: [], running_jobs: 0, max_concurrent_jobs: 0 };
  setText("tool-count", latestTools.length);
  setText("running-jobs-count", latestJobs.running_jobs || 0);
  setText("max-jobs-count", latestJobs.max_concurrent_jobs || 0);
  byId("tools-button")?.setAttribute("aria-label", `${latestTools.length} active tools`);
  byId("jobs-button")?.setAttribute("aria-label", `${latestJobs.running_jobs || 0} of ${latestJobs.max_concurrent_jobs || 0} Serena jobs running`);
  if (byId("jobs-dialog")?.open) {
    renderRunningJobs();
    const expandedJobId = jobsDialogPanel?.expandedEntryId || null;
    if (expandedJobId) void refreshRunningJobDetail(expandedJobId);
  }
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
  const today = new Date();
  if (
    date.getFullYear() === today.getFullYear()
    && date.getMonth() === today.getMonth()
    && date.getDate() === today.getDate()
  ) return "Today";

  const day = date.getDate();
  const mod100 = day % 100;
  const suffix = mod100 >= 11 && mod100 <= 13
    ? "th"
    : ({ 1: "st", 2: "nd", 3: "rd" }[day % 10] || "th");
  const weekday = date.toLocaleDateString("en-GB", { weekday: "long" });
  const month = date.toLocaleDateString("en-GB", { month: "long" });
  const year = date.getFullYear();
  const yearSuffix = year === today.getFullYear() ? "" : ` ${year}`;

  return `${weekday} ${day}${suffix} ${month}${yearSuffix}`;
}

function serenaSessionGroups(serenaPanels, orchestratorPanels = []) {
  const sessions = [
    ...serenaPanels.map(panel => ({ kind: "serena", panel })),
    ...orchestratorPanels.map(panel => ({ kind: "orchestrator", panel })),
  ].sort((a, b) => {
    const activeDelta = Number(Boolean(b.panel?.active)) - Number(Boolean(a.panel?.active));
    if (activeDelta) return activeDelta;
    return Number(b.panel?.updated_at || 0) - Number(a.panel?.updated_at || 0);
  });

  const groups = [];
  let current = null;
  for (const session of sessions) {
    const summary = session.panel || {};
    const day = summary.active ? null : sessionDay(summary.updated_at);
    const key = summary.active ? "active" : day ? `day:${day.key}` : "day:unknown";
    const label = summary.active ? "Active" : day ? sessionDayLabel(day.date) : "Earlier";
    if (!current || current.key !== key) {
      current = { key, label, defaultExpanded: groups.length === 0, panels: [] };
      groups.push(current);
    }
    current.panels.push(session);
  }

  return groups;
}

function serenaSessionGroupNode(group) {
  let entry = serenaOverviewGroupNodes.get(group.key);
  if (!entry) {
    const root = document.createElement("section");
    root.className = "session-group";
    root.dataset.groupKey = group.key;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "session-group-toggle kd-group-divider";

    const chevron = document.createElement("span");
    chevron.className = "kd-group-divider-chevron";
    chevron.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "session-group-label kd-group-divider-label";

    const count = document.createElement("span");
    count.className = "kd-group-divider-count";

    const body = document.createElement("div");
    body.className = "session-group-body";
    body.id = `serena-session-group-${group.key.replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`;
    toggle.setAttribute("aria-controls", body.id);
    toggle.append(chevron, label, count);
    root.append(toggle, body);

    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(expanded));
      body.hidden = !expanded;
      serenaOverviewGroupExpansion.set(group.key, expanded);
    });

    entry = { root, toggle, label, count, body };
    serenaOverviewGroupNodes.set(group.key, entry);
  }

  const expanded = serenaOverviewGroupExpansion.has(group.key)
    ? serenaOverviewGroupExpansion.get(group.key)
    : group.defaultExpanded;
  serenaOverviewGroupExpansion.set(group.key, expanded);
  entry.toggle.setAttribute("aria-expanded", String(expanded));
  entry.body.hidden = !expanded;
  entry.label.textContent = group.label;
  entry.count.textContent = String(group.panels.length);
  return entry;
}

function orchestratorSessionCard(panel) {
  const root = document.createElement("div");
  root.className = "serena-activity-panel orchestrator-session-panel";
  root.dataset.panelId = panel.panel_id;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "activity-header orchestrator-session-card";

  const logo = document.createElement("img");
  logo.className = "activity-logo orchestrator-session-logo";
  logo.src = dashboardAssetUrl("orchestrator-logo.svg");
  logo.alt = "";
  logo.setAttribute("aria-hidden", "true");

  const heading = document.createElement("span");
  heading.className = "activity-heading orchestrator-session-copy";
  const title = document.createElement("span");
  title.className = "activity-title";
  title.textContent = panel.display_name || "Orchestrator";
  const summary = document.createElement("span");
  summary.className = "activity-summary";
  const delegateCount = Number(panel.delegate_count || 0);
  const activeCount = Number(panel.active_count || 0);
  summary.textContent = `${delegateCount} ${delegateCount === 1 ? "delegate" : "delegates"}${activeCount ? ` · ${activeCount} active` : ""}`;
  heading.append(title, summary);

  const meta = document.createElement("span");
  meta.className = "activity-header-meta";

  const chevron = document.createElement("span");
  chevron.textContent = "›";
  chevron.className = "activity-chevron orchestrator-chevron";

  button.append(logo, heading, meta, chevron);
  button.addEventListener("click", () => navigate({ kind: "orchestrator", panelId: panel.panel_id, expandedEntryId: null }));
  root.append(button);
  return root;
}

function renderSerenaOverview(serenaPanels, orchestratorPanels = []) {
  const container = byId("serena-widgets");
  if (!container) return;
  visibleActivityPanels = [];
  selectedSerenaPanel = null;
  selectedSerenaRoot = null;

  if (!serenaPanels.length && !orchestratorPanels.length) {
    for (const { panel } of serenaOverviewPanels.values()) panel.destroy();
    serenaOverviewPanels = new Map();
    serenaOverviewGroupNodes = new Map();
    container.replaceChildren(emptyCard("No session activity recorded yet."));
    return;
  }

  const nextPanels = new Map();
  const nextGroupNodes = new Map();
  const desiredGroups = [];
  for (const group of serenaSessionGroups(serenaPanels, orchestratorPanels)) {
    const groupEntry = serenaSessionGroupNode(group);
    const groupPanels = [];

    for (const session of group.panels) {
      const summary = session.panel;
      if (session.kind === "orchestrator") {
        groupPanels.push(orchestratorSessionCard(summary));
        continue;
      }

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
      groupPanels.push(root);
    }

    groupEntry.body.replaceChildren(...groupPanels);
    nextGroupNodes.set(group.key, groupEntry);
    desiredGroups.push(groupEntry.root);
  }

  for (const [panelId, entry] of serenaOverviewPanels) {
    if (!nextPanels.has(panelId)) entry.panel.destroy();
  }
  serenaOverviewPanels = nextPanels;
  serenaOverviewGroupNodes = nextGroupNodes;
  container.replaceChildren(...desiredGroups);
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

function emptyCard(message) {
  const node = document.createElement("div");
  node.className = "kd-surface empty-card";
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

function renderSelectedOrchestrator(documentState) {
  const container = byId("orchestrator-widgets");
  if (!container || !documentState) return;
  container.replaceChildren();
  visibleActivityPanels = [];
  applyActivityView("orchestrator");
  container.append(selectedBackButton("All sessions"));

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

  const serenaPanel = byId("serena-activity-panel");
  const orchestratorPanel = byId("orchestrator-activity-panel");
  if (serenaPanel) serenaPanel.hidden = activeOverviewView !== "serena";
  if (orchestratorPanel) orchestratorPanel.hidden = activeOverviewView !== "orchestrator";
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

function runningJobsSnapshot() {
  const jobs = Array.isArray(latestJobs.jobs) ? latestJobs.jobs : [];
  const startedAt = jobs.reduce((earliest, job) => {
    const started = Number(job.started_at);
    if (!Number.isFinite(started)) return earliest;
    return earliest === null ? started : Math.min(earliest, started);
  }, null);
  const latest = jobs[0] || null;
  return {
    run_id: null,
    session_title: "Jobs",
    started_at: startedAt,
    updated_at: Date.now() / 1000,
    submission_span_seconds: null,
    tool_count: 0,
    job_count: jobs.length,
    latest_activity: latest
      ? {
          label: latest.label || latest.job_id,
          detail: "",
          scope: latest.project || "",
          status: latest.status,
          started_at: latest.started_at,
          finished_at: latest.finished_at,
        }
      : null,
    calls: [],
    jobs,
    expanded_call: null,
    expanded_job: jobsDialogExpandedJob,
  };
}

function runningJobSummaryDetail(job) {
  const startedAt = Number(job?.started_at);
  return {
    job_id: job.job_id,
    label: job.label || job.job_id,
    project: job.project || "",
    command: job.command || "",
    status: job.status || "running",
    elapsed_seconds: Number.isFinite(startedAt) ? Math.max(0, Date.now() / 1000 - startedAt) : null,
    output: "",
  };
}

async function refreshRunningJobDetail(jobId, knownJob = null) {
  if (!jobId) return;
  const job = knownJob || (latestJobs.jobs || []).find(item => item.job_id === jobId) || null;
  if (!job) return;

  const generation = ++jobsDialogDetailGeneration;
  if (!job.panel_id) {
    jobsDialogExpandedJob = runningJobSummaryDetail(job);
    renderRunningJobs();
    return;
  }

  try {
    const response = await fetchCurrentDocument(
      `/serena/sessions/${encodeURIComponent(job.panel_id)}?expanded=${encodeURIComponent(jobId)}`,
      null,
    );
    if (generation !== jobsDialogDetailGeneration || jobsDialogPanel?.expandedEntryId !== jobId) return;
    const detail = response.data?.expanded_job || null;
    if (detail?.job_id !== jobId) throw new Error("Expanded job detail did not match the requested job");
    jobsDialogExpandedJob = detail;
    renderRunningJobs();
  } catch (error) {
    if (generation !== jobsDialogDetailGeneration || jobsDialogPanel?.expandedEntryId !== jobId) return;
    console.warn("Could not load active job detail", error);
    jobsDialogExpandedJob = runningJobSummaryDetail(job);
    renderRunningJobs();
  }
}

function renderRunningJobs() {
  const container = byId("jobs-dialog-content");
  if (!container) return;

  const jobs = Array.isArray(latestJobs.jobs) ? latestJobs.jobs : [];
  if (!jobs.length) {
    jobsDialogDetailGeneration += 1;
    jobsDialogExpandedJob = null;
    jobsDialogPanel?.destroy();
    jobsDialogPanel = null;
    jobsDialogRoot = null;
    container.replaceChildren(emptyCard("No Serena jobs recorded."));
    return;
  }

  if (!jobsDialogPanel || !jobsDialogRoot || !jobsDialogRoot.isConnected) {
    jobsDialogPanel?.destroy();
    container.replaceChildren();
    jobsDialogRoot = document.createElement("div");
    jobsDialogRoot.className = "jobs-activity-root";
    container.append(jobsDialogRoot);
    jobsDialogPanel = new window.SerenaActivity.ActivityPanel(jobsDialogRoot, {
      initialCollapsed: false,
      onExpandedChange: (entryId, entry) => {
        jobsDialogExpandedJob = null;
        if (!entryId) {
          jobsDialogDetailGeneration += 1;
          return;
        }
        void refreshRunningJobDetail(entryId, entry?.kind === "job" ? entry.item : null);
      },
    });
  }

  const activeIds = new Set(jobs.map(job => job.job_id));
  const expandedJobId = jobsDialogPanel.expandedEntryId || null;
  if (expandedJobId && !activeIds.has(expandedJobId)) {
    jobsDialogDetailGeneration += 1;
    jobsDialogExpandedJob = null;
    jobsDialogPanel.setExpandedEntryId(null);
  } else if (jobsDialogExpandedJob && jobsDialogExpandedJob.job_id !== expandedJobId) {
    jobsDialogExpandedJob = null;
  }

  jobsDialogPanel.render(runningJobsSnapshot());
}

function closeHeaderMenu() {
  const menu = byId("serena-options-menu");
  if (menu?.open) menu.open = false;
}

function setupHeaderMenu() {
  const menu = byId("serena-options-menu");
  if (!menu) return;
  document.addEventListener("click", event => {
    if (!menu.open) return;
    const target = event.target instanceof Node ? event.target : null;
    if (target && !menu.contains(target)) menu.open = false;
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    menu.querySelector("summary")?.focus();
  });
}

function setupJobsDialog() {
  const dialog = byId("jobs-dialog");
  byId("jobs-button")?.addEventListener("click", () => {
    closeHeaderMenu();
    renderRunningJobs();
    dialog?.showModal();
    const expandedJobId = jobsDialogPanel?.expandedEntryId || null;
    if (expandedJobId) void refreshRunningJobDetail(expandedJobId);
    startClock();
  });
  byId("jobs-dialog-close")?.addEventListener("click", () => dialog?.close());
  dialog?.addEventListener("click", event => {
    if (event.target === dialog) dialog.close();
  });
  dialog?.addEventListener("close", startClock);
}

function openResourceDialog() {
  const dialog = byId("resource-dialog");
  const container = byId("resource-dialog-content");
  if (!dialog || !container) return;
  closeHeaderMenu();
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
  const note = enabled
    ? "Enabled"
    : state === "denied"
      ? "Blocked by browser settings"
      : "Enable completion notifications";
  button.setAttribute("aria-label", label);
  button.title = label;
  setText("notification-state-note", note, "Enable completion notifications");
}

async function setupPushNotifications() {
  const button = byId("notification-button");
  const row = byId("notification-row");
  if (!button) return;
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    if (row) row.hidden = true;
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
    if (row) row.hidden = true;
  }
}

function tickVisibleActivity() {
  const now = Date.now() / 1000;
  for (const panel of visibleActivityPanels) panel.tick(now);
  if (byId("jobs-dialog")?.open && jobsDialogPanel) jobsDialogPanel.tick(now);
  for (const item of visibleElapsedNodes) item.node.textContent = window.SerenaActivity.formatLiveDuration(now - item.startedAt);
}

function startClock() {
  const needsClock = !document.hidden && (
    visibleElapsedNodes.length > 0
    || visibleActivityPanels.some(panel => panel.hasLiveActivity())
    || (byId("jobs-dialog")?.open && jobsDialogPanel?.hasLiveActivity())
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

setupHeaderMenu();
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
