from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.dashboard_sessions import OrchestratorDashboardSessionArchive
from serena.dashboard import DashboardServer
from serena.execution_store import ExecutionStore
from solidlsp.ls_config import LanguageServerId


class _DashboardAgent:
    def __init__(self, project=None) -> None:
        self.version = "0.0.0"
        self.callbacks = []
        self.project = project
        self.execution_store = ExecutionStore()

    def register_config_changed_callback(self, callback) -> None:
        self.callbacks.append(callback)

    def get_active_project(self):
        return self.project

    def get_default_project(self):
        return self.project

    def get_active_tool_names(self):
        return []

    def get_exposed_tool_instances(self):
        return []


def test_dashboard_serves_kendell_frontend_and_session_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    agent = _DashboardAgent()
    agent.execution_store.start_execution(
        execution_id="execution-a",
        session_id="session-a",
        project_name="serena",
        tool_name="get_current_config",
        arguments="{}",
    )
    agent.execution_store.finish_execution("execution-a", succeeded=True, result="config")
    dashboard = DashboardServer(
        agent=agent,
    )
    client = dashboard._app.test_client()

    redirect = client.get("/dashboard", base_url="https://serena.kendell.uk")
    response = client.get("/dashboard/")
    dashboard_script = client.get("/dashboard/dashboard.js")
    versioned_dashboard_script = client.get("/dashboard/dashboard.js?v=test")
    state = client.get("/dashboard/api/state?include_state=1").get_json()
    session = client.get("/dashboard/api/session").get_json()
    serena = client.get("/dashboard/api/serena").get_json()
    serena_with_state = client.get("/dashboard/api/serena?include_state=1").get_json()
    serena_widget = client.get("/dashboard/widget/serena")
    serena_panel_widget = client.get(f"/dashboard/widget/serena/{serena['panels'][0]['panel_id']}")
    orchestrator = client.get("/dashboard/api/orchestrator").get_json()

    assert redirect.status_code == 302
    assert redirect.headers["Location"] == "/dashboard/"
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert dashboard_script.status_code == 200
    assert dashboard_script.headers["Cache-Control"] == "private, no-cache"
    assert versioned_dashboard_script.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert b"Serena + Orchestrator" in response.data
    assert b"MCP dashboard" in response.data
    assert b"orchestrator-logo.svg" in response.data
    assert b"One retained activity panel for each ChatGPT conversation" in response.data
    assert b"serena-widgets" in response.data
    assert b"dashboard-bootstrap" in response.data
    assert b"dashboard.js?v=" in response.data
    assert b"styles.css?v=" in response.data
    assert b"Orchestrator" in response.data
    assert b"window.openai" in serena_widget.data
    assert b"get_activity" in serena_widget.data
    assert b"get_activity_job_detail" in serena_widget.data
    assert serena_widget.headers["Cache-Control"] == "private, max-age=3600"
    assert b"get_activity" in serena_panel_widget.data
    assert serena_panel_widget.headers["Cache-Control"] == "private, no-store"
    assert len(serena["panels"]) == 1
    assert serena_with_state["panels"][0]["initial_state"]["run_id"] == serena["panels"][0]["panel_id"]
    assert state["serena"]["panels"][0]["panel_id"] == serena["panels"][0]["panel_id"]
    assert state["orchestrator"] == {"status": "success", "panels": []}
    assert orchestrator == {"status": "success", "panels": []}
    assert session["status"] == "success"
    assert session["runtime_policy"] == "ChatGPT"


def test_custom_dashboard_can_name_retained_serena_conversation_before_first_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    dashboard = DashboardServer(
        agent=_DashboardAgent(),
    )
    client = dashboard._app.test_client()

    assert dashboard.set_serena_session_name("session-a", "Dashboard naming") == "Dashboard naming"
    overview = client.get("/dashboard/api/serena").get_json()

    assert len(overview["panels"]) == 1
    assert overview["panels"][0]["display_name"] == "Dashboard naming"


