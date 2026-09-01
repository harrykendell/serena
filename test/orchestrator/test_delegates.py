"""Behavior tests for the manual ChatGPT delegate workflow."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import RequestParams

from orchestrator.config import OrchestratorConfig
from orchestrator.delegates import CreateDelegateRequest, DelegateError, DelegateKind, DelegateState, DelegateStore
from orchestrator.mcp import OrchestratorMCPFactory


def _mcp_context(session_id: str) -> Any:
    meta = RequestParams.Meta.model_validate({"openai/session": session_id})
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta), session=object())


def _call_tool(server, name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        return await server._tool_manager.call_tool(name, arguments, context=_mcp_context(session_id))

    return asyncio.run(call())


@pytest.fixture
def orchestrator_config(tmp_path: Path) -> OrchestratorConfig:
    """Provides isolated persistent state for one test."""
    return OrchestratorConfig.from_environment(tmp_path / "orchestrator-home")


def _create_arguments() -> dict[str, Any]:
    return {
        "project_name": "serena",
        "kind": "explore",
        "goal": "Inspect the routing boundary and identify the smallest safe change.",
        "known_context": ["The target is the independent Orchestrator package."],
        "scope": ["Read the relevant MCP and persistence code."],
        "out_of_scope": ["Do not implement provider execution."],
        "acceptance_criteria": ["Return a concise conclusion supported by repository evidence."],
        "verification": ["Inspect the relevant tests."],
        "parent_notes": "Keep the hand-back compact.",
    }


def _explore_result() -> dict[str, Any]:
    return {
        "status": "completed",
        "conclusion": "The routing boundary is independent and the change can stay inside Orchestrator.",
        "findings": ["No Serena runtime dependency is required."],
        "evidence": ["src/orchestrator/mcp.py"],
        "recommendation": "Keep the manual delegate flow isolated from Serena.",
        "uncertainties": [],
    }


def test_manual_delegate_flow_survives_server_recreation(orchestrator_config: OrchestratorConfig) -> None:
    """A fresh worker can claim, complete, and hand back only the bounded typed result."""
    parent_server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    created = _call_tool(parent_server, "create_delegate", _create_arguments(), "parent-session")

    assert created["state"] == "WAITING_FOR_CHAT"
    assert created["provider_policy"] == "chat"
    assert created["claim_deadline"] is None
    assert created["fallback"] is None
    assert "goal" not in created
    assert "\n" not in created["launch_prompt"]
    assert created["launch_prompt"] == f"@Orchestrator claim delegate {created['delegate_id']} and complete it independently."

    worker_server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    claimed = _call_tool(worker_server, "claim_delegate", {"delegate_id": created["delegate_id"]}, "worker-session")

    assert claimed["state"] == "RUNNING_CHAT"
    assert claimed["task"]["delegate_id"] == created["delegate_id"]
    assert claimed["task"]["project_name"] == "serena"
    assert claimed["task"]["goal"] == _create_arguments()["goal"]
    assert claimed["task"]["result_schema"]["name"] == "ExploreResult"
    assert "parent_session_id" not in claimed["task"]
    assert "worker_session_id" not in claimed["task"]
    assert "activate the named project in Serena yourself" in claimed["task"]["instructions"]

    completed = _call_tool(
        worker_server,
        "complete_delegate",
        {"delegate_id": created["delegate_id"], "result": _explore_result()},
        "worker-session",
    )
    assert completed == {"delegate_id": created["delegate_id"], "state": "COMPLETED"}

    collecting_server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    collected = _call_tool(collecting_server, "collect_delegate", {"delegate_id": created["delegate_id"]}, "parent-session")

    assert collected == {
        "delegate_id": created["delegate_id"],
        "state": "COMPLETED",
        "result": _explore_result(),
    }


def test_delegate_ownership_rejects_unrelated_sessions(orchestrator_config: OrchestratorConfig) -> None:
    """Only a fresh claiming worker and the creating parent can perform their respective transitions."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    created = _call_tool(server, "create_delegate", _create_arguments(), "parent-session")
    delegate_id = created["delegate_id"]

    with pytest.raises(ToolError, match="parent session cannot claim"):
        _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "parent-session")

    _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "worker-session")

    with pytest.raises(ToolError, match="not claimable"):
        _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "unrelated-worker")
    with pytest.raises(ToolError, match="session that claimed"):
        _call_tool(
            server,
            "complete_delegate",
            {"delegate_id": delegate_id, "result": _explore_result()},
            "unrelated-worker",
        )

    _call_tool(server, "complete_delegate", {"delegate_id": delegate_id, "result": _explore_result()}, "worker-session")

    with pytest.raises(ToolError, match="parent session may collect"):
        _call_tool(server, "collect_delegate", {"delegate_id": delegate_id}, "unrelated-parent")


