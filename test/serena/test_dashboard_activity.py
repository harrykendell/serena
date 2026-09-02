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
