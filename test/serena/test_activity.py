import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import RequestParams

from serena.activity import ACTIVITY_RESOURCE_URI, ActivityTracker, get_mcp_session_id, register_activity_resource
from serena.config.context_mode import SerenaAgentContext
from serena.jobs import JobOutputChunk, JobRecord, JobRuntimeInfo, JobSnapshot, JobStatus
from serena.mcp import SerenaMCPFactory
from serena.tools import Tool


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


class _SlowEchoCommandTool(_EchoCommandTool):
    """Blocking Serena tool used to verify MCP event-loop responsiveness."""

    def apply_ex(self, **kwargs) -> str:
        time.sleep(0.25)
        return super().apply_ex(**kwargs)


class _StartJobResultTool(Tool):
    """Minimal start-job-shaped tool for exercising FastMCP result conversion."""

    def __init__(self) -> None:
        super().__init__(_MockAgent())

    @classmethod
    def get_name_from_cls(cls) -> str:
        return "start_job"

    def apply(self, command: str, label: str) -> str:
        """Return one serialized start-job payload.

        :param command: ignored command text
        :param label: job label returned in the payload
        :return: serialized start-job result
        """
        return f'{{"job_id":"wrapped-job","label":"{label}"}}'

    def apply_ex(self, **kwargs) -> str:
        return self.apply(command=kwargs["command"], label=kwargs["label"])


class _FakeJobSource:
    """Minimal durable-job source for deterministic activity tests."""

    def __init__(self, records: list[JobRecord] | None = None) -> None:
        self.records = records or []
        self.outputs: dict[str, str] = {}

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        return self.records[:limit]

    def get_job(self, job_id: str) -> JobSnapshot:
        record = next(record for record in self.records if record.job_id == job_id)
        return JobSnapshot(
            record=record,
            runtime=JobRuntimeInfo(
                elapsed_seconds=12.5,
                seconds_since_last_output=0.5,
                memory_bytes=2048,
                cpu_seconds=1.25,
                process_count=2,
            ),
            output=JobOutputChunk(
                output=self.outputs.get(job_id, ""),
                next_cursor=None,
                has_more_output=False,
            ),
        )


def _job_record(job_id: str, label: str, status: JobStatus = JobStatus.RUNNING, project_name: str = "thesis") -> JobRecord:
    now = datetime.now(UTC).isoformat()
    return JobRecord(
        job_id=job_id,
        unit_name=f"serena-job-{job_id}.service",
        project_root="/tmp/project",
        cwd="/tmp/project",
        status=status,
        created_at=now,
        project_name=project_name,
        label=label,
        finished_at=now if status.is_terminal else None,
    )


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


def test_activity_tracker_exposes_tool_detail_on_demand() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool(
        "conversation-a",
        "execute_shell_command",
        {"command": "echo hello", "payload": "x" * 9000},
    )
    tracker.finish_tool(call_id, succeeded=True, result={"ok": True, "payload": "y" * 9000})

    assert call_id is not None
    detail = tracker.get_call_detail("conversation-a", run["run_id"], call_id)
    assert detail["tool_name"] == "execute_shell_command"
    assert detail["status"] == "completed"
    assert '"command": "echo hello"' in detail["arguments"]
    assert "... detail omitted ..." in detail["arguments"]
    assert '"ok": true' in detail["result"]
    assert "... detail omitted ..." in detail["result"]


def test_activity_tracker_marks_current_turn_job_and_exposes_other_running_jobs() -> None:
    source = _FakeJobSource([_job_record("other-job", "other optimisation")])
    tracker = ActivityTracker(source)
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "start_job", {"label": "current optimisation"})

    source.records.append(_job_record("current-job", "current optimisation"))
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result={"job_id": "current-job", "label": "current optimisation"},
    )
    snapshot = tracker.get_run("conversation-a", run["run_id"])

    assert [(job["job_id"], job["current_turn"]) for job in snapshot["jobs"]] == [
        ("current-job", True),
        ("other-job", False),
    ]
    assert snapshot["calls"][0]["job_id"] == "current-job"
    assert snapshot["calls"][0]["detail"] == "current optimisation"


def test_activity_tracker_exposes_job_runtime_and_output_on_demand() -> None:
    source = _FakeJobSource([_job_record("current-job", "current optimisation")])
    source.outputs["current-job"] = "step 1\nstep 2"
    tracker = ActivityTracker(source)
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "start_job", {"label": "current optimisation"})
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result={"job_id": "current-job", "label": "current optimisation"},
    )

    detail = tracker.get_job_detail("conversation-a", run["run_id"], "current-job")

    assert detail["label"] == "current optimisation"
    assert detail["status"] == "running"
    assert detail["elapsed_seconds"] == 12.5
    assert detail["seconds_since_last_output"] == 0.5
    assert detail["memory_bytes"] == 2048
    assert detail["process_count"] == 2
    assert detail["output"] == "step 1\nstep 2"


def test_activity_tracker_isolates_conversations() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    with pytest.raises(ValueError, match="not available"):
        tracker.get_run("conversation-b", run["run_id"])


