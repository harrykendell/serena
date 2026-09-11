"""Behaviour tests for incremental shell command output."""

import os
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from serena.tools.cmd_tools import ExecuteShellCommandTool
from serena.util.shell import execute_shell_command


class _RecordingSink:
    def __init__(self) -> None:
        self.first_chunk = Event()
        self._lock = Lock()
        self._content = ""

    def write(self, content: str) -> None:
        with self._lock:
            self._content += content
            if "FIRST" in self._content:
                self.first_chunk.set()

    @property
    def content(self) -> str:
        with self._lock:
            return self._content


def test_execute_shell_command_streams_output_before_process_exit() -> None:
    sink = _RecordingSink()
    program = 'import time; print("FIRST", flush=True); time.sleep(0.5); print("SECOND", flush=True)'
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(program)}"

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(execute_shell_command, command, None, True, sink)
        assert sink.first_chunk.wait(timeout=2)
        assert not future.done()
        result = future.result(timeout=3)

    assert result.return_code == 0
    assert result.stdout == "FIRST\nSECOND\n"
    assert result.stderr == ""
    assert sink.content == "FIRST\nSECOND\n"


def test_execute_shell_command_timeout_terminates_descendants(tmp_path) -> None:
    """A timed-out shell command must reap its child process group before releasing control."""
    marker = tmp_path / "survived.txt"
    command = f"(sleep 0.3; printf survived > {shlex.quote(str(marker))}) & wait"

    with pytest.raises(TimeoutError, match="timed out"):
        execute_shell_command(command, timeout=0.05)

    time.sleep(0.4)
    assert not marker.exists()


def test_execute_shell_command_uses_user_shell_environment(monkeypatch, tmp_path) -> None:
    """Foreground commands resolve executables from the enriched user-shell PATH."""
    custom_bin = tmp_path / "bin"
    custom_bin.mkdir()
    executable = custom_bin / "from-user-path"
    executable.write_text("#!/bin/sh\nprintf 'resolved-from-user-path\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = f"{custom_bin}:/usr/bin:/bin"
    monkeypatch.setattr("serena.util.shell.user_shell_environment", lambda: environment)

    result = execute_shell_command("from-user-path", capture_stderr=True)

    assert result.return_code == 0
    assert result.stdout == "resolved-from-user-path\n"
    assert result.stderr == ""


def test_shell_tool_returns_compact_decision_relevant_payload(tmp_path) -> None:
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = SimpleNamespace(project_root=str(tmp_path), ls_sync_file_system_changes=lambda: 0)
    agent.serena_config.tool_timeout = 30
    tool = ExecuteShellCommandTool(agent)

    response = tool.apply("printf out; printf err >&2; exit 7", capture_stderr=True)

    assert response == {"return_code": 7, "stdout": "out", "stderr": "err"}


def test_shell_tool_returns_complete_output(tmp_path) -> None:
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = SimpleNamespace(project_root=str(tmp_path), ls_sync_file_system_changes=lambda: 0)
    agent.serena_config.tool_timeout = 30
    tool = ExecuteShellCommandTool(agent)
    program = 'print("x" * 1200, flush=True)'
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(program)}"

    response = tool.apply(command)

    assert response == {"return_code": 0, "stdout": "x" * 1200 + "\n"}
