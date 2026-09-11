import asyncio
import json
import time
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, RequestParams, ResourceLink
from pydantic import AnyUrl

from serena.activity import ACTIVITY_RESOURCE_URI, ActivityRunManager, register_activity_resource
from serena.activity_transport import activity_call_detail_payload, activity_job_detail_payload, activity_snapshot_payload
from serena.activity_view import ActivityView
from serena.execution_metadata import ExecutionResultMetadata, extract_execution_result_metadata
from serena.execution_store import ExecutionStore
from serena.git_metrics import GitLineMetrics
from serena.jobs import JobOutputChunk, JobRecord, JobRuntimeInfo, JobSnapshot, JobStatus
from serena.mcp import SerenaMCPFactory
from serena.session import get_mcp_session_id
from serena.tool_output import ToolOutputStore
from serena.tools import Tool


class _MockAgent:
    def __init__(self) -> None:
        self.serena_config = SimpleNamespace(tool_timeout=30, default_max_tool_answer_tokens=4_000)
        self._output_dir = TemporaryDirectory(prefix="serena-activity-test-output-")
        self.tool_output_store = ToolOutputStore(root=Path(self._output_dir.name))

    @staticmethod
    def get_active_project_for_session(session_id: str):
        return None

    @staticmethod
    def submission_project_context(session_id: str):
        del session_id
        return nullcontext()


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

    def apply(self, command: str, label: str) -> dict[str, str]:
        """Return one native start-job payload.

        :param command: ignored command text
        :param label: job label returned in the payload
        :return: native start-job result
        """
        return {"job_id": "wrapped-job", "label": label}

    def apply_ex(self, **kwargs) -> dict[str, str]:
        return self.apply(command=kwargs["command"], label=kwargs["label"])


class _FakeJobSource:
    """Minimal durable-job source for deterministic activity tests."""

    def __init__(self, records: list[JobRecord] | None = None) -> None:
        self.records = records or []
        self.outputs: dict[str, str] = {}
        self.max_concurrent_jobs = 12

    def list_running_jobs(self) -> list[JobRecord]:
        return [record for record in self.records if record.status is JobStatus.RUNNING]

    def get_job_record(self, job_id: str) -> JobRecord:
        try:
            return next(record for record in self.records if record.job_id == job_id)
        except StopIteration:
            raise KeyError(job_id) from None

    def get_job_records(self, job_ids: set[str]) -> list[JobRecord]:
        return [record for record in self.records if record.job_id in job_ids]

    def get_job(self, job_id: str) -> JobSnapshot:
        record = self.get_job_record(job_id)
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


