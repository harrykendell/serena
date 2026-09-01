"""Behavior tests for O05 Codex provider execution and worktree safety."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import RequestParams

from orchestrator.config import OrchestratorConfig
from orchestrator.mcp import OrchestratorMCPFactory


def _mcp_context(session_id: str) -> Any:
    meta = RequestParams.Meta.model_validate({"openai/session": session_id})
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta), session=object())


def _call_tool(server, name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        return await server._tool_manager.call_tool(name, arguments, context=_mcp_context(session_id))

    return asyncio.run(call())


def _fake_codex(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        r"""#!/usr/bin/env python3
import json
import subprocess
import sys
import time
from pathlib import Path


def argument(name):
    index = sys.argv.index(name)
    return sys.argv[index + 1]

assert sys.argv[1:4] == ["--ask-for-approval", "never", "exec"]
for required in ("--ephemeral", "--json", "--output-schema", "--output-last-message", "--sandbox", "-C"):
    assert required in sys.argv, sys.argv
assert argument("--color") == "never"
assert argument("--model") == "configured-codex-model"
assert argument("-c") == 'model_reasoning_effort="high"'
sandbox = argument("--sandbox")
if sandbox == "read-only":
    assert "--skip-git-repo-check" in sys.argv

prompt = sys.stdin.read()
output = Path(argument("--output-last-message"))
if "COUNT_LAUNCH" in prompt:
    with Path("launch-count.txt").open("a", encoding="utf-8") as marker:
        marker.write("launch\n")
print(json.dumps({
    "type": "turn.completed",
    "model": "fake-codex-model",
    "reasoning_effort": "high",
    "usage": {
        "input_tokens": 120,
        "cached_input_tokens": 40,
        "cache_write_input_tokens": 5,
        "output_tokens": 30,
        "reasoning_output_tokens": 7
    }
}), flush=True)

if "SLEEP_PROVIDER" in prompt:
    time.sleep(5)

if "FAIL_PROVIDER" in prompt:
    print("fake provider failure", file=sys.stderr, flush=True)
    raise SystemExit(4)

if "MODIFY_CODE" in prompt:
    Path("codex-output.txt").write_text("isolated codex change\n", encoding="utf-8")
    subprocess.run(["git", "add", "codex-output.txt"], check=True)
    subprocess.run(["git", "commit", "-m", "Fake Codex change"], check=True, stdout=subprocess.DEVNULL)
    result = {
        "status": "completed",
        "summary": "Implemented the requested isolated change.",
        "changed_files": [],
        "tests": ["fake verification: passed"],
        "commit": None,
        "worktree": None,
        "diff_summary": "",
        "remaining_issues": [],
        "artifacts": []
    }
else:
    result = {
        "status": "completed",
        "conclusion": "Read-only Codex inspection completed.",
        "findings": ["The provider returned bounded structured output."],
        "evidence": ["fake provider"],
        "recommendation": "Continue with the bounded result.",
        "uncertainties": []
    }

output.write_text(json.dumps(result), encoding="utf-8")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _config(
    tmp_path: Path,
    executable: Path,
    *,
    concurrency: int = 2,
    timeout: float = 3.0,
    auto_claim_timeout: float = 90.0,
    auto_token_budget: int | None = None,
) -> OrchestratorConfig:
    return OrchestratorConfig(
        state_root=(tmp_path / "orchestrator-home").resolve(),
        codex_executable=str(executable),
        codex_concurrency=concurrency,
        codex_timeout_seconds=timeout,
        codex_model="configured-codex-model",
        codex_reasoning_effort="high",
        auto_claim_timeout_seconds=auto_claim_timeout,
        codex_auto_token_budget=auto_token_budget,
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "orchestrator@example.invalid")
    _git(repository, "config", "user.name", "Orchestrator Test")
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Baseline")
    return repository


def _create_codex(server, project_root: Path, *, kind: str, goal: str, session_id: str = "parent-session") -> dict[str, Any]:
    return _call_tool(
        server,
        "create_delegate",
        {
            "project_name": "test-project",
            "project_root": str(project_root),
            "kind": kind,
            "provider_policy": "codex",
            "goal": goal,
            "acceptance_criteria": ["Return the bounded structured result."],
            "verification": ["Perform the requested verification."],
        },
        session_id,
    )