def test_dashboard_revalidates_unchanged_panel_overview_without_response_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    dashboard = DashboardServer(
        agent=_DashboardAgent(),
    )
    dashboard.set_serena_session_name("session-a", "Cached session")
    client = dashboard._app.test_client()

    first = client.get("/dashboard/api/serena")
    second = client.get("/dashboard/api/serena", headers={"If-None-Match": first.headers["ETag"]})

    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "private, no-cache"
    assert second.status_code == 304
    assert second.data == b""


def test_dashboard_bootstraps_inactive_serena_panels_with_compact_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    agent = _DashboardAgent()
    for task in range(1, 13):
        execution_id = f"execution-{task}"
        agent.execution_store.start_execution(
            execution_id=execution_id,
            session_id="session-a",
            project_name="serena",
            tool_name="read_file",
            arguments=f'{{"relative_path": "file-{task}.txt"}}',
            started_at=float(task),
        )
        agent.execution_store.finish_execution(execution_id, succeeded=True, result=f"file {task}")
    dashboard = DashboardServer(
        agent=agent,
    )
    client = dashboard._app.test_client()

    panel = client.get("/dashboard/api/serena?include_state=1").get_json()["panels"][0]
    state = panel["initial_state"]

    assert panel["active"] is False
    assert state["summary_only"] is True
    assert state["tool_count"] == 12
    assert len(state["calls"]) == 8
    assert state["calls"][-1]["scope"] == "file-12.txt"


def test_dashboard_orders_serena_panels_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    dashboard = DashboardServer(
        agent=_DashboardAgent(),
    )

    dashboard.set_serena_session_name("session-a", "First session")
    dashboard.set_serena_session_name("session-b", "Second session")
    panels = dashboard._app.test_client().get("/dashboard/api/serena").get_json()["panels"]

    assert [panel["display_name"] for panel in panels] == ["Second session", "First session"]


def test_dashboard_orders_orchestrator_panels_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator_root = tmp_path / "orchestrator-home"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(orchestrator_root))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    archive = OrchestratorDashboardSessionArchive(OrchestratorConfig.from_environment(orchestrator_root))
    archive.set_display_name("session-a", "First session")
    archive.set_display_name("session-b", "Second session")
    dashboard = DashboardServer(
        agent=_DashboardAgent(),
    )

    panels = dashboard._app.test_client().get("/dashboard/api/orchestrator").get_json()["panels"]

    assert [panel["display_name"] for panel in panels] == ["Second session", "First session"]