class _ActivityHarness:
    """Test helper that drives the production store/run/view split through UI-shaped results."""

    def __init__(
        self,
        job_source: _FakeJobSource,
        execution_store: ExecutionStore | None = None,
        git_metrics_source: object | None = None,
    ) -> None:
        self._temporary_store_dir: TemporaryDirectory[str] | None = None
        if execution_store is None:
            self._temporary_store_dir = TemporaryDirectory(prefix="serena-activity-store-test-")
            execution_store = ExecutionStore(Path(self._temporary_store_dir.name))
        self.execution_store = execution_store
        self.run_manager = ActivityRunManager(execution_store)
        self.view = ActivityView(execution_store, job_source, git_metrics_source)

    @staticmethod
    def extract_result_metadata(result: object | None) -> ExecutionResultMetadata:
        return extract_execution_result_metadata(result)

    def start_run(self, session_id: str, project_name: str) -> dict[str, Any]:
        run = self.run_manager.start_run(session_id, project_name)
        return self._snapshot_payload(self.view.for_run(session_id, run.run_id, refresh_git_metrics=True))

    def update_project(self, session_id: str, project_name: str) -> None:
        self.run_manager.update_project(session_id, project_name)

    def start_tool(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        project_name: str = "",
        execution_id: str | None = None,
    ) -> str:
        execution_id = execution_id or uuid.uuid4().hex
        run = self.execution_store.get_current_activity_run(session_id)
        effective_project = project_name or (run.project_name if run is not None else "")
        self.execution_store.start_execution(
            execution_id=execution_id,
            session_id=session_id,
            project_name=effective_project,
            tool_name=tool_name,
            arguments=arguments,
        )
        self.run_manager.associate_execution(session_id, execution_id, project_name=effective_project)
        return execution_id

    def finish_tool(
        self,
        call_id: str | None,
        succeeded: bool,
        result_serialization: str | None = None,
        error: str | None = None,
        project_name: str | None = None,
        result_metadata: ExecutionResultMetadata | None = None,
        retained_output_id: str | None = None,
        retained_output_chars: int | None = None,
    ) -> None:
        if call_id is None:
            return
        metadata = result_metadata or ExecutionResultMetadata(media=None, durable_job_id=None, durable_job_label=None)
        media = metadata.media if succeeded else None
        self.execution_store.finish_execution(
            call_id,
            succeeded=succeeded,
            result=result_serialization if succeeded and media is None else None,
            error=error,
            project_name=project_name,
            retained_output_id=retained_output_id if succeeded else None,
            retained_output_chars=retained_output_chars if succeeded else None,
            media=media.storage_dict() if media is not None else None,
            durable_job_id=metadata.durable_job_id if succeeded else None,
            durable_job_label=metadata.durable_job_label if succeeded else None,
        )

    def get_run(self, session_id: str, run_id: str, *, refresh_git_metrics: bool = False) -> dict[str, Any]:
        return self._snapshot_payload(self.view.for_run(session_id, run_id, refresh_git_metrics=refresh_git_metrics))

    def get_call_detail(self, session_id: str, run_id: str, call_id: str) -> dict[str, Any]:
        return self._call_detail_payload(self.view.call_detail(session_id, run_id, call_id))

    def get_call_media(self, session_id: str, run_id: str, call_id: str):
        return self.view.call_media(session_id, run_id, call_id)

    def get_job_detail(self, session_id: str, run_id: str, job_id: str) -> dict[str, Any]:
        return self._job_detail_payload(self.view.job_detail(session_id, run_id, job_id))

    @staticmethod
    def _snapshot_payload(snapshot) -> dict[str, Any]:
        return activity_snapshot_payload(snapshot)

    @staticmethod
    def _call_detail_payload(detail) -> dict[str, Any]:
        return activity_call_detail_payload(detail)

    @staticmethod
    def _job_detail_payload(detail) -> dict[str, Any]:
        return activity_job_detail_payload(detail)


class _FixedGitMetricsSource:
    """Provides deterministic cached Git metrics without repository access."""

    def get_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        if project_name == "serena":
            return GitLineMetrics(additions=12, deletions=4, ahead_commits=3)
        return None

    def refresh_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        return self.get_project_git_metrics(project_name)


class _RefreshingGitMetricsSource:
    """Exposes distinct cached and refreshed metrics for activity polling tests."""

    def __init__(self) -> None:
        self.refresh_calls = 0

    def get_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        if project_name == "serena":
            return GitLineMetrics(additions=1, deletions=1, ahead_commits=1)
        return None

    def refresh_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        self.refresh_calls += 1
        if project_name == "serena":
            return GitLineMetrics(additions=8, deletions=3, ahead_commits=4)
        return None


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
    tracker = _ActivityHarness(_FakeJobSource(), git_metrics_source=_FixedGitMetricsSource())
    run = tracker.start_run("conversation-a", "serena")

    call_id = tracker.start_tool(
        "conversation-a",
        "execute_shell_command",
        {"command": "uv run poe test", "unrelated": "not displayed"},
    )
    tracker.finish_tool(call_id, succeeded=True)

    snapshot = tracker.get_run("conversation-a", run["run_id"])
    assert snapshot["project_name"] == "serena"
    assert snapshot["git_additions"] == 12
    assert snapshot["git_deletions"] == 4
    assert snapshot["git_ahead_commits"] == 3
    assert snapshot["calls"] == [
        {
            "call_id": call_id,
            "tool_name": "execute_shell_command",
            "detail": "uv run poe test",
            "project_name": "serena",
            "started_at": snapshot["calls"][0]["started_at"],
            "finished_at": snapshot["calls"][0]["finished_at"],
            "status": "completed",
        }
    ]
    assert snapshot["calls"][0]["finished_at"] is not None