def test_claim_and_completion_are_idempotent_for_the_owners(orchestrator_config: OrchestratorConfig) -> None:
    """Safe retries by the owning sessions do not duplicate work or overwrite a result."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    created = _call_tool(server, "create_delegate", _create_arguments(), "parent-session")
    delegate_id = created["delegate_id"]

    first_claim = _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "worker-session")
    second_claim = _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "worker-session")
    assert second_claim == first_claim

    first_complete = _call_tool(server, "complete_delegate", {"delegate_id": delegate_id, "result": _explore_result()}, "worker-session")
    second_complete = _call_tool(server, "complete_delegate", {"delegate_id": delegate_id, "result": _explore_result()}, "worker-session")
    assert second_complete == first_complete

    changed_result = _explore_result() | {"conclusion": "A different conclusion must not replace the first hand-back."}
    with pytest.raises(ToolError, match="different result"):
        _call_tool(server, "complete_delegate", {"delegate_id": delegate_id, "result": changed_result}, "worker-session")


def test_result_contract_and_result_budget_are_enforced(orchestrator_config: OrchestratorConfig) -> None:
    """A worker cannot persist a result outside the delegate's kind-specific schema or size budget."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    create_args = _create_arguments() | {"result_budget_chars": 1_000}
    created = _call_tool(server, "create_delegate", create_args, "parent-session")
    delegate_id = created["delegate_id"]
    _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "worker-session")

    invalid_shape = _explore_result() | {"findings": [f"finding-{index}" for index in range(6)]}
    with pytest.raises(ToolError, match="ExploreResult"):
        _call_tool(server, "complete_delegate", {"delegate_id": delegate_id, "result": invalid_shape}, "worker-session")

    oversized = _explore_result() | {"conclusion": "x" * 900, "recommendation": "y" * 900}
    with pytest.raises(ToolError, match="exceeding its 1000-character budget"):
        _call_tool(server, "complete_delegate", {"delegate_id": delegate_id, "result": oversized}, "worker-session")


def test_task_packet_budget_is_enforced(orchestrator_config: OrchestratorConfig) -> None:
    """A parent cannot use delegate creation to copy an unbounded conversation into the worker packet."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    oversized_context = ["x" * 500 for _ in range(20)]
    create_args = _create_arguments() | {
        "known_context": oversized_context,
        "scope": oversized_context,
    }

    with pytest.raises(ToolError, match="task packet exceeds"):
        _call_tool(server, "create_delegate", create_args, "parent-session")


def test_competing_claims_bind_exactly_one_worker(orchestrator_config: OrchestratorConfig) -> None:
    """Concurrent stores cannot both claim the same waiting delegate."""
    creator = DelegateStore(orchestrator_config)
    created = creator.create(
        "parent-session",
        CreateDelegateRequest.model_validate(_create_arguments()),
    )

    def claim(worker_session_id: str) -> DelegateState | str:
        store = DelegateStore(orchestrator_config)
        try:
            return store.claim(created.delegate_id, worker_session_id).state
        except DelegateError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ["worker-a", "worker-b"]))

    assert outcomes.count(DelegateState.RUNNING_CHAT) == 1
    assert sum(isinstance(outcome, str) and "not claimable" in outcome for outcome in outcomes) == 1


def test_auto_claim_and_fallback_boundary_has_single_winner(tmp_path: Path) -> None:
    """A simultaneous ChatGPT claim and expired auto fallback cannot both acquire the delegate."""
    config = OrchestratorConfig(state_root=(tmp_path / "orchestrator-home").resolve(), auto_claim_timeout_seconds=0.02)
    creator = DelegateStore(config)
    arguments = _create_arguments()
    arguments.update({"provider_policy": "auto", "project_root": str(tmp_path)})
    created = creator.create("parent-session", CreateDelegateRequest.model_validate(arguments))
    time.sleep(0.03)

    def claim() -> str:
        try:
            state = DelegateStore(config).claim(created.delegate_id, "worker-session").state
            return f"claimed:{state}"
        except DelegateError as exc:
            return f"claim-error:{exc}"

    def fallback() -> str:
        routed = DelegateStore(config).route_auto_to_codex(created.delegate_id)
        return f"fallback:{routed}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        fallback_future = executor.submit(fallback)
        outcomes = {claim_future.result(), fallback_future.result()}

    final = creator.status(created.delegate_id, "parent-session")
    if final.state == DelegateState.RUNNING_CHAT:
        assert f"claimed:{DelegateState.RUNNING_CHAT}" in outcomes
        assert "fallback:False" in outcomes
    else:
        assert final.state == DelegateState.QUEUED
        assert "fallback:True" in outcomes
        assert any(outcome.startswith("claim-error:") and "not claimable" in outcome for outcome in outcomes)


def test_kind_specific_code_result_is_collected_without_transcript(orchestrator_config: OrchestratorConfig) -> None:
    """Code delegates use their own compact hand-back contract rather than an untyped transcript."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    create_args = _create_arguments() | {
        "kind": DelegateKind.CODE.value,
        "goal": "Implement the isolated change and verify it.",
        "acceptance_criteria": ["Tests pass."],
    }
    created = _call_tool(server, "create_delegate", create_args, "parent-session")
    delegate_id = created["delegate_id"]
    claimed = _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "worker-session")
    assert claimed["task"]["result_schema"]["name"] == "CodeResult"

    result = {
        "status": "completed",
        "summary": "Implemented and verified the isolated change.",
        "changed_files": ["src/example.py"],
        "tests": ["pytest test/example.py -q: passed"],
        "commit": "abc123",
        "worktree": None,
        "diff_summary": "",
        "remaining_issues": [],
        "artifacts": [],
    }
    _call_tool(server, "complete_delegate", {"delegate_id": delegate_id, "result": result}, "worker-session")
    collected = _call_tool(server, "collect_delegate", {"delegate_id": delegate_id}, "parent-session")

    assert collected["result"] == result
    assert set(collected) == {"delegate_id", "state", "result"}


