from __future__ import annotations

import json

from orchestrator.activity import activity_widget_html as orchestrator_activity_widget_html
from serena.activity import activity_widget_html as serena_activity_widget_html


def serena_dashboard_widget_html(panel_id: str | None = None, initial_state: dict[str, object] | None = None) -> str:
    """Returns one retained Serena session widget connected to dashboard data."""
    panel_json = json.dumps(panel_id or "")
    initial_state_json = json.dumps(initial_state).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    adapter = f"""<script>
(() => {{
  const fallbackPanelId = {panel_json};
  const serverInitialOutput = {initial_state_json};
  let bootstrap = {{}};
  try {{ bootstrap = JSON.parse(window.name || "{{}}"); window.name = ""; }} catch (_) {{ bootstrap = {{}}; }}
  const panelId = bootstrap.panel_id || fallbackPanelId;
  const initialOutput = bootstrap.tool_output || serverInitialOutput || {{ run_id: panelId, project_name: "", superseded: false, calls: [], jobs: [] }};
  let initialReadPending = Boolean(bootstrap.tool_output || serverInitialOutput);
  let live = typeof bootstrap.active === "boolean" ? bootstrap.active : true;
  let retainedOutput = !live && initialReadPending ? initialOutput : null;

  async function getJson(path) {{
    const response = await fetch(`/dashboard/api${{path}}`, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
    if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
    const data = await response.json();
    if (data?.status === "error") throw new Error(data.message || "Dashboard API error");
    return data;
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
        if (!live) retainedOutput = initialOutput;
        return initialOutput;
      }}
      if (name === "get_activity") {{
        if (!live && retainedOutput) return retainedOutput;
        const next = await getJson(`/serena/panels/${{encodeURIComponent(panelId)}}`);
        if (!live) retainedOutput = next;
        return next;
      }}
      if (name === "get_activity_detail") return getJson(`/serena/panels/${{encodeURIComponent(panelId)}}/calls/${{encodeURIComponent(args.call_id)}}`);
      if (name === "get_activity_job_detail") return getJson(`/serena/jobs/${{encodeURIComponent(args.job_id)}}`);
      throw new Error(`Unsupported dashboard widget call: ${{name}}`);
    }},
  }};

  window.addEventListener("message", event => {{
    if (event.origin !== location.origin || event.data?.type !== "serena-dashboard-live") return;
    if (event.data.panel_id !== panelId) return;
    const wasLive = live;
    live = Boolean(event.data.active);
    if (live && !wasLive) retainedOutput = null;
  }});
}})();
</script>
<style>html, body {{ overflow: hidden; }}</style>
"""
    return adapter + serena_activity_widget_html()


def orchestrator_dashboard_widget_html(panel_id: str | None = None, initial_state: dict[str, object] | None = None) -> str:
    """Returns one Orchestrator activity widget connected to an operator panel."""
    panel_json = json.dumps(panel_id or "")
    initial_state_json = json.dumps(initial_state).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    adapter = f"""<script>
(() => {{
  const fallbackPanelId = {panel_json};
  const serverInitialOutput = {initial_state_json};
  let bootstrap = {{}};
  try {{ bootstrap = JSON.parse(window.name || "{{}}"); window.name = ""; }} catch (_) {{ bootstrap = {{}}; }}
  const panelId = bootstrap.panel_id || fallbackPanelId;
  const initialOutput = bootstrap.tool_output || serverInitialOutput || {{ run_id: panelId, started_at: 0, superseded: false, delegates: [] }};
  let initialReadPending = Boolean(bootstrap.tool_output || serverInitialOutput);

  async function getJson(path) {{
    const response = await fetch(`/dashboard/api${{path}}`, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
    if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
    const data = await response.json();
    if (data?.status === "error") throw new Error(data.message || "Dashboard API error");
    return data;
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
      if (name === "get_orchestrator_activity" && initialReadPending) {{
        initialReadPending = false;
        return initialOutput;
      }}
      if (name === "get_orchestrator_activity") return getJson(`/orchestrator/panels/${{encodeURIComponent(panelId)}}`);
      if (name === "get_orchestrator_delegate_detail") return getJson(`/orchestrator/delegates/${{encodeURIComponent(args.delegate_id)}}`);
      throw new Error(`Unsupported dashboard widget call: ${{name}}`);
    }},
  }};
}})();
</script>
<style>html, body {{ overflow: hidden; }} .fallback-action {{ display: none !important; }}</style>
"""
    return adapter + orchestrator_activity_widget_html()
