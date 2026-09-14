import sqlite3
import time
from datetime import timedelta
from pathlib import Path

import pytest

from serena.execution_store import ExecutionStore
from serena.git_metrics import GitLineMetrics
from serena.retention import JobRetentionState, SessionRetentionPolicy


def test_execution_store_survives_restart_and_pins_file_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    now = time.time()
    token = "a" * 64
    store = ExecutionStore()
    store.start_execution(
        execution_id="execution-a",
        session_id="chat-a",
        project_name="project-a",
        tool_name="fetch_media_file",
        arguments={"relative_path": "figure.png"},
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

    restored = ExecutionStore()
    sessions = restored.list_sessions()
    calls = restored.list_session_executions("chat-a")

    assert len(sessions) == 1
    assert sessions[0].panel_id == ExecutionStore.panel_id_for_session("chat-a")
    assert calls[0].execution_id == "execution-a"
    assert calls[0].status == "completed"
    assert calls[0].media == {
        "type": "image",
        "name": "figure.png",
        "mime_type": "image/png",
        "uri": f"serena-file://export/{token}",
    }
    assert restored.retained_file_tokens() == {token}
    assert ExecutionStore.retained_file_tokens_from_disk() == {token}


def test_execution_store_tracks_queue_and_running_timestamps(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "execution-store")
    submitted_at = time.time()
    running_at = submitted_at + 3.0
    finished_at = running_at + 2.5

    submitted = store.start_execution(
        execution_id="execution-a",
        session_id="chat-a",
        project_name="project-a",
        tool_name="read_file",
        arguments={"relative_path": "notes.txt"},
        started_at=submitted_at,
    )
    assert submitted.status == "queued"
    assert submitted.running_at is None

    assert store.mark_execution_running("execution-a", running_at=running_at)
    running = store.get_execution("execution-a")
    assert running is not None
    assert running.status == "running"
    assert running.started_at == submitted_at
    assert running.running_at == running_at

    store.finish_execution("execution-a", succeeded=True, result="done", finished_at=finished_at)
    completed = store.get_execution("execution-a")
    assert completed is not None
    assert completed.status == "completed"
    assert completed.running_at == running_at
    assert completed.finished_at == finished_at


def test_execution_store_queue_timeout_prevents_late_start(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "execution-store")
    store.start_execution(
        execution_id="execution-a",
        session_id="chat-a",
        project_name="project-a",
        tool_name="create_text_file",
        arguments={"relative_path": "notes.txt"},
        started_at=100.0,
    )

    message = "Tool request timed out while queued."
    assert store.finish_queued_execution(
        "execution-a",
        status="timed_out",
        error=message,
        finished_at=102.0,
    )
    assert not store.mark_execution_running("execution-a", running_at=103.0)

    timed_out = store.get_execution("execution-a")
    assert timed_out is not None
    assert timed_out.status == "timed_out"
    assert timed_out.running_at is None
    assert timed_out.finished_at == 102.0
    assert timed_out.request_finished_at == 102.0
    assert timed_out.error == message
    assert timed_out.request_error == message


def test_execution_store_upgrades_session_git_snapshot_columns(tmp_path: Path) -> None:
    root = tmp_path / "execution-store"
    root.mkdir()
    database = sqlite3.connect(root / "state.sqlite3")
    database.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            panel_id TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            project_name TEXT NOT NULL DEFAULT ''
        )
        """
    )
    database.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        ("chat-a", ExecutionStore.panel_id_for_session("chat-a"), 1.0, 1.0, "Chat A", "serena"),
    )
    database.execute("PRAGMA user_version=1")
    database.commit()
    database.close()

    store = ExecutionStore(root)
    session = store.get_session_by_panel_id(ExecutionStore.panel_id_for_session("chat-a"))
    assert session is not None
    assert session.git_metrics == GitLineMetrics()

    expected = GitLineMetrics(additions=9, deletions=2, ahead_commits=4)
    store.update_session_git_metrics("chat-a", expected)
    restarted = ExecutionStore(root)
    migrated = restarted.get_session_by_panel_id(ExecutionStore.panel_id_for_session("chat-a"))
    assert migrated is not None
    assert migrated.git_metrics == expected


def test_execution_store_upgrades_execution_running_timestamp_column(tmp_path: Path) -> None:
    root = tmp_path / "execution-store"
    root.mkdir()
    database = sqlite3.connect(root / "state.sqlite3")
    database.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            panel_id TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            project_name TEXT NOT NULL DEFAULT '',
            git_additions INTEGER NOT NULL DEFAULT 0,
            git_deletions INTEGER NOT NULL DEFAULT 0,
            git_ahead_commits INTEGER
        )
        """
    )
    database.execute(
        """
        CREATE TABLE executions (
            execution_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            started_at REAL NOT NULL,
            status TEXT NOT NULL,
            finished_at REAL,
            request_finished_at REAL,
            request_error TEXT,
            result TEXT,
            error TEXT,
            retained_output_id TEXT,
            retained_output_chars INTEGER,
            media_json TEXT,
            durable_job_id TEXT,
            durable_job_label TEXT
        )
        """
    )
    database.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("chat-a", ExecutionStore.panel_id_for_session("chat-a"), 1.0, 1.0, "Chat A", "serena", 0, 0, None),
    )
    database.execute(
        """
        INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("execution-a", "chat-a", "serena", "read_file", "{}", 1.0, "completed", 2.0, 2.0, None, "ok", None, None, None, None, None, None),
    )
    database.execute("PRAGMA user_version=2")
    database.commit()
    database.close()

    store = ExecutionStore(root)
    restored = store.get_execution("execution-a")
    assert restored is not None
    assert restored.running_at is None

    with sqlite3.connect(root / "state.sqlite3") as upgraded:
        columns = {row[1] for row in upgraded.execute("PRAGMA table_info(executions)").fetchall()}
        version = upgraded.execute("PRAGMA user_version").fetchone()[0]
    assert "running_at" in columns
    assert version == 3


def test_execution_store_persists_operator_conversation_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    store = ExecutionStore()

    assert store.set_session_display_name("chat-a", "  Loading scan analysis  ") == "Loading scan analysis"
    restored = ExecutionStore()

    assert restored.list_sessions()[0].session_id == "chat-a"
    assert restored.list_sessions()[0].display_name == "Loading scan analysis"


def test_execution_store_marks_interrupted_calls_terminal_on_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "serena-home"))
    store = ExecutionStore()
    store.start_execution(
        execution_id="execution-a",
        session_id="chat-a",
        project_name="project-a",
        tool_name="read_file",
        arguments={"relative_path": "notes.txt"},
    )

    restored = ExecutionStore()
    call = restored.list_session_executions("chat-a")[0]

    assert call.status == "failed"
    assert call.finished_at is not None
    assert call.error is not None
    assert "restarted" in call.error


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
        arguments={},
    )
    run = store.start_activity_run("chat-a", "project-a")
    store.append_execution_to_current_run("chat-a", "a-1")
    store.finish_execution("a-1", succeeded=True, result="a-1")
    store.start_execution(
        execution_id="a-2",
        session_id="chat-a",
        project_name="project-a",
        tool_name="read_file",
        arguments={},
    )
    store.append_execution_to_current_run("chat-a", "a-2")
    store.finish_execution("a-2", succeeded=True, result="a-2")

    now = 1_011.0
    store.start_execution(
        execution_id="b-1",
        session_id="chat-b",
        project_name="project-b",
        tool_name="read_file",
        arguments={},
    )

    assert [session.session_id for session in store.list_sessions()] == ["chat-b"]
    assert [record.execution_id for record in store.list_executions(newest_first=False)] == ["b-1"]
    assert store.get_activity_run(run.run_id) is None


def test_shared_snapshot_survives_until_last_referencing_session_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    serena_home = tmp_path / "serena-home"
    monkeypatch.setenv("SERENA_HOME", str(serena_home))
    now = 1_000.0
    monkeypatch.setattr("serena.execution_store.time.time", lambda: now)
    store = ExecutionStore(retention=SessionRetentionPolicy(max_age=timedelta(seconds=10), max_artifact_bytes=20))
    token = "c" * 64
    snapshot_root = serena_home / "chat_file_snapshots"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_root / token
    snapshot_path.write_bytes(b"shared snapshot")

    store.start_execution(
        execution_id="a-1",
        session_id="chat-a",
        project_name="project-a",
        tool_name="fetch_media_file",
        arguments={},
    )
    store.finish_execution(
        "a-1",
        succeeded=True,
        media={"type": "file", "name": "shared.bin", "mime_type": "application/octet-stream", "uri": f"serena-file://export/{token}"},
    )

    now = 1_005.0
    store.start_execution(
        execution_id="b-1",
        session_id="chat-b",
        project_name="project-b",
        tool_name="fetch_media_file",
        arguments={},
    )
    store.finish_execution(
        "b-1",
        succeeded=True,
        media={"type": "file", "name": "shared.bin", "mime_type": "application/octet-stream", "uri": f"serena-file://export/{token}"},
    )
    assert {session.session_id for session in store.list_sessions()} == {"chat-a", "chat-b"}

    now = 1_011.0
    assert {session.session_id for session in store.list_sessions()} == {"chat-a", "chat-b"}
    assert store.maintain_retention() is True
    assert [session.session_id for session in store.list_sessions()] == ["chat-b"]
    assert snapshot_path.is_file()

    now = 1_016.0
    assert [session.session_id for session in store.list_sessions()] == ["chat-b"]
    assert store.maintain_retention() is True
    assert store.list_sessions() == []
    assert not snapshot_path.exists()


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
        arguments={},
    )
    store.finish_execution("a-1", succeeded=True, result="{}", durable_job_id=job_id)
    store.sync_job_retention([JobRetentionState(job_id=job_id, session_id="chat-a", is_running=True, finished_at=None)])

    now = 1_020.0
    store.start_execution(
        execution_id="b-1",
        session_id="chat-b",
        project_name="project-b",
        tool_name="read_file",
        arguments={},
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
        arguments={},
    )
    store.finish_execution("b-2", succeeded=True, result="b-2")

    now = 1_031.0
    store.sync_job_retention([JobRetentionState(job_id=job_id, session_id="chat-a", is_running=False, finished_at=1_020.0)])
    assert {session.session_id for session in store.list_sessions()} == {"chat-a", "chat-b"}
    assert store.maintain_retention() is True
    assert [session.session_id for session in store.list_sessions()] == ["chat-b"]


def test_session_summary_counts_each_durable_job_once(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "execution-store")
    started_at = time.time()
    for index, tool_name in enumerate(("start_job", "job_status", "job_status")):
        execution_id = f"execution-{index}"
        store.start_execution(
            execution_id=execution_id,
            session_id="chat-a",
            project_name="serena",
            tool_name=tool_name,
            arguments={},
            started_at=started_at + index,
        )
        store.finish_execution(
            execution_id,
            succeeded=True,
            durable_job_id="job-a",
            durable_job_label="Synthetic job",
            finished_at=started_at + index + 0.5,
        )

    [summary] = store.list_session_execution_summaries()

    assert summary.execution_count == 3
    assert summary.durable_job_count == 1
