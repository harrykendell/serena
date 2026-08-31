import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import RequestParams

from serena.activity import ACTIVITY_RESOURCE_URI, ActivityTracker, get_mcp_session_id, register_activity_resource
from serena.config.context_mode import SerenaAgentContext
from serena.jobs import JobRecord, JobStatus
from serena.mcp import SerenaMCPFactory
from serena.tools import Tool


class _FakeJobSource:
    """Mutable durable-job source for activity tracker tests."""

    def __init__(self, records: list[JobRecord] | None = None) -> None:
        self.records = records or []

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        return self.records[:limit]

    def set_activity_owner(self, job_id: str, owner_token: str) -> JobRecord:
        for index, record in enumerate(self.records):
            if record.job_id != job_id:
                continue
            updated = replace(record, activity_owner_token=owner_token)
            self.records[index] = updated
            return updated
        raise ValueError(f"Unknown job ID {job_id!r}")


def _job_record(
    job_id: str,
    *,
    label: str,
    status: JobStatus = JobStatus.RUNNING,
    project_name: str = "serena",
    finished_at: str | None = None,
) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        unit_name=f"serena-job-{job_id}.service",
        project_root="/tmp/serena",
        cwd="/tmp/serena",
        status=status,
        created_at="2026-08-30T20:00:00+00:00",
        project_name=project_name,
        label=label,
        finished_at=finished_at,
    )


class _MockAgent:
    @staticmethod
    def get_context() -> SerenaAgentContext:
        return SerenaAgentContext.load_default()


class _EchoCommandTool(Tool):
    def __init__(self) -> None:
        super().__init__(_MockAgent())

    def apply(self, command: str) -> str:
        """Echoes one command for MCP wrapper testing.

        :param command: command text to echo
        :return: echoed command
        """
        return command

    def apply_ex(self, **kwargs) -> str:
        return self.apply(command=kwargs["command"])


class _StartJobResultTool(Tool):
    """Minimal start-job-shaped tool for exercising FastMCP result conversion."""

    def __init__(self) -> None:
        super().__init__(_MockAgent())

    @classmethod
    def get_name_from_cls(cls) -> str:
        return "start_job"

    def apply(self, command: str, label: str) -> str:
        """Return one start-job-shaped JSON payload.

        :param command: ignored command text
        :param label: job label returned in the payload
        :return: serialized start-job result
        """
        return f'{{"job_id":"wrapped-job","label":"{label}"}}'

    def apply_ex(self, **kwargs) -> str:
        return self.apply(command=kwargs["command"], label=kwargs["label"])


def test_activity_tracker_records_tool_lifecycle() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    call_id = tracker.start_tool(
        "conversation-a",
        "execute_shell_command",
        {"command": "uv run poe test", "unrelated": "not displayed"},
    )
    tracker.finish_tool(call_id, succeeded=True)

    snapshot = tracker.get_run("conversation-a", run["run_id"])
    assert snapshot["project_name"] == "serena"
    assert snapshot["calls"] == [
        {
            "call_id": call_id,
            "tool_name": "execute_shell_command",
            "detail": "uv run poe test",
            "started_at": snapshot["calls"][0]["started_at"],
            "finished_at": snapshot["calls"][0]["finished_at"],
            "status": "completed",
        }
    ]
    assert snapshot["calls"][0]["finished_at"] is not None


def test_activity_tracker_retains_long_command_history() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    for index in range(150):
        call_id = tracker.start_tool("conversation-a", "echo_command", {"command": f"command {index}"})
        tracker.finish_tool(call_id, succeeded=True)

    snapshot = tracker.get_run("conversation-a", run["run_id"])

    assert len(snapshot["calls"]) == 150
    assert snapshot["calls"][0]["detail"] == "command 0"
    assert snapshot["calls"][-1]["detail"] == "command 149"


def test_activity_tracker_backfills_job_started_before_panel() -> None:
    source = _FakeJobSource([_job_record("abc123", label="full test suite")])
    tracker = ActivityTracker(source)

    call_id = tracker.start_tool(
        "conversation-a",
        "start_job",
        {"command": "uv run poe test", "label": "full test suite"},
    )
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result=(
            [SimpleNamespace(text='{"job_id":"abc123","label":"full test suite"}')],
            {"result": '{"job_id":"abc123","label":"full test suite"}'},
        ),
    )

    run = tracker.start_run("conversation-a", "serena")
    snapshot = tracker.get_run("conversation-a", run["run_id"])

    assert [(job["job_id"], job["label"], job["status"]) for job in snapshot["jobs"]] == [("abc123", "full test suite", "running")]
    assert snapshot["busy"]["this_chat"] is True
    assert snapshot["busy"]["running_jobs"] == 1
    assert snapshot["busy"]["elsewhere"] is False


