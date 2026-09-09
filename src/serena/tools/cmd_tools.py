"""
Tools supporting the execution of (external) commands
"""

import os.path

from serena.errors import UserFacingError
from serena.execution import get_current_execution_id
from serena.tools import Tool, ToolMarkerCanEdit
from serena.util.shell import execute_shell_command


class ExecuteShellCommandTool(Tool, ToolMarkerCanEdit):
    """
    Executes a shell command.
    """

    def apply(
        self,
        command: str,
        cwd: str | None = None,
        capture_stderr: bool = True,
        max_answer_chars: int = -1,
    ) -> str:
        """
        Execute a short, non-interactive shell command and return its output.
        This tool is intended for commands that terminate promptly. Long-running commands are supported by
        ``start_job``, and Serena-managed jobs can be awaited through ``job_status(wait_for='completed')``.

        :param command: the shell command to execute
        :param cwd: the working directory to execute the command in. If None, the project root will be used.
        :param capture_stderr: whether to capture and return stderr output
        :param max_answer_chars: if the output is longer than this number of characters,
            a retained output tail is returned when supported. -1 uses the configured default.
        :return: a JSON object containing the command's stdout and optionally stderr output
        """
        if cwd is None:
            _cwd = self.get_project_root()
        elif os.path.isabs(cwd):
            _cwd = cwd
        else:
            _cwd = os.path.join(self.get_project_root(), cwd)
        if not os.path.isdir(_cwd):
            raise UserFacingError(f"Working directory is not a directory: {cwd or _cwd}")

        effective_max_answer_chars = self._effective_max_answer_chars(max_answer_chars)

        # stream a live transcript keyed to this exact model-visible execution
        try:
            with self.agent.open_tool_output(self.get_name(), execution_id=get_current_execution_id()) as output_writer:
                result = execute_shell_command(
                    command,
                    cwd=_cwd,
                    capture_stderr=capture_stderr,
                    output_sink=output_writer,
                    timeout=self.agent.serena_config.tool_timeout,
                )
        except TimeoutError as error:
            raise UserFacingError(str(error)) from None
        except OSError as error:
            raise UserFacingError(f"Could not execute shell command: {error.strerror or error}") from None
        result_json = result.model_dump_json()
        if len(result_json) <= effective_max_answer_chars:
            return result_json

        if self.agent.tool_is_active("read_tool_output"):
            details = f"return_code={result.return_code}; cwd={result.cwd}"
            return self.agent.render_tool_output_tail(
                output_writer.output_id,
                effective_max_answer_chars,
                details=details,
            )
        return self._limit_length(result_json, max_answer_chars)
