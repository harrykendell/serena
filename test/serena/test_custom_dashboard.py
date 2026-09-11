from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.dashboard_sessions import OrchestratorDashboardSessionArchive
from serena.dashboard import DashboardServer
from serena.execution_store import ExecutionStore
from serena.git_metrics import GitLineMetrics
from serena.jobs import JobManager
from solidlsp.ls_config import LanguageServerId


class _DashboardAgent:
    def __init__(self, project=None) -> None:
        self.version = "0.0.0"
        self.callbacks = []
        self.project = project
        self.execution_store = ExecutionStore()
        self.job_manager = JobManager()

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

    @staticmethod
    def get_project_git_metrics(project_name: str) -> GitLineMetrics | None:
        if project_name == "serena":
            return GitLineMetrics(additions=17, deletions=4, ahead_commits=3)
        return None

    @staticmethod
    def refresh_project_git_metrics(project_name: str) -> GitLineMetrics | None:
        return _DashboardAgent.get_project_git_metrics(project_name)


def _configure_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))


def _one_execution(agent: _DashboardAgent, *, result: str = "config") -> None:
    agent.execution_store.start_execution(
        execution_id="execution-a",
        session_id="session-a",
        project_name="serena",
        tool_name="get_current_config",
        arguments={},
    )
    agent.execution_store.finish_execution("execution-a", succeeded=True, result=result)


def test_dashboard_serves_shell_overview_and_selected_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    agent = _DashboardAgent()
    _one_execution(agent)
    dashboard = DashboardServer(agent=agent)
    client = dashboard._app.test_client()

    redirect = client.get("/dashboard", base_url="https://serena.kendell.uk")
    response = client.get("/dashboard/")
    dashboard_script = client.get("/dashboard/dashboard.js")
    service_worker = client.get("/dashboard/service-worker.js")
    manifest = client.get("/dashboard/manifest.webmanifest")
    versioned_dashboard_script = client.get("/dashboard/dashboard.js?v=test")
    state = client.get("/dashboard/api/state").get_json()
    panel_id = state["serena"]["panels"][0]["panel_id"]
    selected = client.get(f"/dashboard/api/serena/sessions/{panel_id}").get_json()

    assert redirect.status_code == 302
    assert redirect.headers["Location"] == "/dashboard/"
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert dashboard_script.status_code == 200
    assert dashboard_script.headers["Cache-Control"] == "private, no-cache"
    assert service_worker.status_code == 200
    assert manifest.status_code == 200
    assert versioned_dashboard_script.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert b"Serena + Orchestrator" in response.data
    assert b"notification-button" in response.data
    assert b'id="jobs-button"' in response.data
    assert b"dashboard-bootstrap" in response.data
    assert b"serena-widgets" in response.data
    assert b"orchestrator-widgets" in response.data
    assert state["session"]["runtime_policy"] == "ChatGPT"
    assert state["jobs"] == {"status": "success", "jobs": [], "running_jobs": 0, "max_concurrent_jobs": 12}
    assert state["orchestrator"] == {"status": "success", "panels": []}
    assert len(state["serena"]["panels"]) == 1
    assert selected["panel_id"] == panel_id
    assert [call["call_id"] for call in selected["calls"]] == ["execution-a"]


def test_job_notification_link_redirects_to_originating_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    agent = _DashboardAgent()
    job_id = "0123456789abcdef0123456789abcdef"
    agent.execution_store.start_execution(
        execution_id="execution-job",
        session_id="session-a",
        project_name="thesis",
        tool_name="start_job",
        arguments={},
    )
    agent.execution_store.finish_execution(
        "execution-job",
        succeeded=True,
        result="started",
        durable_job_id=job_id,
        durable_job_label="T07 validation",
    )
    dashboard = DashboardServer(agent=agent)
    client = dashboard._app.test_client()

    response = client.get(f"/dashboard/job/{job_id}")
    panel_id = agent.execution_store.panel_id_for_session("session-a")

    assert response.status_code == 302
    assert response.headers["Location"] == f"/dashboard/?panel={panel_id}&job={job_id}"
    assert client.get("/dashboard/job/ffffffffffffffffffffffffffffffff").headers["Location"] == "/dashboard/"