def test_activity_tracker_new_panel_omits_old_terminal_jobs() -> None:
    source = _FakeJobSource([_job_record("old-job", label="old optimisation")])
    tracker = ActivityTracker(source)

    call_id = tracker.start_tool("conversation-a", "start_job", {"command": "long command", "label": "old optimisation"})
    tracker.finish_tool(call_id, succeeded=True, result='{"job_id":"old-job","label":"old optimisation"}')
    source.records[0] = replace(
        source.records[0],
        status=JobStatus.COMPLETED,
        finished_at="2026-08-30T20:10:00+00:00",
    )

    run = tracker.start_run("conversation-a", "serena")
    snapshot = tracker.get_run("conversation-a", run["run_id"])

    assert snapshot["jobs"] == []


def test_activity_tracker_keeps_job_dispatched_during_current_panel_after_completion() -> None:
    source = _FakeJobSource([_job_record("current-job", label="current optimisation")])
    tracker = ActivityTracker(source)
    run = tracker.start_run("conversation-a", "serena")

    call_id = tracker.start_tool("conversation-a", "start_job", {"command": "long command", "label": "current optimisation"})
    tracker.finish_tool(call_id, succeeded=True, result='{"job_id":"current-job","label":"current optimisation"}')
    source.records[0] = replace(
        source.records[0],
        status=JobStatus.COMPLETED,
        finished_at="2026-08-30T20:10:00+00:00",
    )

    snapshot = tracker.get_run("conversation-a", run["run_id"])

    assert [(job["job_id"], job["status"]) for job in snapshot["jobs"]] == [("current-job", "completed")]


def test_activity_tracker_new_panel_backfills_active_tool_from_this_chat() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    call_id = tracker.start_tool("conversation-a", "execute_shell_command", {"command": "sleep 5"})

    run = tracker.start_run("conversation-a", "serena")
    snapshot = tracker.get_run("conversation-a", run["run_id"])

    assert [(call["tool_name"], call["status"]) for call in snapshot["calls"]] == [("execute_shell_command", "running")]
    assert snapshot["busy"]["this_chat"] is True

    tracker.finish_tool(call_id, succeeded=True)
    snapshot = tracker.get_run("conversation-a", run["run_id"])
    assert [(call["tool_name"], call["status"]) for call in snapshot["calls"]] == [("execute_shell_command", "completed")]


def test_activity_tracker_restores_persisted_job_ownership_after_restart() -> None:
    source = _FakeJobSource([_job_record("persisted-job", label="surviving optimisation")])
    first_tracker = ActivityTracker(source)

    call_id = first_tracker.start_tool(
        "conversation-a",
        "start_job",
        {"command": "long command", "label": "surviving optimisation"},
    )
    first_tracker.finish_tool(
        call_id,
        succeeded=True,
        result='{"job_id":"persisted-job","label":"surviving optimisation"}',
    )

    restarted_tracker = ActivityTracker(source)
    run = restarted_tracker.start_run("conversation-a", "serena")
    snapshot = restarted_tracker.get_run("conversation-a", run["run_id"])

    assert [job["job_id"] for job in snapshot["jobs"]] == ["persisted-job"]
    assert snapshot["busy"]["this_chat"] is True
    assert snapshot["busy"]["elsewhere"] is False


def test_activity_tracker_does_not_claim_unowned_job_from_status() -> None:
    source = _FakeJobSource([_job_record("other-job", label="someone else's optimisation")])
    tracker = ActivityTracker(source)
    run = tracker.start_run("conversation-a", "serena")

    call_id = tracker.start_tool("conversation-a", "job_status", {"job_id": "other-job"})
    tracker.finish_tool(call_id, succeeded=True)
    snapshot = tracker.get_run("conversation-a", run["run_id"])

    assert snapshot["jobs"] == []
    assert snapshot["busy"]["this_chat"] is False
    assert snapshot["busy"]["elsewhere"] is True


def test_activity_tracker_reports_other_chat_busy_without_job_details() -> None:
    source = _FakeJobSource([_job_record("other-job", label="private other-chat work", project_name="thesis")])
    tracker = ActivityTracker(source)
    other_call = tracker.start_tool("conversation-b", "execute_shell_command", {"command": "private command"})
    run = tracker.start_run("conversation-a", "serena")

    snapshot = tracker.get_run("conversation-a", run["run_id"])

    assert snapshot["jobs"] == []
    assert snapshot["busy"]["this_chat"] is False
    assert snapshot["busy"]["elsewhere"] is True
    assert snapshot["busy"]["other_active_tools"] == 1
    assert snapshot["busy"]["other_running_jobs"] == 1

    tracker.finish_tool(other_call, succeeded=True)