def test_activity_run_refreshes_git_metrics_only_when_requested() -> None:
    source = _RefreshingGitMetricsSource()
    tracker = _ActivityHarness(_FakeJobSource(), git_metrics_source=source)

    run = tracker.start_run("conversation-a", "serena")
    assert source.refresh_calls == 1
    assert run["git_additions"] == 8
    assert run["git_deletions"] == 3
    assert run["git_ahead_commits"] == 4

    cached = tracker.get_run("conversation-a", run["run_id"])
    assert source.refresh_calls == 1
    assert cached["git_additions"] == 1
    assert cached["git_deletions"] == 1
    assert cached["git_ahead_commits"] == 1

    refreshed = tracker.get_run("conversation-a", run["run_id"], refresh_git_metrics=True)
    assert source.refresh_calls == 2
    assert refreshed["git_additions"] == 8
    assert refreshed["git_deletions"] == 3
    assert refreshed["git_ahead_commits"] == 4


def test_activity_tracker_rehydrates_historical_turn_after_restart(tmp_path: Path) -> None:
    source = _FakeJobSource([_job_record("job-a", "retained job", JobStatus.COMPLETED)])
    store_root = tmp_path / "execution-store"
    tracker = _ActivityHarness(source, execution_store=ExecutionStore(store_root))
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool(
        "conversation-a",
        "search_for_pattern",
        {"substring_pattern": "_ActivityHarness", "relative_path": "src/serena"},
    )
    tracker.finish_tool(call_id, succeeded=True, result_serialization='{"matches":3}')
    job_call_id = tracker.start_tool("conversation-a", "start_job", {"label": "retained job"})
    job_result = {"job_id": "job-a", "label": "retained job"}
    tracker.finish_tool(
        job_call_id,
        succeeded=True,
        result_serialization=json.dumps(job_result, separators=(",", ":")),
        result_metadata=_ActivityHarness.extract_result_metadata(job_result),
    )
    tracker.get_run("conversation-a", run["run_id"])
    interrupted_id = tracker.start_tool("conversation-a", "execute_shell_command", {"command": "sleep 30"})

    restored = _ActivityHarness(source, execution_store=ExecutionStore(store_root))
    snapshot = restored.get_run("conversation-a", run["run_id"])
    detail = restored.get_call_detail("conversation-a", run["run_id"], call_id)

    assert snapshot["superseded"] is True
    assert [call["tool_name"] for call in snapshot["calls"]] == ["search_for_pattern", "start_job", "execute_shell_command"]
    assert snapshot["calls"][-1]["call_id"] == interrupted_id
    assert snapshot["calls"][-1]["status"] == "failed"
    assert snapshot["calls"][-1]["finished_at"] is not None
    assert [(job["job_id"], job["current_turn"]) for job in snapshot["jobs"]] == [("job-a", True)]
    assert detail["arguments"] == {"substring_pattern": "_ActivityHarness", "relative_path": "src/serena"}
    assert json.loads(detail["result"]) == {"matches": 3}


