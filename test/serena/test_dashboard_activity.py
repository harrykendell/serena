import json
from pathlib import Path

import pytest

from serena.dashboard_activity import DashboardActivityArchive
from serena.execution_store import ExecutionStore


def test_dashboard_activity_archive_survives_restart_and_pins_file_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    token = "a" * 48
    store = ExecutionStore()
    store.start_execution(
        execution_id="execution-a",
        session_id="chat-a",
        project_name="project-a",
        tool_name="fetch_media_file",
        arguments='{"relative_path": "figure.png"}',
        started_at=100.0,
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
        finished_at=101.0,
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


def test_dashboard_activity_archive_migrates_legacy_histories_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    serena_home = tmp_path / "serena-home"
    monkeypatch.setenv("SERENA_HOME", str(serena_home))
    dashboard_root = serena_home / "dashboard_activity_sessions"
    activity_root = serena_home / "activity_runs"
    dashboard_root.mkdir(parents=True)
    activity_root.mkdir(parents=True)
    panel_id = DashboardActivityArchive.panel_id_for_session("chat-a")
    (dashboard_root / f"{panel_id}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "panel_id": panel_id,
                "session_id": "chat-a",
                "project_name": "project-a",
                "display_name": "Legacy chat",
                "started_at": 100.0,
                "updated_at": 101.0,
                "calls": [
                    {
                        "call_id": "legacy-call",
                        "tool_name": "read_file",
                        "parameters": "relative_path='old.txt'",
                        "status": "completed",
                        "submitted_at": 100.0,
                        "started_at": 100.0,
                        "finished_at": 101.0,
                        "result": "old result",
                        "project_name": "project-a",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (activity_root / "legacy-run.json").write_text(
        json.dumps(
            {
                "run_id": "legacy-run",
                "session_id": "chat-a",
                "project_name": "project-a",
                "started_at": 100.0,
                "superseded": True,
                "calls": [
                    {
                        "call_id": "legacy-call",
                        "tool_name": "read_file",
                        "arguments": '{"relative_path": "old.txt"}',
                        "started_at": 100.0,
                        "finished_at": 101.0,
                        "status": "completed",
                        "result": "old result",
                        "project_name": "project-a",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = ExecutionStore()
    archive = DashboardActivityArchive(store)
    restored = ExecutionStore()

    assert archive.list_sessions()[0]["display_name"] == "Legacy chat"
    assert archive.list_sessions()[0]["calls"][0]["call_id"] == "legacy-call"
    assert store.get_activity_run("legacy-run") is not None
    assert len(restored.list_executions()) == 1
    assert not list(dashboard_root.glob("*.json"))
    assert not list(activity_root.glob("*.json"))
    assert (serena_home / "execution_store" / "state.json").is_file()
