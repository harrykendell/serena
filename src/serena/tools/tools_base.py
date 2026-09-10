import inspect
import json
from abc import ABC
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cached_property
from types import TracebackType
from typing import TYPE_CHECKING, Any, Optional, Protocol, Self, TypeVar, cast

from mcp import Implementation
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata, func_metadata
from sensai.util import logging

from serena.errors import UserFacingError
from serena.execution import ExecutionAccess
from serena.memories.memory_manager import MemoryManager
from serena.project import Project
from serena.prompt_factory import SerenaPromptFactory
from serena.session import get_mcp_session_id
from serena.structured_output import StructuredOutputCompactor
from serena.util.class_decorators import singleton
from serena.util.ls_diagnostics import DiagnosticsDiff, EditedFilePath, PublishedDiagnosticsSnapshot
from solidlsp.ls_exceptions import LanguageServerOperationError, SolidLSPException

if TYPE_CHECKING:
    from serena.agent import SerenaAgent
    from serena.code_editor import CodeEditor, LanguageServerCodeEditor
    from serena.symbol import LanguageServerSymbolRetriever

log = logging.getLogger(__name__)
T = TypeVar("T")
SUCCESS_RESULT = "OK"


class Component(ABC):
    def __init__(self, agent: "SerenaAgent"):
        self.agent = agent

    def get_project_root(self) -> str:
        """
        :return: the root directory of the active project, raises a ValueError if no active project configuration is set
        """
        return self.project.project_root

    @property
    def prompt_factory(self) -> SerenaPromptFactory:
        return self.agent.prompt_factory

    @property
    def memory_manager(self) -> "MemoryManager":
        return self.project.memory_manager

    def create_language_server_symbol_retriever(self) -> "LanguageServerSymbolRetriever":
        from serena.symbol import LanguageServerSymbolRetriever

        return LanguageServerSymbolRetriever(self.project)

    @property
    def project(self) -> Project:
        return self.agent.get_active_project_or_raise()

    def create_code_editor(self) -> "CodeEditor":
        return self.create_ls_code_editor()

    def create_ls_code_editor(self) -> "LanguageServerCodeEditor":
        from ..code_editor import LanguageServerCodeEditor

        return LanguageServerCodeEditor(self.create_language_server_symbol_retriever())


class ToolMarker:
    """
    Base class for tool markers.
    """


class ToolMarkerCanEdit(ToolMarker):
    """
    Marker class for all tools that can perform editing operations on files.
    """


class ToolMarkerDoesNotRequireActiveProject(ToolMarker):
    pass


class ToolMarkerSymbolicRead(ToolMarker):
    """
    Marker class for tools that perform symbol read operations.
    """


class ToolMarkerSymbolicEdit(ToolMarkerCanEdit):
    """
    Marker class for tools that perform symbolic edit operations.
    """


class ApplyMethodProtocol(Protocol):
    """Callable protocol for the apply method of a tool."""

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        pass


