from pathlib import Path

import pytest

from serena.dashboard_activity import DashboardActivityArchive


def test_dashboard_activity_archive_survives_restart_and_pins_file_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    token = "a" * 48
    archive = DashboardActivityArchive()
    call_id = archive.record_start(
        task_name="Task-1:FetchMediaFileTool",
        session_id="chat-a",
        tool_name="fetch_media_file",
        parameters="relative_path='figure.png'",
        detail="figure.png",
        project_name="project-a",
        timestamp=100.0,
    )
    archive.record_result("Task-1:FetchMediaFileTool", result=f"serena-file://export/{token}")

    restored = DashboardActivityArchive()
    sessions = restored.list_sessions()

    assert len(sessions) == 1
    assert sessions[0]["panel_id"] == DashboardActivityArchive.panel_id_for_session("chat-a")
    assert sessions[0]["calls"][0]["call_id"] == call_id
    assert sessions[0]["calls"][0]["status"] == "completed"
    assert restored.retained_file_tokens() == {token}
    assert DashboardActivityArchive.retained_file_tokens_from_disk() == {token}


def test_dashboard_activity_archive_persists_operator_conversation_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    archive = DashboardActivityArchive()

    assert archive.set_display_name("chat-a", "  Loading scan analysis  ") == "Loading scan analysis"
    restored = DashboardActivityArchive()

    assert restored.list_sessions()[0]["session_id"] == "chat-a"
    assert restored.list_sessions()[0]["display_name"] == "Loading scan analysis"


def test_dashboard_activity_archive_marks_interrupted_calls_terminal_on_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    archive = DashboardActivityArchive()
    archive.record_start(
        task_name="Task-1:ReadFileTool",
        session_id="chat-a",
        tool_name="read_file",
        parameters="relative_path='notes.txt'",
        detail="notes.txt",
        project_name="project-a",
    )

    restored = DashboardActivityArchive()
    call = restored.list_sessions()[0]["calls"][0]

    assert call["status"] == "failed"
    assert call["finished_at"] is not None
    assert "restarted" in call["error"]


def test_dashboard_activity_archive_does_not_reconcile_reused_task_names_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    first = DashboardActivityArchive()
    first.record_start(
        task_name="Task-1:ReadFileTool",
        session_id="chat-a",
        tool_name="read_file",
        parameters="relative_path='old.txt'",
        detail="old.txt",
        project_name="project-a",
        timestamp=100.0,
    )
    first.record_result("Task-1:ReadFileTool", result="old result")

    restored = DashboardActivityArchive()
    restored.record_start(
        task_name="Task-1:ReadFileTool",
        session_id="chat-a",
        tool_name="read_file",
        parameters="relative_path='new.txt'",
        detail="new.txt",
        project_name="project-a",
        timestamp=200.0,
    )
    restored.reconcile_executions(
        [
            {
                "name": "Task-1:ReadFileTool",
                "submitted_at": 201.0,
                "started_at": 202.0,
                "finished_at": 203.0,
                "project": "project-a",
                "status": "completed",
            }
        ]
    )

    calls = restored.list_sessions()[0]["calls"]
    assert calls[0]["submitted_at"] == 100.0
    assert calls[0]["started_at"] == 100.0
    assert calls[0]["finished_at"] != 203.0
    assert calls[1]["submitted_at"] == 201.0
    assert calls[1]["started_at"] == 202.0
    assert calls[1]["finished_at"] == 203.0