def test_activity_tracker_isolates_conversations() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    with pytest.raises(ValueError, match="not available"):
        tracker.get_run("conversation-b", run["run_id"])


def test_activity_tracker_supersedes_previous_panel_in_same_conversation() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    first = tracker.start_run("conversation-a", "serena")
    second = tracker.start_run("conversation-a", "serena")

    first_state = tracker.get_run("conversation-a", first["run_id"])
    second_state = tracker.get_run("conversation-a", second["run_id"])

    assert first_state["superseded"] is True
    assert second_state["superseded"] is False


def test_get_mcp_session_id_prefers_openai_conversation_metadata() -> None:
    meta = RequestParams.Meta.model_validate({"openai/session": "conversation-123"})
    context = SimpleNamespace(request_context=SimpleNamespace(meta=meta), session=object())

    assert get_mcp_session_id(context) == "conversation-123"


def test_mcp_tool_wrapper_records_activity() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    run = tracker.start_run("global", "serena")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_EchoCommandTool(), activity_tracker=tracker)

    result = asyncio.run(mcp_tool.run({"command": "git status"}))

    assert result == "git status"
    snapshot = tracker.get_run("global", run["run_id"])
    assert [(call["tool_name"], call["detail"], call["status"]) for call in snapshot["calls"]] == [
        ("echo_command", "git status", "completed")
    ]


def test_mcp_start_job_wrapper_associates_converted_result() -> None:
    source = _FakeJobSource([_job_record("wrapped-job", label="wrapped label")])
    tracker = ActivityTracker(source)
    run = tracker.start_run("global", "serena")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_StartJobResultTool(), activity_tracker=tracker)

    asyncio.run(mcp_tool.run({"command": "sleep 1", "label": "wrapped label"}, convert_result=True))

    snapshot = tracker.get_run("global", run["run_id"])
    assert [job["job_id"] for job in snapshot["jobs"]] == ["wrapped-job"]
    assert source.records[0].activity_owner_token is not None


def test_activity_resource_uses_mcp_app_contract() -> None:
    async def inspect_resource() -> tuple[object, object]:
        mcp = FastMCP("activity-test")
        register_activity_resource(mcp)
        resources = await mcp.list_resources()
        contents = await mcp.read_resource(ACTIVITY_RESOURCE_URI)
        return resources[0], contents[0]

    resource, content = asyncio.run(inspect_resource())

    assert str(resource.uri) == ACTIVITY_RESOURCE_URI
    assert resource.mimeType == "text/html;profile=mcp-app"
    assert resource.meta == {
        "ui": {"prefersBorder": True},
        "openai/widgetDescription": "Shows Serena tool calls, durable jobs, and whether Serena is busy elsewhere.",
    }
    assert content.mime_type == "text/html;profile=mcp-app"
    assert 'window.openai.callTool("get_activity"' in content.content
    assert 'id="activity-logo"' in content.content
    assert 'id="activity-latest"' in content.content
    assert 'id="activity-latest-detail"' in content.content
    assert 'id="serena-activity" class="activity collapsed"' in content.content
    assert 'id="activity-header" class="header" type="button" aria-expanded="false"' in content.content
    assert 'id="activity-body" class="body" aria-live="polite" hidden' in content.content
    assert "if (state?.run_id && next.run_id !== state.run_id) return;" in content.content
    assert (
        """if (next?.superseded) {
        retirePanel();
        return;
      }
      if (next?.run_id) render(next);"""
        in content.content
    )
    assert "<strong>Serena</strong>" not in content.content


def test_activity_tools_expose_widget_and_private_polling_contract() -> None:
    class Agent:
        @staticmethod
        def get_active_project():
            return None

    async def inspect_tools() -> dict[str, object]:
        factory = SerenaMCPFactory(transport="stdio", context="chatgpt")
        factory.agent = Agent()  # type: ignore[assignment]
        mcp = FastMCP("activity-test")
        factory._register_activity_tools(mcp)
        return {tool.name: tool for tool in await mcp.list_tools()}

    tools = asyncio.run(inspect_tools())
    show_meta = tools["show_activity"].meta
    poll_meta = tools["get_activity"].meta

    assert show_meta is not None
    assert show_meta["ui"] == {"resourceUri": ACTIVITY_RESOURCE_URI, "visibility": ["model", "app"]}
    assert show_meta["openai/outputTemplate"] == ACTIVITY_RESOURCE_URI
    assert poll_meta is not None
    assert poll_meta["ui"] == {"visibility": ["app"]}
    assert poll_meta["openai/visibility"] == "private"