def test_activity_tracker_uses_semantic_tool_detail_lines() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    calls = [
        tracker.start_tool(
            "conversation-a",
            "search_for_pattern",
            {"substring_pattern": "_ActivityHarness.*detail", "relative_path": "src/serena"},
        ),
        tracker.start_tool(
            "conversation-a",
            "find_file",
            {"file_mask": "*.py", "relative_path": "src/serena"},
        ),
        tracker.start_tool(
            "conversation-a",
            "rename_symbol",
            {"name_path": "_ActivityHarness", "relative_path": "src/serena/activity.py", "new_name": "ActivityStore"},
        ),
        tracker.start_tool(
            "conversation-a",
            "replace_content",
            {"relative_path": "src/serena/activity.py", "needle": "old value", "repl": "new value", "mode": "literal"},
        ),
        tracker.start_tool(
            "conversation-a",
            "start_job",
            {"command": "uv run poe test", "label": "Run activity tests", "cwd": "test"},
        ),
        tracker.start_tool(
            "conversation-a",
            "git_branch",
            {"action": "switch", "name": "mcp-media"},
        ),
        tracker.start_tool(
            "conversation-a",
            "read_file",
            {"relative_path": "src/serena/activity.py"},
        ),
    ]

    snapshot = tracker.get_run("conversation-a", run["run_id"])
    assert calls == [call["call_id"] for call in snapshot["calls"]]
    assert [(call["detail"], call.get("scope", "")) for call in snapshot["calls"]] == [
        ("_ActivityHarness.*detail", "src/serena"),
        ("*.py", "src/serena"),
        ("_ActivityHarness → ActivityStore", "src/serena/activity.py"),
        ("old value", "src/serena/activity.py"),
        ("Run activity tests", "test"),
        ("switch · mcp-media", ""),
        ("", "src/serena/activity.py"),
    ]


def test_job_status_detail_prefers_known_job_label() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    start_call_id = tracker.start_tool("conversation-a", "start_job", {"label": "Optimise chapter"})
    result = {"job_id": "job-a", "label": "Optimise chapter"}
    tracker.finish_tool(
        start_call_id,
        succeeded=True,
        result_serialization=json.dumps(result, separators=(",", ":")),
        result_metadata=_ActivityHarness.extract_result_metadata(result),
    )

    status_call_id = tracker.start_tool(
        "conversation-a",
        "job_status",
        {"job_id": "job-a", "cursor": "cursor-a"},
    )

    snapshot = tracker.get_run("conversation-a", run["run_id"])
    status_call = next(call for call in snapshot["calls"] if call["call_id"] == status_call_id)
    assert status_call["detail"] == "Optimise chapter"


def test_job_status_detail_uses_returned_label_when_not_known_at_start() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    status_call_id = tracker.start_tool("conversation-a", "job_status", {"job_id": "job-a"})
    result = {"job_id": "job-a", "label": "Recovered optimisation"}
    tracker.finish_tool(
        status_call_id,
        succeeded=True,
        result_serialization=json.dumps(result, separators=(",", ":")),
        result_metadata=_ActivityHarness.extract_result_metadata(result),
    )

    snapshot = tracker.get_run("conversation-a", run["run_id"])
    status_call = next(call for call in snapshot["calls"] if call["call_id"] == status_call_id)
    assert status_call["detail"] == "Recovered optimisation"


def test_activity_tracker_detail_lines_skip_empty_values_and_remain_bounded() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    tracker.start_tool(
        "conversation-a",
        "search_for_pattern",
        {"substring_pattern": "x" * 220, "relative_path": ""},
    )
    tracker.start_tool(
        "conversation-a",
        "find_symbol",
        {"name_path_pattern": "_ActivityHarness", "relative_path": ""},
    )

    snapshot = tracker.get_run("conversation-a", run["run_id"])
    assert snapshot["calls"][0]["detail"] == "x" * 177 + "..."
    assert snapshot["calls"][0].get("scope", "") == ""
    assert snapshot["calls"][1]["detail"] == "_ActivityHarness"
    assert snapshot["calls"][1].get("scope", "") == ""