class Tool(Component):
    # NOTE: each tool should implement the apply method, which is then used in
    # the central method of the Tool class `apply_ex`.
    # Failure to do so will result in a RuntimeError at tool execution time.
    # The apply method is not declared as part of the base Tool interface since we cannot
    # know the signature of the (input parameters of the) method in advance.
    #
    # The docstring and types of the apply method are used to generate the tool description
    # (which is use by the LLM, so a good description is important)
    # and to validate the tool call arguments.

    SESSION_ID_PARAM_NAME = "session_id"
    """
    parameter name to use in apply method for the client session ID.
    This parameter will be ignored by the MCP interface but will be populated with the session ID of the current client session 
    when the tool is called, allowing tools to be session-aware if needed.
    """

    _last_tool_call_client_str: str | None = None
    """We can only get the client info from within a tool call. Each tool call will update this variable."""

    def __init__(self, agent: "SerenaAgent"):
        super().__init__(agent)

    @cached_property
    def _is_session_aware(self) -> bool:
        """
        :return: whether the tool is session-aware, i.e. whether the apply method expects a session_id (str) parameter.
        """
        # check apply method for session_id arg
        apply_fn = self.get_apply_fn()
        sig = inspect.signature(apply_fn)
        for param in sig.parameters.values():
            if param.name == self.SESSION_ID_PARAM_NAME:
                return True
        return False

    @staticmethod
    def _sanitize_input_param(raw_param: str) -> str:
        # some clients replace < and > with their escaped html versions, we need to counteract this
        return raw_param.replace("&lt;", "<").replace("&gt;", ">")

    @classmethod
    def set_last_tool_call_client_str(cls, client_str: str | None) -> None:
        cls._last_tool_call_client_str = client_str

    @classmethod
    def get_last_tool_call_client_str(cls) -> str | None:
        return cls._last_tool_call_client_str

    @classmethod
    def get_name_from_cls(cls) -> str:
        name = cls.__name__
        if name.endswith("Tool"):
            name = name[:-4]
        # convert to snake_case
        name = "".join(["_" + c.lower() if c.isupper() else c for c in name]).lstrip("_")
        return name

    def get_name(self) -> str:
        return self.get_name_from_cls()

    def get_apply_fn(self) -> ApplyMethodProtocol:
        apply_fn = getattr(self, "apply")
        if apply_fn is None:
            raise RuntimeError(f"apply not defined in {self}. Did you forget to implement it?")
        return apply_fn

    @classmethod
    def can_edit(cls) -> bool:
        """
        Returns whether this tool can perform editing operations on code.

        :return: True if the tool can edit code, False otherwise
        """
        return issubclass(cls, ToolMarkerCanEdit)

    @classmethod
    def get_execution_access(cls) -> ExecutionAccess:
        """Returns the project execution access class required by this tool."""
        return ExecutionAccess.WRITE if cls.can_edit() else ExecutionAccess.READ

    @classmethod
    def get_mcp_tool_meta(cls) -> dict[str, Any] | None:
        """Returns optional MCP ``_meta`` attached to this tool's declaration."""
        return None

    def prepare_mcp_result(self, result: Any) -> Any:
        """Prepares a tool result for transport through MCP."""
        return result

    @classmethod
    def get_tool_description(cls) -> str:
        docstring = cls.__doc__
        if docstring is None:
            return ""
        return docstring.strip()

    @classmethod
    def get_apply_docstring_from_cls(cls) -> str:
        """Get the docstring for the apply method from the class (static metadata).
        Needed for creating MCP tools in a separate process without running into serialization issues.
        """
        # First try to get from __dict__ to handle dynamic docstring changes
        if "apply" in cls.__dict__:
            apply_fn = cls.__dict__["apply"]
        else:
            # Fall back to getattr for inherited methods
            apply_fn = getattr(cls, "apply", None)
            if apply_fn is None:
                raise AttributeError(f"apply method not defined in {cls}. Did you forget to implement it?")

        docstring = apply_fn.__doc__
        if not docstring:
            raise AttributeError(f"apply method has no (or empty) docstring in {cls}. Did you forget to implement it?")
        return docstring.strip()

    def get_apply_docstring(self) -> str:
        """Gets the docstring for the tool application, used by the MCP server."""
        return self.get_apply_docstring_from_cls()

    def get_apply_fn_metadata(self, structured_output: bool | None = None) -> FuncMetadata:
        """Gets the metadata for the tool application function, used by the MCP server."""
        return self.get_apply_fn_metadata_from_cls(structured_output=structured_output)

    @classmethod
    def get_apply_fn_metadata_from_cls(cls, structured_output: bool | None = None) -> FuncMetadata:
        """Get the metadata for the apply method from the class (static metadata).
        Needed for creating MCP tools in a separate process without running into serialization issues.
        """
        # First try to get from __dict__ to handle dynamic docstring changes
        if "apply" in cls.__dict__:
            apply_fn = cls.__dict__["apply"]
        else:
            # Fall back to getattr for inherited methods
            apply_fn = getattr(cls, "apply", None)
            if apply_fn is None:
                raise AttributeError(f"apply method not defined in {cls}. Did you forget to implement it?")

        return func_metadata(apply_fn, skip_names=["self", "cls", cls.SESSION_ID_PARAM_NAME], structured_output=structured_output)

    def _effective_max_answer_chars(self, max_answer_chars: int) -> int:
        """Resolve one response budget while preserving explicit character overrides."""
        effective_max_answer_chars = (
            max_answer_chars if max_answer_chars != -1 else self.agent.serena_config.default_max_tool_answer_tokens * 4
        )
        if effective_max_answer_chars <= 0:
            raise UserFacingError(f"Resolved maximum answer length must be positive, got: {effective_max_answer_chars}")
        return effective_max_answer_chars

    def _limit_length(
        self,
        result: str,
        max_answer_chars: int,
        shortened_result_factories: list[Callable[[], str]] | None = None,
        *,
        prefer_structured_preview: bool = False,
    ) -> str:
        """Limits a tool result while retaining the complete value for exact paging.

        :param result: the full result string
        :param max_answer_chars: maximum allowed characters. -1 means use the configured default.
        :param shortened_result_factories: optional closures producing progressively shorter summaries;
            the richest summary that fits is appended to the retained-output metadata.
        :param prefer_structured_preview: whether valid JSON object/list results should prefer a
            structure-preserving preview before tool-specific summaries.
        :return: the original result when it fits, otherwise a bounded retained-output response
        """
        effective_max_answer_chars = self._effective_max_answer_chars(max_answer_chars)
        if (n_chars := len(result)) <= effective_max_answer_chars:
            return result

        # retain the exact value before deriving any bounded model-facing representation
        output_id = self.agent.retain_tool_output(self.get_name(), result)
        retained_msg = f"truncated=true; total_chars={n_chars}; output_id={output_id}"
        compactor = StructuredOutputCompactor()

        # preserve machine-readable structure first when verbose leaves are the useful part of the result
        if prefer_structured_preview:
            structured_preview = compactor.render_retained_json_preview(result, output_id, effective_max_answer_chars)
            if structured_preview is not None:
                return structured_preview

        # retain domain-specific degradation for tools that know how to summarize their own output
        if shortened_result_factories is not None:
            for make_shorter in shortened_result_factories:
                candidate = f"{retained_msg}\n{make_shorter()}"
                if len(candidate) <= effective_max_answer_chars:
                    return candidate

        # generic structured results remain valid JSON instead of degrading to an arbitrary raw tail
        structured_preview = compactor.render_retained_json_preview(result, output_id, effective_max_answer_chars)
        if structured_preview is not None:
            return structured_preview
        return self.agent.render_tool_output_tail(output_id, effective_max_answer_chars)

    def is_active(self) -> bool:
        return self.agent.tool_is_active(self.get_name())

    def is_readonly(self) -> bool:
        return not self.can_edit()

    def is_symbolic(self) -> bool:
        return issubclass(self.__class__, ToolMarkerSymbolicRead) or issubclass(self.__class__, ToolMarkerSymbolicEdit)

    @classmethod
    def get_param_aliases(cls) -> dict[str, str]:
        """
        :return: a mapping of parameter aliases for the apply method, where the key is the alias and the value is the actual parameter name.
            This can be used to define alternative parameter names for the same parameter.
        """
        return {}

    def _apply_with_lsp_recovery(self, apply_fn: Callable[..., Any], apply_kwargs: dict[str, Any]) -> Any:
        """Applies one tool operation with side-effect-aware recovery from LSP termination.

        Read operations are safe to replay once after restarting the affected language server.
        Mutating operations are never replayed because their partial side effects may be unknown.
        """
        try:
            return apply_fn(**apply_kwargs)
        except SolidLSPException as error:
            if not error.is_language_server_terminated():
                if isinstance(error, LanguageServerOperationError):
                    raise UserFacingError(error.user_message()) from None
                raise

            # recover the affected language server when its identity is available
            affected_language = error.get_affected_language()
            access = self.get_execution_access()
            if affected_language is None:
                if access is ExecutionAccess.WRITE:
                    raise UserFacingError(
                        f"Language server terminated while executing mutating tool '{self.get_name()}'. "
                        "Serena did not replay the operation because it may have partially changed project state, and the affected "
                        "language server could not be identified for automatic recovery. Re-inspect the affected state before "
                        "attempting the edit again."
                    ) from error
                log.error("Language server terminated while executing read tool (%s), but the affected language is unknown.", error)
                raise

            try:
                self.agent.get_language_server_manager_or_raise().restart_language_server(affected_language)
            except Exception as recovery_error:
                if access is ExecutionAccess.WRITE:
                    raise UserFacingError(
                        f"Language server '{affected_language.value}' terminated while executing mutating tool '{self.get_name()}'. "
                        "Serena did not replay the operation because it may have partially changed project state. Automatic "
                        f"language-server recovery also failed: {recovery_error}. Re-inspect the affected state before attempting "
                        "the edit again."
                    ) from recovery_error
                raise

            # replay only operations whose execution contract is read-only
            if access is ExecutionAccess.READ:
                log.error(
                    "Language server %s terminated while executing read tool (%s). Restarted it and retrying the read once.",
                    affected_language.value,
                    error,
                )
                return apply_fn(**apply_kwargs)

            if access is ExecutionAccess.WRITE:
                raise UserFacingError(
                    f"Language server '{affected_language.value}' terminated while executing mutating tool '{self.get_name()}'. "
                    "Serena restarted the language server but did not replay the operation because it may have partially changed "
                    "project state. Re-inspect the affected state before attempting the edit again."
                ) from error

            raise

    def apply_ex(
        self,
        mcp_ctx: Context | None = None,
        execution_id: str | None = None,
        **kwargs,
    ) -> Any:
        """Applies the tool in the execution runtime selected for the current session."""
        # obtain session ID and client info
        session_id = get_mcp_session_id(mcp_ctx)
        if mcp_ctx is not None:
            try:
                client_params = mcp_ctx.session.client_params
                if client_params is not None:
                    client_info = cast(Implementation, client_params.clientInfo)
                    client_str = client_info.title if client_info.title else client_info.name + " " + client_info.version
                    if client_str != self.get_last_tool_call_client_str():
                        log.debug(f"Updating client info: {client_info}")
                        self.set_last_tool_call_client_str(client_str)
            except Exception as error:
                log.info(f"Failed to get client info: {error}.")

        def task() -> Any:
            apply_fn = self.get_apply_fn()

            if not self.is_active():
                raise UserFacingError(
                    f"Tool '{self.get_name_from_cls()}' is not active. Active tools: {self.agent.get_active_tool_names()}"
                )

            # check whether the tool requires an active project and language server
            if not isinstance(self, ToolMarkerDoesNotRequireActiveProject) and self.agent.get_active_project() is None:
                raise UserFacingError(
                    "No active project. Ask the user to provide the project path or to select a project from this list of known "
                    f"projects: {self.agent.serena_config.project_names}"
                )

            # construct apply kwargs, adding session_id if the tool is session-aware
            apply_kwargs = dict(kwargs)
            if self._is_session_aware:
                apply_kwargs["session_id"] = session_id

            # apply the actual tool with side-effect-aware language-server recovery
            result = self._apply_with_lsp_recovery(apply_fn, apply_kwargs)

            try:
                ls_manager = self.agent.get_language_server_manager()
                if ls_manager is not None:
                    ls_manager.save_all_caches()
            except Exception as error:
                log.error(f"Error saving language server cache: {error}")

            return result

        # execute directly in the existing FastMCP worker thread under the project coordinator.
        # MCP-level timeout may stop waiting for this worker, but the coordinator permit remains
        # held until ``task`` really returns.
        return self.agent.execute_tool_call(
            task,
            access=self.get_execution_access(),
            session_id=session_id,
            execution_id=execution_id,
            symbolic_read=isinstance(self, ToolMarkerSymbolicRead),
        )

    @staticmethod
    def _to_json(x: Any) -> str:
        return json.dumps(x, ensure_ascii=False)


