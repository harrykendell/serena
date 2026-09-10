import json
import time
from datetime import timedelta
from pathlib import Path

import pytest

from serena.dashboard_activity import DashboardActivityArchive
from serena.execution_store import ExecutionStore
from serena.retention import JobRetentionState, SessionRetentionPolicy


def test_dashboard_activity_archive_survives_restart_and_pins_file_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    now = time.time()
    token = "a" * 48
    store = ExecutionStore()
    store.start_execution(
        execution_id="execution-a",
        session_id="chat-a",
        project_name="project-a",
        tool_name="fetch_media_file",
        arguments='{"relative_path": "figure.png"}',
        started_at=now,
    )
    store.finish_execution(
        "execution-a",
        succeeded=True,
        media={
            "type": "image",
            "name": "figure.png",
            "mime_type": "image/png",
            "uri": f"serena-file://export/{token}",
        },
        finished_at=now + 1,
    )

    restored = DashboardActivityArchive(ExecutionStore())
    sessions = restored.list_sessions()

    assert len(sessions) == 1
    assert sessions[0]["panel_id"] == DashboardActivityArchive.panel_id_for_session("chat-a")
    assert sessions[0]["calls"][0]["call_id"] == "execution-a"
    assert sessions[0]["calls"][0]["status"] == "completed"
    assert sessions[0]["calls"][0]["media"] == {
        "type": "image",
        "name": "figure.png",
        "mime_type": "image/png",
        "uri": f"serena-file://export/{token}",
    }
    assert restored.retained_file_tokens() == {token}
    assert DashboardActivityArchive.retained_file_tokens_from_disk() == {token}


def test_dashboard_activity_archive_persists_operator_conversation_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    archive = DashboardActivityArchive(ExecutionStore())

    assert archive.set_display_name("chat-a", "  Loading scan analysis  ") == "Loading scan analysis"
    restored = DashboardActivityArchive(ExecutionStore())

    assert restored.list_sessions()[0]["session_id"] == "chat-a"
    assert restored.list_sessions()[0]["display_name"] == "Loading scan analysis"


def test_dashboard_activity_archive_marks_interrupted_calls_terminal_on_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    store = ExecutionStore()
    store.start_execution(
        execution_id="execution-a",
        session_id="chat-a",
        project_name="project-a",
        tool_name="read_file",
        arguments='{"relative_path": "notes.txt"}',
    )

    restored = DashboardActivityArchive(ExecutionStore())
    call = restored.list_sessions()[0]["calls"][0]

    assert call["status"] == "failed"
    assert call["finished_at"] is not None
    assert "restarted" in call["error"]


def test_execution_retention_evicts_oldest_session_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("serena.execution_store.time.time", lambda: now)
    store = ExecutionStore(
        tmp_path / "execution-store",
        retention=SessionRetentionPolicy(max_age=timedelta(seconds=10), max_artifact_bytes=1024 * 1024),
    )

    store.start_execution(
        execution_id="a-1",
        session_id="chat-a",
        project_name="project-a",
        tool_name="read_file",
        arguments="{}",
    )
    run = store.start_activity_run("chat-a", "project-a")
    store.append_execution_to_current_run("chat-a", "a-1")
    store.finish_execution("a-1", succeeded=True, result="a-1")
    store.start_execution(
        execution_id="a-2",
        session_id="chat-a",
        project_name="project-a",
        tool_name="read_file",
        arguments="{}",
    )
    store.append_execution_to_current_run("chat-a", "a-2")
    store.finish_execution("a-2", succeeded=True, result="a-2")

    now = 1_011.0
    store.start_execution(
        execution_id="b-1",
        session_id="chat-b",
        project_name="project-b",
        tool_name="read_file",
        arguments="{}",
    )

    assert [session.session_id for session in store.list_sessions()] == ["chat-b"]
    assert [record.execution_id for record in store.list_executions(newest_first=False)] == ["b-1"]
    assert store.get_activity_run(run.run_id) is None


def test_running_job_protects_session_and_completion_extends_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("serena.execution_store.time.time", lambda: now)
    store = ExecutionStore(
        tmp_path / "execution-store",
        retention=SessionRetentionPolicy(max_age=timedelta(seconds=10), max_artifact_bytes=1024 * 1024),
    )
    job_id = "1" * 32

    store.start_execution(
        execution_id="a-1",
        session_id="chat-a",
        project_name="project-a",
        tool_name="start_job",
        arguments="{}",
    )
    store.finish_execution("a-1", succeeded=True, result="{}", durable_job_id=job_id)
    store.sync_job_retention([JobRetentionState(job_id=job_id, session_id="chat-a", is_running=True, finished_at=None)])

    now = 1_020.0
    store.start_execution(
        execution_id="b-1",
        session_id="chat-b",
        project_name="project-b",
        tool_name="read_file",
        arguments="{}",
    )
    store.finish_execution("b-1", succeeded=True, result="b-1")
    assert {session.session_id for session in store.list_sessions()} == {"chat-a", "chat-b"}

    store.sync_job_retention([JobRetentionState(job_id=job_id, session_id="chat-a", is_running=False, finished_at=now)])
    assert {session.session_id for session in store.list_sessions()} == {"chat-a", "chat-b"}

    now = 1_025.0
    store.start_execution(
        execution_id="b-2",
        session_id="chat-b",
        project_name="project-b",
        tool_name="read_file",
        arguments="{}",
    )
    store.finish_execution("b-2", succeeded=True, result="b-2")

    now = 1_031.0
    store.sync_job_retention([JobRetentionState(job_id=job_id, session_id="chat-a", is_running=False, finished_at=1_020.0)])
    assert [session.session_id for session in store.list_sessions()] == ["chat-b"]


def test_execution_store_rejects_pre_v2_state(tmp_path: Path) -> None:
    root = tmp_path / "execution-store"
    root.mkdir()
    (root / "state.json").write_text(
        json.dumps({"version": 1, "sessions": {}, "executions": {}, "activity_runs": {}}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="requires schema version 2"):
        ExecutionStore(root)