def test_activity_tracker_exposes_tool_detail_on_demand() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool(
        "conversation-a",
        "execute_shell_command",
        {"command": "echo hello", "payload": "x" * 9000},
    )
    canonical_result = json.dumps(
        {
            "truncated": True,
            "total_chars": 9_000,
            "output_id": "retained-output",
            "result": {"ok": True, "payload": "bounded preview"},
        },
        separators=(",", ":"),
    )
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result_serialization=canonical_result,
        retained_output_id="retained-output",
        retained_output_chars=9_000,
    )

    assert call_id is not None
    detail = tracker.get_call_detail("conversation-a", run["run_id"], call_id)
    arguments = detail["arguments"]
    result = json.loads(detail["result"])

    assert detail["tool_name"] == "execute_shell_command"
    assert detail["status"] == "completed"
    assert arguments["command"] == "echo hello"
    assert "chars omitted" in arguments["payload"]
    assert detail["result"] == canonical_result
    assert result["result"] == {"ok": True, "payload": "bounded preview"}
    assert detail["structured_result"] == result


def test_activity_tracker_exposes_typed_shell_result_for_rich_rendering() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "execute_shell_command", {"command": "printf hello"})
    canonical_result = '{"return_code":0,"stdout":"hello"}'
    tracker.finish_tool(call_id, succeeded=True, result_serialization=canonical_result)

    assert call_id is not None
    detail = tracker.get_call_detail("conversation-a", run["run_id"], call_id)
    assert detail["result"] == canonical_result
    assert detail["structured_result"] == {"return_code": 0, "stdout": "hello"}


def test_activity_tracker_preserves_json_looking_string_result_for_rich_rendering() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "read_file", {"relative_path": "payload.txt"})
    logical_result = '{"message": "this is text, not a structured result"}'
    canonical_result = json.dumps(logical_result)
    tracker.finish_tool(call_id, succeeded=True, result_serialization=canonical_result)

    assert call_id is not None
    detail = tracker.get_call_detail("conversation-a", run["run_id"], call_id)
    assert detail["result"] == canonical_result
    assert detail["structured_result"] == logical_result


def test_activity_tracker_preserves_canonical_result_serialization_byte_for_byte() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool(
        "conversation-a",
        "find_symbol",
        {"name_path_pattern": "Thing/run", "include_body": True},
    )
    canonical_result = json.dumps(
        {
            "truncated": True,
            "total_chars": 12_345,
            "output_id": "canonical-output",
            "result": {"name_path": "Thing/run", "body": "bounded preview"},
        },
        separators=(",", ":"),
    )
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result_serialization=canonical_result,
        retained_output_id="canonical-output",
        retained_output_chars=12_345,
    )

    assert call_id is not None
    detail = tracker.get_call_detail("conversation-a", run["run_id"], call_id)
    assert detail["result"] == canonical_result
    assert detail["structured_result"] == json.loads(canonical_result)


def test_activity_tracker_exposes_media_without_serialized_payload_text() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "render_pdf_page", {"relative_path": "figure.pdf", "page": 1})
    link = ResourceLink(
        type="resource_link",
        name="figure-p1.png",
        uri=AnyUrl("serena-file://export/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        mimeType="image/png",
        size=123,
    )

    tracker.finish_tool(
        call_id,
        succeeded=True,
        result_metadata=_ActivityHarness.extract_result_metadata(link),
    )

    assert call_id is not None
    detail = tracker.get_call_detail("conversation-a", run["run_id"], call_id)
    assert detail["result"] is None
    assert detail["media"] == {"type": "image", "name": "figure-p1.png", "mime_type": "image/png"}
    media = tracker.get_call_media("conversation-a", run["run_id"], call_id)
    assert media.uri == "serena-file://export/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_activity_tracker_exposes_media_from_prepared_mcp_result() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "fetch_media_file", {"relative_path": "figure.png"})
    link = ResourceLink(
        type="resource_link",
        name="figure.png",
        uri=AnyUrl("serena-file://export/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        mimeType="image/png",
        size=123,
    )

    logical_result = CallToolResult(content=[link])
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result_metadata=_ActivityHarness.extract_result_metadata(logical_result),
    )

    assert call_id is not None
    detail = tracker.get_call_detail("conversation-a", run["run_id"], call_id)
    assert detail["result"] is None
    assert detail["media"] == {"type": "image", "name": "figure.png", "mime_type": "image/png"}
    media = tracker.get_call_media("conversation-a", run["run_id"], call_id)
    assert media.uri == "serena-file://export/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_activity_tracker_marks_current_turn_job_and_exposes_other_running_jobs() -> None:
    source = _FakeJobSource([_job_record("other-job", "other optimisation")])
    tracker = _ActivityHarness(source)
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "start_job", {"label": "current optimisation"})

    source.records.append(_job_record("current-job", "current optimisation"))
    result = {"job_id": "current-job", "label": "current optimisation"}
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result_serialization=json.dumps(result, separators=(",", ":")),
        result_metadata=_ActivityHarness.extract_result_metadata(result),
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
    tracker = _ActivityHarness(source)
    run = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "start_job", {"label": "current optimisation"})
    result = {"job_id": "current-job", "label": "current optimisation"}
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result_serialization=json.dumps(result, separators=(",", ":")),
        result_metadata=_ActivityHarness.extract_result_metadata(result),
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
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("conversation-a", "serena")

    with pytest.raises(ValueError, match="not available"):
        tracker.get_run("conversation-b", run["run_id"])


