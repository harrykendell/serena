import codecs
import os
import subprocess
from threading import Thread
from typing import Protocol

from pydantic import BaseModel

from solidlsp.util.subprocess_util import subprocess_kwargs


class ShellOutputSink(Protocol):
    """Destination for incremental subprocess output."""

    def write(self, content: str) -> None:
        """Accept one decoded output chunk."""


class ShellCommandResult(BaseModel):
    stdout: str
    return_code: int
    cwd: str
    stderr: str | None = None


def execute_shell_command(
    command: str,
    cwd: str | None = None,
    capture_stderr: bool = False,
    output_sink: ShellOutputSink | None = None,
) -> ShellCommandResult:
    """
    Execute a shell command and return the output.

    :param command: The command to execute.
    :param cwd: The working directory to execute the command in. If None, the current working directory will be used.
    :param capture_stderr: Whether to capture the stderr output.
    :param output_sink: Optional destination receiving decoded stdout/stderr chunks while the process is running.
    :return: The output of the command.
    """
    if cwd is None:
        cwd = os.getcwd()

    process = subprocess.Popen(
        command,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture_stderr else None,
        cwd=cwd,
        **subprocess_kwargs(),
    )
    assert process.stdout is not None

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def consume_stream(stream, chunks: list[str]) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            raw = os.read(stream.fileno(), 4096)
            if not raw:
                break
            text = decoder.decode(raw)
            if text:
                chunks.append(text)
                if output_sink is not None:
                    output_sink.write(text)
        final_text = decoder.decode(b"", final=True)
        if final_text:
            chunks.append(final_text)
            if output_sink is not None:
                output_sink.write(final_text)

    stdout_thread = Thread(target=consume_stream, args=(process.stdout, stdout_chunks), name="ShellStdoutReader")
    stdout_thread.start()
    stderr_thread = None
    if process.stderr is not None:
        stderr_thread = Thread(target=consume_stream, args=(process.stderr, stderr_chunks), name="ShellStderrReader")
        stderr_thread.start()

    process.wait()
    stdout_thread.join()
    if stderr_thread is not None:
        stderr_thread.join()

    return ShellCommandResult(
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks) if capture_stderr else None,
        return_code=process.returncode,
        cwd=cwd,
    )


def subprocess_check_output(
    args: list[str], encoding: str = "utf-8", strip: bool = True, timeout: float | None = None, cwd: str | None = None
) -> str:
    output = subprocess.check_output(
        args, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout, env=os.environ.copy(), cwd=cwd, **subprocess_kwargs()
    ).decode(encoding)
    if strip:
        output = output.strip()
    return output
