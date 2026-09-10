"""
The Serena Model Context Protocol (MCP) Server
"""

import json
import os
import platform
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging import Logger
from typing import TypeVar

from sensai.util import logging
from sensai.util.helper import mark_used
from sensai.util.logging import LogTime
from sensai.util.string import dict_string

from serena import serena_version
from serena.chatgpt_policy import CHATGPT_PRODUCT_PROMPT
from serena.config.serena_config import SerenaConfig, SerenaPaths
from serena.dashboard import DashboardServer, open_url_in_browser
from serena.errors import UserFacingError
from serena.execution import (
    ExecutionAccess,
    ProjectExecutionCoordinator,
    bind_execution_id,
    get_current_execution_id,
    reset_execution_id,
)
from serena.execution_store import ExecutionStore
from serena.jobs import JobManager
from serena.ls_manager import LanguageServerManager
from serena.memories.memory_manager import MemoryManager
from serena.project import Project
from serena.prompt_factory import SerenaPromptFactory
from serena.runtime import AvailableTools, ProjectPromptStatus, ProjectRuntime, SessionRegistry
from serena.tool_output import ToolOutputDescriptor, ToolOutputPage, ToolOutputStore, ToolOutputWriter
from serena.tools import (
    OnboardingTool,
    OpenDashboardTool,
    ReadMemoryTool,
    Tool,
    ToolRegistry,
)
from serena.util.gui import system_has_usable_display
from solidlsp.ls_config import LanguageServerId
from solidlsp.util import subprocess_util
from solidlsp.util.subprocess_util import terminate_process_tree_with_kill_fallback

log = logging.getLogger(__name__)
TTool = TypeVar("TTool", bound="Tool")
T = TypeVar("T")
SUCCESS_RESULT = "OK"


class DashboardManager:
    """Browser entry point for the hosted Serena dashboard."""

    def __init__(self, port: int, host_listen_address: str, open_dashboard_on_launch: bool) -> None:
        dashboard_host = host_listen_address
        if dashboard_host == "0.0.0.0":
            dashboard_host = "localhost"
        self.url = f"http://{dashboard_host}:{port}/dashboard/index.html"

        if open_dashboard_on_launch:
            if not system_has_usable_display():
                log.info("Not opening the Serena dashboard because no usable display was detected.")
            else:
                self.open_dashboard_in_browser()

    def open_dashboard_in_browser(self) -> None:
        """Open the dashboard in the user's default browser."""
        open_url_in_browser(self.url, use_subprocess=True)


