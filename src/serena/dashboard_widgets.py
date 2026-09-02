"""Adapters that reuse ChatGPT activity widgets in the Serena web dashboard."""

from __future__ import annotations

import json

from orchestrator.activity import activity_widget_html as orchestrator_activity_widget_html
from serena.activity import activity_widget_html as serena_activity_widget_html


def serena_dashboard_widget_html(mode: str) -> str:
    """Returns the Serena activity widget connected to global dashboard data."""
    if mode not in {"tools", "jobs"}:
        raise ValueError(f"Unsupported Serena dashboard activity mode {mode!r}")

    mode_json = json.dumps(mode)
    adapter = f"""<script>
(() => {{
  const mode = {mode_json};
  const runId = `dashboard-${{mode}}`;

  async function getJson(path) {{
    const response = await fetch(`/dashboard/api${{path}}`, {{ cache: "no-store", headers: {{ Accept: "application/json" }} }});
    if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
    const data = await response.json();
    if (data?.status === "error") throw new Error(data.message || "Dashboard API error");
    return data;
  }}

  function toolName(name) {{
    const raw = String(name || "execution").replace(/^Task-\\d+:/, "").replace(/^BackgroundTask:/, "");
    if (!raw.endsWith("Tool")) return raw;
    return raw.slice(0, -4).replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2").replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
  }}

  function executionCall(item) {{
    return {{
      call_id: String(item.task_id),
      tool_name: toolName(item.name),
      detail: item.detail || "",
      status: item.status || "completed",
      submitted_at: item.submitted_at ?? item.started_at ?? Date.now() / 1000,
      started_at: item.started_at ?? item.submitted_at ?? Date.now() / 1000,
      finished_at: item.finished_at ?? null,
    }};
  }}

  function seconds(value) {{
    if (!value) return null;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed / 1000 : null;
  }}

  function jobEntry(item) {{
    return {{
      job_id: item.job_id,
      label: item.label || "background job",
      project: item.project || "",
      status: item.status || "completed",
      started_at: seconds(item.created_at) ?? Date.now() / 1000,
      finished_at: seconds(item.finished_at),
      current_turn: true,
    }};
  }}

  async function snapshot() {{
    if (mode === "tools") {{
      const data = await getJson("/executions");
      return {{ run_id: runId, project_name: "all projects", superseded: false, calls: (data.executions || []).map(executionCall), jobs: [] }};
    }}
    const data = await getJson("/jobs");
    return {{ run_id: runId, project_name: "all projects", superseded: false, calls: [], jobs: (data.jobs || []).map(jobEntry) }};
  }}

  function stringify(value) {{
    if (value === null || value === undefined) return null;
    if (typeof value === "string") return value;
    try {{ return JSON.stringify(value, null, 2); }} catch (_) {{ return String(value); }}
  }}

  async function toolDetail(callId) {{
    const data = await getJson("/executions");
    const item = (data.executions || []).find(entry => String(entry.task_id) === String(callId));
    if (!item) throw new Error("Tool execution unavailable");
    let result = item.error || stringify(item.result);
    if (!result && item.stream_output_id) {{
      try {{ result = (await getJson(`/executions/${{encodeURIComponent(item.task_id)}}/output`)).output; }} catch (_) {{ /* retained output is optional */ }}
    }}
    return {{
      call_id: String(item.task_id),
      status: item.status,
      arguments: stringify(item.parameters) || "{{}}",
      result,
    }};
  }}

  async function jobDetail(jobId) {{
    const jobs = await getJson("/jobs");
    const item = (jobs.jobs || []).find(entry => entry.job_id === jobId);
    if (!item) throw new Error("Job unavailable");
    let output = {{ output: "", has_earlier_output: false, earlier_output_omitted: false }};
    try {{ output = await getJson(`/jobs/${{encodeURIComponent(jobId)}}/output`); }} catch (_) {{ /* a new job may not have output yet */ }}
    return {{
      job_id: item.job_id,
      status: item.status,
      project: item.project,
      elapsed_seconds: item.elapsed_seconds,
      seconds_since_last_output: item.seconds_since_last_output,
      memory_bytes: item.memory_bytes,
      cpu_seconds: item.cpu_seconds,
      process_count: item.process_count,
      timeout_seconds: item.timeout_seconds,
      return_code: item.return_code,
      output: output.output || "",
      has_earlier_output: Boolean(output.has_earlier_output),
      earlier_output_omitted: Boolean(output.earlier_output_omitted),
    }};
  }}

  function notifyIntrinsicHeight() {{
    requestAnimationFrame(() => parent.postMessage({{ type: "serena-activity-height", height: document.documentElement.scrollHeight }}, location.origin));
  }}

  window.openai = {{
    toolOutput: {{ run_id: runId, project_name: "all projects", superseded: false, calls: [], jobs: [] }},
    notifyIntrinsicHeight,
    callTool: async (name, args) => {{
      if (name === "get_activity") return snapshot();
      if (name === "get_activity_detail") return toolDetail(args.call_id);
      if (name === "get_activity_job_detail") return jobDetail(args.job_id);
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
