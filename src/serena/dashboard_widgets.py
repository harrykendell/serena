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
  let bootstrap = {{}};
  try {{ bootstrap = JSON.parse(window.name || "{{}}"); window.name = ""; }} catch (_) {{ bootstrap = {{}}; }}
  const panelId = bootstrap.panel_id || fallbackPanelId;
  const initialOutput = bootstrap.tool_output || serverInitialOutput || {{ run_id: panelId, project_name: "", superseded: false, calls: [], jobs: [] }};
  let activityOutput = initialOutput;
  let initialReadPending = Boolean(bootstrap.tool_output || serverInitialOutput);
  let live = typeof bootstrap.active === "boolean" ? bootstrap.active : true;
  let loadedRevision = String(initialOutput?.revision || bootstrap.revision || "");
  let announcedRevision = String(bootstrap.revision || loadedRevision);
  let stateDirty = !initialReadPending;
  let forceFullOnNextRead = Boolean(live && initialOutput?.summary_only);

  async function getJson(path) {{
    const response = await fetch(`/dashboard/api${{path}}`, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
    if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
    const data = await response.json();
    if (data?.status === "error") throw new Error(data.message || "Dashboard API error");
    return data;
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
    return {{
      ...previous,
      ...next,
      partial: false,
      summary_only: false,
      calls,
      jobs: next.jobs || previous.jobs || [],
    }};
  }}

  async function loadActivity(forceFull = false) {{
    const updatedAt = Number(activityOutput?.updated_at);
    const canRequestDelta = !forceFull && !activityOutput?.summary_only && Number.isFinite(updatedAt) && updatedAt > 0;
    const suffix = canRequestDelta ? `?changed_since=${{encodeURIComponent(updatedAt)}}` : "";
    const next = await getJson(`/serena/panels/${{encodeURIComponent(panelId)}}${{suffix}}`);
    activityOutput = mergeActivity(activityOutput, next);
    loadedRevision = String(activityOutput?.revision || announcedRevision || loadedRevision);
    announcedRevision = loadedRevision;
    stateDirty = false;
    forceFullOnNextRead = false;
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
      if (name === "get_activity" && initialReadPending) {{
        initialReadPending = false;
        return activityOutput;
      }}
      if (name === "get_activity") {{
        const forceFull = Boolean(args?.full) || forceFullOnNextRead;
        if (forceFull || stateDirty || !activityOutput) return loadActivity(forceFull);
        return activityOutput;
      }}
      if (name === "get_activity_detail") return getJson(`/serena/panels/${{encodeURIComponent(panelId)}}/calls/${{encodeURIComponent(args.call_id)}}`);
      if (name === "get_activity_job_detail") return getJson(`/serena/jobs/${{encodeURIComponent(args.job_id)}}`);
      throw new Error(`Unsupported dashboard widget call: ${{name}}`);
    }},
  }};

  window.addEventListener("message", event => {{
    if (event.origin !== location.origin || event.data?.type !== "serena-dashboard-panel") return;
    if (event.data.panel_id !== panelId) return;
    const wasLive = live;
    live = Boolean(event.data.active);
    announcedRevision = String(event.data.revision || announcedRevision);
    if (announcedRevision && announcedRevision !== loadedRevision) stateDirty = true;
    if (live && !wasLive && activityOutput?.summary_only) forceFullOnNextRead = true;
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
  let bootstrap = {{}};
  try {{ bootstrap = JSON.parse(window.name || "{{}}"); window.name = ""; }} catch (_) {{ bootstrap = {{}}; }}
  const panelId = bootstrap.panel_id || fallbackPanelId;
  let activityOutput = bootstrap.tool_output || serverInitialOutput || {{ run_id: panelId, started_at: 0, superseded: false, delegates: [] }};
  let initialReadPending = Boolean(bootstrap.tool_output || serverInitialOutput);
  let live = typeof bootstrap.active === "boolean" ? bootstrap.active : true;
  let loadedRevision = String(bootstrap.revision || "");
  let announcedRevision = loadedRevision;
  let stateDirty = !initialReadPending;

  async function getJson(path) {{
    const response = await fetch(`/dashboard/api${{path}}`, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
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
    if (event.origin !== location.origin || event.data?.type !== "serena-dashboard-panel") return;
    if (event.data.panel_id !== panelId) return;
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