def test_status_and_cancellation_are_owned_compact_and_idempotent(orchestrator_config: OrchestratorConfig) -> None:
    """Parent/worker lifecycle controls expose compact state while private cancellation detail stays app-only."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    created = _call_tool(server, "create_delegate", _create_arguments(), "parent-session")
    delegate_id = created["delegate_id"]

    waiting = _call_tool(server, "delegate_status", {"delegate_id": delegate_id}, "parent-session")
    assert waiting["state"] == "WAITING_FOR_CHAT"
    assert waiting["result_available"] is False
    assert "goal" not in waiting
    assert "provider_metadata" not in waiting

    with pytest.raises(ToolError, match="not available to the current session"):
        _call_tool(server, "delegate_status", {"delegate_id": delegate_id}, "unrelated-session")

    _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "worker-session")
    running = _call_tool(server, "delegate_status", {"delegate_id": delegate_id}, "worker-session")
    assert running["state"] == "RUNNING_CHAT"
    assert running["active_provider"] == "chat"

    cancelled = _call_tool(
        server,
        "delegate_cancel",
        {"delegate_id": delegate_id, "reason": "Parent no longer needs this delegated investigation."},
        "parent-session",
    )
    assert cancelled == {"delegate_id": delegate_id, "state": "CANCELLED", "cancelled": True}
    assert _call_tool(server, "delegate_cancel", {"delegate_id": delegate_id}, "worker-session") == cancelled

    terminal = _call_tool(server, "delegate_status", {"delegate_id": delegate_id}, "parent-session")
    assert terminal["state"] == "CANCELLED"
    assert terminal["message"] == "Cancelled."
    assert "Parent no longer needs" not in str(terminal)

    detail = _call_tool(server, "get_orchestrator_delegate_detail", {"delegate_id": delegate_id}, "parent-session")
    assert detail["goal"] == _create_arguments()["goal"]
    assert detail["error"] == "Parent no longer needs this delegated investigation."
    assert [event["event"] for event in detail["audit"]] == ["created", "claimed", "cancelled"]
    assert detail["audit"][-1]["actor"] == "parent"

    with pytest.raises(ToolError, match="cannot be completed from state CANCELLED"):
        _call_tool(
            server,
            "complete_delegate",
            {"delegate_id": delegate_id, "result": _explore_result()},
            "worker-session",
        )


def test_blocked_worker_handback_is_failed_but_collectable(orchestrator_config: OrchestratorConfig) -> None:
    """A typed blocked hand-back records failure without forcing the parent to consume worker transcript or logs."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    created = _call_tool(server, "create_delegate", _create_arguments(), "parent-session")
    delegate_id = created["delegate_id"]
    _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, "worker-session")

    blocked_result = _explore_result() | {
        "status": "blocked",
        "conclusion": "The requested evidence is unavailable in the repository.",
        "recommendation": "Return to the parent for a scope decision.",
    }
    completed = _call_tool(
        server,
        "complete_delegate",
        {"delegate_id": delegate_id, "result": blocked_result},
        "worker-session",
    )
    assert completed == {"delegate_id": delegate_id, "state": "FAILED"}

    status = _call_tool(server, "delegate_status", {"delegate_id": delegate_id}, "parent-session")
    assert status["state"] == "FAILED"
    assert status["result_available"] is True
    assert status["message"] == "Worker returned a blocked result."

    collected = _call_tool(server, "collect_delegate", {"delegate_id": delegate_id}, "parent-session")
    assert collected == {"delegate_id": delegate_id, "state": "FAILED", "result": blocked_result}


def test_internal_failure_retains_only_bounded_private_detail(orchestrator_config: OrchestratorConfig) -> None:
    """Provider-facing failure state is durable without putting private failure detail into normal result collection."""
    store = DelegateStore(orchestrator_config)
    created = store.create("parent-session", CreateDelegateRequest.model_validate(_create_arguments()))
    failure = store.fail(created.delegate_id, "provider process ended before producing a typed result")

    assert failure.state == DelegateState.FAILED
    assert failure.result_available is False
    assert failure.message == "provider process ended before producing a typed result"
    detail = store.private_detail(created.delegate_id, "parent-session")
    assert detail.audit[-1].event == "failed"
    assert detail.audit[-1].actor == "system"
    with pytest.raises(DelegateError, match="no collectable result"):
        store.collect(created.delegate_id, "parent-session")
