"""Behavior tests for the Orchestrator-only ChatGPT delegate panel."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from orchestrator.activity import ACTIVITY_RESOURCE_URI, OrchestratorActivityTracker, register_activity_resource
from orchestrator.config import OrchestratorConfig
from orchestrator.delegates import CreateDelegateRequest, DelegateKind, DelegateStore
from orchestrator.mcp import OrchestratorMCPFactory


def _request(goal: str) -> CreateDelegateRequest:
    return CreateDelegateRequest(
        project_name="serena",
        kind=DelegateKind.EXPLORE,
        goal=goal,
        acceptance_criteria=["Return a concise finding."],
    )


@pytest.fixture
def orchestrator_config(tmp_path: Path) -> OrchestratorConfig:
    """Provides isolated persistent state for one activity test."""
    return OrchestratorConfig.from_environment(tmp_path / "orchestrator-home")


def test_activity_runs_are_session_owned_and_superseded_without_cross_absorption(orchestrator_config: OrchestratorConfig) -> None:
    """New panels supersede only their own session and old panels do not absorb later delegates."""
    store = DelegateStore(orchestrator_config)
    first_delegate = store.create("parent-a", _request("Inspect first task."))
    other_delegate = store.create("parent-b", _request("Inspect unrelated task."))
    tracker = OrchestratorActivityTracker(store)

    first_run = tracker.start_run("parent-a")
    other_run = tracker.start_run("parent-b")
    second_run = tracker.start_run("parent-a")

    later_delegate = store.create("parent-a", _request("Inspect later task."))
    tracker.note_delegate("parent-a", later_delegate.delegate_id)

    old_snapshot = tracker.get_run("parent-a", first_run["run_id"])
    current_snapshot = tracker.get_run("parent-a", second_run["run_id"])
    other_snapshot = tracker.get_run("parent-b", other_run["run_id"])

    assert old_snapshot["superseded"] is True
    assert {item["delegate_id"] for item in old_snapshot["delegates"]} == {first_delegate.delegate_id}
    assert {item["delegate_id"] for item in current_snapshot["delegates"]} == {
        first_delegate.delegate_id,
        later_delegate.delegate_id,
    }
    assert {item["delegate_id"] for item in other_snapshot["delegates"]} == {other_delegate.delegate_id}

    store.cancel(first_delegate.delegate_id, "parent-a")
    terminal_old_snapshot = tracker.get_run("parent-a", first_run["run_id"])
    assert len(terminal_old_snapshot["delegates"]) == 1
    assert terminal_old_snapshot["delegates"][0]["state"] == "CANCELLED"


def test_activity_resource_is_orchestrator_specific_and_uses_private_detail_tools() -> None:
    """The widget has its own resource and polls only Orchestrator-specific helper tools."""

    async def inspect_resource() -> tuple[object, object]:
        mcp = FastMCP("orchestrator-activity-test")
        register_activity_resource(mcp)
        resources = await mcp.list_resources()
        contents = await mcp.read_resource(ACTIVITY_RESOURCE_URI)
        return resources[0], contents[0]

    resource, content = asyncio.run(inspect_resource())

    assert str(resource.uri) == ACTIVITY_RESOURCE_URI
    assert resource.mimeType == "text/html;profile=mcp-app"
    assert resource.meta == {
        "ui": {"prefersBorder": True},
        "openai/widgetDescription": "Shows Orchestrator delegates, lifecycle state, and private on-demand detail.",
    }
    assert content.mime_type == "text/html;profile=mcp-app"
    assert 'id="orchestrator-activity"' in content.content
    assert 'window.openai.callTool("get_orchestrator_activity"' in content.content
    assert 'window.openai.callTool("get_orchestrator_delegate_detail"' in content.content
    assert "get_activity_job_detail" not in content.content
    assert "Serena" not in content.content


def test_activity_tools_use_orchestrator_specific_widget_and_private_polling_contract(
    orchestrator_config: OrchestratorConfig,
) -> None:
    """Only the panel opener is model-visible; polling and private detail stay app-only."""

    async def inspect_tools() -> dict[str, object]:
        server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
        return {tool.name: tool for tool in await server.list_tools()}

    tools = asyncio.run(inspect_tools())
    show_meta = tools["show_orchestrator_activity"].meta
    poll_meta = tools["get_orchestrator_activity"].meta
    detail_meta = tools["get_orchestrator_delegate_detail"].meta

    assert show_meta is not None
    assert show_meta["ui"] == {"resourceUri": ACTIVITY_RESOURCE_URI, "visibility": ["model", "app"]}
    assert show_meta["openai/outputTemplate"] == ACTIVITY_RESOURCE_URI
    assert poll_meta is not None
    assert poll_meta["ui"] == {"visibility": ["app"]}
    assert poll_meta["openai/visibility"] == "private"
    assert detail_meta is not None
    assert detail_meta["ui"] == {"visibility": ["app"]}
    assert detail_meta["openai/visibility"] == "private"
