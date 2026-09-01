"""Behavior tests for bounded Orchestrator fan-out/fan-in batches."""

from __future__ import annotations

import asyncio
import json
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


@pytest.fixture
def orchestrator_config(tmp_path: Path) -> OrchestratorConfig:
    """Provides isolated persistent state for one batch test."""
    return OrchestratorConfig.from_environment(tmp_path / "orchestrator-home")


def _task(index: int, *, kind: str = "explore") -> dict[str, Any]:
    return {
        "kind": kind,
        "goal": f"Independently inspect analysis question {index}.",
        "acceptance_criteria": [f"Return one supported conclusion for question {index}."],
        "scope": ["Read only; do not modify repository state."],
    }


def _result(index: int, conclusion: str | None = None) -> dict[str, Any]:
    return {
        "status": "completed",
        "conclusion": conclusion or f"Conclusion {index}.",
        "findings": [f"Finding {index}."],
        "evidence": [f"evidence-{index}"],
        "recommendation": f"Recommendation {index}.",
        "uncertainties": [],
    }


def _complete(server, delegate_id: str, worker_session: str, index: int, conclusion: str | None = None) -> dict[str, Any]:
    claimed = _call_tool(server, "claim_delegate", {"delegate_id": delegate_id}, worker_session)
    _call_tool(
        server,
        "complete_delegate",
        {"delegate_id": delegate_id, "result": _result(index, conclusion)},
        worker_session,
    )
    return claimed


def test_batch_default_fan_out_is_two_and_pending_work_waits_for_a_slot(orchestrator_config: OrchestratorConfig) -> None:
    """A larger batch exposes two ChatGPT workers by default and promotes pending work only after completion."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    created = _call_tool(
        server,
        "delegate_batch",
        {"project_name": "serena", "tasks": [_task(0), _task(1), _task(2)]},
        "parent-session",
    )

    assert created["state"] == "RUNNING"
    assert created["concurrency"] == 2
    assert created["active"] == 2
    assert created["pending"] == 1
    assert len(created["launches"]) == 2
    assert all(launch["provider_policy"] == "chat" for launch in created["launches"])
    assert [task["state"] for task in created["tasks"]] == ["WAITING_FOR_CHAT", "WAITING_FOR_CHAT", "PENDING"]

    with pytest.raises(ToolError, match="Only the parent session"):
        _call_tool(server, "delegate_batch", {"batch_id": created["batch_id"]}, "foreign-parent")

    first_delegate = created["launches"][0]["delegate_id"]
    claimed = _complete(server, first_delegate, "worker-0", 0)
    assert any("read-only batch task" in item for item in claimed["task"]["out_of_scope"])
    refreshed = _call_tool(server, "delegate_batch", {"batch_id": created["batch_id"]}, "parent-session")

    assert refreshed["pending"] == 0
    assert refreshed["active"] == 2
    assert refreshed["terminal"] == 1
    assert len(refreshed["launches"]) == 1
    assert refreshed["launches"][0]["task_index"] == 2
    assert refreshed["tasks"][0]["state"] == "COMPLETED"


def test_batch_fan_in_is_compact_and_bounded(orchestrator_config: OrchestratorConfig) -> None:
    """Fan-in returns deterministic digests inside the batch result budget rather than concatenated child results."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    created = _call_tool(
        server,
        "delegate_batch",
        {
            "project_name": "serena",
            "tasks": [_task(0), _task(1)],
            "result_budget_chars": 1_000,
        },
        "parent-session",
    )

    for index, launch in enumerate(created["launches"]):
        _complete(server, launch["delegate_id"], f"worker-{index}", index, "x" * 2_000)

    completed = _call_tool(server, "delegate_batch", {"batch_id": created["batch_id"]}, "parent-session")

    assert completed["state"] == "COMPLETED"
    assert completed["active"] == 0
    assert completed["pending"] == 0
    assert completed["terminal"] == 2
    assert len(json.dumps(completed["aggregate"], separators=(",", ":"))) <= 1_000
    assert "recommendation" not in completed["aggregate"]
    assert all("summary" in item for item in completed["aggregate"]["items"])


def test_batch_rejects_modifying_code_fan_out(orchestrator_config: OrchestratorConfig) -> None:
    """The O07 batch surface refuses modifying code delegates."""
    server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()

    with pytest.raises(ToolError, match="read-only analysis tasks only"):
        _call_tool(
            server,
            "delegate_batch",
            {"project_name": "serena", "tasks": [_task(0, kind="code")]},
            "parent-session",
        )
