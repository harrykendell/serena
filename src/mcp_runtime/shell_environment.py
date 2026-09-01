"""User shell environment discovery for non-interactive MCP subprocesses."""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

_ENV_START = b"__MCP_USER_ENV_START__\0"
_ENV_END = b"__MCP_USER_ENV_END__\0"
_DISCOVERY_TIMEOUT_SECONDS = 5.0


class UserShellEnvironment:
    """Resolves and caches exported variables from the user's configured shell startup files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: dict[str, str] | None = None

    def get(self) -> dict[str, str]:
        """Returns the process environment enriched by exported user-shell variables."""
        with self._lock:
            if self._cached is None:
                self._cached = self._discover()
            return dict(self._cached)

    def _discover(self) -> dict[str, str]:
        """Loads an interactive login-shell environment, falling back safely to the process environment."""
        inherited = dict(os.environ)
        shell = self._shell_executable(inherited)
        if shell is None:
            return inherited

        marker_command = "printf '__MCP_USER_ENV_START__\\0'; env -0; printf '__MCP_USER_ENV_END__\\0'"
        try:
            completed = subprocess.run(
                [str(shell), "-lic", marker_command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_DISCOVERY_TIMEOUT_SECONDS,
                env=inherited,
            )
        except (OSError, subprocess.TimeoutExpired):
            return inherited
        if completed.returncode != 0:
            return inherited

        discovered = self._parse_exported_environment(completed.stdout)
        if not discovered:
            return inherited
        inherited.update(discovered)
        return inherited

    @staticmethod
    def _shell_executable(environment: Mapping[str, str]) -> Path | None:
        """Returns a safe absolute shell executable path suitable for login-shell discovery."""
        configured = environment.get("SHELL", "/bin/bash")
        try:
            parts = shlex.split(configured)
        except ValueError:
            return None
        if len(parts) != 1:
            return None
        shell = Path(parts[0]).expanduser()
        if not shell.is_absolute() or not shell.is_file() or not os.access(shell, os.X_OK):
            return None
        return shell

    @staticmethod
    def _parse_exported_environment(output: bytes) -> dict[str, str]:
        """Extracts NUL-delimited ``env`` output between markers, ignoring shell startup noise."""
        start = output.find(_ENV_START)
        if start < 0:
            return {}
        start += len(_ENV_START)
        end = output.find(_ENV_END, start)
        if end < 0:
            return {}

        parsed: dict[str, str] = {}
        for entry in output[start:end].split(b"\0"):
            if not entry or b"=" not in entry:
                continue
            name, value = entry.split(b"=", 1)
            try:
                decoded_name = name.decode("utf-8")
                decoded_value = value.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if decoded_name:
                parsed[decoded_name] = decoded_value
        return parsed


_USER_SHELL_ENVIRONMENT = UserShellEnvironment()


def user_shell_environment() -> dict[str, str]:
    """Returns the cached exported user-shell environment for child processes."""
    return _USER_SHELL_ENVIRONMENT.get()