def _create_auto(server, project_root: Path, *, goal: str, session_id: str = "parent-session") -> dict[str, Any]:
    return _call_tool(
        server,
        "create_delegate",
        {
            "project_name": "test-project",
            "project_root": str(project_root),
            "kind": "explore",
            "provider_policy": "auto",
            "goal": goal,
            "acceptance_criteria": ["Return the bounded structured result."],
        },
        session_id,
    )


def _wait_for_state(
    server, delegate_id: str, states: set[str], *, session_id: str = "parent-session", timeout: float = 5.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _call_tool(server, "delegate_status", {"delegate_id": delegate_id}, session_id)
        if last["state"] in states:
            return last
        time.sleep(0.03)
    raise AssertionError(f"delegate did not reach {states}; last status={last}")


def test_codex_provider_resolves_executable_from_user_shell_path(monkeypatch, tmp_path: Path) -> None:
    """Codex launches through the enriched user-shell PATH when the service environment itself does not contain it."""
    fake_codex = _fake_codex(tmp_path)
    custom_bin = tmp_path / "user-bin"
    custom_bin.mkdir()
    (custom_bin / "codex").symlink_to(fake_codex)
    environment = {"PATH": f"{custom_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}
    monkeypatch.setattr("orchestrator.providers.user_shell_environment", lambda: environment)
    config = OrchestratorConfig(
        state_root=(tmp_path / "orchestrator-home").resolve(),
        codex_executable="codex",
        codex_concurrency=1,
        codex_timeout_seconds=3.0,
        codex_model="configured-codex-model",
        codex_reasoning_effort="high",
    )
    project = tmp_path / "project"
    project.mkdir()
    server = OrchestratorMCPFactory(config).create_mcp_server()

    created = _create_codex(server, project, kind="explore", goal="Inspect through the user PATH.")
    _wait_for_state(server, created["delegate_id"], {"COMPLETED"})
    collected = _call_tool(server, "collect_delegate", {"delegate_id": created["delegate_id"]}, "parent-session")

    assert collected["state"] == "COMPLETED"
    assert collected["result"]["conclusion"] == "Read-only Codex inspection completed."


def test_auto_provider_falls_back_to_codex_exactly_once(tmp_path: Path) -> None:
    """An unclaimed auto delegate persists its deadline then starts one Codex execution."""
    executable = _fake_codex(tmp_path)
    project = tmp_path / "auto-project"
    project.mkdir()
    factory = OrchestratorMCPFactory(_config(tmp_path, executable, auto_claim_timeout=0.05))
    server = factory.create_mcp_server()
    try:
        created = _create_auto(server, project, goal="COUNT_LAUNCH inspect after the ChatGPT claim window.")
        assert created["state"] == "WAITING_FOR_CHAT"
        assert created["provider_policy"] == "auto"
        assert created["claim_deadline"] is not None
        assert created["fallback"] == "codex"
        assert created["launch_prompt"].startswith("@Orchestrator claim delegate ")

        terminal = _wait_for_state(server, created["delegate_id"], {"COMPLETED"})
        assert terminal["provider_policy"] == "auto"
        assert terminal["active_provider"] == "codex"
        assert (project / "launch-count.txt").read_text(encoding="utf-8").splitlines() == ["launch"]
    finally:
        factory.close()


def test_auto_claim_before_deadline_prevents_fallback(tmp_path: Path) -> None:
    """A ChatGPT claim wins before the persisted deadline and Codex never starts."""
    executable = _fake_codex(tmp_path)
    project = tmp_path / "claimed-auto-project"
    project.mkdir()
    factory = OrchestratorMCPFactory(_config(tmp_path, executable, auto_claim_timeout=0.12))
    server = factory.create_mcp_server()
    try:
        created = _create_auto(server, project, goal="COUNT_LAUNCH stay with the claimed ChatGPT worker.")
        claimed = _call_tool(server, "claim_delegate", {"delegate_id": created["delegate_id"]}, "worker-session")
        assert claimed["state"] == "RUNNING_CHAT"

        time.sleep(0.18)
        status = _call_tool(server, "delegate_status", {"delegate_id": created["delegate_id"]}, "parent-session")
        assert status["state"] == "RUNNING_CHAT"
        assert status["active_provider"] == "chat"
        assert not (project / "launch-count.txt").exists()
    finally:
        factory.close()


def test_auto_scheduler_recovers_expired_deadline_after_restart(tmp_path: Path) -> None:
    """A recreated Orchestrator resumes fallback from the persisted deadline without an in-memory timer."""
    executable = _fake_codex(tmp_path)
    project = tmp_path / "restart-auto-project"
    project.mkdir()
    config = _config(tmp_path, executable, auto_claim_timeout=0.06)

    first_factory = OrchestratorMCPFactory(config)
    first_server = first_factory.create_mcp_server()
    created = _create_auto(first_server, project, goal="COUNT_LAUNCH recover this fallback after restart.")
    first_factory.close()
    time.sleep(0.08)

    second_factory = OrchestratorMCPFactory(config)
    second_server = second_factory.create_mcp_server()
    try:
        terminal = _wait_for_state(second_server, created["delegate_id"], {"COMPLETED"})
        assert terminal["active_provider"] == "codex"
        assert (project / "launch-count.txt").read_text(encoding="utf-8").splitlines() == ["launch"]
    finally:
        second_factory.close()


def test_auto_fallback_obeys_budget_and_parent_can_reroute(tmp_path: Path) -> None:
    """Automatic fallback stays claimable when over budget, while an explicit parent Codex reroute may proceed."""
    executable = _fake_codex(tmp_path)
    project = tmp_path / "budget-auto-project"
    project.mkdir()
    factory = OrchestratorMCPFactory(_config(tmp_path, executable, auto_claim_timeout=0.04, auto_token_budget=100))
    server = factory.create_mcp_server()
    try:
        seed = _create_codex(server, project, kind="explore", goal="Consume the configured automatic provider budget.")
        _wait_for_state(server, seed["delegate_id"], {"COMPLETED"})

        created = _create_auto(server, project, goal="COUNT_LAUNCH wait when automatic Codex budget is exhausted.")
        deadline = time.monotonic() + 2.0
        status: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status = _call_tool(server, "delegate_status", {"delegate_id": created["delegate_id"]}, "parent-session")
            if status.get("message") and "budget exhausted" in status["message"]:
                break
            time.sleep(0.02)
        assert status is not None
        assert status["state"] == "WAITING_FOR_CHAT"
        assert "budget exhausted" in status["message"]
        assert not (project / "launch-count.txt").exists()

        rerouted = _call_tool(
            server,
            "delegate_reroute",
            {"delegate_id": created["delegate_id"], "provider_policy": "codex"},
            "parent-session",
        )
        assert rerouted["provider_policy"] == "codex"
        _wait_for_state(server, created["delegate_id"], {"COMPLETED"})
        assert (project / "launch-count.txt").read_text(encoding="utf-8").splitlines() == ["launch"]
    finally:
        factory.close()


def test_auto_fallback_stays_claimable_when_codex_is_unavailable(tmp_path: Path) -> None:
    """An unavailable Codex executable blocks only fallback and does not fail the waiting delegate."""
    project = tmp_path / "unavailable-auto-project"
    project.mkdir()
    missing = tmp_path / "missing-codex"
    factory = OrchestratorMCPFactory(_config(tmp_path, missing, auto_claim_timeout=0.03))
    server = factory.create_mcp_server()
    try:
        created = _create_auto(server, project, goal="Remain claimable without a Codex executable.")
        deadline = time.monotonic() + 2.0
        status: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status = _call_tool(server, "delegate_status", {"delegate_id": created["delegate_id"]}, "parent-session")
            if status.get("message") and "unavailable" in status["message"]:
                break
            time.sleep(0.02)
        assert status is not None
        assert status["state"] == "WAITING_FOR_CHAT"
        assert "unavailable" in status["message"]
    finally:
        factory.close()


def test_codex_read_only_success_parses_usage_and_keeps_provider_logs_private(tmp_path: Path) -> None:
    """A successful Codex task produces only its typed result while usage/log detail remains private."""
    executable = _fake_codex(tmp_path)
    project = tmp_path / "read-only-project"
    project.mkdir()
    config = _config(tmp_path, executable)
    server = OrchestratorMCPFactory(config).create_mcp_server()

    created = _create_codex(server, project, kind="explore", goal="Inspect this project without changes.")
    assert created["provider_policy"] == "codex"
    assert created["state"] == "QUEUED"
    assert created["launch_prompt"] == ""

    terminal = _wait_for_state(server, created["delegate_id"], {"COMPLETED"})
    assert terminal["result_available"] is True
    assert "usage" not in terminal
    collected = _call_tool(server, "collect_delegate", {"delegate_id": created["delegate_id"]}, "parent-session")
    assert collected["result"]["conclusion"] == "Read-only Codex inspection completed."

    detail = _call_tool(
        server,
        "get_orchestrator_delegate_detail",
        {"delegate_id": created["delegate_id"]},
        "parent-session",
    )
    metadata = detail["provider_metadata"]
    assert metadata["sandbox"] == "read-only"
    assert metadata["usage"] == {
        "input_tokens": 120,
        "cached_input_tokens": 40,
        "cache_write_input_tokens": 5,
        "output_tokens": 30,
        "reasoning_output_tokens": 7,
    }
    assert metadata["model"] == "configured-codex-model"
    assert metadata["reasoning_effort"] == "high"
    assert (config.state_root / metadata["event_log"]).is_file()
    assert (config.state_root / metadata["stderr_log"]).is_file()


def test_modifying_codex_uses_unique_worktree_and_never_changes_live_checkout(tmp_path: Path) -> None:
    """A code delegate commits in an isolated branch and returns Git-observed review metadata without merging it."""
    executable = _fake_codex(tmp_path)
    repository = _repository(tmp_path)
    live_head = _git(repository, "rev-parse", "HEAD")
    (repository / "local-uncommitted.txt").write_text("keep me local\n", encoding="utf-8")
    config = _config(tmp_path, executable)
    server = OrchestratorMCPFactory(config).create_mcp_server()

    first = _create_codex(server, repository, kind="code", goal="MODIFY_CODE implement the isolated change.")
    second = _create_codex(server, repository, kind="code", goal="MODIFY_CODE implement another isolated change.")
    first_status = _wait_for_state(server, first["delegate_id"], {"COMPLETED"})
    _wait_for_state(server, second["delegate_id"], {"COMPLETED"})
    assert "uncommitted changes" in first_status["message"]

    first_result = _call_tool(server, "collect_delegate", {"delegate_id": first["delegate_id"]}, "parent-session")["result"]
    second_result = _call_tool(server, "collect_delegate", {"delegate_id": second["delegate_id"]}, "parent-session")["result"]

    assert first_result["worktree"] != second_result["worktree"]
    assert Path(first_result["worktree"]).is_dir()
    assert Path(second_result["worktree"]).is_dir()
    assert first_result["commit"] and second_result["commit"]
    assert first_result["changed_files"] == ["codex-output.txt"]
    assert "codex-output.txt" in first_result["diff_summary"]
    assert first_result["tests"] == ["fake verification: passed"]

    assert _git(repository, "rev-parse", "HEAD") == live_head
    assert not (repository / "codex-output.txt").exists()
    assert (repository / "local-uncommitted.txt").read_text(encoding="utf-8") == "keep me local\n"
    assert _git(repository, "status", "--porcelain") == "?? local-uncommitted.txt"

    first_detail = _call_tool(
        server,
        "get_orchestrator_delegate_detail",
        {"delegate_id": first["delegate_id"]},
        "parent-session",
    )
    assert first_detail["provider_metadata"]["live_checkout_dirty"] is True
    assert first_detail["provider_metadata"]["branch"].startswith("orchestrator/d_")


def test_codex_failure_and_unavailable_worktree_fail_safely(tmp_path: Path) -> None:
    """Provider errors and worktree isolation errors become explicit failures without falling back to live edits."""
    executable = _fake_codex(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config = _config(tmp_path, executable)
    server = OrchestratorMCPFactory(config).create_mcp_server()

    failed = _create_codex(server, project, kind="explore", goal="FAIL_PROVIDER demonstrate provider failure.")
    failed_status = _wait_for_state(server, failed["delegate_id"], {"FAILED"})
    assert "status 4" in failed_status["message"]
    with pytest.raises(ToolError, match="no collectable result"):
        _call_tool(server, "collect_delegate", {"delegate_id": failed["delegate_id"]}, "parent-session")

    nongit = tmp_path / "not-a-git-repository"
    nongit.mkdir()
    marker = nongit / "must-not-change.txt"
    marker.write_text("untouched\n", encoding="utf-8")
    unsafe = _create_codex(server, nongit, kind="code", goal="MODIFY_CODE this must never run in the live directory.")
    unsafe_status = _wait_for_state(server, unsafe["delegate_id"], {"FAILED"})
    assert "live checkout was not used" in unsafe_status["message"]
    assert marker.read_text(encoding="utf-8") == "untouched\n"
    assert not (nongit / "codex-output.txt").exists()


def test_codex_cancellation_and_timeout_are_terminal_and_kill_execution(tmp_path: Path) -> None:
    """Cancellation and timeout terminate Codex execution without allowing a late provider result to overwrite terminal state."""
    executable = _fake_codex(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    cancellation_config = _config(tmp_path / "cancel", executable, timeout=10.0)
    cancellation_server = OrchestratorMCPFactory(cancellation_config).create_mcp_server()
    cancelled = _create_codex(cancellation_server, project, kind="explore", goal="SLEEP_PROVIDER wait until cancelled.")
    _wait_for_state(cancellation_server, cancelled["delegate_id"], {"RUNNING_CODEX"})
    response = _call_tool(
        cancellation_server,
        "delegate_cancel",
        {"delegate_id": cancelled["delegate_id"], "reason": "Stop the unattended run."},
        "parent-session",
    )
    assert response == {"delegate_id": cancelled["delegate_id"], "state": "CANCELLED", "cancelled": True}
    time.sleep(0.15)
    assert (
        _call_tool(cancellation_server, "delegate_status", {"delegate_id": cancelled["delegate_id"]}, "parent-session")["state"]
        == "CANCELLED"
    )

    timeout_config = _config(tmp_path / "timeout", executable, timeout=0.15)
    timeout_server = OrchestratorMCPFactory(timeout_config).create_mcp_server()
    timed_out = _create_codex(timeout_server, project, kind="explore", goal="SLEEP_PROVIDER exceed the configured timeout.")
    timeout_status = _wait_for_state(timeout_server, timed_out["delegate_id"], {"TIMED_OUT"})
    assert timeout_status["message"] == "Timed out."
    timeout_detail = _call_tool(
        timeout_server,
        "get_orchestrator_delegate_detail",
        {"delegate_id": timed_out["delegate_id"]},
        "parent-session",
    )
    assert "execution timeout" in timeout_detail["error"]


def test_codex_concurrency_cap_keeps_excess_delegate_queued(tmp_path: Path) -> None:
    """Orchestrator's Codex concurrency ceiling queues excess work independently of Serena jobs."""
    executable = _fake_codex(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config = _config(tmp_path, executable, concurrency=1, timeout=10.0)
    server = OrchestratorMCPFactory(config).create_mcp_server()

    first = _create_codex(server, project, kind="explore", goal="SLEEP_PROVIDER first long task.")
    second = _create_codex(server, project, kind="explore", goal="SLEEP_PROVIDER second long task.")
    _wait_for_state(server, first["delegate_id"], {"RUNNING_CODEX"})
    second_status = _call_tool(server, "delegate_status", {"delegate_id": second["delegate_id"]}, "parent-session")
    assert second_status["state"] == "QUEUED"

    _call_tool(server, "delegate_cancel", {"delegate_id": first["delegate_id"]}, "parent-session")
    _wait_for_state(server, second["delegate_id"], {"RUNNING_CODEX"})
    _call_tool(server, "delegate_cancel", {"delegate_id": second["delegate_id"]}, "parent-session")
