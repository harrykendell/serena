(() => {
  "use strict";

  const root = document.getElementById("serena-activity-root");
  if (!root || !window.SerenaActivity?.ActivityPanel) return;

  const BRIDGE_CALL_TIMEOUT_MS = 5000;
  const BRIDGE_MAX_PENDING_CALLS = 4;

  const warning = document.createElement("div");
  warning.className = "activity-inline-warning";
  warning.hidden = true;
  root.insertAdjacentElement("afterend", warning);

  let snapshot = null;
  let runId = null;
  let pollTimer = null;
  let clockTimer = null;
  let retired = false;
  let initialCollapseResolved = false;
  let detailGeneration = 0;
  let heightFrame = null;
  let lastNotifiedHeight = null;
  let pollWarning = null;
  let detailWarning = null;
  let pendingBridgeCalls = 0;

  function unwrap(result) {
    return result?.structuredContent ?? result?.structured_content ?? result;
  }

  function status(value) {
    return String(value || "").toLowerCase();
  }

  function isActive(item) {
    const current = status(item?.status);
    return current === "starting" || current === "running" || current === "queued" || current === "pending" || current === "waiting";
  }

  function hasActiveActivity(next) {
    return [...(next?.calls || []), ...(next?.jobs || [])].some(isActive);
  }

  function latestActivityTimestamp(next) {
    const values = [...(next?.calls || []), ...(next?.jobs || [])]
      .map(item => Number(item?.finished_at ?? item?.started_at ?? item?.submitted_at))
      .filter(Number.isFinite);
    return values.length ? Math.max(...values) : null;
  }

  function pollDelay(next) {
    if ((next?.calls || []).some(isActive)) return 500;
    if ((next?.jobs || []).some(isActive)) return 3000;
    return 5000;
  }

  function notifyHeight() {
    if (heightFrame !== null) return;
    heightFrame = requestAnimationFrame(() => {
      heightFrame = null;
      const height = document.documentElement.scrollHeight;
      if (height === lastNotifiedHeight) return;
      lastNotifiedHeight = height;
      window.openai?.notifyIntrinsicHeight?.();
    });
  }


  function syncWarning() {
    const message = pollWarning || detailWarning;
    warning.textContent = message || "";
    warning.hidden = !message;
    notifyHeight();
  }

  async function callTool(name, args) {
    if (!window.openai?.callTool) throw new Error("Serena activity bridge is unavailable");
    if (pendingBridgeCalls >= BRIDGE_MAX_PENDING_CALLS) {
      throw new Error("Serena activity bridge has too many unresolved calls");
    }

    pendingBridgeCalls += 1;
    const bridgePromise = Promise.resolve().then(() => window.openai.callTool(name, args));
    bridgePromise.then(
      () => { pendingBridgeCalls -= 1; },
      () => { pendingBridgeCalls -= 1; },
    );

    let timeout = null;
    const timeoutPromise = new Promise((_, reject) => {
      timeout = setTimeout(
        () => reject(new Error(`Serena activity bridge call timed out: ${name}`)),
        BRIDGE_CALL_TIMEOUT_MS,
      );
    });
    try {
      return unwrap(await Promise.race([bridgePromise, timeoutPromise]));
    } finally {
      if (timeout !== null) clearTimeout(timeout);
    }
  }

  function entryKind(next, entryId) {
    if ((next?.calls || []).some(item => item.call_id === entryId)) return "call";
    if ((next?.jobs || []).some(item => item.job_id === entryId)) return "job";
    return null;
  }

  function preserveExpandedDetail(next) {
    const entryId = panel.expandedEntryId;
    if (!entryId || !next?.run_id) return next;
    const kind = entryKind(next, entryId);
    if (kind === "call") {
      const detail = next.expanded_call?.call_id === entryId
        ? next.expanded_call
        : snapshot?.expanded_call?.call_id === entryId ? snapshot.expanded_call : null;
      return { ...next, expanded_call: detail, expanded_job: null };
    }
    if (kind === "job") {
      const detail = next.expanded_job?.job_id === entryId
        ? next.expanded_job
        : snapshot?.expanded_job?.job_id === entryId ? snapshot.expanded_job : null;
      return { ...next, expanded_call: null, expanded_job: detail };
    }
    return next;
  }

  function mergeExpandedDetail(detailState, entryId) {
    if (!snapshot || !detailState || snapshot.run_id !== detailState.run_id) return detailState;
    const kind = entryKind(snapshot, entryId);
    if (kind === "call" && detailState.expanded_call?.call_id === entryId) {
      return { ...snapshot, expanded_call: detailState.expanded_call, expanded_job: null };
    }
    if (kind === "job" && detailState.expanded_job?.job_id === entryId) {
      return { ...snapshot, expanded_call: null, expanded_job: detailState.expanded_job };
    }
    return preserveExpandedDetail(detailState);
  }

  function hasExpandedDetail(entryId) {
    if (!snapshot || panel.expandedEntryId !== entryId) return false;
    const kind = entryKind(snapshot, entryId);
    if (kind === "call") return snapshot.expanded_call?.call_id === entryId;
    if (kind === "job") return snapshot.expanded_job?.job_id === entryId;
    return false;
  }

  async function withExpandedDetail(next) {
    const entryId = panel.expandedEntryId;
    if (!entryId || !next?.run_id) return { ...next, expanded_call: null, expanded_job: null };
    const kind = entryKind(next, entryId);
    if (kind === "call") {
      const detail = await callTool("get_activity_detail", { run_id: next.run_id, call_id: entryId });
      return { ...next, expanded_call: detail, expanded_job: null };
    }
    if (kind === "job") {
      const detail = await callTool("get_activity_job_detail", { run_id: next.run_id, job_id: entryId });
      return { ...next, expanded_call: null, expanded_job: detail };
    }
    panel.setExpandedEntryId(null);
    return { ...next, expanded_call: null, expanded_job: null };
  }

  function applyInitialCollapsedPolicy(next) {
    if (initialCollapseResolved) return;
    const count = (next?.calls || []).length + (next?.jobs || []).length;
    if (!count) {
      panel.setCollapsed(true);
      return;
    }
    const latest = latestActivityTimestamp(next);
    const recent = hasActiveActivity(next) || (latest !== null && Date.now() / 1000 - latest <= 30);
    panel.setCollapsed(!recent);
    initialCollapseResolved = true;
  }

  async function loadMedia(callId, media) {
    const result = await callTool("get_activity_media", { run_id: runId, call_id: callId });
    const content = result?.content ?? result?.structuredContent?.content ?? result?.structured_content?.content ?? [];
    const image = content.find(block => block?.type === "image" && block.data);
    if (image) {
      const mime = image.mimeType || image.mime_type || media?.mime_type || "image/png";
      return { type: "image", src: `data:${mime};base64,${image.data}`, name: media?.name };
    }
    const audio = content.find(block => block?.type === "audio" && block.data);
    if (audio) {
      const mime = audio.mimeType || audio.mime_type || media?.mime_type || "audio/mpeg";
      return { type: "audio", src: `data:${mime};base64,${audio.data}`, name: media?.name };
    }
    const file = content.find(block => block?.type === "resource_link" && block.uri);
    if (file) return { type: "file", src: file.uri, name: file.name || media?.name };
    return { type: media?.media_type || "file", src: media?.uri || "", name: media?.name };
  }

  async function expand(entryId, retryCount = 0) {
    const generation = ++detailGeneration;
    if (!snapshot || !entryId) {
      detailWarning = null;
      syncWarning();
      if (snapshot) {
        snapshot = { ...snapshot, expanded_call: null, expanded_job: null };
        panel.render(snapshot);
      }
      return;
    }
    try {
      const next = await withExpandedDetail(snapshot);
      if (generation !== detailGeneration || panel.expandedEntryId !== entryId) return;
      detailWarning = null;
      syncWarning();
      render(mergeExpandedDetail(next, entryId));
    } catch (_) {
      if (generation !== detailGeneration || panel.expandedEntryId !== entryId) return;
      detailWarning = "Expanded detail unavailable; retrying.";
      syncWarning();
      if (retryCount >= 2) return;
      setTimeout(() => {
        if (retired || generation !== detailGeneration || panel.expandedEntryId !== entryId || hasExpandedDetail(entryId)) return;
        void expand(entryId, retryCount + 1);
      }, 500 * (retryCount + 1));
    }
  }

  const panel = new window.SerenaActivity.ActivityPanel(root, {
    initialCollapsed: false,
    onExpandedChange: entryId => { void expand(entryId); },
    loadMedia,
    onHeightChange: notifyHeight,
  });

  function render(next) {
    if (!next?.run_id) return;
    if (runId && next.run_id !== runId) return;
    if (runId && Number(next.updated_at || 0) < Number(snapshot?.updated_at || 0)) return;
    const displayed = preserveExpandedDetail(next);
    runId = displayed.run_id;
    snapshot = displayed;
    applyInitialCollapsedPolicy(displayed);
    panel.render(displayed);
    syncClock();
  }


  function syncClock() {
    // Embedded ChatGPT frames may report themselves hidden while their activity panel is visible.
    const needsClock = !retired && panel.hasLiveActivity();
    if (needsClock && clockTimer === null) {
      clockTimer = setInterval(() => panel.tick(Date.now() / 1000), 1000);
    } else if (!needsClock && clockTimer !== null) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  }

  function retire() {
    retired = true;
    if (pollTimer !== null) clearTimeout(pollTimer);
    if (clockTimer !== null) clearInterval(clockTimer);
    if (heightFrame !== null) cancelAnimationFrame(heightFrame);
    pollTimer = null;
    clockTimer = null;
    heightFrame = null;
    pollWarning = null;
    detailWarning = null;
    warning.textContent = "";
    warning.hidden = true;
    panel.retire();
  }

  async function poll() {
    if (retired) return;
    if (!runId || !window.openai?.callTool) {
      if (runId && !window.openai?.callTool) {
        pollWarning = "Live updates unavailable; retrying.";
        syncWarning();
      }
      pollTimer = setTimeout(poll, 250);
      return;
    }

    let next = snapshot;
    try {
      const state = await callTool("get_activity", { run_id: runId });
      pollWarning = null;
      if (state?.run_id === runId) {
        render(state);
        next = snapshot;
        if (panel.expandedEntryId) {
          const entryId = panel.expandedEntryId;
          const generation = detailGeneration;
          try {
            const detailed = await withExpandedDetail(state);
            if (generation === detailGeneration && panel.expandedEntryId === entryId) {
              next = mergeExpandedDetail(detailed, entryId);
              detailWarning = null;
              render(next);
              next = snapshot;
            }
          } catch (_) {
            if (generation === detailGeneration && panel.expandedEntryId === entryId) {
              detailWarning = hasExpandedDetail(entryId)
                ? "Detail refresh unavailable; showing last result."
                : "Expanded detail unavailable; retrying.";
            }
          }
        } else {
          detailWarning = null;
        }
      }
      syncWarning();
      if (next?.superseded && !hasActiveActivity(next)) {
        retire();
        return;
      }
    } catch (_) {
      pollWarning = "Live updates unavailable; retrying.";
      syncWarning();
    }
    pollTimer = setTimeout(poll, pollDelay(next));
  }

  function acceptGlobals(event) {
    const next = event?.detail?.globals?.toolOutput;
    if (!next?.run_id) return;
    if (runId && next.run_id !== runId) return;
    if (runId && Number(next.updated_at || 0) <= Number(snapshot?.updated_at || 0)) return;

    // seed a new iframe, or recover from a newer same-run host snapshot if app polling stalled.
    detailGeneration += 1;
    panel.setExpandedEntryId(null);
    pollWarning = null;
    detailWarning = null;
    syncWarning();
    render(next);
    if (next.superseded && !hasActiveActivity(next)) retire();
  }

  window.addEventListener("openai:set_globals", acceptGlobals, { passive: true });

  const initial = window.openai?.toolOutput;
  if (initial?.run_id) render(initial);
  syncClock();
  void poll();
})();
