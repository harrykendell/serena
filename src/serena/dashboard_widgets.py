from __future__ import annotations

import json

from orchestrator.activity import activity_widget_html as orchestrator_activity_widget_html
from serena.activity import activity_widget_html as serena_activity_widget_html


def serena_dashboard_widget_html(panel_id: str | None = None, initial_state: dict[str, object] | None = None) -> str:
    """Returns one retained Serena session widget connected to dashboard data."""
    panel_json = json.dumps(panel_id or "")
    initial_state_json = json.dumps(initial_state).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    adapter = f"""<script data-cfasync="false">
(() => {{
  const fallbackPanelId = {panel_json};
  const serverInitialOutput = {initial_state_json};
  let panelId = fallbackPanelId;
  const initialOutput = serverInitialOutput || {{ run_id: panelId, project_name: "", superseded: false, calls: [], jobs: [] }};
  let activityOutput = initialOutput;
  let initialReadPending = Boolean(serverInitialOutput);
  let live = true;
  let loadedRevision = String(initialOutput?.revision || "");
  let announcedRevision = loadedRevision;
  let stateDirty = !initialReadPending;

  async function applyDashboardBootstrap(bootstrap) {{
    if (!bootstrap?.panel_id) return;
    panelId = String(bootstrap.panel_id);
    live = typeof bootstrap.active === "boolean" ? bootstrap.active : true;
    if (bootstrap.tool_output?.run_id) {{
      activityOutput = bootstrap.tool_output;
      initialReadPending = true;
      loadedRevision = String(activityOutput.revision || bootstrap.revision || "");
      announcedRevision = String(bootstrap.revision || loadedRevision);
      stateDirty = false;
      window.openai.toolOutput = activityOutput;
      window.dispatchEvent(new CustomEvent("openai:set_globals", {{ detail: {{ globals: {{ toolOutput: activityOutput }} }} }}));
      if (activityOutput.summary_only && activityOutput.initial_expanded) {{
        try {{
          await loadActivity(true);
          initialReadPending = false;
          window.openai.toolOutput = activityOutput;
          window.dispatchEvent(new CustomEvent("openai:set_globals", {{ detail: {{ globals: {{ toolOutput: activityOutput }} }} }}));
        }} catch (_) {{
          // Retain the compact bootstrap if historical hydration is temporarily unavailable.
        }}
      }}
    }}
    requestAnimationFrame(() => parent.postMessage({{ type: "serena-dashboard-widget-ready", panel_id: panelId }}, location.origin));
  }}

  async function getJson(path) {{
    const url = new URL(`/dashboard/api${{path}}`, location.origin);
    const response = await fetch(url, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
    if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
    const data = await response.json();
    if (data?.status === "error") throw new Error(data.message || "Dashboard API error");
    return data;
  }}

  function boundedSummaryCalls(calls) {{
    const active = calls.filter(call => call.status === "running" || call.status === "queued");
    const terminal = calls.filter(call => call.status !== "running" && call.status !== "queued").slice(-8);
    const selected = [...active, ...terminal];
    const seen = new Set();
    return selected.filter(call => {{
      const key = String(call.call_id || "");
      if (key && seen.has(key)) return false;
      if (key) seen.add(key);
      return true;
    }});
  }}

  function mergeActivity(previous, next) {{
    if (!next?.partial || !previous?.run_id) return next;
    const calls = [...(previous.calls || [])];
    const positions = new Map(calls.map((call, index) => [call.call_id, index]));
    for (const call of next.calls || []) {{
      const index = positions.get(call.call_id);
      if (index === undefined) {{
        positions.set(call.call_id, calls.length);
        calls.push(call);
      }} else {{
        calls[index] = call;
      }}
    }}
    const summaryOnly = Boolean(previous.summary_only);
    return {{
      ...previous,
      ...next,
      partial: false,
      summary_only: summaryOnly,
      calls: summaryOnly ? boundedSummaryCalls(calls) : calls,
      jobs: next.jobs || previous.jobs || [],
    }};
  }}

  async function loadActivity(forceFull = false) {{
    const updatedAt = Number(activityOutput?.updated_at);
    const canRequestDelta = !forceFull && Number.isFinite(updatedAt) && updatedAt > 0;
    const suffix = canRequestDelta ? `?changed_since=${{encodeURIComponent(updatedAt)}}` : "";
    const next = await getJson(`/serena/panels/${{encodeURIComponent(panelId)}}${{suffix}}`);
    activityOutput = mergeActivity(activityOutput, next);
    loadedRevision = String(activityOutput?.revision || announcedRevision || loadedRevision);
    announcedRevision = loadedRevision;
    stateDirty = false;
    return activityOutput;
  }}

  function notifyIntrinsicHeight() {{
    requestAnimationFrame(() => {{
      const activity = document.querySelector(".activity");
      const height = Math.ceil(activity ? activity.getBoundingClientRect().height : document.body.getBoundingClientRect().height);
      parent.postMessage({{ type: "serena-activity-height", height }}, location.origin);
    }});
  }}

  window.openai = {{
    toolOutput: initialOutput,
    notifyIntrinsicHeight,
    callTool: async (name, args) => {{
      if (name === "get_activity") {{
        const forceFull = Boolean(args?.full);
        if (forceFull) {{
          initialReadPending = false;
          return loadActivity(true);
        }}
        if (initialReadPending) {{
          initialReadPending = false;
          return activityOutput;
        }}
        if (stateDirty || !activityOutput) return loadActivity(false);
        return activityOutput;
      }}
      if (name === "get_activity_detail") return getJson(`/serena/panels/${{encodeURIComponent(panelId)}}/calls/${{encodeURIComponent(args.call_id)}}`);
      if (name === "get_activity_job_detail") return getJson(`/serena/jobs/${{encodeURIComponent(args.job_id)}}`);
      throw new Error(`Unsupported dashboard widget call: ${{name}}`);
    }},
  }};

  window.addEventListener("message", event => {{
    if (event.origin !== location.origin) return;
    if (event.data?.type === "serena-dashboard-bootstrap") {{
      applyDashboardBootstrap(event.data.bootstrap);
      return;
    }}
    if (event.data?.type === "serena-dashboard-focus-job" && event.data.panel_id === panelId) {{
      window.dispatchEvent(new CustomEvent("serena:focus-job", {{ detail: {{ job_id: String(event.data.job_id || "") }} }}));
      return;
    }}
    if (event.data?.type !== "serena-dashboard-panel" || event.data.panel_id !== panelId) return;
    live = Boolean(event.data.active);
    announcedRevision = String(event.data.revision || announcedRevision);
    if (announcedRevision && announcedRevision !== loadedRevision) stateDirty = true;
  }});
}})();
</script>
<style>html, body {{ overflow: hidden; }}</style>
"""
    widget = serena_activity_widget_html().replace("<script>", '<script data-cfasync="false">')
    return adapter + widget


