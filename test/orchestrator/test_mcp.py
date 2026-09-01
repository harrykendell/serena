"""Tests for the independent Orchestrator MCP skeleton."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.types import RequestParams

from orchestrator.config import OrchestratorConfig
from orchestrator.guidance import DELEGATION_GUIDE_RESOURCE_URI
from orchestrator.mcp import OrchestratorMCPFactory
from orchestrator.session import get_mcp_session_id
from serena.mcp import SerenaMCPFactory
from serena.tools import ToolRegistry

_ORCHESTRATOR_DELEGATE_TOOL_NAMES = {
    "create_delegate",
    "claim_delegate",
    "complete_delegate",
    "collect_delegate",
    "delegate_status",
    "delegate_cancel",
    "delegate_batch",
}


def _tool_names(server) -> set[str]:
    async def inspect() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    return asyncio.run(inspect())


@pytest.fixture
def orchestrator_config(tmp_path: Path) -> OrchestratorConfig:
    """Provides an isolated Orchestrator state root."""
    return OrchestratorConfig.from_environment(tmp_path / "orchestrator-home")


@pytest.fixture
def side_by_side_servers(monkeypatch: pytest.MonkeyPatch, orchestrator_config: OrchestratorConfig):
    """Provides independently constructed Serena and Orchestrator MCP servers."""
    serena_factory = SerenaMCPFactory(transport="stdio", context="chatgpt")
    monkeypatch.setattr("serena.mcp.SerenaConfig.from_config_file", lambda: MagicMock())
    monkeypatch.setattr(serena_factory, "_create_serena_agent", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(serena_factory, "_get_initial_instructions", lambda: "")

    return serena_factory.create_mcp_server(), OrchestratorMCPFactory(orchestrator_config).create_mcp_server()


def test_orchestrator_uses_openai_session_identity() -> None:
    """Orchestrator prefers the ChatGPT conversation identity from request metadata."""
    meta = RequestParams.Meta.model_validate({"openai/session": "conversation-123"})
    context = SimpleNamespace(request_context=SimpleNamespace(meta=meta), session=object())

    assert get_mcp_session_id(context) == "conversation-123"


def test_orchestrator_creates_only_its_own_state_layout(orchestrator_config: OrchestratorConfig) -> None:
    """Creating the server initializes only the Orchestrator-owned persistent layout."""
    OrchestratorMCPFactory(orchestrator_config).create_mcp_server()

    assert orchestrator_config.state_root.is_dir()
    assert orchestrator_config.delegates_dir.is_dir()
    assert orchestrator_config.batches_dir.is_dir()
    assert orchestrator_config.logs_dir.is_dir()
    assert orchestrator_config.worktrees_dir.is_dir()
    assert orchestrator_config.provider_state_dir.is_dir()


def test_orchestrator_exposes_delegation_routing_guide(orchestrator_config: OrchestratorConfig) -> None:
    """Orchestrator exposes route selection and both supported hand-off workflows."""

    async def inspect() -> tuple[set[str], str]:
        server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
        resources = {str(resource.uri) for resource in await server.list_resources()}
        contents = await server.read_resource(DELEGATION_GUIDE_RESOURCE_URI)
        return resources, contents[0].content

    resources, content = asyncio.run(inspect())

    assert DELEGATION_GUIDE_RESOURCE_URI in resources
    assert "## Route selection" in content
    assert "## Parent -> ChatGPT worker -> Serena -> Orchestrator" in content
    assert "## Parent -> Codex worktree -> review" in content
    assert "Orchestrator does not resolve Serena projects" in content
    assert "## Checkout ownership" in content
    assert "Serena work is live" in content
    assert "Codex code work is isolated" in content
    assert "Never instruct Codex to modify Serena's live checkout" in content
    assert "integration into the live checkout explicit" in content


def test_create_delegate_exposes_checkout_ownership(orchestrator_config: OrchestratorConfig) -> None:
    """The primary delegation tool tells agents which checkout each coding route may mutate."""

    async def inspect() -> str:
        server = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
        tools = await server.list_tools()
        return next(tool.description for tool in tools if tool.name == "create_delegate")

    description = asyncio.run(inspect())

    assert "Serena-backed ChatGPT work edits the live checkout" in description
    assert "Codex execution uses an Orchestrator-isolated Git worktree" in description
    assert "never route modifying Codex into the live checkout" in description


def test_serena_and_orchestrator_have_disjoint_tool_surfaces(side_by_side_servers) -> None:
    """The two MCPs expose independent namespaces with no cross-service tools."""
    serena_server, orchestrator_server = side_by_side_servers
    orchestrator_tools = _tool_names(orchestrator_server)
    serena_tool_names = set(ToolRegistry().get_tool_names())

    assert {"orchestrator_info", *_ORCHESTRATOR_DELEGATE_TOOL_NAMES} <= orchestrator_tools
    assert orchestrator_tools.isdisjoint(serena_tool_names)
    assert orchestrator_tools.isdisjoint(_tool_names(serena_server))
    assert _ORCHESTRATOR_DELEGATE_TOOL_NAMES.isdisjoint(serena_tool_names)


def test_recreating_either_server_preserves_orchestrator_state(
    monkeypatch: pytest.MonkeyPatch, orchestrator_config: OrchestratorConfig
) -> None:
    """Server recreation does not couple Serena lifecycle to Orchestrator persistence."""
    first_orchestrator = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()
    state_probe = orchestrator_config.delegates_dir / "persistence-probe.json"
    state_probe.write_text('{"delegate":"reserved-for-o03"}', encoding="utf-8")

    serena_factory = SerenaMCPFactory(transport="stdio", context="chatgpt")
    monkeypatch.setattr("serena.mcp.SerenaConfig.from_config_file", lambda: MagicMock())
    monkeypatch.setattr(serena_factory, "_create_serena_agent", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(serena_factory, "_get_initial_instructions", lambda: "")
    serena_factory.create_mcp_server()

    second_orchestrator = OrchestratorMCPFactory(orchestrator_config).create_mcp_server()

    assert first_orchestrator is not second_orchestrator
    assert state_probe.read_text(encoding="utf-8") == '{"delegate":"reserved-for-o03"}'
