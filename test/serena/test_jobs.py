import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.job_runner import run_job
from serena.jobs import (
    JobBackend,
    JobLimitError,
    JobManager,
    JobOutputChunk,
    JobPersistenceInfo,
    JobRecord,
    JobRuntimeInfo,
    JobStatus,
    JobStore,
)
from serena.tools.job_tools import CancelJobTool, JobStatusTool, StartJobTool


class FakeJobBackend(JobBackend):
    def __init__(self):
        self.running: set[str] = set()
        self.output: dict[str, list[str]] = {}

    def start(self, record: JobRecord, command_file: Path, state_file: Path) -> None:
        assert state_file.exists()
        assert command_file.exists()
        command_file.unlink()
        self.running.add(record.job_id)
        self.output[record.job_id] = []

    def is_running(self, record: JobRecord) -> bool:
        return record.job_id in self.running

    def cancel(self, record: JobRecord) -> None:
        self.running.discard(record.job_id)

    def read_output(
        self,
        record: JobRecord,
        cursor: str | None,
        max_chars: int,
        output_mode: str = "latest",
    ) -> JobOutputChunk:
        messages = self.output.get(record.job_id, [])
        if cursor == "stale":
            latest = self.read_output(record, None, max_chars, output_mode="latest")
            return JobOutputChunk(**{**latest.__dict__, "cursor_reset": True})
        if cursor is None and output_mode == "latest":
            selected: list[str] = []
            chars = 0
            for message in reversed(messages):
                prospective = chars + (1 if selected else 0) + len(message)
                if selected and prospective > max_chars:
                    break
                selected.append(message)
                chars = prospective
            selected.reverse()
            oldest_index = len(messages) - len(selected)
            return JobOutputChunk(
                output="\n".join(selected),
                next_cursor=str(len(messages)) if messages else None,
                has_more_output=False,
                oldest_cursor=str(oldest_index) if selected else None,
                has_earlier_output=oldest_index > 0,
                earlier_output_omitted=oldest_index > 0,
            )

        start = int(cursor) if cursor is not None else 0
        selected = []
        chars = 0
        for message in messages[start:]:
            prospective = chars + (1 if selected else 0) + len(message)
            if selected and prospective > max_chars:
                break
            selected.append(message)
            chars = prospective
        next_index = start + len(selected)
        return JobOutputChunk(
            output="\n".join(selected),
            next_cursor=str(next_index),
            has_more_output=next_index < len(messages),
            oldest_cursor=str(start) if selected else None,
        )

    def read_output_before(self, record: JobRecord, cursor: str, max_chars: int) -> JobOutputChunk:
        messages = self.output.get(record.job_id, [])
        boundary = int(cursor)
        selected: list[str] = []
        chars = 0
        for message in reversed(messages[:boundary]):
            prospective = chars + (1 if selected else 0) + len(message)
            if selected and prospective > max_chars:
                break
            selected.append(message)
            chars = prospective
        selected.reverse()
        start = boundary - len(selected)
        return JobOutputChunk(
            output="\n".join(selected),
            next_cursor=str(boundary - 1) if selected else None,
            has_more_output=False,
            oldest_cursor=str(start) if selected else None,
            has_earlier_output=start > 0,
            earlier_output_omitted=start > 0,
        )

    def runtime_info(self, record: JobRecord) -> JobRuntimeInfo:
        return JobRuntimeInfo(
            elapsed_seconds=12.5,
            seconds_since_last_output=1.5,
            memory_bytes=1024,
            cpu_seconds=2.25,
            process_count=3,
        )

    def persistence_info(self) -> JobPersistenceInfo:
        return JobPersistenceInfo(
            survives_serena_restart=True,
            survives_logout=False,
            survives_reboot=False,
            linger_enabled=False,
        )


class FailingJobBackend(FakeJobBackend):
    def start(self, record: JobRecord, command_file: Path, state_file: Path) -> None:
        raise RuntimeError("backend start failed")


def _manager(tmp_path: Path, backend: FakeJobBackend, max_jobs: int = 6, output_limit: int = 100) -> JobManager:
    return JobManager(
        store=JobStore(tmp_path / "jobs"),
        backend=backend,
        max_concurrent_jobs=max_jobs,
        output_char_limit=output_limit,
    )