class EditingToolWithDiagnostics(Tool, ToolMarkerCanEdit):
    """
    Base class for editing tools that want to capture and report changes in LSP diagnostics before and after the edit.
    """

    ENABLE_DIAGNOSTICS: bool = False
    """
    Global flag to enable/disable diagnostics for LSP-based editing tools derived from this class.
    The feature is currently disabled, because per-edit diagnostics are a questionable feature, since individual
    edits often intentionally introduce diagnostics (e.g. function signature mismatches or even syntax errors) that 
    are then resolved in subsequent edits.
    """

    DIAGNOSTICS_KEY = "diagnostics[warning-or-higher]"

    class DiagnosticsContext:
        def __init__(self, tool: "EditingToolWithDiagnostics", *edited_relative_paths: str) -> None:
            self._tool = tool
            self._is_diagnostics_enabled = tool.ENABLE_DIAGNOSTICS
            self._edited_files = [EditedFilePath(path, path) for path in edited_relative_paths]
            self._before_edit_diagnostics_snapshot: PublishedDiagnosticsSnapshot | None = None
            self._symbol_retriever: Optional["LanguageServerSymbolRetriever"] | None = None
            if self._is_diagnostics_enabled:
                self._symbol_retriever = tool.create_language_server_symbol_retriever()
                self._before_edit_diagnostics_snapshot = PublishedDiagnosticsSnapshot(self._edited_files, self._symbol_retriever)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def format_result(
            self,
            base_result: str,
        ) -> str:
            if not self._is_diagnostics_enabled:
                return base_result

            if self._before_edit_diagnostics_snapshot is None:
                return base_result

            assert self._symbol_retriever is not None
            diagnostics_diff = DiagnosticsDiff(self._before_edit_diagnostics_snapshot, self._edited_files, self._symbol_retriever)
            grouped_diagnostics = diagnostics_diff.get_grouped_diagnostics().get_dict()

            if not grouped_diagnostics:
                return base_result
            else:
                result_dict = {
                    "result": base_result,
                    EditingToolWithDiagnostics.DIAGNOSTICS_KEY: grouped_diagnostics,
                }
                return self._tool._to_json(result_dict)