def orchestrator_dashboard_widget_html(panel_id: str | None = None, initial_state: dict[str, object] | None = None) -> str:
    """Returns one Orchestrator activity widget connected to an operator panel."""
    panel_json = json.dumps(panel_id or "")
    initial_state_json = json.dumps(initial_state).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    adapter = f"""<script data-cfasync="false">
(() => {{
  const fallbackPanelId = {panel_json};
  const serverInitialOutput = {initial_state_json};
  let panelId = fallbackPanelId;
  let activityOutput = serverInitialOutput || {{ run_id: panelId, started_at: 0, superseded: false, delegates: [] }};
  let initialReadPending = Boolean(serverInitialOutput);
  let live = true;
  let loadedRevision = "";
  let announcedRevision = "";
  let stateDirty = !initialReadPending;

  function applyDashboardBootstrap(bootstrap) {{
    if (!bootstrap?.panel_id) return;
    panelId = String(bootstrap.panel_id);
    live = typeof bootstrap.active === "boolean" ? bootstrap.active : true;
    if (bootstrap.tool_output?.run_id) {{
      activityOutput = bootstrap.tool_output;
      initialReadPending = true;
      loadedRevision = String(bootstrap.revision || "");
      announcedRevision = loadedRevision;
      stateDirty = false;
      window.openai.toolOutput = activityOutput;
      window.dispatchEvent(new CustomEvent("openai:set_globals", {{ detail: {{ globals: {{ toolOutput: activityOutput }} }} }}));
    }}
    requestAnimationFrame(() => parent.postMessage({{ type: "serena-dashboard-widget-ready", panel_id: panelId }}, location.origin));
  }}

  async function getJson(path) {{
    const url = new URL(`/dashboard/api${{path}}`, location.origin);
    const response = await fetch(url, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
    if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
    const data = await response.json();
    if (data?.status === "error") throw new Error(data.message || "Dashboard API error");
    return data;
  }}

  async function loadActivity() {{
    activityOutput = await getJson(`/orchestrator/panels/${{encodeURIComponent(panelId)}}`);
    loadedRevision = announcedRevision || loadedRevision;
    stateDirty = false;
    return activityOutput;
  }}

  function notifyIntrinsicHeight() {{
    requestAnimationFrame(() => {{
      const activity = document.querySelector(".activity");
      const height = Math.ceil(activity ? activity.getBoundingClientRect().height : document.body.getBoundingClientRect().height);
      parent.postMessage({{ type: "serena-activity-height", height }}, location.origin);
    }});
  }}

  window.openai = {{
    toolOutput: activityOutput,
    notifyIntrinsicHeight,
    callTool: async (name, args) => {{
      if (name === "get_orchestrator_activity" && initialReadPending) {{
        initialReadPending = false;
        return activityOutput;
      }}
      if (name === "get_orchestrator_activity") {{
        if (!live || !stateDirty) return activityOutput;
        return loadActivity();
      }}
      if (name === "get_orchestrator_delegate_detail") return getJson(`/orchestrator/delegates/${{encodeURIComponent(args.delegate_id)}}`);
      throw new Error(`Unsupported dashboard widget call: ${{name}}`);
    }},
  }};

  window.addEventListener("message", event => {{
    if (event.origin !== location.origin) return;
    if (event.data?.type === "serena-dashboard-bootstrap") {{
      applyDashboardBootstrap(event.data.bootstrap);
      return;
    }}
    if (event.data?.type !== "serena-dashboard-panel" || event.data.panel_id !== panelId) return;
    live = Boolean(event.data.active);
    announcedRevision = String(event.data.revision || announcedRevision);
    if (announcedRevision && announcedRevision !== loadedRevision) stateDirty = true;
  }});
}})();
</script>
<style>html, body {{ overflow: hidden; }} .fallback-action {{ display: none !important; }}</style>
"""
    widget = orchestrator_activity_widget_html().replace("<script>", '<script data-cfasync="false">')
    return adapter + widget
