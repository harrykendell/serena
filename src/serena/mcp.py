"""
The Serena Model Context Protocol (MCP) Server
"""

import asyncio
import base64
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, cast

import docstring_parser
from mcp.server.fastmcp import server
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.server import Context, FastMCP, Settings
from mcp.server.fastmcp.tools.base import Tool as FastMCPTool
from mcp.server.session import ServerSessionT
from mcp.shared.context import LifespanContextT, RequestT
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp.types import AudioContent, CallToolResult, ContentBlock, Icon, ImageContent, ResourceLink, TextContent, ToolAnnotations
from pydantic import AnyUrl, ValidationError
from pydantic_settings import SettingsConfigDict
from sensai.util import logging

from serena.activity import ACTIVITY_RESOURCE_URI, ActivityRunManager, register_activity_resource
from serena.activity_transport import activity_call_detail_payload, activity_job_detail_payload, activity_snapshot_payload
from serena.activity_view import ActivityView
from serena.agent import (
    SerenaAgent,
)
from serena.chatgpt_policy import CHATGPT_TOOL_DESCRIPTION_OVERRIDES
from serena.config.serena_config import SerenaConfig
from serena.constants import SERENA_LOG_FORMAT
from serena.errors import UserFacingError
from serena.execution import ExecutionAccess, bind_execution_id, get_current_execution_id, reset_execution_id
from serena.execution_metadata import ExecutionResultMetadata, extract_execution_result_metadata
from serena.result_presentation import ToolResultPresentation, ToolResultPresenter
from serena.session import get_mcp_session_id
from serena.tools import Tool, ToolMarkerExplicitResultPaging
from serena.tools.media_tools import read_result_file_link, register_file_export_resource
from serena.util.exception import show_fatal_exception_safe

log = logging.getLogger(__name__)


_SERVER_ICON_PATH = Path(__file__).parent / "resources" / "kendell_dashboard" / "serena-icon-128.png"


def _server_icons() -> list[Icon]:
    """Returns the Serena server icon embedded as a credential-free data URI."""
    encoded = base64.b64encode(_SERVER_ICON_PATH.read_bytes()).decode("ascii")
    return [Icon(src=f"data:image/png;base64,{encoded}", mimeType="image/png", sizes=["128x128"])]


def configure_logging(*args, **kwargs) -> None:
    # We only do something here if logging has not yet been configured.
    # Normally, logging is configured in the MCP server startup script.
    if not logging.is_enabled():
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format=SERENA_LOG_FORMAT)


# patch the logging configuration function in fastmcp, because it's hard-coded and broken
server.configure_logging = configure_logging  # type: ignore


_MAX_EXTERNAL_ERROR_CHARS = 4000


def _bound_external_error(text: str) -> str:
    """Bounds one model-visible error string without changing its semantic content."""
    message = text.strip()
    if len(message) <= _MAX_EXTERNAL_ERROR_CHARS:
        return message
    return f"{message[: _MAX_EXTERNAL_ERROR_CHARS - 3]}..."


def _format_validation_error(error: ValidationError) -> str:
    """Formats Pydantic validation entries as one compact agent-recovery message."""
    details: list[str] = []
    for entry in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in entry.get("loc", ())) or "arguments"
        message = str(entry.get("msg", "invalid value")).strip()
        details.append(f"{location} {message}")
    return _bound_external_error(f"Invalid arguments: {'; '.join(details)}.")


def _format_unexpected_error(error: Exception) -> str:
    """Formats one bounded internal failure without traceback or wrapper nesting."""
    first_line = next((line.strip() for line in str(error).splitlines() if line.strip()), "")
    if first_line:
        return _bound_external_error(f"{error.__class__.__name__}: {first_line}")
    return error.__class__.__name__


