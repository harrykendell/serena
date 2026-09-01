"""Behavior tests for exported user-shell environment discovery."""

from __future__ import annotations

import os
from pathlib import Path

from mcp_runtime.shell_environment import UserShellEnvironment


def test_user_shell_environment_loads_exported_interactive_login_bash_values(monkeypatch, tmp_path: Path) -> None:
    """Exported values from the user's Bash startup files augment the service environment despite startup noise."""
    custom_bin = tmp_path / "custom-bin"
    custom_bin.mkdir()
    (tmp_path / ".bash_profile").write_text("source ~/.bashrc\n", encoding="utf-8")
    (tmp_path / ".bashrc").write_text(
        f"printf 'startup-noise\\n'\nexport MCP_RUNTIME_TEST=loaded\nexport PATH={custom_bin}:$PATH\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    environment = UserShellEnvironment().get()

    assert environment["MCP_RUNTIME_TEST"] == "loaded"
    assert environment["PATH"].split(os.pathsep)[0] == str(custom_bin)


def test_user_shell_environment_falls_back_when_configured_shell_is_unavailable(monkeypatch) -> None:
    """Invalid shell configuration leaves the service environment intact rather than blocking subprocess launch."""
    monkeypatch.setenv("SHELL", "/definitely/missing/shell")
    monkeypatch.setenv("MCP_RUNTIME_FALLBACK", "kept")

    environment = UserShellEnvironment().get()

    assert environment["MCP_RUNTIME_FALLBACK"] == "kept"