def test_job_limit_is_six_and_cancelled_job_frees_capacity(tmp_path: Path) -> None:
    backend = FakeJobBackend()
    manager = _manager(tmp_path, backend)

    records = [manager.start_job(f"echo {index}", str(tmp_path), label=f"job {index}")[0] for index in range(6)]

    with pytest.raises(JobLimitError, match="limit of 6"):
        manager.start_job("echo seventh", str(tmp_path), label="seventh")

    cancelled = manager.cancel_job(records[0].job_id)
    replacement, running_jobs = manager.start_job("echo replacement", str(tmp_path), label="replacement")

    assert cancelled.status is JobStatus.CANCELLED
    assert replacement.status is JobStatus.RUNNING
    assert running_jobs == 6


def test_start_requires_distinctive_nonempty_label(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeJobBackend())

    with pytest.raises(ValueError, match="label must not be empty"):
        manager.start_job("echo hello", str(tmp_path), label="  ")


def test_failed_start_becomes_terminal_and_removes_private_command(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    manager = JobManager(store=JobStore(jobs_dir), backend=FailingJobBackend())

    with pytest.raises(RuntimeError, match="backend start failed"):
        manager.start_job("echo secret", str(tmp_path), label="failing start")

    records = JobStore(jobs_dir).list_records()
    assert len(records) == 1
    assert records[0].status is JobStatus.FAILED
    assert list(jobs_dir.glob(".*.command")) == []


def test_manager_removes_orphaned_command_for_terminal_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    job_id = "0123456789abcdef0123456789abcdef"
    store.create(
        JobRecord(
            job_id=job_id,
            unit_name=f"serena-job-{job_id}.service",
            project_root=str(tmp_path),
            cwd=str(tmp_path),
            status=JobStatus.FAILED,
            created_at="2026-08-28T18:00:00+00:00",
            label="old failed job",
        )
    )
    command_file = store.create_command_file(job_id, "secret")

    JobManager(store=store, backend=FakeJobBackend())

    assert not command_file.exists()


def test_job_state_is_recoverable_with_latest_start_and_incremental_output(tmp_path: Path) -> None:
    backend = FakeJobBackend()
    first_manager = _manager(tmp_path, backend)
    record, _ = first_manager.start_job("echo hello", str(tmp_path), label="test build", project_name="demo")
    backend.output[record.job_id].extend(["line one", "line two", "line three"])

    second_manager = _manager(tmp_path, backend, output_limit=14)
    latest = second_manager.get_job(record.job_id)
    from_start = second_manager.get_job(record.job_id, output_mode="start")
    assert latest.output is not None
    assert from_start.output is not None
    backend.output[record.job_id].append("line four")
    incremental = second_manager.get_job(record.job_id, latest.output.next_cursor)

    assert latest.record.label == "test build"
    assert latest.record.project_name == "demo"
    assert latest.output.output == "line three"
    assert latest.output.earlier_output_omitted is True
    assert from_start.output.output == "line one"
    assert from_start.output.has_more_output is True
    assert incremental.output is not None
    assert incremental.output.output == "line four"


def test_job_output_can_page_backward_from_latest_tail(tmp_path: Path) -> None:
    backend = FakeJobBackend()
    manager = _manager(tmp_path, backend, output_limit=14)
    record, _ = manager.start_job("echo hello", str(tmp_path), label="history test")
    backend.output[record.job_id].extend(["line one", "line two", "line three", "line four"])

    latest = manager.get_job(record.job_id)
    assert latest.output is not None
    assert latest.output.output == "line four"
    assert latest.output.oldest_cursor == "3"
    assert latest.output.has_earlier_output is True

    previous = manager.get_job_output_before(record.job_id, latest.output.oldest_cursor)
    assert previous.output is not None
    assert previous.output.output == "line three"
    assert previous.output.oldest_cursor == "2"
    assert previous.output.has_earlier_output is True


def test_stale_cursor_recovers_to_latest_output(tmp_path: Path) -> None:
    backend = FakeJobBackend()
    manager = _manager(tmp_path, backend)
    record, _ = manager.start_job("echo hello", str(tmp_path), label="cursor recovery")
    backend.output[record.job_id].append("latest")

    snapshot = manager.get_job(record.job_id, cursor="stale")

    assert snapshot.output is not None
    assert snapshot.output.output == "latest"
    assert snapshot.output.cursor_reset is True


def test_job_listing_always_keeps_running_jobs_visible(tmp_path: Path) -> None:
    backend = FakeJobBackend()
    manager = _manager(tmp_path, backend)
    running, _ = manager.start_job("echo running", str(tmp_path), label="long optimisation")

    for index in range(25):
        record, _ = manager.start_job(f"echo {index}", str(tmp_path), label=f"short {index}")
        manager.cancel_job(record.job_id)

    listed = manager.list_jobs(limit=20)

    assert listed[0].job_id == running.job_id
    assert len(listed) == 20


def test_terminal_job_listing_is_ordered_by_finish_time(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    older_start = "0123456789abcdef0123456789abcdef"
    newer_start = "fedcba9876543210fedcba9876543210"
    store.create(
        JobRecord(
            job_id=older_start,
            unit_name=f"serena-job-{older_start}.service",
            project_root=str(tmp_path),
            cwd=str(tmp_path),
            status=JobStatus.COMPLETED,
            created_at="2026-08-28T17:00:00+00:00",
            finished_at="2026-08-28T19:00:00+00:00",
            return_code=0,
            label="finished later",
        )
    )
    store.create(
        JobRecord(
            job_id=newer_start,
            unit_name=f"serena-job-{newer_start}.service",
            project_root=str(tmp_path),
            cwd=str(tmp_path),
            status=JobStatus.COMPLETED,
            created_at="2026-08-28T18:00:00+00:00",
            finished_at="2026-08-28T18:30:00+00:00",
            return_code=0,
            label="finished earlier",
        )
    )
    manager = JobManager(store=store, backend=FakeJobBackend())

    listed = manager.list_jobs(limit=20)

    assert [record.label for record in listed] == ["finished later", "finished earlier"]


def test_timeout_is_reported_as_distinct_terminal_state(tmp_path: Path) -> None:
    backend = FakeJobBackend()
    manager = _manager(tmp_path, backend)
    record, _ = manager.start_job("sleep 10", str(tmp_path), label="bounded run", timeout_seconds=1)
    time.sleep(1.05)
    backend.running.discard(record.job_id)

    snapshot = manager.get_job(record.job_id)

    assert snapshot.record.status is JobStatus.TIMED_OUT
    assert "runtime limit" in (snapshot.record.status_message or "")


def test_start_job_rejects_working_directory_outside_project(tmp_path: Path) -> None:
    backend = FakeJobBackend()
    manager = _manager(tmp_path, backend)
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()

    with pytest.raises(ValueError, match="within the active project"):
        manager.start_job("echo nope", str(project), label="invalid cwd", cwd=str(outside))


def test_job_tools_return_chat_friendly_telemetry_and_persistence(tmp_path: Path) -> None:
    backend = FakeJobBackend()
    manager = _manager(tmp_path, backend)
    project = MagicMock(project_root=str(tmp_path), project_name="demo")
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project

    start_tool = StartJobTool(agent)
    status_tool = JobStatusTool(agent)
    cancel_tool = CancelJobTool(agent)
    start_tool._job_manager = manager
    status_tool._job_manager = manager
    cancel_tool._job_manager = manager

    started = json.loads(start_tool.apply("echo hello", label="demo test", timeout_seconds=60))
    backend.output[started["job_id"]].append("hello")
    status = json.loads(status_tool.apply(started["job_id"]))
    listed = json.loads(status_tool.apply())
    cancelled = json.loads(cancel_tool.apply(started["job_id"]))

    assert started["max_concurrent_jobs"] == 6
    assert started["running_jobs"] == 1
    assert started["timeout_seconds"] == 60
    assert started["persistence"]["survives_serena_restart"] is True
    assert started["persistence"]["survives_logout"] is False
    assert "Continue other useful work" in started["next_step"]
    assert status["output"] == "hello"
    assert status["next_cursor"] == "1"
    assert status["runtime"]["memory_bytes"] == 1024
    assert status["runtime"]["process_count"] == 3
    assert "poll later" in status["next_step"]
    assert listed["jobs"][0]["label"] == "demo test"
    assert listed["jobs"][0]["runtime"]["cpu_seconds"] == 2.25
    assert cancelled["status"] == "cancelled"


def test_runner_records_terminal_exit_status_and_consumes_command(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    job_id = "0123456789abcdef0123456789abcdef"
    record = JobRecord(
        job_id=job_id,
        unit_name=f"serena-job-{job_id}.service",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        status=JobStatus.RUNNING,
        created_at="2026-08-28T18:00:00+00:00",
        label="runner test",
    )
    store.create(record)
    command_file = store.create_command_file(job_id, "exit 3")

    return_code = run_job(store.state_file(job_id), command_file)
    finished = store.read(job_id)

    assert return_code == 3
    assert finished.status is JobStatus.FAILED
    assert finished.return_code == 3
    assert finished.finished_at is not None
    assert not command_file.exists()
