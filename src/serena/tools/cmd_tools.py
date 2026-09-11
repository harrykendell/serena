"""
Tools supporting the execution of (external) commands
"""

import os.path

from serena.errors import UserFacingError
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
    ) -> dict[str, object]:
        """
        Execute a short, non-interactive shell command and return its output.
        This tool is intended for commands that terminate promptly. Long-running commands are supported by
        ``start_job``, and Serena-managed jobs can be awaited through ``job_status(wait_for='completed')``.

        :param command: the shell command to execute
        :param cwd: the working directory to execute the command in. If None, the project root will be used.
        :param capture_stderr: whether to capture and return stderr output
        :return: native result containing the return code plus complete non-empty stdout/stderr output
        """
        if cwd is None:
            _cwd = self.get_project_root()
        elif os.path.isabs(cwd):
            _cwd = cwd
        else:
            _cwd = os.path.join(self.get_project_root(), cwd)
        if not os.path.isdir(_cwd):
            raise UserFacingError(f"Working directory is not a directory: {cwd or _cwd}")

        # stream internally to avoid pipe deadlock while still returning the complete logical result
        try:
            try:
                result = execute_shell_command(
                    command,
                    cwd=_cwd,
                    capture_stderr=capture_stderr,
                    timeout=self.agent.serena_config.tool_timeout,
                )
            except TimeoutError as error:
                raise UserFacingError(str(error)) from None
            except OSError as error:
                raise UserFacingError(f"Could not execute shell command: {error.strerror or error}") from None
        finally:
            self.project.ls_sync_file_system_changes()
        payload: dict[str, object] = {"return_code": result.return_code}
        if result.stdout:
            payload["stdout"] = result.stdout
        if result.stderr:
            payload["stderr"] = result.stderr
        return payload