def test_dashboard_registers_single_web_push_subscription(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    dashboard = DashboardServer(agent=_DashboardAgent())
    client = dashboard._app.test_client()

    config = client.get("/dashboard/api/push/config").get_json()
    response = client.post(
        "/dashboard/api/push/subscribe",
        json={
            "endpoint": "https://push.example.invalid/subscription",
            "keys": {"p256dh": "public-key", "auth": "auth-secret"},
        },
    )

    assert isinstance(config["public_key"], str)
    assert config["public_key"].startswith("B")
    assert response.status_code == 200
    assert response.get_json() == {"status": "success"}


def test_custom_dashboard_can_name_retained_serena_conversation_before_first_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    dashboard = DashboardServer(agent=_DashboardAgent())
    client = dashboard._app.test_client()

    assert dashboard.set_serena_session_name("session-a", "Dashboard naming") == "Dashboard naming"
    state = client.get("/dashboard/api/state").get_json()

    assert len(state["serena"]["panels"]) == 1
    panel = state["serena"]["panels"][0]
    assert panel["display_name"] == "Dashboard naming"
    selected = client.get(f"/dashboard/api/serena/sessions/{panel['panel_id']}").get_json()
    assert selected["session_title"] == "Dashboard naming"


def test_dashboard_revalidates_unchanged_overview_without_response_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    dashboard = DashboardServer(agent=_DashboardAgent())
    dashboard.set_serena_session_name("session-a", "Cached session")
    client = dashboard._app.test_client()

    first = client.get("/dashboard/api/state")
    second = client.get("/dashboard/api/state", headers={"If-None-Match": first.headers["ETag"]})

    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "private, no-cache"
    assert second.status_code == 304
    assert second.data == b""


def test_dashboard_overview_is_compact_and_selected_session_is_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    agent = _DashboardAgent()
    for task in range(1, 13):
        execution_id = f"execution-{task}"
        agent.execution_store.start_execution(
            execution_id=execution_id,
            session_id="session-a",
            project_name="serena",
            tool_name="read_file",
            arguments={"relative_path": f"file-{task}.txt"},
            started_at=float(task),
        )
        agent.execution_store.finish_execution(execution_id, succeeded=True, result=f"file {task}")
    dashboard = DashboardServer(agent=agent)
    client = dashboard._app.test_client()

    state = client.get("/dashboard/api/state").get_json()
    summary = state["serena"]["panels"][0]

    assert summary["active"] is False
    assert summary["tool_count"] == 12
    assert summary["submission_span_seconds"] == 11.0
    assert summary["git_additions"] == 17
    assert summary["git_deletions"] == 4
    assert summary["git_ahead_commits"] == 3
    assert summary["latest_activity"]["scope"] == "file-12.txt"

    selected = client.get(f"/dashboard/api/serena/sessions/{summary['panel_id']}").get_json()
    assert selected["submission_span_seconds"] == 11.0
    assert selected["git_additions"] == 17
    assert selected["git_deletions"] == 4
    assert selected["git_ahead_commits"] == 3
    assert len(selected["calls"]) == 12


def test_dashboard_selected_session_defers_call_body_until_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    agent = _DashboardAgent()
    for index in range(2):
        execution_id = f"execution-{index}"
        agent.execution_store.start_execution(
            execution_id=execution_id,
            session_id="session-a",
            project_name="serena",
            tool_name="read_file",
            arguments={"relative_path": f"file-{index}.txt"},
        )
        agent.execution_store.finish_execution(execution_id, succeeded=True, result=f"retained body {index}")
    dashboard = DashboardServer(agent=agent)
    client = dashboard._app.test_client()
    panel_id = agent.execution_store.panel_id_for_session("session-a")

    document = client.get(f"/dashboard/api/serena/sessions/{panel_id}").get_json()
    assert len(document["calls"]) == 2
    assert all("result" not in call for call in document["calls"])
    assert document["expanded_call"] is None
    assert document["expanded_job"] is None

    expanded = client.get(f"/dashboard/api/serena/sessions/{panel_id}?expanded=execution-1").get_json()
    assert expanded["expanded_call"]["call_id"] == "execution-1"
    assert expanded["expanded_call"]["result"] == "retained body 1"
    assert expanded["expanded_job"] is None


def test_dashboard_orders_serena_sessions_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    dashboard = DashboardServer(agent=_DashboardAgent())
    dashboard.set_serena_session_name("session-a", "First session")
    dashboard.set_serena_session_name("session-b", "Second session")

    panels = dashboard._app.test_client().get("/dashboard/api/state").get_json()["serena"]["panels"]

    assert [panel["display_name"] for panel in panels] == ["Second session", "First session"]


def test_dashboard_orders_orchestrator_sessions_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    orchestrator_root = tmp_path / "orchestrator-home"
    archive = OrchestratorDashboardSessionArchive(OrchestratorConfig.from_environment(orchestrator_root))
    archive.set_display_name("session-a", "First session")
    archive.set_display_name("session-b", "Second session")
    dashboard = DashboardServer(agent=_DashboardAgent())

    panels = dashboard._app.test_client().get("/dashboard/api/state").get_json()["orchestrator"]["panels"]

    assert [panel["display_name"] for panel in panels] == ["Second session", "First session"]


def test_custom_dashboard_shows_named_orchestrator_conversation_before_first_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_roots(tmp_path, monkeypatch)
    orchestrator_root = tmp_path / "orchestrator-home"
    config = OrchestratorConfig.from_environment(orchestrator_root)
    OrchestratorDashboardSessionArchive(config).set_display_name("session-a", "Automatic Session Titles")
    dashboard = DashboardServer(agent=_DashboardAgent())
    client = dashboard._app.test_client()

    panel = client.get("/dashboard/api/state").get_json()["orchestrator"]["panels"][0]
    selected = client.get(f"/dashboard/api/orchestrator/sessions/{panel['panel_id']}").get_json()

    assert panel["display_name"] == "Automatic Session Titles"
    assert panel["delegates"] == []
    assert selected["display_name"] == "Automatic Session Titles"
    assert selected["delegates"] == []


def test_retained_serena_session_preserves_semantic_detail_scope_and_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    agent = _DashboardAgent()
    agent.execution_store.start_execution(
        execution_id="search-execution",
        session_id="session-a",
        project_name="serena",
        tool_name="search_for_pattern",
        arguments={"substring_pattern": "ActivityRunManager.*detail", "relative_path": "src/serena"},
    )
    agent.execution_store.finish_execution("search-execution", succeeded=True, result="matches")
    agent.execution_store.start_execution(
        execution_id="replace-execution",
        session_id="session-a",
        project_name="serena",
        tool_name="replace_in_files",
        arguments={"needle": "old value", "repl": "new value", "mode": "literal", "relative_path": "src/serena"},
    )
    agent.execution_store.finish_execution("replace-execution", succeeded=True, result="changed")
    dashboard = DashboardServer(agent=agent)
    client = dashboard._app.test_client()
    panel_id = client.get("/dashboard/api/state").get_json()["serena"]["panels"][0]["panel_id"]

    selected = client.get(f"/dashboard/api/serena/sessions/{panel_id}").get_json()
    calls = {call["tool_name"]: call for call in selected["calls"]}
    assert calls["search_for_pattern"]["detail"] == "ActivityRunManager.*detail"
    assert calls["search_for_pattern"]["scope"] == "src/serena"
    assert calls["replace_in_files"]["detail"] == "old value"
    assert calls["replace_in_files"]["scope"] == "src/serena"

    expanded = client.get(f"/dashboard/api/serena/sessions/{panel_id}?expanded=replace-execution").get_json()
    assert expanded["expanded_call"]["structured_arguments"] == {
        "needle": "old value",
        "repl": "new value",
        "mode": "literal",
        "relative_path": "src/serena",
    }


def test_retained_serena_session_exposes_typed_shell_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    agent = _DashboardAgent()
    agent.execution_store.start_execution(
        execution_id="shell-execution",
        session_id="session-a",
        project_name="serena",
        tool_name="execute_shell_command",
        arguments={"command": "printf hello"},
    )
    agent.execution_store.finish_execution(
        "shell-execution",
        succeeded=True,
        result='{"return_code": 0, "stdout": "hello"}',
    )
    dashboard = DashboardServer(agent=agent)
    client = dashboard._app.test_client()
    panel_id = client.get("/dashboard/api/state").get_json()["serena"]["panels"][0]["panel_id"]

    expanded = client.get(f"/dashboard/api/serena/sessions/{panel_id}?expanded=shell-execution").get_json()

    assert expanded["expanded_call"]["structured_result"] == {"return_code": 0, "stdout": "hello"}


def test_retained_serena_session_serves_rendered_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_roots(tmp_path, monkeypatch)
    serena_home = tmp_path / "serena-home"
    snapshot_root = serena_home / "chat_file_snapshots"
    snapshot_root.mkdir(parents=True, mode=0o700)
    snapshot_root.chmod(0o700)
    token = "a" * 64
    image_bytes = b"\x89PNG\r\n\x1a\nretained-preview"

    agent = _DashboardAgent()
    (snapshot_root / token).write_bytes(image_bytes)
    agent.execution_store.start_execution(
        execution_id="render-execution",
        session_id="session-a",
        project_name="serena",
        tool_name="render_pdf_page",
        arguments={"relative_path": "figure.pdf", "page": 1, "dpi": 150},
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
    restored_dashboard = DashboardServer(agent=_DashboardAgent())
    client = restored_dashboard._app.test_client()
    panel_id = client.get("/dashboard/api/state").get_json()["serena"]["panels"][0]["panel_id"]
    expanded = client.get(f"/dashboard/api/serena/sessions/{panel_id}?expanded=render-execution").get_json()

    assert expanded["expanded_call"]["result"] is None
    assert expanded["expanded_call"]["media"] == {
        "type": "image",
        "name": "figure-p1.png",
        "mime_type": "image/png",
    }
    response = client.get(f"/dashboard/api/serena/sessions/{panel_id}/media/render-execution")
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
    dashboard = DashboardServer(agent=_DashboardAgent(project))

    session = dashboard._app.test_client().get("/dashboard/api/state").get_json()["session"]

    assert session["languages"] == ["python", "html"]


def test_memory_endpoint_reads_active_project_memory() -> None:
    memory_manager = MagicMock()
    memory_manager.load_memory.return_value = "# Critical info\n\nMemory body"
    project = SimpleNamespace(memory_manager=memory_manager)
    dashboard = DashboardServer(agent=_DashboardAgent(project))

    response = dashboard._app.test_client().get("/dashboard/api/memory?name=critical_info").get_json()

    assert response == {
        "status": "success",
        "memory_name": "critical_info",
        "content": "# Critical info\n\nMemory body",
    }
    memory_manager.load_memory.assert_called_once_with("critical_info")
