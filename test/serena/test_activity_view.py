import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from serena.activity_view import ActivityDetailFormatter, ActivityView
from serena.execution_store import ExecutionStore
from serena.git_metrics import GitLineMetrics
from serena.jobs import JobRecord, JobStatus


class _OverviewJobSource:
    """Counts the lightweight job query while rejecting detail/terminal-history access."""

    max_concurrent_jobs = 12

    def __init__(self, running_records: list[JobRecord]) -> None:
        self.running_records = running_records
        self.running_queries = 0

    def list_running_jobs(self) -> list[JobRecord]:
        self.running_queries += 1
        return self.running_records

    def get_job_record(self, job_id: str) -> Any:
        raise AssertionError(f"overview unexpectedly read individual job metadata for {job_id}")

    def get_job_records(self, job_ids: set[str]) -> list[Any]:
        raise AssertionError(f"overview unexpectedly expanded terminal jobs: {job_ids}")

    def get_job(self, job_id: str) -> Any:
        raise AssertionError(f"overview unexpectedly collected job telemetry for {job_id}")


class _CountingGitMetricsSource:
    """Counts cached Git reads and rejects refresh/subprocess-triggering access."""

    def __init__(self) -> None:
        self.cached_reads: list[str] = []

    def get_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        self.cached_reads.append(project_name)
        return GitLineMetrics(additions=3, deletions=1, ahead_commits=2)

    def refresh_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        raise AssertionError(f"overview unexpectedly refreshed Git metrics for {project_name}")


def test_dashboard_overview_scales_from_lightweight_indexes_without_history_expansion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "execution-store"
    store = ExecutionStore(root)
    base = time.time() - 2_000.0
    with store.batch_updates():
        for index in range(1_000):
            execution_id = f"execution-{index}"
            session_id = f"session-{index}"
            store.start_execution(
                execution_id=execution_id,
                session_id=session_id,
                project_name="serena",
                tool_name="read_file",
                arguments={"relative_path": f"file-{index}.txt"},
            )
            store.set_session_display_name(session_id, f"Session {index}")
            store.finish_execution(
                execution_id,
                succeeded=True,
                result="historical-result:" + ("x" * 2_000),
            )

    original_get_execution = store.get_execution
    original_parse_result = ActivityDetailFormatter.parse_result

    def reject_session_expansion(session_id: str):
        raise AssertionError(f"overview unexpectedly expanded session {session_id}")

    def reject_full_execution(execution_id: str):
        raise AssertionError(f"overview unexpectedly copied full execution {execution_id}")

    def reject_result_decode(self, result: str | None):
        raise AssertionError("overview unexpectedly decoded a historical result")

    monkeypatch.setattr(store, "list_session_executions", reject_session_expansion)
    monkeypatch.setattr(store, "get_execution", reject_full_execution)
    monkeypatch.setattr(ActivityDetailFormatter, "parse_result", reject_result_decode)
    active_job = JobRecord(
        job_id="active-job",
        unit_name="serena-job-active-job.service",
        project_root="/tmp/serena",
        cwd="/tmp/serena",
        status=JobStatus.RUNNING,
        created_at=datetime.fromtimestamp(base + 17, UTC).isoformat(),
        session_id="session-17",
        project_name="serena",
        label="Active job",
    )
    jobs = _OverviewJobSource([active_job])
    git = _CountingGitMetricsSource()
    view = ActivityView(store, jobs, git)

    overview = view.dashboard_overview()

    assert len(overview.sessions) == 1_000
    assert overview.sessions[0].session_id == "session-17"
    assert overview.sessions[0].active is True
    assert all(not session.active for session in overview.sessions[1:])
    assert overview.sessions[1].session_id == "session-999"
    assert jobs.running_queries == 1
    assert git.cached_reads == []

    selected = view.for_session(ExecutionStore.panel_id_for_session("session-999"))
    assert len(selected.calls) == 1
    assert selected.expanded_call is None

    monkeypatch.setattr(store, "get_execution", original_get_execution)
    monkeypatch.setattr(ActivityDetailFormatter, "parse_result", staticmethod(original_parse_result))
    detailed = view.for_session(
        ExecutionStore.panel_id_for_session("session-999"),
        expanded_entry_id="execution-999",
    )
    assert detailed.expanded_call is not None
    assert detailed.expanded_call.result is not None
    assert detailed.expanded_call.result.startswith("historical-result:")


def test_retained_sessions_keep_session_owned_git_metrics(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "execution-store")
    for session_id in ("session-a", "session-b"):
        execution_id = f"execution-{session_id}"
        store.start_execution(
            execution_id=execution_id,
            session_id=session_id,
            project_name="serena",
            tool_name="read_file",
            arguments={"relative_path": f"{session_id}.txt"},
        )
        store.finish_execution(execution_id, succeeded=True, result="ok")

    metrics_a = GitLineMetrics(additions=7, deletions=2, ahead_commits=1)
    metrics_b = GitLineMetrics(additions=11, deletions=5, ahead_commits=3)
    store.update_session_git_metrics("session-a", metrics_a)
    store.update_session_git_metrics("session-b", metrics_b)
    git = _CountingGitMetricsSource()
    view = ActivityView(store, _OverviewJobSource([]), git)

    first = {session.session_id: session for session in view.dashboard_overview().sessions}
    assert first["session-a"].git_metrics == metrics_a
    assert first["session-b"].git_metrics == metrics_b

    updated_b = GitLineMetrics(additions=19, deletions=8, ahead_commits=4)
    store.update_session_git_metrics("session-b", updated_b)
    second = {session.session_id: session for session in view.dashboard_overview().sessions}

    assert second["session-a"].git_metrics == metrics_a
    assert second["session-b"].git_metrics == updated_b
    assert view.for_session(ExecutionStore.panel_id_for_session("session-a")).git_metrics == metrics_a
    assert git.cached_reads == []
