"""Behaviour tests for incremental shell command output."""

import re
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, current_thread
from types import SimpleNamespace
from unittest.mock import MagicMock

from serena.tool_output import ToolOutputStore
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


def test_shell_tool_oversize_response_reuses_live_transcript_id(tmp_path) -> None:
    store = ToolOutputStore()
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = SimpleNamespace(project_root=str(tmp_path))
    agent.serena_config.default_max_tool_answer_chars = 400
    agent.open_tool_output.side_effect = store.open
    agent.render_tool_output_tail.side_effect = store.render_tail
    agent.tool_is_active.return_value = True
    tool = ExecuteShellCommandTool(agent)
    program = 'print("x" * 1200, flush=True)'
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(program)}"

    try:
        response = tool.apply(command, max_answer_chars=400)
        match = re.search(r"Shell transcript retained as ([0-9a-f]{32})", response)
        assert match is not None
        output_id = match.group(1)
        descriptor = store.describe_execution(current_thread().name)
        page = store.read(output_id, 0, 2_000)

        assert descriptor is not None
        assert descriptor.output_id == output_id
        assert page.content == "x" * 1200 + "\n"
        assert "return_code=0" in response
    finally:
        store.close()
