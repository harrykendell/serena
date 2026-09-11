(() => {
  "use strict";

  const root = document.getElementById("serena-activity-root");
  if (!root || !window.SerenaActivity?.ActivityPanel) return;

  let snapshot = null;
  let runId = null;
  let pollTimer = null;
  let clockTimer = null;
  let retired = false;
  let initialCollapseResolved = false;
  let detailGeneration = 0;

  function unwrap(result) {
    return result?.structuredContent ?? result?.structured_content ?? result;
  }

  function status(value) {
    return String(value || "").toLowerCase();
  }

  function isRunning(item) {
    return status(item?.status) === "running";
  }

  function hasRunningActivity(next) {
    return [...(next?.calls || []), ...(next?.jobs || [])].some(isRunning);
  }

  function latestActivityTimestamp(next) {
    const values = [...(next?.calls || []), ...(next?.jobs || [])]
      .map(item => Number(item?.finished_at ?? item?.started_at ?? item?.submitted_at))
      .filter(Number.isFinite);
    return values.length ? Math.max(...values) : null;
  }

  function pollDelay(next) {
    if ((next?.calls || []).some(isRunning)) return 500;
    if ((next?.jobs || []).some(isRunning)) return 3000;
    return 5000;
  }

  function notifyHeight() {
    requestAnimationFrame(() => window.openai?.notifyIntrinsicHeight?.());
  }

  async function callTool(name, args) {
    if (!window.openai?.callTool) throw new Error("Serena activity bridge is unavailable");
    return unwrap(await window.openai.callTool(name, args));
  }

  function entryKind(next, entryId) {
    if ((next?.calls || []).some(item => item.call_id === entryId)) return "call";
    if ((next?.jobs || []).some(item => item.job_id === entryId)) return "job";
    return null;
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
    const recent = hasRunningActivity(next) || (latest !== null && Date.now() / 1000 - latest <= 30);
    panel.setCollapsed(!recent);
    initialCollapseResolved = true;
  }

  async function loadMedia(callId, media) {
    const result = await window.openai.callTool("get_activity_media", { run_id: runId, call_id: callId });
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

  async function expand(entryId) {
    const generation = ++detailGeneration;
    if (!snapshot || !entryId) {
      if (snapshot) {
        snapshot = { ...snapshot, expanded_call: null, expanded_job: null };
        panel.render(snapshot);
      }
      return;
    }
    try {
      const next = await withExpandedDetail(snapshot);
      if (generation !== detailGeneration || panel.expandedEntryId !== entryId) return;
      snapshot = next;
      panel.render(snapshot);
    } catch (_) {
      // Keep the row open with its loading placeholder across transient failures.
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
    runId = next.run_id;
    snapshot = next;
    applyInitialCollapsedPolicy(next);
    panel.render(next);
    syncClock();
  }


  function syncClock() {
    const needsClock = !retired && !document.hidden && panel.hasLiveActivity();
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
    pollTimer = null;
    clockTimer = null;
    panel.retire();
  }

  async function poll() {
    if (retired) return;
    if (!runId || !window.openai?.callTool) {
      pollTimer = setTimeout(poll, 250);
      return;
    }
    let next = snapshot;
    try {
      const state = await callTool("get_activity", { run_id: runId });
      if (state?.run_id === runId) {
        next = panel.expandedEntryId ? await withExpandedDetail(state) : state;
        render(next);
      }
      if (next?.superseded && !hasRunningActivity(next)) {
        retire();
        return;
      }
    } catch (_) {
      // Preserve the last complete snapshot on transient bridge/server failures.
    }
    pollTimer = setTimeout(poll, pollDelay(next));
  }

  function acceptGlobals(event) {
    const next = event?.detail?.globals?.toolOutput;
    if (!next?.run_id || (runId && next.run_id !== runId)) return;
    detailGeneration += 1;
    panel.setExpandedEntryId(null);
    render(next);
    if (next.superseded && !hasRunningActivity(next)) retire();
  }

  window.addEventListener("openai:set_globals", acceptGlobals, { passive: true });
  document.addEventListener("visibilitychange", syncClock, { passive: true });

  const initial = window.openai?.toolOutput;
  if (initial?.run_id) render(initial);
  syncClock();
  void poll();
})();
