import asyncio
import logging
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
from serena.execution import ExecutionAccess
from serena.mcp import SerenaMCPFactory
from serena.project import Project
from serena.tools import (
    ActivateProjectTool,
    CreateTextFileTool,
    ExecuteShellCommandTool,
    FindSymbolTool,
    ReadFileTool,
    ReadMemoryTool,
    ReplaceContentTool,
    ToolCallError,
    WriteMemoryTool,
)


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
            catch_exceptions=False,
        ),
    )


def test_project_tool_execution_access_contract() -> None:
    assert ReadFileTool.get_execution_access() is ExecutionAccess.READ
    assert ReadMemoryTool.get_execution_access() is ExecutionAccess.READ
    assert FindSymbolTool.get_execution_access() is ExecutionAccess.READ
    assert CreateTextFileTool.get_execution_access() is ExecutionAccess.WRITE
    assert WriteMemoryTool.get_execution_access() is ExecutionAccess.WRITE
    assert ExecuteShellCommandTool.get_execution_access() is ExecutionAccess.WRITE
    assert ActivateProjectTool.get_execution_access() is ExecutionAccess.SESSION_CONTROL


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
                catch_exceptions=False,
            )
            assert first_entered.wait(timeout=5)
            second = executor.submit(
                tool.apply_ex,
                relative_path="second.txt",
                content="second",
                mcp_ctx=_mcp_context("session-b"),
                catch_exceptions=False,
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

    monkeypatch.setattr(agent, "_init_project_language_backend", blocking_init)
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
            catch_exceptions=False,
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

    monkeypatch.setattr(agent, "_init_project_language_backend", controlled_init)
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
                catch_exceptions=False,
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

    monkeypatch.setattr(agent, "_init_project_language_backend", failing_init)
    _activate(agent, "session-a", "project_a")
    assert init_entered.wait(timeout=5)
    _activate(agent, "session-b", "project_a")
    release_init.set()
    assert init_failed.wait(timeout=5)

    read_tool = agent.get_tool(ReadFileTool)
    errors: list[ToolCallError] = []
    for session_id in ("session-a", "session-b"):
        try:
            read_tool.apply_ex(
                relative_path="value.txt",
                mcp_ctx=_mcp_context(session_id),
                catch_exceptions=False,
            )
        except ToolCallError as exc:
            errors.append(exc)

    assert len(errors) == 2
    assert all("runtime initialization failed" in error.get_error_message() for error in errors)


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
        catch_exceptions=False,
    )

    assert result == "OK"
    assert target.read_text() == '<a href="new">dashboard</a>'


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
            catch_exceptions=False,
        )
        assert first_entered.wait(timeout=5)
        second = executor.submit(
            tool.apply_ex,
            relative_path="value.txt",
            mcp_ctx=_mcp_context("session-b"),
            catch_exceptions=False,
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
            catch_exceptions=False,
        )
        assert first_entered.wait(timeout=5)
        second = executor.submit(
            tool.apply_ex,
            relative_path="second.txt",
            mcp_ctx=_mcp_context("session-b"),
            catch_exceptions=False,
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
            catch_exceptions=False,
        )
        assert read_entered.wait(timeout=5)
        write_future = executor.submit(
            write_tool.apply_ex,
            relative_path="written.txt",
            content="written",
            mcp_ctx=_mcp_context("session-write"),
            catch_exceptions=False,
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
            catch_exceptions=False,
        )
        assert first_entered.wait(timeout=5)

        second = executor.submit(
            tool.apply_ex,
            relative_path="second.txt",
            content="second",
            mcp_ctx=_mcp_context("session-b"),
            catch_exceptions=False,
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
            catch_exceptions=False,
        )
        assert first_entered.wait(timeout=5)

        second = executor.submit(
            tool.apply_ex,
            relative_path="second.txt",
            content="second",
            mcp_ctx=_mcp_context("session-b"),
            catch_exceptions=False,
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
            catch_exceptions=False,
        )
        assert first_entered.wait(timeout=5)
        with pytest.raises(TimeoutError):
            first.result(timeout=0.15)

        second = executor.submit(
            tool.apply_ex,
            relative_path="second.txt",
            content="second",
            mcp_ctx=_mcp_context("session-b"),
            catch_exceptions=False,
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
        catch_exceptions=False,
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
            catch_exceptions=False,
        )
        assert activation_entered.wait(timeout=5)

        submitted_read = executor.submit(
            read_tool.apply_ex,
            relative_path="value.txt",
            mcp_ctx=_mcp_context("session-a"),
            catch_exceptions=False,
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