def test_custom_dashboard_shows_named_orchestrator_conversation_before_first_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retained Orchestrator session metadata creates a named dashboard panel before delegation starts."""
    orchestrator_root = tmp_path / "orchestrator-home"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(orchestrator_root))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    config = OrchestratorConfig.from_environment(orchestrator_root)
    OrchestratorDashboardSessionArchive(config).set_display_name("session-a", "Automatic Session Titles")

    dashboard = DashboardServer(
        agent=_DashboardAgent(),
    )
    overview = dashboard._app.test_client().get("/dashboard/api/orchestrator").get_json()

    assert len(overview["panels"]) == 1
    panel = overview["panels"][0]
    assert panel["display_name"] == "Automatic Session Titles"
    assert panel["delegates"] == []


def test_retained_serena_panel_preserves_semantic_detail_and_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    agent = _DashboardAgent()
    agent.execution_store.start_execution(
        execution_id="search-execution",
        session_id="session-a",
        project_name="serena",
        tool_name="search_for_pattern",
        arguments='{"substring_pattern": "ActivityTracker.*detail", "relative_path": "src/serena"}',
    )
    agent.execution_store.start_execution(
        execution_id="replace-execution",
        session_id="session-a",
        project_name="serena",
        tool_name="replace_in_files",
        arguments='{"needle": "old value", "repl": "new value", "mode": "literal", "relative_path": "src/serena"}',
    )
    dashboard = DashboardServer(
        agent=agent,
    )
    client = dashboard._app.test_client()

    overview = client.get("/dashboard/api/serena").get_json()
    panel_id = overview["panels"][0]["panel_id"]
    panel = client.get(f"/dashboard/api/serena/panels/{panel_id}").get_json()

    calls = {call["tool_name"]: call for call in panel["calls"]}
    assert calls["search_for_pattern"]["detail"] == "ActivityTracker.*detail"
    assert calls["search_for_pattern"]["scope"] == "src/serena"
    assert calls["replace_in_files"]["detail"] == "old value"
    assert calls["replace_in_files"]["scope"] == "src/serena"

    detail = client.get(f"/dashboard/api/serena/panels/{panel_id}/calls/{calls['replace_in_files']['call_id']}").get_json()
    assert detail["structured_arguments"] == {
        "needle": "old value",
        "repl": "new value",
        "mode": "literal",
        "relative_path": "src/serena",
    }


def test_retained_serena_panel_serves_rendered_media_instead_of_result_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    serena_home = tmp_path / "serena-home"
    monkeypatch.setenv("SERENA_HOME", str(serena_home))
    snapshot_root = serena_home / "chat_file_snapshots"
    snapshot_root.mkdir(parents=True, mode=0o700)
    snapshot_root.chmod(0o700)
    token = "a" * 48
    image_bytes = b"\x89PNG\r\n\x1a\nretained-preview"
    (snapshot_root / token).write_bytes(image_bytes)

    agent = _DashboardAgent()
    agent.execution_store.start_execution(
        execution_id="render-execution",
        session_id="session-a",
        project_name="serena",
        tool_name="render_pdf_page",
        arguments='{"relative_path": "figure.pdf", "page": 1, "dpi": 150}',
    )
    agent.execution_store.finish_execution(
        "render-execution",
        succeeded=True,
        media={
            "type": "image",
            "name": "figure-p1.png",
            "mime_type": "image/png",
            "uri": f"serena-file://export/{token}",
        },
    )
    restored_dashboard = DashboardServer(
        agent=_DashboardAgent(),
    )
    client = restored_dashboard._app.test_client()
    overview = client.get("/dashboard/api/serena").get_json()
    panel_id = overview["panels"][0]["panel_id"]
    panel = client.get(f"/dashboard/api/serena/panels/{panel_id}").get_json()
    call_id = panel["calls"][0]["call_id"]
    detail = client.get(f"/dashboard/api/serena/panels/{panel_id}/calls/{call_id}").get_json()

    assert detail["result"] is None
    assert detail["media"] == {
        "type": "image",
        "name": "figure-p1.png",
        "mime_type": "image/png",
        "url": f"/dashboard/api/serena/panels/{panel_id}/calls/{call_id}/media",
    }
    response = client.get(detail["media"]["url"])
    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.data == image_bytes


def test_custom_dashboard_uses_default_project_and_dynamic_languages() -> None:
    memory_manager = SimpleNamespace(list_memories=lambda: SimpleNamespace(get_full_list=list))
    project = SimpleNamespace(
        project_name="project-a",
        project_root="/tmp/project-a",
        memory_manager=memory_manager,
        get_language_server_candidates=lambda: [LanguageServerId.PYTHON, LanguageServerId.HTML],
    )
    dashboard = DashboardServer(
        agent=_DashboardAgent(project),
    )

    session = dashboard._app.test_client().get("/dashboard/api/session").get_json()

    assert session["active_project"] == {"name": "project-a", "path": "/tmp/project-a"}
    assert session["languages"] == ["python", "html"]


def test_memory_endpoint_reads_active_project_memory() -> None:
    memory_manager = MagicMock()
    memory_manager.load_memory.return_value = "# Critical info\n\nMemory body"
    project = SimpleNamespace(memory_manager=memory_manager)
    dashboard = DashboardServer(
        agent=_DashboardAgent(project),
    )

    response = dashboard._app.test_client().get("/dashboard/api/memory?name=critical_info").get_json()

    assert response == {
        "status": "success",
        "memory_name": "critical_info",
        "content": "# Critical info\n\nMemory body",
    }
    memory_manager.load_memory.assert_called_once_with("critical_info")
