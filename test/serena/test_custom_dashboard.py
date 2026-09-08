from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import Image
from mcp.types import ResourceLink
from pydantic import AnyUrl

from orchestrator.config import OrchestratorConfig
from orchestrator.dashboard_sessions import OrchestratorDashboardSessionArchive
from serena.custom_dashboard import DashboardExecutionHistory, DashboardJobOverview
from serena.dashboard import SerenaDashboardAPI
from serena.jobs import JobPersistenceInfo, JobRecord, JobRuntimeInfo, JobSnapshot, JobStatus
from serena.task_executor import TaskExecutor
from serena.tool_output import ToolOutputPage
from serena.tools import FetchMediaFileTool
from solidlsp.ls_config import LanguageServerId


class _DummyMemoryLogHandler:
    def __init__(self) -> None:
        self.callbacks = []

    def add_emit_callback(self, callback) -> None:
        self.callbacks.append(callback)

    def emit_message(self, message: str) -> None:
        for callback in self.callbacks:
            callback(message)

    def get_log_messages(self, from_idx: int = 0):
        del from_idx
        return SimpleNamespace(messages=[], max_idx=-1)

    def clear_log_messages(self) -> None:
        pass


class _DashboardAgent:
    def __init__(self, project=None) -> None:
        self.version = "0.0.0"
        self.callbacks = []
        self.project = project
        self.current_tasks: list[TaskExecutor.TaskInfo] = []
        self.output_descriptor: object | None = None
        self.output_page: ToolOutputPage | None = None

    def register_config_changed_callback(self, callback) -> None:
        self.callbacks.append(callback)

    def get_active_project(self):
        return self.project

    def get_default_project(self):
        return self.project

    def get_current_tasks(self):
        return list(self.current_tasks)

    def describe_tool_execution_output(self, execution_name: str):
        del execution_name
        return self.output_descriptor

    def read_tool_execution_tail(self, execution_name: str, max_chars: int):
        del execution_name, max_chars
        return self.output_page

    def get_last_executed_task(self):
        return None

    def get_active_tool_names(self):
        return []

    def get_active_modes(self):
        return SimpleNamespace(get_modes=lambda include_background_base_modes=False: [])

    def get_context(self):
        return SimpleNamespace(name="chatgpt")

    def get_exposed_tool_instances(self):
        return []


def _task_info(name: str, *, is_running: bool, state: str, task_id: int, result=None) -> TaskExecutor.TaskInfo:
    future: Future = Future()
    if state == "completed":
        future.set_result(result)
    elif state == "failed":
        future.set_exception(RuntimeError("failed"))
    elif state == "cancelled":
        future.cancel()
    started_at = 1_001.0 if is_running or state != "pending" else None
    finished_at = 1_002.0 if state in {"completed", "failed", "cancelled"} else None
    return TaskExecutor.TaskInfo(
        name=name,
        is_running=is_running,
        future=future,
        task_id=task_id,
        logged=True,
        submitted_at=1_000.0,
        started_at=started_at,
        finished_at=finished_at,
    )


def test_custom_dashboard_serves_fork_specific_frontend_and_session_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    log_handler = _DummyMemoryLogHandler()
    dashboard = SerenaDashboardAPI(
        memory_log_handler=log_handler,
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
    )
    client = dashboard._app.test_client()
    log_handler.emit_message(
        "INFO [Task-1:GetCurrentConfigTool] serena.tools.tools_base:_log_tool_application:291 - "
        "get_current_config: ; project: serena; session_id: session-a"
    )

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
    assert session["context"] == "chatgpt"


def test_custom_dashboard_can_name_retained_serena_conversation_before_first_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    log_handler = _DummyMemoryLogHandler()
    dashboard = SerenaDashboardAPI(
        memory_log_handler=log_handler,
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
    )
    client = dashboard._app.test_client()

    assert dashboard.set_serena_session_name("session-a", "Dashboard naming") == "Dashboard naming"
    overview = client.get("/dashboard/api/serena").get_json()

    assert len(overview["panels"]) == 1
    assert overview["panels"][0]["display_name"] == "Dashboard naming"