class SerenaFastMCPTool(FastMCPTool):
    def __init__(
        self,
        tool: Tool,
        openai_tool_compatible: bool,
        structured_output: bool | None,
        activity_run_manager: ActivityRunManager | None = None,
    ):
        """
        :param tool: the Serena tool
        :param openai_tool_compatible: whether to process the tool schema to be compatible with OpenAI tools
            (doesn't accept integer, needs number instead, etc.). This allows using Serena MCP within Codex.
        :param structured_output: whether to use structured output for the tool (None = auto)
        """
        func_name = tool.get_name()
        func_doc = tool.get_apply_docstring() or ""
        func_arg_metadata = tool.get_apply_fn_metadata(structured_output=structured_output)
        if not isinstance(tool, ToolMarkerExplicitResultPaging):
            func_arg_metadata.output_schema = ToolResultPresenter.extend_output_schema(func_arg_metadata.output_schema)
        is_async = False
        parameters = func_arg_metadata.arg_model.model_json_schema()
        if openai_tool_compatible:
            parameters = SerenaMCPFactory._sanitize_for_openai_tools(parameters)

        docstring = docstring_parser.parse(func_doc)

        # mount the fixed ChatGPT tool description override, if any
        overridden_description = CHATGPT_TOOL_DESCRIPTION_OVERRIDES.get(func_name)

        if overridden_description is not None:
            func_doc = overridden_description
        elif docstring.description:
            func_doc = docstring.description
        else:
            func_doc = ""
        func_doc = func_doc.strip().strip(".")
        if func_doc:
            func_doc += "."
        if docstring.returns and (docstring_returns_descr := docstring.returns.description):
            prefix = " " if func_doc else ""
            func_doc = f"{func_doc}{prefix}Returns {docstring_returns_descr.strip().strip('.')}."

        # add parameter descriptions from the parsed docstring
        docstring_params = {param.arg_name: param for param in docstring.params}
        parameters_properties: dict[str, dict[str, Any]] = parameters["properties"]
        for parameter, properties in parameters_properties.items():
            if (param_doc := docstring_params.get(parameter)) and param_doc.description:
                param_desc = f"{param_doc.description.strip().strip('.') + '.'}"
                properties["description"] = param_desc[0].upper() + param_desc[1:]

        def execute_fn(**kwargs) -> Any:
            execution_id = get_current_execution_id()
            if execution_id is not None:
                kwargs["execution_id"] = execution_id
            return tool.apply_ex(**kwargs)

        # derive a readable title and MCP capability hints
        tool_title = " ".join(word.capitalize() for word in func_name.split("_"))
        can_edit = tool.can_edit()
        annotations = ToolAnnotations(
            title=tool_title,
            readOnlyHint=not can_edit,
            destructiveHint=can_edit,
        )

        super().__init__(
            fn=execute_fn,
            name=func_name,
            description=func_doc,
            parameters=parameters,
            fn_metadata=func_arg_metadata,
            is_async=is_async,
            # keep the value in sync with the kwarg name in Tool.apply_ex
            context_kwarg="mcp_ctx",
            annotations=annotations,
            title=tool_title,
            meta=tool.get_mcp_tool_meta(),
        )

        self._tool = tool
        self._param_aliases = tool.get_param_aliases()
        self._activity_run_manager = activity_run_manager
        self._agent = tool.agent
        self._execution_access = tool.get_execution_access()
        self._execution_store = activity_run_manager.execution_store if activity_run_manager is not None else tool.agent.execution_store

    def _present_result(self, logical_result: object) -> ToolResultPresentation:
        """Presents one complete logical result at the MCP execution boundary."""
        max_chars = int(self._agent.serena_config.default_max_tool_answer_tokens) * 4
        presenter = ToolResultPresenter(self._agent.tool_output_store, max_chars=max_chars)
        return presenter.present(
            logical_result,
            semantic_paging=isinstance(self._tool, ToolMarkerExplicitResultPaging),
        )

    def _convert_presented_result(self, presentation: ToolResultPresentation, prepared_result: object) -> object:
        """Converts one canonical presentation to FastMCP's model-facing result shape."""
        # native MCP results already own their content representation
        if isinstance(prepared_result, CallToolResult):
            return self.fn_metadata.convert_result(prepared_result)

        # ordinary content uses the exact serialization measured by the central presenter
        persisted = presentation.persisted_serialization
        if persisted is None:
            raise TypeError("Ordinary presented result must have a persisted serialization")
        transport_value = presentation.transport_value
        transport_text = transport_value if isinstance(transport_value, str) else persisted
        content: list[ContentBlock] = [TextContent(type="text", text=transport_text)]

        # ordinary truncation uses the canonical envelope; pathological budgets may only fit a smaller JSON value
        if presentation.retained_output_id is not None:
            canonical_envelope = False
            if isinstance(transport_value, dict):
                envelope = cast(dict[str, object], transport_value)
                canonical_envelope = envelope.get("truncated") is True and {
                    "total_chars",
                    "output_id",
                    "result",
                }.issubset(envelope)
            if self.fn_metadata.output_schema is None:
                return content
            if not canonical_envelope:
                return CallToolResult(content=content, isError=False)
            return content, transport_value

        # preserve FastMCP's original structured-output validation/wrapping without its alternate text rendering
        if self.fn_metadata.output_schema is None:
            return content
        structured_value = {"result": prepared_result} if self.fn_metadata.wrap_output else prepared_result
        assert self.fn_metadata.output_model is not None, "Output model must be set if output schema is defined"
        validated = self.fn_metadata.output_model.model_validate(structured_value)
        structured_content = validated.model_dump(mode="json", by_alias=True)
        return content, structured_content

    async def run(
        self,
        arguments: dict[str, Any],
        context: Context[ServerSessionT, LifespanContextT, RequestT] | None = None,
        convert_result: bool = False,
    ) -> Any:
        # apply parameter aliases before validation, persistence and activity display
        for param_alias, param_name in self._param_aliases.items():
            if param_alias in arguments and param_name not in arguments:
                arguments[param_name] = arguments.pop(param_alias)

        # create the authoritative execution before leaving the MCP event loop
        session_id = get_mcp_session_id(context)
        project = self._agent.get_active_project_for_session(session_id)
        submission_project_name = project.project_name if project is not None else ""
        execution_id = uuid.uuid4().hex
        execution_store = self._execution_store
        with execution_store.batch_updates():
            execution_store.start_execution(
                execution_id=execution_id,
                session_id=session_id,
                project_name=submission_project_name,
                tool_name=self.name,
                arguments=arguments,
            )
            if self._activity_run_manager is not None:
                if submission_project_name:
                    self._activity_run_manager.update_project(session_id, submission_project_name)
                self._activity_run_manager.associate_execution(
                    session_id,
                    execution_id,
                    project_name=submission_project_name,
                )

        def finish_execution(
            *,
            succeeded: bool,
            presentation: ToolResultPresentation | None = None,
            error: str | None = None,
            project_name: str | None = None,
            result_metadata: ExecutionResultMetadata | None = None,
        ) -> None:
            metadata = result_metadata or ExecutionResultMetadata(media=None, durable_job_id=None, durable_job_label=None)
            media = metadata.media if succeeded else None
            durable_job_id = metadata.durable_job_id if succeeded else None
            durable_job_label = metadata.durable_job_label if succeeded else None
            result_serialization = presentation.persisted_serialization if succeeded and presentation is not None else None
            retained_output_id = presentation.retained_output_id if succeeded and presentation is not None else None
            retained_output_chars = presentation.retained_output_chars if succeeded and presentation is not None else None

            with execution_store.batch_updates():
                if self._activity_run_manager is not None and project_name:
                    self._activity_run_manager.update_project(session_id, project_name)
                execution_store.finish_execution(
                    execution_id,
                    succeeded=succeeded,
                    result=result_serialization if media is None else None,
                    error=error,
                    project_name=project_name,
                    retained_output_id=retained_output_id,
                    retained_output_chars=retained_output_chars,
                    media=media.storage_dict() if media is not None else None,
                    durable_job_id=durable_job_id,
                    durable_job_label=durable_job_label,
                )

        def completed_project_name() -> str:
            current_project = self._agent.get_active_project_for_session(session_id)
            current_project_name = current_project.project_name if current_project is not None else ""
            return current_project_name if self.name == "activate_project" else submission_project_name

        def finish_unexpected(error: Exception, *, project_name: str) -> str:
            message = _format_unexpected_error(error)
            log.error("Unexpected error executing tool %s: %s", self.name, error, exc_info=error)
            finish_execution(succeeded=False, error=message, project_name=project_name)
            return message

        def detach_worker(worker_task: asyncio.Task[Any], request_error: str) -> None:
            """Keeps execution live until an abandoned request's worker actually stops."""
            execution_store.mark_request_abandoned(execution_id, error=request_error)

            def finalize_detached_worker(completed: asyncio.Task[Any]) -> None:
                try:
                    completed.result()
                except BaseException as worker_error:
                    log.debug(
                        "Detached worker for execution %s ended with %s: %s",
                        execution_id,
                        worker_error.__class__.__name__,
                        worker_error,
                    )
                finish_execution(
                    succeeded=False,
                    error=request_error,
                    project_name=completed_project_name(),
                )

            worker_task.add_done_callback(finalize_detached_worker)

        # propagate the execution identifier through the FastMCP worker thread
        execution_token = bind_execution_id(execution_id)
        try:
            try:
                arguments_pre_parsed = self.fn_metadata.pre_parse_json(arguments)
                arguments_model = self.fn_metadata.arg_model.model_validate(arguments_pre_parsed)
                arguments_parsed = arguments_model.model_dump_one_level()
                if self.context_kwarg is not None:
                    arguments_parsed[self.context_kwarg] = context

                if self._execution_access is ExecutionAccess.SESSION_CONTROL:
                    worker_task = asyncio.create_task(asyncio.to_thread(self.fn, **arguments_parsed))
                else:
                    with self._agent.submission_project_context(session_id):
                        worker_task = asyncio.create_task(asyncio.to_thread(self.fn, **arguments_parsed))
            except ValidationError as error:
                message = _format_validation_error(error)
                finish_execution(succeeded=False, error=message, project_name=submission_project_name)
                raise ToolError(message) from None
            except UrlElicitationRequiredError:
                finish_execution(succeeded=False, project_name=submission_project_name)
                raise
            except ToolError as error:
                finish_execution(succeeded=False, error=str(error), project_name=submission_project_name)
                raise
            except UserFacingError as error:
                message = str(error)
                finish_execution(succeeded=False, error=message, project_name=submission_project_name)
                raise ToolError(message) from None
            except Exception as error:
                message = finish_unexpected(error, project_name=submission_project_name)
                raise ToolError(message) from None
            except BaseException:
                finish_execution(succeeded=False, project_name=submission_project_name)
                raise

            try:
                result = await asyncio.wait_for(
                    asyncio.shield(worker_task),
                    timeout=self._agent.serena_config.tool_timeout,
                )
            except TimeoutError:
                message = f"Tool execution timed out after {self._agent.serena_config.tool_timeout} seconds."
                detach_worker(worker_task, message)
                raise ToolError(message) from None
            except asyncio.CancelledError:
                message = "MCP request was cancelled while the tool worker was still running."
                detach_worker(worker_task, message)
                raise
            except UrlElicitationRequiredError:
                finish_execution(succeeded=False, project_name=submission_project_name)
                raise
            except ToolError as error:
                finish_execution(succeeded=False, error=str(error), project_name=submission_project_name)
                raise
            except UserFacingError as error:
                message = str(error)
                finish_execution(succeeded=False, error=message, project_name=submission_project_name)
                raise ToolError(message) from None
            except Exception as error:
                message = finish_unexpected(error, project_name=submission_project_name)
                raise ToolError(message) from None
            except BaseException:
                finish_execution(succeeded=False, project_name=submission_project_name)
                raise

            logical_result = result
            try:
                result_metadata = extract_execution_result_metadata(logical_result)
                presentation = self._present_result(logical_result)
                prepared_result = self._tool.prepare_mcp_result(presentation.transport_value)
                result = self._convert_presented_result(presentation, prepared_result) if convert_result else prepared_result
            except Exception as error:
                message = finish_unexpected(error, project_name=completed_project_name())
                raise ToolError(message) from None

            finish_execution(
                succeeded=True,
                presentation=presentation,
                project_name=completed_project_name(),
                result_metadata=result_metadata,
            )
            return result
        finally:
            reset_execution_id(execution_token)