class EditedFileContext:
    """
    Context manager for file editing.

    Create the context, then use `set_updated_content` to set the new content, the original content
    being provided in `original_content`.
    When exiting the context without an exception, the updated content will be written back to the file.
    """

    def __init__(self, relative_path: str, code_editor: "CodeEditor"):
        self._relative_path = relative_path
        self._code_editor = code_editor
        self._edited_file: CodeEditor.EditedFile | None = None
        self._edited_file_context: Any = None

    def __enter__(self) -> Self:
        self._edited_file_context = self._code_editor.edited_file_context(self._relative_path)
        self._edited_file = self._edited_file_context.__enter__()
        return self

    def get_original_content(self) -> str:
        """
        :return: the original content of the file before any modifications.
        """
        assert self._edited_file is not None
        return self._edited_file.get_contents()

    def set_updated_content(self, content: str) -> None:
        """
        Sets the updated content of the file, which will be written back to the file
        when the context is exited without an exception.

        :param content: the updated content of the file
        """
        assert self._edited_file is not None
        self._edited_file.set_contents(content)

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        assert self._edited_file_context is not None
        self._edited_file_context.__exit__(exc_type, exc_value, traceback)


@dataclass(kw_only=True)
class RegisteredTool:
    """One tool in Serena's explicit ChatGPT MCP catalogue."""

    tool_class: type[Tool]
    tool_name: str

    @property
    def class_docstring(self) -> str:
        """:return: the tool description (high-level class docstring)"""
        return self.tool_class.get_tool_description()


