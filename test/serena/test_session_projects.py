import asyncio
import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import RequestParams

from serena.activity import ActivityTracker
from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.errors import UserFacingError
from serena.execution import ExecutionAccess
from serena.mcp import SerenaMCPFactory
from serena.project import Project
from serena.tools import (
    ActivateProjectTool,
    CreateTextFileTool,
    ExecuteShellCommandTool,
    FindSymbolTool,
    GitStatusTool,
    JobStatusTool,
    ReadFileTool,
    ReadMemoryTool,
    ReadToolOutputTool,
    RenameSymbolTool,
    RenderPdfPageTool,
    ReplaceContentTool,
    WriteMemoryTool,
)
from serena.util.yaml import load_yaml, save_yaml
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_exceptions import LanguageServerOperationError, SolidLSPException
from solidlsp.ls_process import LanguageServerTerminatedException


class _EmptyJobSource:
    def list_jobs(self, limit: int = 20) -> list[Any]:
        return []


def _mcp_context(session_id: str) -> Any:
    meta = RequestParams.Meta.model_validate({"openai/session": session_id})
    return SimpleNamespace(
        request_context=SimpleNamespace(meta=meta),
        session=SimpleNamespace(client_params=None),
    )


@pytest.fixture
def multi_project_agent(tmp_path: Path) -> tuple[SerenaAgent, dict[str, Path]]:
    config = SerenaConfig(log_level=logging.ERROR, tool_timeout=30).with_headless_mode_overrides()
    roots: dict[str, Path] = {}
    registered_projects: list[RegisteredProject] = []
    for name in ("project_a", "project_b", "project_c"):
        root = tmp_path / name
        root.mkdir()
        roots[name] = root
        project = Project(
            project_root=str(root),
            project_config=ProjectConfig(project_name=name, language_servers=[]),
            serena_config=config,
        )
        registered_projects.append(RegisteredProject.from_project_instance(project))
    config.projects = registered_projects

    agent = SerenaAgent(serena_config=config)
    try:
        yield agent, roots
    finally:
        agent.on_shutdown(timeout=5)


def _activate(agent: SerenaAgent, session_id: str, project_name: str) -> str:
    tool = agent.get_tool(ActivateProjectTool)
    return cast(
        str,
        tool.apply_ex(
            project=project_name,
            mcp_ctx=_mcp_context(session_id),
        ),
    )