def test_activity_tracker_supersedes_previous_panel_in_same_conversation() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
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
    tracker = _ActivityHarness(source)
    first = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "start_job", {"command": "sleep 5"})

    result = {"job_id": "first-job", "label": "first job"}
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result_serialization=json.dumps(result, separators=(",", ":")),
        result_metadata=_ActivityHarness.extract_result_metadata(result),
    )
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
    tracker = _ActivityHarness(source)
    first = tracker.start_run("conversation-a", "serena")
    call_id = tracker.start_tool("conversation-a", "start_job", {"command": "sleep 5"})

    second = tracker.start_run("conversation-a", "serena")
    result = {"job_id": "shared-job", "label": "shared job"}
    tracker.finish_tool(
        call_id,
        succeeded=True,
        result_serialization=json.dumps(result, separators=(",", ":")),
        result_metadata=_ActivityHarness.extract_result_metadata(result),
    )

    first_state = tracker.get_run("conversation-a", first["run_id"])
    second_state = tracker.get_run("conversation-a", second["run_id"])

    assert [(job["job_id"], job["current_turn"]) for job in first_state["jobs"]] == [("shared-job", True)]
    assert [(job["job_id"], job["current_turn"]) for job in second_state["jobs"]] == [("shared-job", True)]


def test_get_mcp_session_id_prefers_openai_conversation_metadata() -> None:
    meta = RequestParams.Meta.model_validate({"openai/session": "conversation-123"})
    context = SimpleNamespace(request_context=SimpleNamespace(meta=meta), session=object())

    assert get_mcp_session_id(context) == "conversation-123"


def test_mcp_tool_wrapper_records_activity() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("global", "serena")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_EchoCommandTool(), activity_run_manager=tracker.run_manager)

    result = asyncio.run(mcp_tool.run({"command": "git status"}))

    assert result == "git status"
    snapshot = tracker.get_run("global", run["run_id"])
    assert [(call["tool_name"], call["detail"], call["status"]) for call in snapshot["calls"]] == [
        ("echo_command", "git status", "completed")
    ]


def test_mcp_tool_wrapper_records_execution_without_activity_panel() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_EchoCommandTool(), activity_run_manager=tracker.run_manager)

    assert asyncio.run(mcp_tool.run({"command": "git status"})) == "git status"

    records = tracker.execution_store.list_executions()
    assert len(records) == 1
    assert records[0].session_id == "global"
    assert records[0].tool_name == "echo_command"
    assert records[0].arguments == {"command": "git status"}
    assert records[0].status == "completed"