def test_dashboard_revalidates_unchanged_panel_overview_without_response_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    dashboard = SerenaDashboardAPI(
        memory_log_handler=_DummyMemoryLogHandler(),
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
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
    log_handler = _DummyMemoryLogHandler()
    dashboard = SerenaDashboardAPI(
        memory_log_handler=log_handler,
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
    )
    client = dashboard._app.test_client()
    for task in range(1, 13):
        log_handler.emit_message(
            f"INFO [Task-{task}:ReadFileTool] serena.tools.tools_base:_log_tool_application:291 - "
            f"read_file: relative_path='file-{task}.txt'; project: serena; session_id: session-a"
        )
        log_handler.emit_message(f"INFO [Task-{task}:ReadFileTool] serena.tools.tools_base:apply_ex:410 - Result: file {task}")

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
    dashboard = SerenaDashboardAPI(
        memory_log_handler=_DummyMemoryLogHandler(),
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
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
    dashboard = SerenaDashboardAPI(
        memory_log_handler=_DummyMemoryLogHandler(),
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
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

    dashboard = SerenaDashboardAPI(
        memory_log_handler=_DummyMemoryLogHandler(),
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
    )
    overview = dashboard._app.test_client().get("/dashboard/api/orchestrator").get_json()

    assert len(overview["panels"]) == 1
    panel = overview["panels"][0]
    assert panel["display_name"] == "Automatic Session Titles"
    assert panel["delegates"] == []


def test_retained_serena_panel_preserves_semantic_detail_and_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "orchestrator-home"))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    log_handler = _DummyMemoryLogHandler()
    dashboard = SerenaDashboardAPI(
        memory_log_handler=log_handler,
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
    )
    client = dashboard._app.test_client()

    log_handler.emit_message(
        "INFO [Task-1:SearchForPatternTool] serena.tools.tools_base:_log_tool_application:291 - "
        "search_for_pattern: substring_pattern='ActivityTracker.*detail', relative_path='src/serena'; "
        "project: serena; session_id: session-a"
    )
    log_handler.emit_message(
        "INFO [Task-2:ReplaceInFilesTool] serena.tools.tools_base:_log_tool_application:291 - "
        "replace_in_files: needle='old value', repl='new value', mode='literal', relative_path='src/serena'; "
        "project: serena; session_id: session-a"
    )

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

    log_handler = _DummyMemoryLogHandler()
    dashboard = SerenaDashboardAPI(
        memory_log_handler=log_handler,
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
    )
    client = dashboard._app.test_client()
    log_handler.emit_message(
        "INFO [Task-3:RenderPdfPageTool] serena.tools.tools_base:_log_tool_application:291 - "
        "render_pdf_page: relative_path='figure.pdf', page=1, dpi=150; project: serena; session_id: session-a"
    )
    log_handler.emit_message(
        "INFO [Task-3:RenderPdfPageTool] serena.tools.tools_base:apply_ex:410 - Result: "
        f"_NativeMediaResult(media=<Image>, file_link=ResourceLink(name='figure-p1.png', "
        f"uri=AnyUrl('serena-file://export/{token}'), mimeType='image/png', size={len(image_bytes)}))"
    )

    restored_dashboard = SerenaDashboardAPI(
        memory_log_handler=_DummyMemoryLogHandler(),
        tool_names=[],
        agent=_DashboardAgent(),
        tool_usage_stats=None,
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
    dashboard = SerenaDashboardAPI(
        memory_log_handler=_DummyMemoryLogHandler(),
        tool_names=[],
        agent=_DashboardAgent(project),
        tool_usage_stats=None,
    )

    session = dashboard._app.test_client().get("/dashboard/api/session").get_json()

    assert session["active_project"] == {"name": "project-a", "path": "/tmp/project-a"}
    assert session["languages"] == ["python", "html"]


def test_custom_dashboard_serves_live_execution_output_endpoint() -> None:
    agent = _DashboardAgent()
    running = _task_info("Task-7:ExecuteShellCommandTool", is_running=True, state="pending", task_id=7)
    agent.current_tasks = [running]
    agent.output_descriptor = SimpleNamespace(output_id="abc123", total_chars=5)
    agent.output_page = ToolOutputPage(
        output_id="abc123",
        tool_name="execute_shell_command",
        total_chars=5,
        offset=0,
        content="hello",
        next_offset=None,
    )
    dashboard = SerenaDashboardAPI(
        memory_log_handler=_DummyMemoryLogHandler(),
        tool_names=[],
        agent=agent,
        tool_usage_stats=None,
    )
    client = dashboard._app.test_client()

    executions = client.get("/dashboard/api/executions").get_json()
    response = client.get("/dashboard/api/executions/7/output")

    assert executions["executions"][0]["stream_output_id"] == "abc123"
    assert response.status_code == 200
    assert response.get_json()["output"] == "hello"


def test_memory_endpoint_reads_active_project_memory() -> None:
    memory_manager = MagicMock()
    memory_manager.load_memory.return_value = "# Critical info\n\nMemory body"
    project = SimpleNamespace(memory_manager=memory_manager)
    dashboard = SerenaDashboardAPI(
        memory_log_handler=_DummyMemoryLogHandler(),
        tool_names=[],
        agent=_DashboardAgent(project),
        tool_usage_stats=None,
    )

    response = dashboard._app.test_client().get("/dashboard/api/memory?name=critical_info").get_json()

    assert response == {
        "status": "success",
        "memory_name": "critical_info",
        "content": "# Critical info\n\nMemory body",
    }
    memory_manager.load_memory.assert_called_once_with("critical_info")


def test_execution_history_combines_live_tasks_and_completed_history() -> None:
    agent = MagicMock()
    callbacks = []
    agent.register_config_changed_callback.side_effect = callbacks.append
    agent.get_current_tasks.return_value = [
        _task_info("Task-3:FindSymbolTool", is_running=True, state="pending", task_id=3),
        _task_info("Task-4:GitStatusTool", is_running=False, state="pending", task_id=4),
        _task_info("Task-5:init_project_services", is_running=False, state="pending", task_id=5),
    ]
    completed = _task_info("Task-2:ActivateProjectTool", is_running=False, state="completed", task_id=2)
    log_handler = _DummyMemoryLogHandler()

    history = DashboardExecutionHistory(agent, log_handler)
    agent.get_last_executed_task.return_value = _task_info("Task-1:init_project_services", is_running=False, state="completed", task_id=1)
    callbacks[0]()
    agent.get_last_executed_task.return_value = completed
    callbacks[0]()
    callbacks[0]()
    result = history.get_executions()

    assert [item["status"] for item in result["executions"]] == ["running", "queued", "completed"]
    assert [item["task_id"] for item in result["executions"]] == [3, 4, 2]
    assert result["running"] == 1
    assert result["queued"] == 1
    assert result["done"] == 1


def test_execution_history_captures_parameters_and_result_from_tool_logs() -> None:
    agent = MagicMock()
    callbacks = []
    agent.register_config_changed_callback.side_effect = callbacks.append
    agent.get_current_tasks.return_value = []
    completed = _task_info("Task-2:ActivateProjectTool", is_running=False, state="completed", task_id=2)
    agent.get_last_executed_task.return_value = completed
    log_handler = _DummyMemoryLogHandler()
    history = DashboardExecutionHistory(agent, log_handler)

    log_handler.emit_message(
        "INFO  2026-08-29 19:00:00 [Task-2:ActivateProjectTool] serena.tools.tools_base:_log_tool_application:288 - "
        "activate_project: project='serena', session_id='internal'; project: serena; session_id: abc123"
    )
    callbacks[0]()
    log_handler.emit_message(
        "INFO  2026-08-29 19:00:01 [Task-2:ActivateProjectTool] serena.tools.tools_base:apply_ex:410 - Result: Project activated"
    )

    execution = history.get_executions()["executions"][0]
    assert execution["parameters"] == "project='serena'"
    assert execution["detail"] == "serena"
    assert execution["project"] == "serena"
    assert execution["session_id"] == "abc123"
    assert execution["submitted_at"] == 1_000.0
    assert execution["elapsed_seconds"] == 1.0
    assert execution["result"] == "Project activated"
    assert execution["error"] is None


def test_execution_history_exposes_live_output_for_exact_running_task() -> None:
    agent = MagicMock()
    callbacks = []
    agent.register_config_changed_callback.side_effect = callbacks.append
    running = _task_info("Task-7:ExecuteShellCommandTool", is_running=True, state="pending", task_id=7)
    agent.get_current_tasks.return_value = [running]
    agent.get_last_executed_task.return_value = None
    agent.describe_tool_execution_output.return_value = SimpleNamespace(output_id="abc123", total_chars=11)
    agent.read_tool_execution_tail.return_value = ToolOutputPage(
        output_id="abc123",
        tool_name="execute_shell_command",
        total_chars=11,
        offset=0,
        content="hello world",
        next_offset=None,
    )
    history = DashboardExecutionHistory(agent, _DummyMemoryLogHandler())

    execution = history.get_executions()["executions"][0]
    output = history.get_output(7)

    assert execution["stream_output_id"] == "abc123"
    assert execution["stream_output_chars"] == 11
    assert output == {
        "status": "success",
        "task_id": 7,
        "output_id": "abc123",
        "offset": 0,
        "end_offset": 11,
        "total_chars": 11,
        "output": "hello world",
    }
    agent.read_tool_execution_tail.assert_called_once_with("Task-7:ExecuteShellCommandTool", 12_000)


def test_execution_history_exposes_native_media_without_polling_binary_data() -> None:
    agent = MagicMock()
    callbacks = []
    agent.register_config_changed_callback.side_effect = callbacks.append
    agent.get_current_tasks.return_value = []
    image_bytes = b"\x89PNG\r\n\x1a\npreview"
    completed = _task_info(
        "Task-7:RenderPdfPageTool",
        is_running=False,
        state="completed",
        task_id=7,
        result=Image(data=image_bytes, format="png"),
    )
    agent.get_last_executed_task.return_value = completed
    history = DashboardExecutionHistory(agent, _DummyMemoryLogHandler())

    callbacks[0]()
    execution = history.get_executions()["executions"][0]
    media = history.get_media(7)

    assert execution["media"] == {"type": "image"}
    assert execution["result"] is None
    assert media.media_type == "image"
    assert media.mime_type == "image/png"
    assert media.data == image_bytes


def test_execution_history_exposes_wrapped_media_result(tmp_path) -> None:
    image_bytes = b"\x89PNG\r\n\x1a\nwrapped-preview"
    (tmp_path / "preview.png").write_bytes(image_bytes)
    project = MagicMock()
    project.project_root = tmp_path
    project.project_name = "test"
    tool_agent = MagicMock()
    tool_agent.get_active_project_or_raise.return_value = project
    wrapped_result = FetchMediaFileTool(tool_agent).apply("preview.png")

    agent = MagicMock()
    callbacks = []
    agent.register_config_changed_callback.side_effect = callbacks.append
    agent.get_current_tasks.return_value = []
    completed = _task_info(
        "Task-8:FetchMediaFileTool",
        is_running=False,
        state="completed",
        task_id=8,
        result=wrapped_result,
    )
    agent.get_last_executed_task.return_value = completed
    history = DashboardExecutionHistory(agent, _DummyMemoryLogHandler())

    callbacks[0]()
    execution = history.get_executions()["executions"][0]
    media = history.get_media(8)

    assert execution["media"] == {
        "type": "image",
        "name": wrapped_result.file_link.name,
        "mime_type": "image/png",
    }
    assert execution["result"] is None
    assert media.media_type == "image"
    assert media.mime_type == "image/png"
    assert media.file_name == wrapped_result.file_link.name
    assert media.data == image_bytes


def test_execution_history_exposes_exported_pdf_as_file(monkeypatch) -> None:
    pdf_bytes = b"%PDF-1.4\npreview"
    link = ResourceLink(
        type="resource_link",
        name="paper.pdf",
        uri=AnyUrl("serena-file://export/test-token"),
        mimeType="application/pdf",
        size=len(pdf_bytes),
    )
    monkeypatch.setattr("serena.custom_dashboard.read_result_file_link", lambda file_link: pdf_bytes)

    agent = MagicMock()
    callbacks = []
    agent.register_config_changed_callback.side_effect = callbacks.append
    agent.get_current_tasks.return_value = []
    completed = _task_info(
        "Task-9:DownloadFileTool",
        is_running=False,
        state="completed",
        task_id=9,
        result=link,
    )
    agent.get_last_executed_task.return_value = completed
    history = DashboardExecutionHistory(agent, _DummyMemoryLogHandler())

    callbacks[0]()
    execution = history.get_executions()["executions"][0]
    media = history.get_media(9)

    assert execution["media"] == {"type": "file", "name": "paper.pdf", "mime_type": "application/pdf"}
    assert execution["result"] is None
    assert media.media_type == "file"
    assert media.mime_type == "application/pdf"
    assert media.file_name == "paper.pdf"
    assert media.data == pdf_bytes


def test_execution_history_reports_failed_and_cancelled_tasks() -> None:
    agent = MagicMock()
    callbacks = []
    agent.register_config_changed_callback.side_effect = callbacks.append
    agent.get_current_tasks.return_value = []
    history = DashboardExecutionHistory(agent, _DummyMemoryLogHandler())

    agent.get_last_executed_task.return_value = _task_info("Task-1:ReadFileTool", is_running=False, state="failed", task_id=1)
    callbacks[0]()
    agent.get_last_executed_task.return_value = _task_info("Task-2:SearchForPatternTool", is_running=False, state="cancelled", task_id=2)
    callbacks[0]()

    result = history.get_executions()

    assert [item["status"] for item in result["executions"]] == ["cancelled", "failed"]


def test_job_overview_requests_full_retained_history() -> None:
    job_manager = MagicMock()
    job_manager.max_concurrent_jobs = 6
    job_manager.persistence_info.return_value = JobPersistenceInfo(
        survives_serena_restart=True,
        survives_logout=False,
        survives_reboot=False,
        linger_enabled=False,
    )
    runtime = JobRuntimeInfo(
        elapsed_seconds=12.0,
        seconds_since_last_output=1.0,
        memory_bytes=1024,
        cpu_seconds=2.0,
        process_count=1,
    )
    job_manager.list_job_snapshots.return_value = [
        JobSnapshot(
            record=JobRecord(
                job_id="0123456789abcdef0123456789abcdef",
                unit_name="serena-job-0123456789abcdef0123456789abcdef.service",
                project_root="/tmp/project",
                cwd="/tmp/project",
                status=JobStatus.RUNNING,
                created_at="2026-08-29T18:00:00+00:00",
                project_name="demo",
                label="Running job",
            ),
            runtime=runtime,
        ),
        JobSnapshot(
            record=JobRecord(
                job_id="fedcba9876543210fedcba9876543210",
                unit_name="serena-job-fedcba9876543210fedcba9876543210.service",
                project_root="/tmp/project",
                cwd="/tmp/project",
                status=JobStatus.COMPLETED,
                created_at="2026-08-29T17:00:00+00:00",
                finished_at="2026-08-29T17:01:00+00:00",
                return_code=0,
                project_name="demo",
                label="Completed job",
            ),
            runtime=runtime,
        ),
    ]

    result = DashboardJobOverview(job_manager).get_jobs()

    job_manager.list_job_snapshots.assert_called_once_with(limit=1000, running_only=False)
    assert result["running_jobs"] == 1
    assert result["terminal_jobs"] == 1
    assert [job["label"] for job in result["jobs"]] == ["Running job", "Completed job"]
