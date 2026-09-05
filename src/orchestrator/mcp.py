"""MCP server factory for the independent Orchestrator service."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.server import Context, FastMCP, Settings
from mcp.types import ToolAnnotations
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from orchestrator.activity import ACTIVITY_RESOURCE_URI, OrchestratorActivityTracker, register_activity_resource
from orchestrator.batches import BatchTaskRequest, CreateDelegateBatchRequest, DelegateBatchError, DelegateBatchStore
from orchestrator.config import OrchestratorConfig
from orchestrator.dashboard_sessions import OrchestratorDashboardSessionArchive
from orchestrator.delegates import CreateDelegateRequest, DelegateError, DelegateKind, DelegateState, DelegateStore, ProviderPolicy
from orchestrator.guidance import DELEGATION_GUIDE_RESOURCE_URI, register_delegation_guide_resource
from orchestrator.providers import CodexCliProvider, DelegateProvider, ProviderRouter
from orchestrator.scheduler import AutoFallbackScheduler
from orchestrator.session import get_mcp_session_id


@dataclass(frozen=True)
class OrchestratorInfo:
    """Describes the Orchestrator service and its request-local identity."""

    service: str
    status: str
    session_id: str
    state_root: str


class OrchestratorMCPFactory:
    """Creates an Orchestrator MCP server with Orchestrator-owned state only."""

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self._config = config or OrchestratorConfig.from_environment()
        self._delegate_store = DelegateStore(self._config)
        self._dashboard_sessions = OrchestratorDashboardSessionArchive(self._config)
        self._batch_store = DelegateBatchStore(self._config, self._delegate_store)
        providers: list[DelegateProvider] = []
        if self._config.codex_enabled:
            providers.append(CodexCliProvider(self._config, self._delegate_store))
        self._providers = ProviderRouter(providers)
        self._scheduler = AutoFallbackScheduler(self._config, self._delegate_store, self._providers) if self._config.codex_enabled else None
        self._activity_tracker = OrchestratorActivityTracker(self._delegate_store)

    @property
    def config(self) -> OrchestratorConfig:
        """Returns the immutable Orchestrator configuration."""
        return self._config

    def close(self) -> None:
        """Stops Orchestrator-owned background scheduling for this factory."""
        if self._scheduler is not None:
            self._scheduler.close()

    def create_mcp_server(
        self,
        host: str = "127.0.0.1",
        port: int = 8100,
        streamable_http_path: str = "/mcp",
    ) -> FastMCP:
        """Creates the independent Orchestrator MCP server.

        :param host: host to bind for network transports.
        :param port: port to bind for network transports.
        :param streamable_http_path: Streamable HTTP endpoint path exposed by the server.
        :return: configured FastMCP server.
        """
        self._config.ensure_state_layout()

        # prevent project-local .env files from changing MCP process settings
        Settings.model_config = SettingsConfigDict(env_prefix="FASTMCP_")
        mcp = FastMCP(
            name="Orchestrator",
            host=host,
            port=port,
            streamable_http_path=streamable_http_path,
            instructions=(
                "Orchestrator is an independent delegation service for bounded human-run ChatGPT delegates. "
                "Codex execution and automatic provider fallback are currently disabled. "
                "Use Serena separately for coding/project/jobs work inside each worker Chat. "
                f"Read {DELEGATION_GUIDE_RESOURCE_URI} for route selection and hand-off examples."
            ),
        )
        register_activity_resource(mcp)
        register_delegation_guide_resource(mcp)
        self._register_info_tools(mcp)
        self._register_delegate_tools(mcp)
        self._register_activity_tools(mcp)
        return mcp

    def _register_info_tools(self, mcp: FastMCP) -> None:
        """Registers the minimal O02 health and service-information surface."""

        @mcp.tool(
            name="orchestrator_info",
            title="Orchestrator Info",
            description="Returns Orchestrator health, request session identity, and state root.",
        )
        def orchestrator_info(mcp_ctx: Context) -> dict[str, Any]:
            info = OrchestratorInfo(
                service="orchestrator",
                status="ok",
                session_id=get_mcp_session_id(mcp_ctx),
                state_root=str(self._config.state_root),
            )
            return asdict(info)

    def _register_delegate_tools(self, mcp: FastMCP) -> None:
        """Registers the bounded human-run ChatGPT delegate lifecycle surface."""

        @mcp.tool(
            name="create_delegate",
            title="Create Delegate",
            description=(
                "Creates a bounded delegate for a human-run ChatGPT worker. Use provider_policy=chat; Codex execution "
                "and automatic fallback are currently disabled. If the task modifies code, the worker should use Serena "
                "in its own ChatGPT conversation, which edits the active project's live checkout."
            ),
        )
        def create_delegate(
            project_name: str,
            kind: DelegateKind,
            goal: str,
            acceptance_criteria: list[str],
            mcp_ctx: Context,
            provider_policy: ProviderPolicy = ProviderPolicy.CHAT,
            project_root: str | None = None,
            known_context: list[str] | None = None,
            scope: list[str] | None = None,
            out_of_scope: list[str] | None = None,
            verification: list[str] | None = None,
            parent_notes: str = "",
            base_revision: str | None = None,
            result_budget_chars: int = 6_000,
        ) -> dict[str, Any]:
            session_id = get_mcp_session_id(mcp_ctx)
            try:
                if not self._config.codex_enabled and provider_policy != ProviderPolicy.CHAT:
                    raise ValueError(
                        "Codex delegation is currently disabled; use provider_policy=chat and run the returned prompt "
                        "in a human-run ChatGPT conversation."
                    )
                request = CreateDelegateRequest(
                    project_name=project_name,
                    project_root=project_root,
                    kind=kind,
                    provider_policy=provider_policy,
                    goal=goal,
                    known_context=known_context or [],
                    scope=scope or [],
                    out_of_scope=out_of_scope or [],
                    acceptance_criteria=acceptance_criteria,
                    verification=verification or [],
                    parent_notes=parent_notes,
                    base_revision=base_revision,
                    result_budget_chars=result_budget_chars,
                )
                response = self._delegate_store.create(session_id, request)
                self._activity_tracker.note_delegate(session_id, response.delegate_id)
                self._providers.start(response.provider_policy, response.delegate_id)
                if response.provider_policy == ProviderPolicy.AUTO and self._scheduler is not None:
                    self._scheduler.notify()
                return response.model_dump(mode="json")
            except (DelegateError, ValidationError, ValueError) as exc:
                raise ToolError(str(exc)) from exc

        @mcp.tool(
            name="delegate_batch",
            title="Delegate Batch",
            description=(
                "Creates or refreshes a bounded read-only fan-out/fan-in batch. Omit batch_id and provide project_name/tasks "
                "to create a ChatGPT-first batch; provide batch_id alone to refresh it, expose newly available slots, and "
                "return a compact aggregate of terminal typed results. Default concurrency is two."
            ),
        )
        def delegate_batch(
            mcp_ctx: Context,
            batch_id: str | None = None,
            project_name: str | None = None,
            tasks: list[BatchTaskRequest] | None = None,
            provider_policy: ProviderPolicy = ProviderPolicy.CHAT,
            project_root: str | None = None,
            concurrency: int = 2,
            result_budget_chars: int = 6_000,
        ) -> dict[str, Any]:
            session_id = get_mcp_session_id(mcp_ctx)
            try:
                if not self._config.codex_enabled and provider_policy != ProviderPolicy.CHAT:
                    raise ValueError(
                        "Codex delegation is currently disabled; use provider_policy=chat and run each returned prompt "
                        "in a human-run ChatGPT conversation."
                    )
                if batch_id is None:
                    if project_name is None or tasks is None:
                        raise DelegateBatchError("Creating a delegate batch requires project_name and tasks.")
                    request = CreateDelegateBatchRequest(
                        project_name=project_name,
                        project_root=project_root,
                        tasks=tasks,
                        provider_policy=provider_policy,
                        concurrency=concurrency,
                        result_budget_chars=result_budget_chars,
                    )
                    response = self._batch_store.create(session_id, request)
                else:
                    if project_name is not None or tasks is not None:
                        raise DelegateBatchError("Refreshing a delegate batch accepts batch_id without new project_name/tasks.")
                    response = self._batch_store.refresh(batch_id, session_id)

                automatic = False
                for launch in response.launches:
                    self._activity_tracker.note_delegate(session_id, launch.delegate_id)
                    self._providers.start(launch.provider_policy, launch.delegate_id)
                    automatic = automatic or launch.provider_policy == ProviderPolicy.AUTO
                if automatic and self._scheduler is not None:
                    self._scheduler.notify()
                return response.model_dump(mode="json")
            except (DelegateBatchError, DelegateError, ValidationError, ValueError) as exc:
                raise ToolError(str(exc)) from exc

        @mcp.tool(
            name="claim_delegate",
            title="Claim Delegate",
            description="Atomically claims a waiting ChatGPT delegate for this session and returns its bounded task packet.",
        )
        def claim_delegate(delegate_id: str, mcp_ctx: Context) -> dict[str, Any]:
            session_id = get_mcp_session_id(mcp_ctx)
            try:
                response = self._delegate_store.claim(delegate_id, session_id)
                self._activity_tracker.note_delegate(session_id, delegate_id)
                return response.model_dump(mode="json")
            except DelegateError as exc:
                raise ToolError(str(exc)) from exc

        @mcp.tool(
            name="complete_delegate",
            title="Complete Delegate",
            description="Validates and persists the bounded typed result from the ChatGPT session that claimed the delegate.",
        )
        def complete_delegate(delegate_id: str, result: dict[str, Any], mcp_ctx: Context) -> dict[str, Any]:
            session_id = get_mcp_session_id(mcp_ctx)
            try:
                response = self._delegate_store.complete(delegate_id, session_id, result)
                self._activity_tracker.note_delegate(session_id, delegate_id)
                return response.model_dump(mode="json")
            except DelegateError as exc:
                raise ToolError(str(exc)) from exc

        @mcp.tool(
            name="collect_delegate",
            title="Collect Delegate",
            description="Returns only a completed or failed delegate's bounded typed result to the ChatGPT session that created it.",
        )
        def collect_delegate(delegate_id: str, mcp_ctx: Context) -> dict[str, Any]:
            session_id = get_mcp_session_id(mcp_ctx)
            try:
                response = self._delegate_store.collect(delegate_id, session_id)
                self._activity_tracker.note_delegate(session_id, delegate_id)
                return response.model_dump(mode="json")
            except DelegateError as exc:
                raise ToolError(str(exc)) from exc

        @mcp.tool(
            name="delegate_status",
            title="Delegate Status",
            description="Returns compact lifecycle state for a delegate owned by the current parent or worker session.",
        )
        def delegate_status(delegate_id: str, mcp_ctx: Context) -> dict[str, Any]:
            session_id = get_mcp_session_id(mcp_ctx)
            try:
                response = self._delegate_store.status(delegate_id, session_id)
                self._activity_tracker.note_delegate(session_id, delegate_id)
                return response.model_dump(mode="json")
            except DelegateError as exc:
                raise ToolError(str(exc)) from exc

        if self._config.codex_enabled:

            @mcp.tool(
                name="delegate_reroute",
                title="Reroute Delegate",
                description=(
                    "Explicitly changes the provider policy while a delegate is still waiting for ChatGPT. "
                    "Use codex to start unattended work now, chat to disable fallback, or auto to restart the claim window."
                ),
                annotations=ToolAnnotations(title="Reroute Delegate", readOnlyHint=False, destructiveHint=False),
                meta={
                    "ui": {"visibility": ["model", "app"]},
                    "openai/widgetAccessible": True,
                },
                structured_output=True,
            )
            def delegate_reroute(delegate_id: str, provider_policy: ProviderPolicy, mcp_ctx: Context) -> dict[str, Any]:
                session_id = get_mcp_session_id(mcp_ctx)
                try:
                    response = self._delegate_store.reroute_waiting(delegate_id, session_id, provider_policy)
                    if response.state == DelegateState.QUEUED:
                        self._providers.start(response.provider_policy, delegate_id)
                    elif response.provider_policy == ProviderPolicy.AUTO and self._scheduler is not None:
                        self._scheduler.notify()
                    self._activity_tracker.note_delegate(session_id, delegate_id)
                    return response.model_dump(mode="json")
                except DelegateError as exc:
                    raise ToolError(str(exc)) from exc

        @mcp.tool(
            name="delegate_cancel",
            title="Cancel Delegate",
            description="Cancels active delegate work owned by this parent or worker session.",
        )
        def delegate_cancel(delegate_id: str, mcp_ctx: Context, reason: str = "") -> dict[str, Any]:
            session_id = get_mcp_session_id(mcp_ctx)
            try:
                before = self._delegate_store.status(delegate_id, session_id)
                response = self._delegate_store.cancel(delegate_id, session_id, reason)
                self._providers.cancel(before.provider_policy, delegate_id)
                self._activity_tracker.note_delegate(session_id, delegate_id)
                return response.model_dump(mode="json")
            except DelegateError as exc:
                raise ToolError(str(exc)) from exc

    def _register_activity_tools(self, mcp: FastMCP) -> None:
        """Registers the Orchestrator activity widget and app-private polling tools."""

        @mcp.tool(
            name="show_orchestrator_activity",
            title="Show Orchestrator Activity",
            description=(
                "Shows a compact live Orchestrator delegate panel for this ChatGPT session. Supply conversation_title "
                "as a concise 3-8 word description of the current ChatGPT conversation, inferred from conversation context; "
                "refresh it whenever this panel is reopened if the conversation focus has materially changed. Call it once "
                "before a multi-step delegation workflow."
            ),
            annotations=ToolAnnotations(title="Show Orchestrator Activity", readOnlyHint=True, destructiveHint=False),
            meta={
                "ui": {"resourceUri": ACTIVITY_RESOURCE_URI, "visibility": ["model", "app"]},
                "openai/outputTemplate": ACTIVITY_RESOURCE_URI,
                "openai/widgetAccessible": True,
                "openai/toolInvocation/invoking": "Opening Orchestrator activity...",
                "openai/toolInvocation/invoked": "Orchestrator activity",
            },
            structured_output=True,
        )
        async def show_orchestrator_activity(conversation_title: str, mcp_ctx: Context) -> dict[str, Any]:
            session_id = get_mcp_session_id(mcp_ctx)
            await asyncio.to_thread(self._dashboard_sessions.set_display_name, session_id, conversation_title)
            return await asyncio.to_thread(self._activity_tracker.start_run, session_id)

        @mcp.tool(
            name="get_orchestrator_activity",
            title="Get Orchestrator Activity",
            description="Returns one Orchestrator activity-panel snapshot. Intended for the activity app only.",
            annotations=ToolAnnotations(title="Get Orchestrator Activity", readOnlyHint=True, destructiveHint=False),
            meta={
                "ui": {"visibility": ["app"]},
                "openai/widgetAccessible": True,
                "openai/visibility": "private",
            },
            structured_output=True,
        )
        async def get_orchestrator_activity(run_id: str, mcp_ctx: Context) -> dict[str, Any]:
            try:
                return await asyncio.to_thread(self._activity_tracker.get_run, get_mcp_session_id(mcp_ctx), run_id)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc

        @mcp.tool(
            name="get_orchestrator_delegate_detail",
            title="Get Orchestrator Delegate Detail",
            description="Returns authorized private detail and audit history for one delegate. Intended for the activity app only.",
            annotations=ToolAnnotations(title="Get Orchestrator Delegate Detail", readOnlyHint=True, destructiveHint=False),
            meta={
                "ui": {"visibility": ["app"]},
                "openai/widgetAccessible": True,
                "openai/visibility": "private",
            },
            structured_output=True,
        )
        def get_orchestrator_delegate_detail(delegate_id: str, mcp_ctx: Context) -> dict[str, Any]:
            try:
                detail = self._delegate_store.private_detail(delegate_id, get_mcp_session_id(mcp_ctx))
                return detail.model_dump(mode="json")
            except DelegateError as exc:
                raise ToolError(str(exc)) from exc