def test_mcp_tool_wrapper_tracks_logical_result_when_transport_conversion_is_enabled() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("global", "serena")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_EchoCommandTool(), activity_run_manager=tracker.run_manager)

    asyncio.run(mcp_tool.run({"command": "git status"}, convert_result=True))

    call_id = tracker.get_run("global", run["run_id"])["calls"][0]["call_id"]
    detail = tracker.get_call_detail("global", run["run_id"], call_id)
    assert detail["result"] == '"git status"'
    assert json.loads(detail["result"]) == "git status"


def test_mcp_tool_wrapper_keeps_activity_polling_responsive_during_blocking_tool() -> None:
    tracker = _ActivityHarness(_FakeJobSource())
    run = tracker.start_run("global", "serena")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_SlowEchoCommandTool(), activity_run_manager=tracker.run_manager)

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
    tracker = _ActivityHarness(source)
    run = tracker.start_run("global", "serena")
    mcp_tool = SerenaMCPFactory.make_mcp_tool(_StartJobResultTool(), activity_run_manager=tracker.run_manager)

    asyncio.run(mcp_tool.run({"command": "sleep 1", "label": "wrapped label"}, convert_result=True))

    snapshot = tracker.get_run("global", run["run_id"])
    assert [(job["job_id"], job["current_turn"]) for job in snapshot["jobs"]] == [("wrapped-job", True)]


def test_mcp_start_job_metadata_is_extracted_before_central_presentation(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeJobSource([_job_record("wrapped-job", "wrapped label")])
    tracker = _ActivityHarness(source)
    run = tracker.start_run("global", "serena")
    tool = _StartJobResultTool()
    logical_result = {
        "prefix": "x" * 10_000,
        "job_id": "wrapped-job",
        "label": "wrapped label",
        "suffix": "y" * 10_000,
    }
    monkeypatch.setattr(tool, "apply", lambda command, label: logical_result)
    mcp_tool = SerenaMCPFactory.make_mcp_tool(tool, activity_run_manager=tracker.run_manager)

    asyncio.run(mcp_tool.run({"command": "sleep 1", "label": "wrapped label"}, convert_result=True))

    snapshot = tracker.get_run("global", run["run_id"])
    assert [(job["job_id"], job["current_turn"]) for job in snapshot["jobs"]] == [("wrapped-job", True)]
    execution = tracker.execution_store.list_executions(newest_first=True)[0]
    assert execution.durable_job_id == "wrapped-job"
    assert execution.durable_job_label == "wrapped label"
    assert execution.retained_output_id is not None


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
        "openai/widgetDescription": "Shows Serena tool calls, current-turn jobs, and a compact indicator for other running jobs.",
    }
    assert content.mime_type == "text/html;profile=mcp-app"
    assert content.content.startswith("<!doctype html>")
    assert '"get_activity"' in content.content
    assert '"get_activity_detail"' in content.content
    assert '"get_activity_media"' in content.content
    assert '"get_activity_job_detail"' in content.content


def test_activity_tools_expose_widget_and_private_polling_contract() -> None:
    class Agent:
        @staticmethod
        def get_active_project_for_session(session_id: str):
            return None

    async def inspect_tools() -> dict[str, Any]:
        factory = SerenaMCPFactory(transport="stdio")
        factory.agent = Agent()  # type: ignore[assignment]
        harness = _ActivityHarness(_FakeJobSource())
        factory._activity_run_manager = harness.run_manager
        factory._activity_view = harness.view
        mcp = FastMCP("activity-test")
        factory._register_activity_tools(mcp)
        return {tool.name: tool for tool in await mcp.list_tools()}

    tools = asyncio.run(inspect_tools())
    show_meta = tools["show_activity"].meta
    poll_meta = tools["get_activity"].meta
    detail_meta = tools["get_activity_detail"].meta
    media_meta = tools["get_activity_media"].meta
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
    assert media_meta is not None
    assert media_meta["ui"] == {"visibility": ["app"]}
    assert media_meta["openai/visibility"] == "private"
    assert job_detail_meta is not None
    assert job_detail_meta["ui"] == {"visibility": ["app"]}
    assert job_detail_meta["openai/visibility"] == "private"