@singleton
class ToolRegistry:
    def __init__(self) -> None:
        from serena.tools import MCP_TOOL_CLASSES

        self._tool_dict: dict[str, RegisteredTool] = {}
        for tool_class in MCP_TOOL_CLASSES:
            name = tool_class.get_name_from_cls()
            if name in self._tool_dict:
                raise ValueError(f"Duplicate tool name found: {name}. Tool classes must have unique names.")
            self._tool_dict[name] = RegisteredTool(tool_class=tool_class, tool_name=name)

    def get_registered_tools_by_module(self) -> dict[str, list[RegisteredTool]]:
        """
        :return: the registered tools grouped by their module (ordered alphabetically by module and tool name)
        """
        module_dict: dict[str, list[RegisteredTool]] = {}
        for tool in self._tool_dict.values():
            module = tool.tool_class.__module__
            if module not in module_dict:
                module_dict[module] = []
            module_dict[module].append(tool)
        sorted_module_dict = {}
        for module in sorted(module_dict.keys()):
            sorted_module_dict[module] = sorted(module_dict[module], key=lambda t: t.tool_name)
        return sorted_module_dict

    def get_tool_class_by_name(self, tool_name: str) -> type[Tool]:
        if tool_name not in self._tool_dict:
            raise ValueError(f"Tool named '{tool_name}' not found.")
        return self._tool_dict[tool_name].tool_class

    def get_all_tool_classes(self) -> list[type[Tool]]:
        return list(t.tool_class for t in self._tool_dict.values())

    def get_tool_names(self) -> list[str]:
        """
        :return: the list of all tool names.
        """
        return list(self._tool_dict.keys())

    def print_tool_overview(self, tools: Iterable[type[Tool] | Tool] | None = None) -> None:
        """Prints a summary of the fixed ChatGPT tool catalogue or the supplied tools."""
        selected_tools = self.get_all_tool_classes() if tools is None else tools
        tool_dict = {tool.get_name_from_cls(): tool for tool in selected_tools}
        for tool_name in sorted(tool_dict):
            tool = tool_dict[tool_name]
            print(f" * `{tool_name}`: {tool.get_tool_description().strip()}")
