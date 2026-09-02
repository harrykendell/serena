from __future__ import annotations

import json

from orchestrator.activity import activity_widget_html as orchestrator_activity_widget_html
from serena.activity import activity_widget_html as serena_activity_widget_html


def serena_dashboard_widget_html(panel_id: str) -> str:
    """Returns one retained Serena session widget connected to dashboard data."""
    panel_json = json.dumps(panel_id)
    adapter = f"""<script>
(() => {{
  const panelId = {panel_json};

  async function getJson(path) {{
    const response = await fetch(`/dashboard/api${{path}}`, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
    if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
    const data = await response.json();
    if (data?.status === "error") throw new Error(data.message || "Dashboard API error");
    return data;
  }}

  function notifyIntrinsicHeight() {{
    requestAnimationFrame(() => parent.postMessage({{ type: "serena-activity-height", height: document.documentElement.scrollHeight }}, location.origin));
  }}

  window.openai = {{
    toolOutput: {{ run_id: panelId, project_name: "", superseded: false, calls: [], jobs: [] }},
    notifyIntrinsicHeight,
    callTool: async (name, args) => {{
      if (name === "get_activity") return getJson(`/serena/panels/${{encodeURIComponent(panelId)}}`);
      if (name === "get_activity_detail") return getJson(`/serena/panels/${{encodeURIComponent(panelId)}}/calls/${{encodeURIComponent(args.call_id)}}`);
      if (name === "get_activity_job_detail") return getJson(`/serena/jobs/${{encodeURIComponent(args.job_id)}}`);
      throw new Error(`Unsupported dashboard widget call: ${{name}}`);
    }},
  }};
}})();
</script>
<style>html, body {{ overflow: hidden; }}</style>
"""
    return adapter + serena_activity_widget_html()


def orchestrator_dashboard_widget_html(panel_id: str) -> str:
    """Returns one Orchestrator activity widget connected to an operator panel."""
    panel_json = json.dumps(panel_id)
    adapter = f"""<script>
(() => {{
  const panelId = {panel_json};

  async function getJson(path) {{
    const response = await fetch(`/dashboard/api${{path}}`, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
    if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
    const data = await response.json();
    if (data?.status === "error") throw new Error(data.message || "Dashboard API error");
    return data;
  }}

  function notifyIntrinsicHeight() {{
    requestAnimationFrame(() => parent.postMessage({{ type: "serena-activity-height", height: document.documentElement.scrollHeight }}, location.origin));
  }}

  window.openai = {{
    toolOutput: {{ run_id: panelId, started_at: 0, superseded: false, delegates: [] }},
    notifyIntrinsicHeight,
    callTool: async (name, args) => {{
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