class SerenaMCPFactory:
    """
    Factory for the creation of the Serena MCP server with an associated SerenaAgent.
    """

    def __init__(
        self,
        transport: Literal["stdio", "sse", "streamable-http"],
        project: str | None = None,
    ):
        """
        Creates the fixed ChatGPT Serena MCP runtime.

        :param transport: transport to use for the MCP server
        :param project: absolute project path or registered project name to activate at startup
        """
        self.transport = transport
        self.project = project
        self.agent: SerenaAgent | None = None
        self._activity_run_manager: ActivityRunManager | None = None
        self._activity_view: ActivityView | None = None

    @staticmethod
    def _sanitize_for_openai_tools(schema: dict) -> dict:
        """
        This method was written by GPT-5, I have not reviewed it in detail.
        Only called when `openai_tool_compatible` is True.

        Make a Pydantic/JSON Schema object compatible with OpenAI tool schema.
        - 'integer' -> 'number' (+ multipleOf: 1)
        - remove 'null' from union type arrays
        - coerce integer-only enums to number
        - best-effort simplify oneOf/anyOf when they only differ by integer/number
        """
        s = deepcopy(schema)

        def walk(node):
            if not isinstance(node, dict):
                # lists get handled by parent calls
                return node

            # ---- handle type ----
            t = node.get("type")
            if isinstance(t, str):
                if t == "integer":
                    node["type"] = "number"
                    # preserve existing multipleOf but ensure it's integer-like
                    if "multipleOf" not in node:
                        node["multipleOf"] = 1
            elif isinstance(t, list):
                # remove 'null' (OpenAI tools don't support nullables)
                t2 = [x if x != "integer" else "number" for x in t if x != "null"]
                if not t2:
                    # fall back to object if it somehow becomes empty
                    t2 = ["object"]
                node["type"] = t2[0] if len(t2) == 1 else t2
                if "integer" in t or "number" in t2:
                    # if integers were present, keep integer-like restriction
                    node.setdefault("multipleOf", 1)

            # ---- enums of integers -> number ----
            if "enum" in node and isinstance(node["enum"], list):
                vals = node["enum"]
                if vals and all(isinstance(v, int) for v in vals):
                    node.setdefault("type", "number")
                    # keep them as ints; JSON 'number' covers ints
                    node.setdefault("multipleOf", 1)

            # ---- normalize anyOf/oneOf unions for OpenAI-compatible schemas ----
            for key in ("oneOf", "anyOf"):
                if key in node and isinstance(node[key], list):
                    simplified = [walk(sub) for sub in node[key]]

                    # optional parameters are omitted rather than sent as JSON null
                    non_null = [sub for sub in simplified if sub.get("type") != "null"]
                    if non_null:
                        simplified = non_null

                    # collapse a single remaining branch into the parent schema
                    if len(simplified) == 1:
                        only = simplified[0]
                        node.pop(key, None)
                        for k, v in only.items():
                            if k not in node:
                                node[k] = v
                        continue

                    # collapse branches that become identical after recursive normalization
                    changed = False
                    try:
                        import json

                        canon = [json.dumps(x, sort_keys=True) for x in simplified]
                        if len(set(canon)) == 1:
                            only = simplified[0]
                            node.pop(key, None)
                            for k, v in only.items():
                                if k not in node:
                                    node[k] = v
                            changed = True
                    except Exception:
                        pass
                    if changed:
                        continue

                    node[key] = simplified

                    # OpenAI-compatible tool parameters require a top-level type even when
                    # branch-specific constraints remain in anyOf/oneOf.
                    branch_types = [sub.get("type") for sub in simplified]
                    if "type" not in node and branch_types and all(isinstance(item, str) for item in branch_types):
                        unique_types = list(dict.fromkeys(cast(list[str], branch_types)))
                        node["type"] = unique_types[0] if len(unique_types) == 1 else unique_types

            # ---- recurse into known schema containers ----
            for child_key in ("properties", "patternProperties", "definitions", "$defs"):
                if child_key in node and isinstance(node[child_key], dict):
                    for k, v in list(node[child_key].items()):
                        node[child_key][k] = walk(v)

            # arrays/items
            if "items" in node:
                node["items"] = walk(node["items"])

            # allOf/if/then/else - pass through with integer→number conversions applied inside
            for key in ("allOf",):
                if key in node and isinstance(node[key], list):
                    node[key] = [walk(x) for x in node[key]]

            if "if" in node:
                node["if"] = walk(node["if"])
            if "then" in node:
                node["then"] = walk(node["then"])
            if "else" in node:
                node["else"] = walk(node["else"])

            return node

        sanitized = walk(s)

        # OpenAI tool parameters are expected to expose a top-level type even when
        # Pydantic represents the parameter through a local schema reference.
        definitions = sanitized.get("$defs", {})
        properties = sanitized.get("properties", {})
        if not isinstance(definitions, dict) or not isinstance(properties, dict):
            return sanitized
        for property_schema in properties.values():
            if not isinstance(property_schema, dict):
                continue
            ref = property_schema.get("$ref")
            if "type" in property_schema or not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                continue
            referenced_schema = definitions.get(ref.removeprefix("#/$defs/"))
            if isinstance(referenced_schema, dict) and isinstance(referenced_schema.get("type"), str):
                property_schema["type"] = referenced_schema["type"]

        return sanitized

    @staticmethod
    def make_mcp_tool(
        tool: Tool,
        openai_tool_compatible: bool = True,
        structured_output: bool | None = None,
        activity_run_manager: ActivityRunManager | None = None,
    ) -> SerenaFastMCPTool:
        """
        Creates an MCP tool from a Serena Tool instance.

        :param tool: the Serena Tool instance to convert.
        :param openai_tool_compatible: whether to process the tool schema to be compatible with OpenAI tools
            (doesn't accept integer, needs number instead, etc.). This allows using Serena MCP within codex.
        :param structured_output: whether to use structured output for the tool (None = auto)
        :param activity_run_manager: optional activity lifecycle recorder for ChatGPT UI integration
        """
        return SerenaFastMCPTool(
            tool,
            openai_tool_compatible=openai_tool_compatible,
            structured_output=structured_output,
            activity_run_manager=activity_run_manager,
        )

    def _iter_tools(self) -> Iterator[Tool]:
        assert self.agent is not None
        yield from self.agent.get_exposed_tool_instances()

    # noinspection PyProtectedMember
    def _set_mcp_tools(self, mcp: FastMCP, openai_tool_compatible: bool, structured_output: bool | None) -> None:
        """
        Update the tools in the MCP server

        :param mcp: The MCP server to update
        :param openai_tool_compatible: whether to process the tool schema to be compatible with OpenAI tools
        :param structured_output: whether to use structured output for the tools (None = auto)
        """
        if mcp is not None:
            mcp._tool_manager._tools = {}
            for tool in self._iter_tools():
                mcp_tool = self.make_mcp_tool(
                    tool,
                    openai_tool_compatible=openai_tool_compatible,
                    structured_output=structured_output,
                    activity_run_manager=self._activity_run_manager,
                )
                mcp._tool_manager._tools[tool.get_name()] = mcp_tool
            self._register_activity_tools(mcp)
            log.info(f"Starting MCP server with {len(mcp._tool_manager._tools)} tools: {list(mcp._tool_manager._tools.keys())}")

    def _register_activity_tools(self, mcp: FastMCP) -> None:
        """Registers ChatGPT-only activity tools outside Serena's serial executor."""
        assert self.agent is not None
        assert self._activity_run_manager is not None
        assert self._activity_view is not None
        agent = self.agent
        activity_run_manager = self._activity_run_manager
        activity_view = self._activity_view

        def activity_session_id(run_id: str) -> str:
            run = agent.execution_store.get_activity_run(run_id)
            if run is None:
                raise ValueError("Activity run is not available")
            return run.session_id

        @mcp.tool(
            name="show_activity",
            title="Show Serena Activity",
            description=(
                "Shows one compact live Serena command panel for a multi-step assistant response. Supply conversation_title "
                "as a concise 3-8 word description of the current ChatGPT conversation, inferred from the conversation context; "
                "refresh it whenever this panel is reopened if the conversation focus has materially changed. Call this at most "
                "once after each user message, before the first substantive Serena tool. If show_activity has already been called "
                "since the user's latest message, never call it again in that response, including after tool results, progress "
                "updates, errors, retries, reconnects, or context compaction. Do not call it for a single quick lookup."
            ),
            annotations=ToolAnnotations(title="Show Serena Activity", readOnlyHint=True, destructiveHint=False),
            meta={
                "ui": {"resourceUri": ACTIVITY_RESOURCE_URI, "visibility": ["model", "app"]},
                "openai/outputTemplate": ACTIVITY_RESOURCE_URI,
                "openai/widgetAccessible": True,
                "openai/toolInvocation/invoking": "Opening Serena activity...",
                "openai/toolInvocation/invoked": "Serena activity",
            },
            structured_output=False,
        )
        async def show_activity(conversation_title: str, mcp_ctx: Context) -> CallToolResult:
            session_id = get_mcp_session_id(mcp_ctx)
            project = agent.get_active_project_for_session(session_id)
            project_name = project.project_name if project is not None else ""
            execution_id = uuid.uuid4().hex
            store = agent.execution_store
            store.start_execution(
                execution_id=execution_id,
                session_id=session_id,
                project_name=project_name,
                tool_name="show_activity",
                arguments={"conversation_title": conversation_title},
            )
            try:
                await asyncio.to_thread(agent.set_dashboard_session_name, session_id, conversation_title)
                run = await asyncio.to_thread(activity_run_manager.start_run, session_id, project_name)
                snapshot = await asyncio.to_thread(
                    activity_view.for_run,
                    session_id,
                    run.run_id,
                    refresh_git_metrics=True,
                )
                result = activity_snapshot_payload(snapshot)
            except Exception as exc:
                store.finish_execution(
                    execution_id,
                    succeeded=False,
                    error=store.serialize_auxiliary_value(exc),
                    project_name=project_name,
                )
                raise
            presentation = ToolResultPresenter(
                agent.tool_output_store,
                max_chars=int(agent.serena_config.default_max_tool_answer_tokens) * 4,
            ).present(result)
            store.finish_execution(
                execution_id,
                succeeded=True,
                result=presentation.persisted_serialization,
                project_name=project_name,
                retained_output_id=presentation.retained_output_id,
                retained_output_chars=presentation.retained_output_chars,
            )
            payload = cast(dict[str, Any], presentation.transport_value)
            serialized = presentation.persisted_serialization
            assert serialized is not None
            return CallToolResult(
                content=[TextContent(type="text", text=serialized)],
                structuredContent=payload,
                isError=False,
            )

        @mcp.tool(
            name="get_activity",
            title="Get Serena Activity",
            description="Returns the current state of one Serena activity panel. Intended for the activity app only.",
            annotations=ToolAnnotations(title="Get Serena Activity", readOnlyHint=True, destructiveHint=False),
            meta={
                "ui": {"visibility": ["app"]},
                "openai/widgetAccessible": True,
                "openai/visibility": "private",
            },
            structured_output=True,
        )
        async def get_activity(run_id: str) -> dict[str, Any]:
            snapshot = await asyncio.to_thread(
                activity_view.for_run,
                activity_session_id(run_id),
                run_id,
            )
            return activity_snapshot_payload(snapshot)

        @mcp.tool(
            name="get_activity_detail",
            title="Get Serena Activity Detail",
            description="Returns parameters and the persisted canonical result for one Serena tool call. Intended for the activity app only.",
            annotations=ToolAnnotations(title="Get Serena Activity Detail", readOnlyHint=True, destructiveHint=False),
            meta={
                "ui": {"visibility": ["app"]},
                "openai/widgetAccessible": True,
                "openai/visibility": "private",
            },
            structured_output=True,
        )
        def get_activity_detail(run_id: str, call_id: str) -> dict[str, Any]:
            detail = activity_view.call_detail(activity_session_id(run_id), run_id, call_id)
            return activity_call_detail_payload(detail)

        @mcp.tool(
            name="get_activity_media",
            title="Get Serena Activity Media",
            description="Returns retained image, audio, or file content for one Serena tool call. Intended for the activity app only.",
            annotations=ToolAnnotations(title="Get Serena Activity Media", readOnlyHint=True, destructiveHint=False),
            meta={
                "ui": {"visibility": ["app"]},
                "openai/widgetAccessible": True,
                "openai/visibility": "private",
            },
            structured_output=False,
        )
        def get_activity_media(run_id: str, call_id: str) -> CallToolResult:
            media = activity_view.call_media(activity_session_id(run_id), run_id, call_id)
            link = ResourceLink(
                type="resource_link",
                name=media.name,
                uri=AnyUrl(media.uri),
                mimeType=media.mime_type,
            )
            if media.media_type == "file":
                return CallToolResult(content=[link])

            data = base64.b64encode(read_result_file_link(link)).decode("ascii")
            if media.media_type == "image":
                content = ImageContent(type="image", data=data, mimeType=media.mime_type)
            else:
                content = AudioContent(type="audio", data=data, mimeType=media.mime_type)
            return CallToolResult(content=[content, link])

        @mcp.tool(
            name="get_activity_job_detail",
            title="Get Serena Activity Job Detail",
            description="Returns runtime metadata and bounded output for one Serena job. Intended for the activity app only.",
            annotations=ToolAnnotations(title="Get Serena Activity Job Detail", readOnlyHint=True, destructiveHint=False),
            meta={
                "ui": {"visibility": ["app"]},
                "openai/widgetAccessible": True,
                "openai/visibility": "private",
            },
            structured_output=True,
        )
        async def get_activity_job_detail(run_id: str, job_id: str) -> dict[str, Any]:
            detail = await asyncio.to_thread(
                activity_view.job_detail,
                activity_session_id(run_id),
                run_id,
                job_id,
            )
            return activity_job_detail_payload(detail)

    def _create_serena_agent(
        self,
        serena_config: SerenaConfig,
        project_activation_error: str | None = None,
        web_dashboard_port: int | None = None,
    ) -> SerenaAgent:
        return SerenaAgent(
            project=self.project,
            serena_config=serena_config,
            project_activation_error=project_activation_error,
            web_dashboard_port=web_dashboard_port,
        )

    def create_mcp_server(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        streamable_http_path: str = "/mcp",
        enable_web_dashboard: bool | None = None,
        web_dashboard_port: int | None = None,
        open_web_dashboard: bool | None = None,
        log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
        trace_lsp_communication: bool | None = None,
        tool_timeout: float | None = None,
        project_activation_error: str | None = None,
    ) -> FastMCP:
        """
        Creates the fixed ChatGPT MCP server.

        :param host: host to bind to
        :param port: port to bind to
        :param streamable_http_path: streamable HTTP endpoint path
        :param enable_web_dashboard: optional dashboard-enabled override
        :param web_dashboard_port: optional exact dashboard port
        :param open_web_dashboard: optional dashboard launch override
        :param log_level: optional log-level override
        :param trace_lsp_communication: optional LSP tracing override
        :param tool_timeout: optional tool execution timeout override
        :param project_activation_error: initial project activation error to report to the client
        :return: configured FastMCP server
        """
        try:
            config = SerenaConfig.from_config_file()

            # apply runtime deployment overrides
            if enable_web_dashboard is not None:
                config.web_dashboard = enable_web_dashboard
            if open_web_dashboard is not None:
                config.web_dashboard_open_on_launch = open_web_dashboard
            if log_level is not None:
                normalized_level = cast(Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], log_level.upper())
                config.log_level = logging.getLevelNamesMapping()[normalized_level]
            if trace_lsp_communication is not None:
                config.trace_lsp_communication = trace_lsp_communication
            if tool_timeout is not None:
                config.tool_timeout = tool_timeout
            self.agent = self._create_serena_agent(
                config,
                project_activation_error=project_activation_error,
                web_dashboard_port=web_dashboard_port,
            )
            self._activity_run_manager = ActivityRunManager(self.agent.execution_store)
            self._activity_view = ActivityView(
                execution_store=self.agent.execution_store,
                job_source=self.agent.job_manager,
                git_metrics_source=self.agent,
            )
        except Exception as e:
            show_fatal_exception_safe(e)
            raise

        # isolate FastMCP settings from project-local .env files
        Settings.model_config = SettingsConfigDict(env_prefix="FASTMCP_")
        instructions = self._get_initial_instructions()
        log.info("MCP server initial instructions:\n%s", instructions)
        mcp = FastMCP(
            name="Serena",
            lifespan=self.server_lifespan,
            website_url="https://mcp.kendell.uk/",
            icons=_server_icons(),
            host=host,
            port=port,
            streamable_http_path=streamable_http_path,
            instructions=instructions,
        )
        register_file_export_resource(mcp)
        register_activity_resource(mcp)
        return mcp

    @asynccontextmanager
    async def server_lifespan(self, mcp_server: FastMCP) -> AsyncIterator[None]:
        """
        Configures one fixed ChatGPT tool surface for the MCP connection lifetime.

        :param mcp_server: MCP server instance to configure
        """
        assert self.agent is not None
        self._set_mcp_tools(mcp_server, openai_tool_compatible=True, structured_output=None)
        log.info("MCP server lifetime setup complete")
        try:
            yield
        finally:
            if self.transport == "stdio":
                log.info("MCP server shutting down")
                if self.agent is not None:
                    self.agent.on_shutdown()
            else:
                log.info("Client disconnected")

    def _get_initial_instructions(self) -> str:
        assert self.agent is not None
        return self.agent.create_connection_prompt()