def test_unknown_project_activation_is_user_facing(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _ = multi_project_agent
    tool = agent.get_tool(ActivateProjectTool)

    with pytest.raises(UserFacingError, match="not found"):
        tool.apply(project="missing-project", session_id="session-a")


def test_unavailable_registered_project_activation_is_user_facing(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, roots = multi_project_agent
    shutil.rmtree(roots["project_a"])
    tool = agent.get_tool(ActivateProjectTool)

    with pytest.raises(UserFacingError, match="is unavailable: directory does not exist"):
        tool.apply(project="project_a", session_id="session-a")


def test_ambiguous_project_activation_is_user_facing(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _ = multi_project_agent
    agent.serena_config.projects[1].project_config.project_name = "project_a"
    tool = agent.get_tool(ActivateProjectTool)

    with pytest.raises(UserFacingError, match="Multiple projects found with name 'project_a'"):
        tool.apply(project="project_a", session_id="session-a")


def test_invalid_project_configuration_is_user_facing_during_activation(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, roots = multi_project_agent
    invalid_root = roots["project_a"].parent / "invalid_project"
    invalid_root.mkdir()
    ProjectConfig.autogenerate(invalid_root, agent.serena_config)
    config_path = Path(agent.serena_config.get_project_yml_location(invalid_root))
    config_data = load_yaml(str(config_path))
    config_data["language_servers"] = ["not-a-language-server"]
    save_yaml(str(config_path), config_data)
    tool = agent.get_tool(ActivateProjectTool)

    with pytest.raises(UserFacingError, match="Invalid project configuration"):
        tool.apply(project=str(invalid_root), session_id="session-a")


def test_project_tool_execution_access_contract() -> None:
    assert ReadFileTool.get_execution_access() is ExecutionAccess.READ
    assert ReadMemoryTool.get_execution_access() is ExecutionAccess.READ
    assert FindSymbolTool.get_execution_access() is ExecutionAccess.READ
    assert CreateTextFileTool.get_execution_access() is ExecutionAccess.WRITE
    assert WriteMemoryTool.get_execution_access() is ExecutionAccess.WRITE
    assert ExecuteShellCommandTool.get_execution_access() is ExecutionAccess.WRITE
    assert ActivateProjectTool.get_execution_access() is ExecutionAccess.SESSION_CONTROL


def test_lsp_termination_restarts_and_replays_read_once(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    tool = agent.get_tool(FindSymbolTool)
    apply_calls = 0
    restarted_languages: list[LanguageServerId] = []

    def recovering_read(**kwargs: Any) -> str:
        nonlocal apply_calls
        del kwargs
        apply_calls += 1
        if apply_calls == 1:
            raise SolidLSPException(
                "language server stopped",
                LanguageServerTerminatedException("terminated", LanguageServerId.PYTHON),
            )
        return "recovered"

    monkeypatch.setattr(tool, "apply", recovering_read)
    monkeypatch.setattr(
        agent,
        "get_language_server_manager_or_raise",
        lambda: SimpleNamespace(restart_language_server=restarted_languages.append),
    )

    result = tool.apply_ex(
        name_path_pattern="Example",
        mcp_ctx=_mcp_context("session-a"),
    )

    assert result == "recovered"
    assert apply_calls == 2
    assert restarted_languages == [LanguageServerId.PYTHON]


def test_lsp_termination_restarts_but_does_not_replay_write(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    tool = agent.get_tool(RenameSymbolTool)
    apply_calls = 0
    restarted_languages: list[LanguageServerId] = []

    def interrupted_write(**kwargs: Any) -> str:
        nonlocal apply_calls
        del kwargs
        apply_calls += 1
        raise SolidLSPException(
            "language server stopped",
            LanguageServerTerminatedException("terminated", LanguageServerId.PYTHON),
        )

    monkeypatch.setattr(tool, "apply", interrupted_write)
    monkeypatch.setattr(
        agent,
        "get_language_server_manager_or_raise",
        lambda: SimpleNamespace(restart_language_server=restarted_languages.append),
    )

    with pytest.raises(UserFacingError, match="Re-inspect the affected state"):
        tool.apply_ex(
            name_path="Example",
            relative_path="example.py",
            new_name="Renamed",
            mcp_ctx=_mcp_context("session-a"),
        )

    assert apply_calls == 1
    assert restarted_languages == [LanguageServerId.PYTHON]


def test_lsp_termination_replays_read_at_most_once(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    tool = agent.get_tool(FindSymbolTool)
    apply_calls = 0
    restarted_languages: list[LanguageServerId] = []

    def repeatedly_failing_read(**kwargs: Any) -> str:
        nonlocal apply_calls
        del kwargs
        apply_calls += 1
        raise SolidLSPException(
            "language server stopped",
            LanguageServerTerminatedException("terminated", LanguageServerId.PYTHON),
        )

    monkeypatch.setattr(tool, "apply", repeatedly_failing_read)
    monkeypatch.setattr(
        agent,
        "get_language_server_manager_or_raise",
        lambda: SimpleNamespace(restart_language_server=restarted_languages.append),
    )

    with pytest.raises(SolidLSPException, match="language server stopped"):
        tool.apply_ex(
            name_path_pattern="Example",
            mcp_ctx=_mcp_context("session-a"),
        )

    assert apply_calls == 2
    assert restarted_languages == [LanguageServerId.PYTHON]


def test_lsp_operation_failure_is_concise_user_facing_mcp_error(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    tool = agent.get_tool(FindSymbolTool)

    def rejected_read(**kwargs: Any) -> str:
        del kwargs
        raise LanguageServerOperationError(
            "Error processing request textDocument/documentSymbol with params:\n{'large': 'request payload'}",
            cause=ValueError("Language server rejected the request"),
        )

    monkeypatch.setattr(tool, "apply", rejected_read)
    mcp_tool = SerenaMCPFactory.make_mcp_tool(tool)

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({"name_path_pattern": "Example"}, context=_mcp_context("session-a"))

        message = str(exc_info.value)
        assert message == "Language server rejected the request"
        assert "request payload" not in message
        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.error == message

    asyncio.run(scenario())


def test_unclassified_lsp_failure_remains_internal_at_mcp_boundary(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    tool = agent.get_tool(FindSymbolTool)

    def broken_read(**kwargs: Any) -> str:
        del kwargs
        raise SolidLSPException("internal language-server invariant failed")

    monkeypatch.setattr(tool, "apply", broken_read)
    mcp_tool = SerenaMCPFactory.make_mcp_tool(tool)

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({"name_path_pattern": "Example"}, context=_mcp_context("session-a"))

        message = str(exc_info.value)
        assert message == "SolidLSPException: internal language-server invariant failed"
        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.error == message

    asyncio.run(scenario())


def test_startup_project_sessions_share_serialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = SerenaConfig(log_level=logging.ERROR, tool_timeout=30).with_headless_mode_overrides()
    root = tmp_path / "project_a"
    root.mkdir()
    project = Project(
        project_root=str(root),
        project_config=ProjectConfig(project_name="project_a", language_servers=[]),
        serena_config=config,
    )
    config.projects = [RegisteredProject.from_project_instance(project)]
    agent = SerenaAgent(project="project_a", serena_config=config)

    try:
        tool = agent.get_tool(CreateTextFileTool)
        original_apply = tool.apply
        first_entered = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()

        def blocking_apply(relative_path: str, content: str) -> str:
            if relative_path == "first.txt":
                first_entered.set()
                assert release_first.wait(timeout=5)
            elif relative_path == "second.txt":
                second_entered.set()
            return original_apply(relative_path=relative_path, content=content)

        monkeypatch.setattr(tool, "apply", blocking_apply)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                tool.apply_ex,
                relative_path="first.txt",
                content="first",
                mcp_ctx=_mcp_context("session-a"),
            )
            assert first_entered.wait(timeout=5)
            second = executor.submit(
                tool.apply_ex,
                relative_path="second.txt",
                content="second",
                mcp_ctx=_mcp_context("session-b"),
            )
            assert not second_entered.wait(timeout=0.25)
            release_first.set()
            first.result(timeout=5)
            second.result(timeout=5)

        assert second_entered.is_set()
        assert (root / "first.txt").read_text() == "first"
        assert (root / "second.txt").read_text() == "second"
    finally:
        agent.on_shutdown(timeout=5)


def test_session_activation_does_not_change_startup_default(tmp_path: Path) -> None:
    config = SerenaConfig(log_level=logging.ERROR, tool_timeout=30).with_headless_mode_overrides()
    registered_projects: list[RegisteredProject] = []
    for name in ("project_a", "project_b"):
        root = tmp_path / name
        root.mkdir()
        project = Project(
            project_root=str(root),
            project_config=ProjectConfig(project_name=name, language_servers=[]),
            serena_config=config,
        )
        registered_projects.append(RegisteredProject.from_project_instance(project))
    config.projects = registered_projects
    agent = SerenaAgent(project="project_a", serena_config=config)

    try:
        assert agent.get_default_project().project_name == "project_a"
        assert agent.get_active_project_for_session("unbound-session").project_name == "project_a"

        assert "project_b" in _activate(agent, "session-a", "project_b")

        assert agent.get_active_project_for_session("session-a").project_name == "project_b"
        assert agent.get_active_project_for_session("unbound-session").project_name == "project_a"
        assert agent.get_default_project().project_name == "project_a"
    finally:
        agent.on_shutdown(timeout=5)


def test_new_runtime_initializes_once_before_first_project_tool(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    init_entered = threading.Event()
    release_init = threading.Event()
    init_calls: list[str] = []

    def blocking_init(project: Project) -> None:
        init_calls.append(project.project_name)
        init_entered.set()
        assert release_init.wait(timeout=5)

    monkeypatch.setattr(agent, "_init_project_language_servers", blocking_init)
    _activate(agent, "session-a", "project_a")
    assert init_entered.wait(timeout=5)
    _activate(agent, "session-b", "project_a")
    assert init_calls == ["project_a"]

    read_tool = agent.get_tool(ReadFileTool)
    original_read = read_tool.apply
    read_entered = threading.Event()

    def tracking_read(relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> str:
        read_entered.set()
        return original_read(relative_path, start_line, end_line, max_answer_chars)

    monkeypatch.setattr(read_tool, "apply", tracking_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        read_future = executor.submit(
            read_tool.apply_ex,
            relative_path="value.txt",
            mcp_ctx=_mcp_context("session-b"),
        )
        assert not read_entered.wait(timeout=0.25)
        release_init.set()
        assert read_future.result(timeout=5) == "alpha"

    assert init_calls == ["project_a"]


def test_project_runtime_initialization_is_independent_across_projects(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_b"] / "value.txt").write_text("beta")
    project_a_init_entered = threading.Event()
    release_project_a_init = threading.Event()
    project_b_initialized = threading.Event()

    def controlled_init(project: Project) -> None:
        if project.project_name == "project_a":
            project_a_init_entered.set()
            assert release_project_a_init.wait(timeout=5)
        elif project.project_name == "project_b":
            project_b_initialized.set()

    monkeypatch.setattr(agent, "_init_project_language_servers", controlled_init)
    _activate(agent, "session-a", "project_a")
    assert project_a_init_entered.wait(timeout=5)

    try:
        _activate(agent, "session-b", "project_b")
        assert project_b_initialized.wait(timeout=1)
        read_tool = agent.get_tool(ReadFileTool)
        assert (
            read_tool.apply_ex(
                relative_path="value.txt",
                mcp_ctx=_mcp_context("session-b"),
            )
            == "beta"
        )
    finally:
        release_project_a_init.set()


def test_runtime_initialization_failure_is_shared_by_bound_sessions(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    init_entered = threading.Event()
    release_init = threading.Event()
    init_failed = threading.Event()

    def failing_init(project: Project) -> None:
        assert project.project_name == "project_a"
        init_entered.set()
        assert release_init.wait(timeout=5)
        init_failed.set()
        raise RuntimeError("runtime initialization failed")

    monkeypatch.setattr(agent, "_init_project_language_servers", failing_init)
    _activate(agent, "session-a", "project_a")
    assert init_entered.wait(timeout=5)
    _activate(agent, "session-b", "project_a")
    release_init.set()
    assert init_failed.wait(timeout=5)

    read_tool = agent.get_tool(ReadFileTool)
    errors: list[RuntimeError] = []
    for session_id in ("session-a", "session-b"):
        try:
            read_tool.apply_ex(
                relative_path="value.txt",
                mcp_ctx=_mcp_context(session_id),
            )
        except RuntimeError as exc:
            errors.append(exc)

    assert len(errors) == 2
    assert all("runtime initialization failed" in str(error) for error in errors)


def test_runtime_user_facing_initialization_failure_remains_concise_at_mcp_boundary(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    init_failed = threading.Event()

    def failing_init(project: Project) -> None:
        assert project.project_name == "project_a"
        init_failed.set()
        raise UserFacingError("Language server manager is unavailable: invalid tool timeout configuration.")

    monkeypatch.setattr(agent, "_init_project_language_servers", failing_init)
    _activate(agent, "session-a", "project_a")
    assert init_failed.wait(timeout=5)
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReadFileTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({"relative_path": "value.txt"}, context=_mcp_context("session-a"))

        message = str(exc_info.value)
        assert message == "Language server manager is unavailable: invalid tool timeout configuration."
        assert "RuntimeError" not in message
        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.error == message

    asyncio.run(scenario())


def test_sessions_bind_projects_independently(multi_project_agent: tuple[SerenaAgent, dict[str, Path]]) -> None:
    agent, _ = multi_project_agent

    assert agent.get_default_project() is None
    assert agent.get_active_project_for_session("unbound-session") is None

    assert "project_a" in _activate(agent, "session-a", "project_a")
    assert "project_b" in _activate(agent, "session-b", "project_b")
    assert "project_c" in _activate(agent, "session-c", "project_c")

    assert agent.get_active_project_for_session("session-a").project_name == "project_a"
    assert agent.get_active_project_for_session("session-b").project_name == "project_b"
    assert agent.get_active_project_for_session("session-c").project_name == "project_c"
    assert agent.get_active_project_for_session("unbound-session") is None
    assert agent.get_default_project() is None


def test_replace_content_works_without_language_server(multi_project_agent: tuple[SerenaAgent, dict[str, Path]]) -> None:
    agent, roots = multi_project_agent
    target = roots["project_a"] / "index.html"
    target.write_text('<a href="old">dashboard</a>')
    _activate(agent, "session-a", "project_a")

    tool = agent.get_tool(ReplaceContentTool)
    result = tool.apply_ex(
        relative_path="index.html",
        needle='href="old"',
        repl='href="new"',
        mode="literal",
        mcp_ctx=_mcp_context("session-a"),
    )

    assert result == "OK"
    assert target.read_text() == '<a href="new">dashboard</a>'


def test_replace_content_mcp_failure_is_single_line_and_persisted(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, roots = multi_project_agent
    target = roots["project_a"] / "index.html"
    target.write_text('<a href="old">dashboard</a>')
    _activate(agent, "session-a", "project_a")

    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReplaceContentTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run(
                {
                    "relative_path": "index.html",
                    "needle": 'href="missing"',
                    "repl": 'href="new"',
                    "mode": "literal",
                },
                context=_mcp_context("session-a"),
            )

        message = str(exc_info.value)
        assert "No matches of search expression found." in message
        assert "\n" not in message
        assert "Error executing tool" not in message
        assert "Traceback" not in message

        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.status == "failed"
        assert record.error == message

    asyncio.run(scenario())


def test_read_file_missing_path_is_concise_mcp_failure(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _roots = multi_project_agent
    _activate(agent, "session-a", "project_a")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReadFileTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({"relative_path": "missing.txt"}, context=_mcp_context("session-a"))

        message = str(exc_info.value)
        assert message == "File not found: missing.txt"
        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.status == "failed"
        assert record.error == message

    asyncio.run(scenario())


def test_invalid_response_budget_is_concise_mcp_failure(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    _activate(agent, "session-a", "project_a")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReadFileTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run(
                {"relative_path": "value.txt", "max_answer_chars": 0},
                context=_mcp_context("session-a"),
            )

        message = str(exc_info.value)
        assert message == "Resolved maximum answer length must be positive, got: 0"
        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.error == message

    asyncio.run(scenario())


def test_read_tool_output_failures_are_concise_mcp_errors(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _roots = multi_project_agent
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReadToolOutputTool))

    async def scenario() -> None:
        cases = [
            ({"output_id": "missing-output"}, "Tool output 'missing-output' is unavailable or expired"),
            ({"output_id": "missing-output", "offset": -1}, "offset must be non-negative"),
            ({"output_id": "missing-output", "max_chars": 0}, "max_chars must be between 1 and 20000"),
        ]
        for arguments, expected in cases:
            with pytest.raises(ToolError) as exc_info:
                await mcp_tool.run(arguments, context=_mcp_context("session-a"))
            message = str(exc_info.value)
            assert message == expected
            assert "Traceback" not in message
            assert "ValueError:" not in message

    asyncio.run(scenario())


def test_pdf_request_failures_are_concise_mcp_errors(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, roots = multi_project_agent
    _activate(agent, "session-a", "project_a")
    (roots["project_a"] / "notes.txt").write_text("not a pdf")
    (roots["project_a"] / "empty.pdf").write_bytes(b"%PDF-1.4\n")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(RenderPdfPageTool))

    async def scenario() -> None:
        cases = [
            ({"relative_path": "notes.txt", "page": 1}, "render_pdf_page only accepts PDF files"),
            ({"relative_path": "empty.pdf", "page": 0}, "page must be a 1-based positive integer"),
            ({"relative_path": "empty.pdf", "page": 1, "dpi": 600}, "dpi must be between 72 and 300"),
        ]
        for arguments, expected in cases:
            with pytest.raises(ToolError) as exc_info:
                await mcp_tool.run(arguments, context=_mcp_context("session-a"))
            message = str(exc_info.value)
            assert message == expected
            assert "Traceback" not in message
            assert "ValueError:" not in message
            assert "RuntimeError:" not in message

    asyncio.run(scenario())


def test_replace_content_ambiguous_match_is_concise_mcp_failure(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "sample.txt").write_text("start A\nstart B\nend\n")
    _activate(agent, "session-a", "project_a")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReplaceContentTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run(
                {"relative_path": "sample.txt", "needle": "start.*?end", "repl": "X", "mode": "regex"},
                context=_mcp_context("session-a"),
            )

        message = str(exc_info.value)
        assert message.startswith("Match is ambiguous:")
        assert "Traceback" not in message
        assert "ValueError" not in message
        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.error == message

    asyncio.run(scenario())


def test_mcp_validation_failure_is_compact_and_persisted(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReadFileTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({}, context=_mcp_context("session-a"))

        message = str(exc_info.value)
        assert message.startswith("Invalid arguments:")
        assert "relative_path" in message
        assert "\n" not in message
        assert "https://" not in message

        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.status == "failed"
        assert record.error == message

    asyncio.run(scenario())


def test_mcp_user_facing_failure_has_no_additional_wrapper(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    _activate(agent, "session-a", "project_a")
    tool = agent.get_tool(ReadFileTool)

    def expected_failure(**kwargs: Any) -> str:
        del kwargs
        raise UserFacingError("Expected request failure.")

    monkeypatch.setattr(tool, "apply", expected_failure)
    mcp_tool = SerenaMCPFactory.make_mcp_tool(tool)

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({"relative_path": "value.txt"}, context=_mcp_context("session-a"))

        assert str(exc_info.value) == "Expected request failure."
        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.error == "Expected request failure."

    asyncio.run(scenario())


def test_job_status_invalid_id_is_concise_mcp_failure_and_persisted(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _ = multi_project_agent
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(JobStatusTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({"job_id": "not-a-job-id"}, context=_mcp_context("session-a"))

        message = str(exc_info.value)
        assert message == "Invalid job ID 'not-a-job-id'"
        assert "ValueError:" not in message
        assert "Error executing tool" not in message
        assert "\n" not in message

        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.error == message

    asyncio.run(scenario())


def test_shell_nonzero_exit_remains_successful_mcp_result(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ExecuteShellCommandTool))

    async def scenario() -> None:
        result = await mcp_tool.run({"command": "exit 7"}, context=_mcp_context("session-a"))
        payload = json.loads(result)
        assert payload["return_code"] == 7

        record = agent.execution_store.list_session_executions("session-a")[-1]
        assert record.status == "completed"
        assert record.error is None

    asyncio.run(scenario())


def test_git_rejection_is_concise_mcp_failure(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(GitStatusTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({}, context=_mcp_context("session-a"))

        message = str(exc_info.value)
        assert "not a git repository" in message.lower()
        assert "RuntimeError:" not in message
        assert "Error executing tool" not in message
        assert "Traceback" not in message

    asyncio.run(scenario())


def test_missing_memory_is_concise_mcp_failure(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]],
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReadMemoryTool))

    async def scenario() -> None:
        with pytest.raises(ToolError) as exc_info:
            await mcp_tool.run({"memory_name": "missing"}, context=_mcp_context("session-a"))

        message = str(exc_info.value)
        assert message == "Memory named 'missing' not found"
        assert "FileNotFoundError:" not in message
        assert "Error executing tool" not in message
        assert "Traceback" not in message

    asyncio.run(scenario())


def test_different_project_reads_can_interleave(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    (roots["project_b"] / "value.txt").write_text("beta")
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_b")

    tool = agent.get_tool(ReadFileTool)
    original_apply = tool.apply
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def blocking_apply(relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> str:
        result = original_apply(relative_path, start_line, end_line, max_answer_chars)
        if result == "alpha":
            first_entered.set()
            assert release_first.wait(timeout=5)
        elif result == "beta":
            second_entered.set()
        return result

    monkeypatch.setattr(tool, "apply", blocking_apply)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            tool.apply_ex,
            relative_path="value.txt",
            mcp_ctx=_mcp_context("session-a"),
        )
        assert first_entered.wait(timeout=5)
        second = executor.submit(
            tool.apply_ex,
            relative_path="value.txt",
            mcp_ctx=_mcp_context("session-b"),
        )
        assert second_entered.wait(timeout=1)
        release_first.set()
        assert first.result(timeout=5) == "alpha"
        assert second.result(timeout=5) == "beta"


def test_same_project_plain_reads_can_overlap(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "first.txt").write_text("first")
    (roots["project_a"] / "second.txt").write_text("second")
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_a")

    tool = agent.get_tool(ReadFileTool)
    original_apply = tool.apply
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def blocking_apply(relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> str:
        result = original_apply(relative_path, start_line, end_line, max_answer_chars)
        if relative_path == "first.txt":
            first_entered.set()
            assert release_first.wait(timeout=5)
        elif relative_path == "second.txt":
            second_entered.set()
        return result

    monkeypatch.setattr(tool, "apply", blocking_apply)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            tool.apply_ex,
            relative_path="first.txt",
            mcp_ctx=_mcp_context("session-a"),
        )
        assert first_entered.wait(timeout=5)
        second = executor.submit(
            tool.apply_ex,
            relative_path="second.txt",
            mcp_ctx=_mcp_context("session-b"),
        )
        try:
            assert second_entered.wait(timeout=1)
        finally:
            release_first.set()
        assert first.result(timeout=5) == "first"
        assert second.result(timeout=5) == "second"


def test_same_project_write_waits_for_active_read(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    _activate(agent, "session-read", "project_a")
    _activate(agent, "session-write", "project_a")

    read_tool = agent.get_tool(ReadFileTool)
    write_tool = agent.get_tool(CreateTextFileTool)
    original_read = read_tool.apply
    original_write = write_tool.apply
    read_entered = threading.Event()
    write_entered = threading.Event()
    release_read = threading.Event()

    def blocking_read(relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> str:
        read_entered.set()
        assert release_read.wait(timeout=5)
        return original_read(relative_path, start_line, end_line, max_answer_chars)

    def tracking_write(relative_path: str, content: str) -> str:
        write_entered.set()
        return original_write(relative_path=relative_path, content=content)

    monkeypatch.setattr(read_tool, "apply", blocking_read)
    monkeypatch.setattr(write_tool, "apply", tracking_write)

    with ThreadPoolExecutor(max_workers=2) as executor:
        read_future = executor.submit(
            read_tool.apply_ex,
            relative_path="value.txt",
            mcp_ctx=_mcp_context("session-read"),
        )
        assert read_entered.wait(timeout=5)
        write_future = executor.submit(
            write_tool.apply_ex,
            relative_path="written.txt",
            content="written",
            mcp_ctx=_mcp_context("session-write"),
        )
        assert not write_entered.wait(timeout=0.25)
        release_read.set()
        assert read_future.result(timeout=5) == "alpha"
        write_future.result(timeout=5)

    assert write_entered.is_set()
    assert (roots["project_a"] / "written.txt").read_text() == "written"


def test_different_project_writes_can_overlap(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_b")

    tool = agent.get_tool(CreateTextFileTool)
    original_apply = tool.apply
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def blocking_apply(relative_path: str, content: str) -> str:
        result = original_apply(relative_path=relative_path, content=content)
        if relative_path == "first.txt":
            first_entered.set()
            assert release_first.wait(timeout=5)
        elif relative_path == "second.txt":
            second_entered.set()
        return result

    monkeypatch.setattr(tool, "apply", blocking_apply)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            tool.apply_ex,
            relative_path="first.txt",
            content="first",
            mcp_ctx=_mcp_context("session-a"),
        )
        assert first_entered.wait(timeout=5)

        second = executor.submit(
            tool.apply_ex,
            relative_path="second.txt",
            content="second",
            mcp_ctx=_mcp_context("session-b"),
        )
        assert second_entered.wait(timeout=1)
        assert (roots["project_b"] / "second.txt").read_text() == "second"

        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert (roots["project_a"] / "first.txt").read_text() == "first"


def test_same_project_writes_are_serialized(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_a")

    tool = agent.get_tool(CreateTextFileTool)
    original_apply = tool.apply
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def blocking_apply(relative_path: str, content: str) -> str:
        if relative_path == "first.txt":
            first_entered.set()
            assert release_first.wait(timeout=5)
        elif relative_path == "second.txt":
            second_entered.set()
        return original_apply(relative_path=relative_path, content=content)

    monkeypatch.setattr(tool, "apply", blocking_apply)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            tool.apply_ex,
            relative_path="first.txt",
            content="first",
            mcp_ctx=_mcp_context("session-a"),
        )
        assert first_entered.wait(timeout=5)

        second = executor.submit(
            tool.apply_ex,
            relative_path="second.txt",
            content="second",
            mcp_ctx=_mcp_context("session-b"),
        )
        assert not second_entered.wait(timeout=0.25)

        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert second_entered.is_set()
    assert (roots["project_a"] / "first.txt").read_text() == "first"
    assert (roots["project_a"] / "second.txt").read_text() == "second"


def test_timed_out_writer_keeps_exclusion_until_operation_stops(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_a")

    tool = agent.get_tool(CreateTextFileTool)
    original_apply = tool.apply
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def blocking_apply(relative_path: str, content: str) -> str:
        if relative_path == "first.txt":
            first_entered.set()
            assert release_first.wait(timeout=5)
        elif relative_path == "second.txt":
            second_entered.set()
        return original_apply(relative_path=relative_path, content=content)

    monkeypatch.setattr(tool, "apply", blocking_apply)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            tool.apply_ex,
            relative_path="first.txt",
            content="first",
            mcp_ctx=_mcp_context("session-a"),
        )
        assert first_entered.wait(timeout=5)
        with pytest.raises(TimeoutError):
            first.result(timeout=0.15)

        second = executor.submit(
            tool.apply_ex,
            relative_path="second.txt",
            content="second",
            mcp_ctx=_mcp_context("session-b"),
        )
        try:
            assert not second_entered.wait(timeout=0.3)
        finally:
            release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)


def test_mcp_timeout_returns_before_worker_without_releasing_write_exclusion(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_a")

    tool = agent.get_tool(CreateTextFileTool)
    original_apply = tool.apply
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def blocking_apply(relative_path: str, content: str) -> str:
        if relative_path == "first.txt":
            first_entered.set()
            assert release_first.wait(timeout=5)
        elif relative_path == "second.txt":
            second_entered.set()
        return original_apply(relative_path=relative_path, content=content)

    monkeypatch.setattr(tool, "apply", blocking_apply)
    mcp_tool = SerenaMCPFactory.make_mcp_tool(tool)

    async def scenario() -> None:
        agent.serena_config.tool_timeout = 0.15
        first = asyncio.create_task(
            mcp_tool.run(
                {"relative_path": "first.txt", "content": "first"},
                context=_mcp_context("session-a"),
            )
        )
        assert await asyncio.to_thread(first_entered.wait, 5)
        with pytest.raises(ToolError, match="timed out"):
            await first

        # the model-visible request has ended, but the authoritative execution remains live
        first_record = agent.execution_store.list_session_executions("session-a")[-1]
        assert first_record.status == "running"
        assert first_record.finished_at is None
        assert first_record.request_finished_at is not None
        assert first_record.request_error is not None and "timed out" in first_record.request_error

        agent.serena_config.tool_timeout = 2
        second = asyncio.create_task(
            mcp_tool.run(
                {"relative_path": "second.txt", "content": "second"},
                context=_mcp_context("session-b"),
            )
        )
        await asyncio.sleep(0.3)
        assert not second_entered.is_set()

        release_first.set()
        await second
        assert second_entered.is_set()

        # terminal lifecycle is recorded only after the detached worker really stops
        for _ in range(50):
            first_record = agent.execution_store.list_session_executions("session-a")[-1]
            if first_record.status != "running":
                break
            await asyncio.sleep(0.01)
        assert first_record.status == "failed"
        assert first_record.finished_at is not None
        assert first_record.request_finished_at is not None
        assert first_record.finished_at >= first_record.request_finished_at
        assert first_record.error == first_record.request_error

    asyncio.run(scenario())


def test_mcp_cancellation_keeps_execution_live_until_worker_stops(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_a")

    tool = agent.get_tool(CreateTextFileTool)
    original_apply = tool.apply
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def blocking_apply(relative_path: str, content: str) -> str:
        if relative_path == "cancelled.txt":
            first_entered.set()
            assert release_first.wait(timeout=5)
        elif relative_path == "after-cancel.txt":
            second_entered.set()
        return original_apply(relative_path=relative_path, content=content)

    monkeypatch.setattr(tool, "apply", blocking_apply)
    mcp_tool = SerenaMCPFactory.make_mcp_tool(tool)

    async def scenario() -> None:
        first = asyncio.create_task(
            mcp_tool.run(
                {"relative_path": "cancelled.txt", "content": "first"},
                context=_mcp_context("session-a"),
            )
        )
        assert await asyncio.to_thread(first_entered.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        first_record = agent.execution_store.list_session_executions("session-a")[-1]
        assert first_record.status == "running"
        assert first_record.finished_at is None
        assert first_record.request_finished_at is not None
        assert first_record.request_error is not None and "cancelled" in first_record.request_error

        agent.serena_config.tool_timeout = 2
        second = asyncio.create_task(
            mcp_tool.run(
                {"relative_path": "after-cancel.txt", "content": "second"},
                context=_mcp_context("session-b"),
            )
        )
        await asyncio.sleep(0.3)
        assert not second_entered.is_set()

        release_first.set()
        await second
        assert second_entered.is_set()

        for _ in range(50):
            first_record = agent.execution_store.list_session_executions("session-a")[-1]
            if first_record.status != "running":
                break
            await asyncio.sleep(0.01)
        assert first_record.status == "failed"
        assert first_record.finished_at is not None
        assert first_record.request_finished_at is not None
        assert first_record.finished_at >= first_record.request_finished_at
        assert first_record.error == first_record.request_error

    asyncio.run(scenario())


def test_switching_one_session_does_not_shutdown_shared_project(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_a")
    shared_project = agent.get_active_project_for_session("session-b")
    assert shared_project is not None
    shutdown_calls: list[float] = []
    monkeypatch.setattr(shared_project, "shutdown", lambda timeout=2.0: shutdown_calls.append(timeout))

    _activate(agent, "session-a", "project_b")

    assert shutdown_calls == []


def test_switching_global_scope_does_not_shutdown_project_used_by_mcp_session(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = multi_project_agent
    _activate(agent, "global", "project_a")
    _activate(agent, "session-b", "project_a")
    shared_project = agent.get_active_project_for_session("session-b")
    assert shared_project is not None
    shutdown_calls: list[float] = []
    monkeypatch.setattr(shared_project, "shutdown", lambda timeout=2.0: shutdown_calls.append(timeout))

    _activate(agent, "global", "project_b")

    assert shutdown_calls == []
    assert agent.get_active_project_for_session("session-b") is shared_project


def test_switching_one_session_does_not_redirect_another(multi_project_agent: tuple[SerenaAgent, dict[str, Path]]) -> None:
    agent, roots = multi_project_agent
    _activate(agent, "session-a", "project_a")
    _activate(agent, "session-b", "project_a")
    _activate(agent, "session-a", "project_b")

    assert agent.get_active_project_for_session("session-a").project_name == "project_b"
    assert agent.get_active_project_for_session("session-b").project_name == "project_a"

    tool = agent.get_tool(CreateTextFileTool)
    tool.apply_ex(
        relative_path="still-a.txt",
        content="a",
        mcp_ctx=_mcp_context("session-b"),
    )
    assert (roots["project_a"] / "still-a.txt").read_text() == "a"
    assert not (roots["project_b"] / "still-a.txt").exists()


def test_queued_tool_remains_pinned_to_project_selected_at_submission(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    (roots["project_b"] / "value.txt").write_text("beta")
    _activate(agent, "session-a", "project_a")

    activation_tool = agent.get_tool(ActivateProjectTool)
    original_activate = activation_tool.apply
    activation_entered = threading.Event()
    release_activation = threading.Event()

    def blocking_activate(project: str, session_id: str) -> str:
        if project == "project_b":
            activation_entered.set()
            assert release_activation.wait(timeout=5)
        return original_activate(project=project, session_id=session_id)

    monkeypatch.setattr(activation_tool, "apply", blocking_activate)

    read_tool = agent.get_tool(ReadFileTool)
    original_read = read_tool.apply
    read_entered = threading.Event()
    release_read = threading.Event()

    def blocking_read(relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> str:
        read_entered.set()
        assert release_read.wait(timeout=5)
        return original_read(relative_path, start_line, end_line, max_answer_chars)

    monkeypatch.setattr(read_tool, "apply", blocking_read)

    with ThreadPoolExecutor(max_workers=2) as executor:
        activation = executor.submit(
            activation_tool.apply_ex,
            project="project_b",
            mcp_ctx=_mcp_context("session-a"),
        )
        assert activation_entered.wait(timeout=5)

        submitted_read = executor.submit(
            read_tool.apply_ex,
            relative_path="value.txt",
            mcp_ctx=_mcp_context("session-a"),
        )
        assert read_entered.wait(timeout=5)

        release_activation.set()
        assert "project_b" in activation.result(timeout=5)
        assert agent.get_active_project_for_session("session-a").project_name == "project_b"

        release_read.set()
        assert submitted_read.result(timeout=5) == "alpha"


def test_mcp_tool_pins_project_before_fastmcp_worker_starts(
    multi_project_agent: tuple[SerenaAgent, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, roots = multi_project_agent
    (roots["project_a"] / "value.txt").write_text("alpha")
    (roots["project_b"] / "value.txt").write_text("beta")
    _activate(agent, "session-a", "project_a")

    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ReadFileTool))
    original_to_thread = asyncio.to_thread
    worker_scheduled = asyncio.Event()
    release_worker = asyncio.Event()

    async def delayed_to_thread(function, /, *args, **kwargs):
        worker_scheduled.set()
        await release_worker.wait()
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", delayed_to_thread)

    async def scenario() -> None:
        submitted_read = asyncio.create_task(
            mcp_tool.run(
                {"relative_path": "value.txt"},
                context=_mcp_context("session-a"),
            )
        )
        await asyncio.wait_for(worker_scheduled.wait(), timeout=5)

        _activate(agent, "session-a", "project_b")
        assert agent.get_active_project_for_session("session-a").project_name == "project_b"

        release_worker.set()
        assert await asyncio.wait_for(submitted_read, timeout=5) == "alpha"

    asyncio.run(scenario())


def test_activation_message_embeds_memories_from_new_project(multi_project_agent: tuple[SerenaAgent, dict[str, Path]]) -> None:
    agent, _ = multi_project_agent
    project_a = agent.serena_config.get_project("project_a")
    project_b = agent.serena_config.get_project("project_b")
    assert project_a is not None
    assert project_b is not None
    project_a.memory_manager.save_memory("marker", "memory-from-a", is_tool_context=False)
    project_b.memory_manager.save_memory("marker", "memory-from-b", is_tool_context=False)
    project_b.project_config.initial_prompt = '{{ embed_memory("marker") }}'

    _activate(agent, "session-a", "project_a")
    activation_message = _activate(agent, "session-a", "project_b")

    assert "memory-from-b" in activation_message
    assert "memory-from-a" not in activation_message


def test_activity_project_attribution_follows_session_activation(multi_project_agent: tuple[SerenaAgent, dict[str, Path]]) -> None:
    agent, _ = multi_project_agent
    tracker = ActivityTracker(_EmptyJobSource())
    run = tracker.start_run("session-a", "")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(agent.get_tool(ActivateProjectTool), activity_tracker=tracker)

    asyncio.run(mcp_tool.run({"project": "project_a"}, context=_mcp_context("session-a")))

    snapshot = tracker.get_run("session-a", run["run_id"])
    assert snapshot["project_name"] == "project_a"
    assert snapshot["calls"][0]["tool_name"] == "activate_project"
    assert snapshot["calls"][0]["project_name"] == "project_a"