def test_activity_tracker_supersedes_previous_panel_in_same_conversation() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    first = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "execute_shell_command", {"command": "sleep 5"})

    second = tracker.start_run("conversation-a", "serena")
    second_state = tracker.get_run("conversation-a", second["run_id"])

    assert tracker.get_run("conversation-a", first["run_id"])["superseded"] is True
    assert second_state["superseded"] is False
    assert [(call["call_id"], call["status"]) for call in second_state["calls"]] == [(call_id, "running")]

    tracker.finish_tool(call_id, succeeded=True)
    assert tracker.get_run("conversation-a", first["run_id"])["calls"][0]["status"] == "completed"
    assert tracker.get_run("conversation-a", second["run_id"])["calls"][0]["status"] == "completed"


def test_superseded_panel_retains_its_jobs_without_absorbing_background_jobs() -> None:
    source = _FakeJobSource(
        [
            _job_record("first-job", "first job"),
            _job_record("background-job", "background job"),
        ]
    )
    tracker = ActivityTracker(source)
    first = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "start_job", {"command": "sleep 5"})

    tracker.finish_tool(call_id, succeeded=True, result={"job_id": "first-job", "label": "first job"})
    second = tracker.start_run("conversation-a", "serena")

    first_state = tracker.get_run("conversation-a", first["run_id"])
    second_state = tracker.get_run("conversation-a", second["run_id"])

    assert [(job["job_id"], job["current_turn"]) for job in first_state["jobs"]] == [("first-job", True)]
    assert {(job["job_id"], job["current_turn"]) for job in second_state["jobs"]} == {
        ("first-job", False),
        ("background-job", False),
    }


def test_carried_start_job_is_retained_by_old_and_new_panels() -> None:
    source = _FakeJobSource([_job_record("shared-job", "shared job")])
    tracker = ActivityTracker(source)
    first = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "start_job", {"command": "sleep 5"})

    second = tracker.start_run("conversation-a", "serena")
    tracker.finish_tool(call_id, succeeded=True, result={"job_id": "shared-job", "label": "shared job"})

    first_state = tracker.get_run("conversation-a", first["run_id"])
    second_state = tracker.get_run("conversation-a", second["run_id"])

    assert [(job["job_id"], job["current_turn"]) for job in first_state["jobs"]] == [("shared-job", True)]
    assert [(job["job_id"], job["current_turn"]) for job in second_state["jobs"]] == [("shared-job", True)]


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


def test_mcp_tool_wrapper_keeps_activity_polling_responsive_during_blocking_tool() -> None:
    tracker = ActivityTracker(_FakeJobSource())
    run = tracker.start_run("global", "serena")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_SlowEchoCommandTool(), activity_tracker=tracker)

    async def exercise() -> str:
        invocation = asyncio.create_task(mcp_tool.run({"command": "slow command"}))
        await asyncio.sleep(0.05)

        snapshot = tracker.get_run("global", run["run_id"])
        assert invocation.done() is False
        assert [call["status"] for call in snapshot["calls"]] == ["running"]

        result = await invocation
        terminal = tracker.get_run("global", run["run_id"])
        assert terminal["calls"][0]["status"] == "completed"
        return result

    assert asyncio.run(exercise()) == "slow command"


def test_mcp_start_job_wrapper_associates_converted_result_with_current_turn() -> None:
    source = _FakeJobSource([_job_record("wrapped-job", "wrapped label")])
    tracker = ActivityTracker(source)
    run = tracker.start_run("global", "serena")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_StartJobResultTool(), activity_tracker=tracker)

    asyncio.run(mcp_tool.run({"command": "sleep 1", "label": "wrapped label"}, convert_result=True))

    snapshot = tracker.get_run("global", run["run_id"])
    assert [(job["job_id"], job["current_turn"]) for job in snapshot["jobs"]] == [("wrapped-job", True)]


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
        "openai/widgetDescription": "Shows Serena tool calls, current-turn jobs, and a compact indicator for other running jobs.",
    }
    assert content.mime_type == "text/html;profile=mcp-app"
    assert 'window.openai.callTool("get_activity"' in content.content
    assert 'window.openai.callTool("get_activity_detail"' in content.content
    assert 'window.openai.callTool("get_activity_job_detail"' in content.content
    assert 'id="activity-logo" class="logo"' in content.content
    assert 'id="activity-header-tool">Waiting for activity</strong>' in content.content
    assert 'id="activity-header-elapsed" class="summary"' in content.content
    assert 'id="activity-other-jobs" class="other-jobs" type="button" aria-expanded="false" hidden' in content.content


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
    detail_meta = tools["get_activity_detail"].meta
    job_detail_meta = tools["get_activity_job_detail"].meta

    assert show_meta is not None
    assert show_meta["ui"] == {"resourceUri": ACTIVITY_RESOURCE_URI, "visibility": ["model", "app"]}
    assert show_meta["openai/outputTemplate"] == ACTIVITY_RESOURCE_URI
    assert poll_meta is not None
    assert poll_meta["ui"] == {"visibility": ["app"]}
    assert poll_meta["openai/visibility"] == "private"
    assert detail_meta is not None
    assert detail_meta["ui"] == {"visibility": ["app"]}
    assert detail_meta["openai/visibility"] == "private"
    assert job_detail_meta is not None
    assert job_detail_meta["ui"] == {"visibility": ["app"]}
    assert job_detail_meta["openai/visibility"] == "private"