class SerenaAgent:
    def __init__(
        self,
        project: str | None = None,
        *,
        project_activation_callback: Callable[[], None] | None = None,
        project_activation_error: str | None = None,
        serena_config: SerenaConfig | None = None,
        web_dashboard_port: int | None = None,
    ):
        """
        Creates the fixed ChatGPT Serena runtime.

        :param project: project to activate immediately, by path or registered name
        :param project_activation_callback: callback invoked after project activation
        :param project_activation_error: initial project-resolution error exposed in the instruction prompt
        :param serena_config: Serena configuration, or ``None`` to load the default configuration
        :param web_dashboard_port: exact dashboard port, or ``None`` to select a secondary port automatically
        """
        self._startup_project: Project | None = None
        self._session_id_context: ContextVar[str] = ContextVar(f"serena_session_{id(self)}", default="global")
        self._execution_project_context: ContextVar[tuple[bool, ProjectRuntime | None, Project | None]] = ContextVar(
            f"serena_execution_project_{id(self)}", default=(False, None, None)
        )
        self._submission_project_context: ContextVar[tuple[bool, ProjectRuntime | None, Project | None]] = ContextVar(
            f"serena_submission_project_{id(self)}", default=(False, None, None)
        )
        self._session_registry = SessionRegistry()
        self._project_activation_callback = project_activation_callback
        self._project_activation_error = project_activation_error
        self._dashboard_manager: DashboardManager | None = None
        self._execution_store = ExecutionStore()
        self._tool_output_store = ToolOutputStore(execution_store=self._execution_store)
        self._job_manager = JobManager(retention_observer=self._execution_store)
        self._no_project_prompt_status = ProjectPromptStatus()
        self.version = serena_version()
        self._config_changed_callbacks: list[Callable[[], None]] = []
        self._config_changed_dispatch_lock = threading.Lock()

        # load runtime configuration
        self.serena_config = serena_config or SerenaConfig.from_config_file()
        serena_log_level = self.serena_config.log_level
        if Logger.root.level != serena_log_level:
            log.info("Changing the root logger level to %s", serena_log_level)
            Logger.root.setLevel(serena_log_level)

        # instantiate the explicit ChatGPT tool catalogue
        registry = ToolRegistry()
        self._all_tools: dict[type[Tool], Tool] = {tool_class: tool_class(self) for tool_class in registry.get_all_tool_classes()}
        exposed_tool_instances = list(self._all_tools.values())
        if not (self.serena_config.web_dashboard and not self.serena_config.web_dashboard_open_on_launch):
            exposed_tool_instances = [tool for tool in exposed_tool_instances if not isinstance(tool, OpenDashboardTool)]
        self._exposed_tools = AvailableTools(exposed_tool_instances)
        self._no_project_tools = self._create_active_tools_for_project(None)

        # log fundamental runtime information
        log.info(
            "Starting Serena server (version=%s, process id=%s, parent process id=%s); Python version=%s, platform=%s",
            self.version,
            os.getpid(),
            os.getppid(),
            platform.python_version(),
            platform.platform(),
        )
        log.info("Configuration file: %s", self.serena_config.config_file_path)
        log.info("Available projects: %s", ", ".join(self.serena_config.project_names))
        log.info("Exposed ChatGPT tools (%s): %s", len(self._exposed_tools), ", ".join(self._exposed_tools.tool_names))

        self._prompt_tool_names_mapping = self._create_prompt_tool_names_mapping()
        self._unscoped_execution_coordinator = ProjectExecutionCoordinator()
        self.prompt_factory = SerenaPromptFactory()

        # activate the startup project through the same session registry used by MCP sessions
        if project is not None:
            try:
                self.activate_project_from_path_or_name(project)
                self._startup_project = self.get_active_project_for_session("global")
            except Exception as e:
                log.error("Error activating project '%s' at startup: %s", project, e, exc_info=e)
                self._project_activation_error = str(e)

        # create the dashboard server if enabled
        dashboard_server: DashboardServer | None = None
        if self.serena_config.web_dashboard:
            dashboard_server = DashboardServer(
                agent=self,
                host=self.serena_config.web_dashboard_listen_address,
                trusted_hosts=self.serena_config.web_dashboard_trusted_hosts,
                port=web_dashboard_port,
            )

        self._on_config_changed()

        if dashboard_server:
            dashboard_thread, port = dashboard_server.run_in_thread()
            self._dashboard_manager = DashboardManager(
                port,
                self.serena_config.web_dashboard_listen_address,
                self.serena_config.web_dashboard_open_on_launch,
            )
            mark_used(dashboard_thread)
            log.info("Serena web dashboard started at %s", self._dashboard_manager.url)

    def get_language_server_manager(self) -> LanguageServerManager | None:
        project = self.get_active_project()
        if project is not None:
            return project.language_server_manager
        return None

    def get_language_server_manager_or_raise(self) -> LanguageServerManager:
        active_project = self.get_active_project_or_raise()
        return active_project.get_language_server_manager_or_raise()

    def get_log_inspection_instructions(self) -> str:
        if self.serena_config.web_dashboard:
            return f"Live logs can be inspected via the dashboard at {self.get_dashboard_url()}"
        else:
            log_path = SerenaPaths().last_returned_log_file_path
            if log_path is not None:
                return f"Find the current log file here: {log_path}"
            else:
                return "Unfortunately, logs are not available. We recommend enabling the web dashboard/logging in general."

    def retain_tool_output(self, tool_name: str, content: str) -> str:
        """Retains one complete tool result and returns its stable identifier."""
        return self._tool_output_store.retain(tool_name, content, execution_id=get_current_execution_id())

    def retain_tool_output_with_tail(self, tool_name: str, content: str, max_answer_chars: int) -> str:
        """Retains an oversized tool result and returns a bounded identified tail."""
        return self._tool_output_store.retain_with_tail(
            tool_name,
            content,
            max_answer_chars,
            execution_id=get_current_execution_id(),
        )

    def read_tool_output(self, output_id: str, offset: int, max_chars: int) -> ToolOutputPage:
        """Read one page from a previously retained oversized tool result."""
        return self._tool_output_store.read(output_id, offset, max_chars)

    @property
    def execution_store(self) -> ExecutionStore:
        """Returns the authoritative persisted Serena execution/session store."""
        return self._execution_store

    @property
    def tool_output_store(self) -> ToolOutputStore:
        """Returns the retained-output store used by MCP result presentation."""
        return self._tool_output_store

    @property
    def job_manager(self) -> JobManager:
        """Returns the process-wide persistent Serena job manager."""
        return self._job_manager

    def open_tool_output(self, tool_name: str, execution_id: str | None = None) -> ToolOutputWriter:
        """Open an append-only retained output stream for one tool execution."""
        return self._tool_output_store.open(tool_name, execution_id)

    def render_tool_output_tail(
        self,
        output_id: str,
        max_answer_chars: int,
        *,
        details: str | None = None,
    ) -> str:
        """Renders a compact identified tail from an already retained tool output."""
        return self._tool_output_store.render_tail(output_id, max_answer_chars, details=details)

    def read_tool_execution_tail(self, execution_id: str, max_chars: int) -> ToolOutputPage | None:
        """Read the newest retained output tail for one exact tool execution."""
        return self._tool_output_store.read_execution_tail(execution_id, max_chars)

    def describe_tool_execution_output(self, execution_id: str) -> ToolOutputDescriptor | None:
        """Return retained-output metadata for one exact tool execution, if available."""
        return self._tool_output_store.describe_execution(execution_id)

    def get_dashboard_url(self) -> str | None:
        """
        :return: the URL of the web dashboard, or None if the dashboard is not running
        """
        if self._dashboard_manager is None:
            return None
        return self._dashboard_manager.url

    def set_dashboard_session_name(self, session_id: str, display_name: str) -> str:
        """Sets the retained operator-facing name for one client session."""
        return self._execution_store.set_session_display_name(session_id, display_name)

    def open_dashboard(self) -> bool:
        """Opens the Serena dashboard when browser launch is available.

        :return: ``True`` if a browser launch was attempted, otherwise ``False``
        :raises UserFacingError: if the dashboard service is unavailable
        """
        if self._dashboard_manager is None:
            raise UserFacingError("Dashboard is not running")

        if not system_has_usable_display():
            log.warning("Not opening the Serena web dashboard because no usable display was detected.")
            return False

        self._dashboard_manager.open_dashboard_in_browser()
        return True

    def get_exposed_tool_instances(self) -> list["Tool"]:
        """
        :return: the tool instances which are exposed (e.g. to the MCP client).
            Note that the set of exposed tools is fixed for the session, as
            clients don't react to changes in the set of tools, so this is the superset
            of tools that can be offered during the session.
            If a client should attempt to use a tool that is dynamically disabled
            (e.g. because a project is activated that disables it), it will receive an error.
        """
        return list(self._exposed_tools.tools)

    @contextmanager
    def session_context(self, session_id: str) -> Iterator[None]:
        """Binds project-dependent lookups in the current thread to ``session_id``."""
        token = self._session_id_context.set(session_id)
        try:
            yield
        finally:
            self._session_id_context.reset(token)

    def get_active_project_for_session(self, session_id: str) -> Project | None:
        """Returns the project selected by ``session_id`` without changing execution context."""
        runtime = self._session_registry.get_runtime_for_session(session_id)
        if runtime is not None:
            return runtime.project
        return self._startup_project

    def _get_project_runtime(self, session_id: str | None = None) -> ProjectRuntime | None:
        """Returns the project runtime pinned to this execution, or the session's current runtime."""
        if session_id is None:
            execution_is_pinned, runtime, _ = self._execution_project_context.get()
            if execution_is_pinned:
                return runtime
            session_id = self._session_id_context.get()
        return self._session_registry.get_runtime_for_session(session_id)

    def get_default_project(self) -> Project | None:
        """:return: the immutable project selected at process startup, if any"""
        return self._startup_project

    def get_active_project(self) -> Project | None:
        """
        :return: the project pinned to this execution, otherwise the current session selection
        """
        execution_is_pinned, _, project = self._execution_project_context.get()
        if execution_is_pinned:
            return project
        return self.get_active_project_for_session(self._session_id_context.get())

    def get_active_project_or_raise(self) -> Project:
        """
        :return: the active project or raises an exception if no project is active
        """
        project = self.get_active_project()
        if project is None:
            raise UserFacingError("No active project. Please activate a project first.")
        return project

    @staticmethod
    def _create_prompt_tool_names_mapping() -> dict[str, str]:
        """Creates the prompt mapping for canonical tool names."""
        return {tool_class.get_name_from_cls(): tool_class.get_name_from_cls() for tool_class in ToolRegistry().get_all_tool_classes()}

    @staticmethod
    def _format_prompt_tag(text: str, tag: str, tag_name_attr: str | None = None) -> str:
        open_tag = f"<{tag}" + (f' name="{tag_name_attr}"' if tag_name_attr is not None else "") + ">"
        close_tag = f"</{tag}>"
        return f"{open_tag}\n{text.strip()}\n{close_tag}"

    def _render_prompt(self, prompt_template: str, tag: str | None = None, tag_name_attr: str | None = None) -> str:
        """
        Renders the given prompt template, providing the necessary variables and functions

        :param prompt_template: the template text (jinja2) to render
        :param tag: if not None, wraps the rendered prompt text in a tag
        :param tag_name_attr: for the case where tag is not None, specifies the value of the "name" attribute of the tag
        :return: the rendered prompt
        """

        def embed_memory(memory_name: str) -> str:
            try:
                memory_manager = self._get_memory_manager()
                return self._format_prompt_tag(memory_manager.load_memory(memory_name), tag="memory", tag_name_attr=memory_name)
            except Exception as e:
                log.error("Tried to embed memory '%s' but failed to load it: %s", memory_name, e)
                return ""

        text = self.prompt_factory.render_template(
            prompt_template,
            available_tools=self._exposed_tools.tool_names,
            available_markers=self._exposed_tools.tool_marker_names,
            tool_names=self._prompt_tool_names_mapping,
            embed_memory=embed_memory,
        )

        if tag is not None:
            text = self._format_prompt_tag(text, tag=tag, tag_name_attr=tag_name_attr)

        return text

    def create_connection_prompt(self) -> str:
        """
        Returns the bootstrap prompt to be sent at MCP connection time.

        :return: the prompt
        """
        return self.prompt_factory.create_connection_prompt()

    def _create_global_memory_manager(self) -> MemoryManager:
        """
        :return: a memory manager for global memories only (no project memories)
        """
        return MemoryManager(serena_data_folder=None, read_only_memory_patterns=self.serena_config.read_only_memory_patterns)

    def _get_memory_manager(self) -> MemoryManager:
        """
        :return: the memory manager for the active project (if any) or a global memory manager if no project is active
        """
        project = self.get_active_project()
        if project is not None:
            return project.memory_manager
        return self._create_global_memory_manager()

    def create_system_prompt(self, session_id: str = "global") -> str:
        """
        Returns the fixed ChatGPT Serena instruction manual.

        :param session_id: client session ID, or ``global`` at connection time
        :return: rendered instruction prompt
        """
        with self.session_context(session_id):
            prompt_status = self._get_project_prompt_status()
            active_project = self.get_active_project()
            global_memories = self._create_global_memory_manager().list_global_memories()
            global_memories_str = dict_string(global_memories.to_dict()) if len(global_memories) > 0 else ""

            system_prompt = self.prompt_factory.create_system_prompt(
                chatgpt_product_prompt=self._render_prompt(CHATGPT_PRODUCT_PROMPT, tag="product-policy"),
                global_memories_list=global_memories_str,
            )

            if active_project is not None and not prompt_status.is_project_activation_message_already_provided(session_id):
                system_prompt += "\n\n" + self._format_prompt_tag(self.get_project_activation_message(session_id), tag="active-project")
            elif active_project is None and self._project_activation_error:
                system_prompt += f"\n\nNo project is active ({self._project_activation_error})."

            return self._format_prompt_tag(system_prompt, tag="serena")

    def get_project_activation_message(self, session_id: str) -> str:
        """Returns the project information that is supplied upon activation."""
        with self.session_context(session_id):
            runtime = self._session_registry.get_runtime_for_session(session_id)
            if runtime is None and self._startup_project is not None:
                runtime = self._resolve_execution_runtime(session_id)
            assert runtime is not None, "A project must be active before calling this."
            proj = runtime.project

            with self.active_project_context(proj):
                if proj.is_newly_created:
                    msg = f"Created and activated a new project with name '{proj.project_name}' at {proj.project_root}.\n"
                else:
                    msg = f"The project with name '{proj.project_name}' at {proj.project_root} is activated.\n"
                language_servers_str = ", ".join(ls.value for ls in proj.project_config.language_servers) or "none"
                auto_detection = "enabled" if proj.project_config.auto_detect_language_servers else "disabled"
                msg += (
                    f"Configured language servers: {language_servers_str}; automatic detection: {auto_detection}. "
                    "Language servers start lazily when semantic tools need them.\n"
                )
                msg += f"File encoding: {proj.project_config.encoding}.\n"

                if runtime.active_tools.contains_tool_class(ReadMemoryTool):
                    project_memories = runtime.memory_manager.list_project_memories()
                    if project_memories:
                        msg += (
                            f"{json.dumps(project_memories.to_dict())}\n"
                            + f"Use the `{ReadMemoryTool.get_name_from_cls()}` tool to read these memories later if they are relevant to the task.\n"
                        )
                    elif runtime.active_tools.contains_tool_class(OnboardingTool):
                        msg += (
                            f"Onboarding has not been performed yet. Ask the user whether to perform onboarding via the "
                            f"`{OnboardingTool.get_name_from_cls()}` tool.\n"
                        )

                if proj.project_config.initial_prompt:
                    msg += "\n" + self._render_prompt(proj.project_config.initial_prompt, tag="project-instructions")

                runtime.prompt_status.mark_project_activation_message_as_provided(session_id)
                return msg

    def _create_active_tools_for_project(self, project: Project | None) -> AvailableTools:
        """Returns the fixed ChatGPT tools permitted by the current project policy."""
        if project is not None and project.project_config.read_only:
            return self._exposed_tools.without_editing_tools()
        return self._exposed_tools

    def _create_project_runtime(self, project: Project) -> ProjectRuntime:
        """Creates the isolated mutable execution state for one cached project."""
        project.set_agent(self)
        return ProjectRuntime(
            project=project,
            active_tools=self._create_active_tools_for_project(project),
        )

    def _start_project_runtime_initialization(self, runtime: ProjectRuntime) -> None:
        """Starts one runtime's activation command and language-server initialisation exactly once."""

        def initialize() -> None:
            self._run_project_activation_command(runtime.project)
            self._init_project_language_servers(runtime.project)

        runtime.readiness.start(
            initialize,
            thread_name=f"SerenaProjectInit[{runtime.project.project_name}]",
        )

    def _resolve_execution_runtime(self, session_id: str) -> ProjectRuntime | None:
        """Pins the runtime selected by ``session_id`` at submission time."""
        runtime = self._session_registry.get_runtime_for_session(session_id)
        if runtime is not None:
            return runtime
        if self._startup_project is None:
            return None

        runtime, runtime_created, _ = self._session_registry.bind(
            session_id,
            self._startup_project,
            self._create_project_runtime,
        )
        if runtime_created:
            self._start_project_runtime_initialization(runtime)
        return runtime

    @contextmanager
    def submission_project_context(self, session_id: str) -> Iterator[None]:
        """Pins the project runtime selected when a model-visible call is submitted.

        The context is propagated by ``asyncio.to_thread`` into the FastMCP worker so a
        concurrent session rebind cannot redirect an already-submitted project operation.
        """
        runtime = self._resolve_execution_runtime(session_id)
        project = runtime.project if runtime is not None else self.get_active_project_for_session(session_id)
        token = self._submission_project_context.set((True, runtime, project))
        try:
            yield
        finally:
            self._submission_project_context.reset(token)

    def execute_tool_call(
        self,
        call: Callable[[], T],
        *,
        access: ExecutionAccess,
        session_id: str,
        execution_id: str | None = None,
        symbolic_read: bool = False,
    ) -> T:
        """Executes one tool call in the runtime selected at submission time."""
        submission_pinned, runtime, project = self._submission_project_context.get()
        if not submission_pinned:
            runtime = self._resolve_execution_runtime(session_id)
            project = runtime.project if runtime is not None else self.get_active_project_for_session(session_id)
        coordinator = runtime.execution_coordinator if runtime is not None else self._unscoped_execution_coordinator

        def bound_call() -> T:
            project_token = self._execution_project_context.set((True, runtime, project))
            execution_token = bind_execution_id(execution_id)
            try:
                with self.session_context(session_id):
                    if runtime is not None and access is not ExecutionAccess.SESSION_CONTROL:
                        runtime.readiness.wait_until_ready()
                    return coordinator.execute(access, call, symbolic_read=symbolic_read)
            finally:
                if access is not ExecutionAccess.READ:
                    try:
                        with self._config_changed_dispatch_lock:
                            self._on_config_changed()
                    except Exception as exc:
                        log.error("Error while propagating Serena state after execution: %s", exc, exc_info=exc)
                reset_execution_id(execution_token)
                self._execution_project_context.reset(project_token)

        return bound_call()

    def _get_project_prompt_status(self) -> ProjectPromptStatus:
        """Returns prompt-provision state for the current execution session."""
        runtime = self._get_project_runtime()
        if runtime is not None:
            return runtime.prompt_status
        return self._no_project_prompt_status

    def _get_active_tools(self) -> AvailableTools:
        """Returns active tools for the current execution session."""
        runtime = self._get_project_runtime()
        if runtime is not None:
            return runtime.active_tools
        return self._no_project_tools

    def execute_task(
        self,
        task: Callable[[], T],
        name: str | None = None,
        logged: bool = True,
        timeout: float | None = None,
        session_id: str | None = None,
        access: ExecutionAccess = ExecutionAccess.WRITE,
    ) -> T:
        """Executes an internal operation synchronously through project coordination.

        ``name``, ``logged`` and ``timeout`` remain accepted for transitional internal callers;
        normal model-visible tool timeouts are enforced at the MCP boundary.
        """
        del name, logged, timeout
        resolved_session_id = session_id or self._session_id_context.get()
        return self.execute_tool_call(
            task,
            access=access,
            session_id=resolved_session_id,
        )

    def _on_config_changed(self) -> None:
        for callback in self._config_changed_callbacks:
            try:
                callback()
            except Exception as e:
                log.error(f"Error in config changed callback {callback}: {e}", exc_info=e)

    def register_config_changed_callback(self, callback: Callable[[], None]) -> None:
        """
        Registers a callback to be called when the agent configuration has (potentially) changed.

        :param callback: the callback function to register
        """
        self._config_changed_callbacks.append(callback)

    def _activate_project(self, project: Project) -> bool:
        """Activates ``project`` for the current session and returns whether the binding changed."""
        session_id = self._session_id_context.get()
        current_runtime = self._session_registry.get_runtime_for_session(session_id)
        if current_runtime is not None and current_runtime.project.project_root == project.project_root:
            return False

        log.info("Activating %s at %s for session %s", project.project_name, project.project_root, session_id)
        runtime, runtime_created, binding_changed = self._session_registry.bind(
            session_id,
            project,
            self._create_project_runtime,
        )
        if not binding_changed:
            return False
        if runtime_created:
            self._start_project_runtime_initialization(runtime)

        if self._project_activation_callback is not None:
            self._project_activation_callback()
        return True

    @staticmethod
    def _run_project_activation_command(project: Project) -> None:
        """
        Runs the given project's activation_command (if set and the project is trusted).
        Failures are logged.
        """
        activation_command = project.project_config.activation_command
        if not activation_command:
            return
        if not project.is_trusted():
            log.warning(
                f"Project path {project.project_root} is not trusted, ignoring activation_command "
                "from project configuration. To trust the project, modify the trusted path patterns "
                "in the global configuration."
            )
            return
        timeout = project.project_config.activation_command_timeout
        cmd = subprocess_util.convert_shell_cmd(activation_command)
        log.info(f"Running activation_command for project '{project.project_name}': {cmd}")
        try:
            with LogTime("Project activation command", logger=log):
                p = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=project.project_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **subprocess_util.subprocess_kwargs(),
                )
                try:
                    _, stderr = p.communicate(timeout=timeout)
                    if p.returncode != 0:
                        log.error(f"activation_command for project '{project.project_name}' failed (exit {p.returncode}): {stderr.strip()}")
                except subprocess.TimeoutExpired:
                    log.error(
                        f"Activation_command for project '{project.project_name}' timed out after "
                        f"{timeout}s; terminating process and continuing with backend initialisation."
                    )
                    terminate_process_tree_with_kill_fallback(p, terminate_timeout=5.0, process_name="activation_command")
        except Exception:
            log.exception(f"Unexpected error running activation_command for project '{project.project_name}'")

    def _init_project_language_servers(self, project: Project) -> None:
        """Initialises language-server services owned by ``project``."""
        with LogTime("Language server initialization", logger=log):
            project.create_language_server_manager()

    def activate_project_from_path_or_name(self, project_root_or_name: str) -> bool:
        """
        Activates a project from a path or registered name.

        If a project path has not yet been registered, Serena creates its project configuration first.

        :return: ``True`` if the session binding changed, otherwise ``False``
        :raises UserFacingError: if the project can neither be found nor created
        """
        project_instance: Project | None = self.serena_config.get_project(project_root_or_name)
        if project_instance is not None:
            if not os.path.isdir(project_instance.project_root):
                raise UserFacingError(
                    f"Project '{project_instance.project_name}' is unavailable: directory does not exist: {project_instance.project_root}"
                )
            log.info("Found registered project '%s' at path %s", project_instance.project_name, project_instance.project_root)
        elif os.path.isdir(project_root_or_name):
            project_instance = self.serena_config.add_project_from_path(project_root_or_name)
            log.info("Added new project %s for path %s", project_instance.project_name, project_instance.project_root)

        if project_instance is None:
            raise UserFacingError(
                f"Project '{project_root_or_name}' not found: Not a valid project name or directory. "
                f"Existing project names: {self.serena_config.project_names}"
            )

        return self._activate_project(project_instance)

    def get_active_tool_names(self) -> list[str]:
        """
        :return: the list of names of the active tools for the current project, sorted alphabetically
        """
        return self._get_active_tools().tool_names

    def tool_is_active(self, tool_name: str) -> bool:
        """
        :param tool_name: the name of the tool to check
        :return: True if the tool is active, False otherwise
        """
        return self._get_active_tools().contains_tool_name(tool_name)

    def tool_is_exposed(self, tool_name: str) -> bool:
        """
        :param tool_name: the name of the tool to check
        :return: True if the tool is in the exposed tool set, False otherwise
        """
        return self._exposed_tools.contains_tool_name(tool_name)

    def get_current_config_overview(self) -> str:
        """Returns the current fixed-runtime configuration and active project/tool state."""
        active_project = self.get_active_project()
        result_str = "Current configuration:\n"
        result_str += f"Serena version: {self.version}\n"
        result_str += f"Loglevel: {self.serena_config.log_level}, trace_lsp_communication={self.serena_config.trace_lsp_communication}\n"
        if active_project is not None:
            result_str += f"Active project: {active_project.project_name}\n"
            result_str += f"Language server status: {active_project.get_language_server_manager_status()}\n"
            result_str += f"Project read-only: {active_project.project_config.read_only}\n"
        else:
            result_str += "No active project\n"
        result_str += "Available projects:\n" + "\n".join(self.serena_config.project_names) + "\n"
        result_str += "Runtime policy: ChatGPT\n"
        result_str += "Active tools:\n"
        active_tool_names = self.get_active_tool_names()
        chunk_size = 4
        for i in range(0, len(active_tool_names), chunk_size):
            result_str += "  " + ", ".join(active_tool_names[i : i + chunk_size]) + "\n"

        inactive_tool_names = [name for name in self._exposed_tools.tool_names if name not in active_tool_names]
        if inactive_tool_names:
            result_str += "Exposed but inactive tools:\n"
            for i in range(0, len(inactive_tool_names), chunk_size):
                result_str += "  " + ", ".join(inactive_tool_names[i : i + chunk_size]) + "\n"

        return result_str

    def reset_language_server_manager(self) -> None:
        """
        Starts/resets the language server manager for the current project
        """
        self.get_active_project_or_raise().create_language_server_manager()

    def add_language_server(self, ls_id: LanguageServerId) -> None:
        """
        Adds a new language server to the active project, spawning it and updating project configuration synchronously.

        :param ls_id: the language server to add
        """
        self.execute_task(
            lambda: self.get_active_project_or_raise().add_language_server(ls_id),
            name=f"AddLanguage:{ls_id.value}",
            access=ExecutionAccess.WRITE,
        )

    def remove_language_server(self, ls_id: LanguageServerId) -> None:
        """
        Removes a language server from the active project, shutting down the respective server and updating the project configuration.

        :param ls_id: the language to remove
        """
        self.execute_task(
            lambda: self.get_active_project_or_raise().remove_language_server(ls_id),
            name=f"RemoveLanguage:{ls_id.value}",
            access=ExecutionAccess.WRITE,
        )

    def get_tool(self, tool_class: type[TTool]) -> TTool:
        return self._all_tools[tool_class]

    def print_tool_overview(self) -> None:
        ToolRegistry().print_tool_overview(self._get_active_tools().tools)

    def __del__(self) -> None:
        if "_session_registry" in self.__dict__:
            self.on_shutdown()

    def on_shutdown(self, timeout: float = 2.0) -> None:
        """Shuts down cached project runtimes and persistent Serena services."""
        log.info("SerenaAgent is shutting down ...")
        if hasattr(self, "_tool_output_store"):
            self._tool_output_store.close()
        self._dashboard_manager = None

        # let in-flight one-time runtime initialisation settle before shutting down project services
        runtimes = self._session_registry.get_runtimes() if hasattr(self, "_session_registry") else []
        for runtime in runtimes:
            runtime.readiness.join(timeout=timeout)

        # each project root has exactly one cached runtime, so shut each project down once
        for runtime in runtimes:
            log.info("Shutting down project '%s' ...", runtime.project.project_name)
            runtime.project.shutdown(timeout=timeout)

        if hasattr(self, "_session_registry"):
            self._session_registry.clear()
        self._startup_project = None

    def shutdown(self) -> None:
        """
        Triggers a hard shutdown of the agent, freeing resources and signalling the process to terminate
        """
        # perform clean-up right away, because kill does not result in normal deletion of the object
        self.on_shutdown()

        # signal process termination
        os.kill(os.getpid(), signal.SIGTERM)

    def get_tool_by_name(self, tool_name: str) -> Tool:
        tool_class = ToolRegistry().get_tool_class_by_name(tool_name)
        return self.get_tool(tool_class)

    def get_active_language_server_ids(self) -> list[LanguageServerId]:
        ls_manager = self.get_language_server_manager()
        if ls_manager is None:
            return []
        return ls_manager.get_active_language_server_ids()

    @contextmanager
    def active_project_context(self, project: Project) -> Iterator[None]:
        """Temporarily pins project-dependent lookups in the current execution context."""
        execution_token = self._execution_project_context.set((True, None, project))
        try:
            yield
        finally:
            self._execution_project_context.reset(execution_token)
