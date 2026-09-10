"""Project runtime and session binding primitives for the standalone Serena server."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from serena.execution import ProjectExecutionCoordinator, RuntimeReadiness
from serena.git_metrics import GitProjectMetrics
from serena.ls_manager import LanguageServerManager
from serena.memories.memory_manager import MemoryManager
from serena.project import Project
from serena.tools.tools_base import Tool, ToolMarker
from serena.util.inspection import iter_subclasses


class AvailableTools:
    """Represents the tools available for one Serena runtime scope."""

    def __init__(self, tools: list[Tool]):
        self.tools = tools
        self.tool_names = sorted(tool.get_name_from_cls() for tool in tools)
        self._tool_name_set = set(self.tool_names)
        self.tool_marker_names = set()
        for marker_class in iter_subclasses(ToolMarker):
            for tool in tools:
                if isinstance(tool, marker_class):
                    self.tool_marker_names.add(marker_class.__name__)

    def __len__(self) -> int:
        return len(self.tools)

    def contains_tool_name(self, tool_name: str) -> bool:
        return tool_name in self._tool_name_set

    def contains_tool_class(self, tool_class: type[Tool]) -> bool:
        return self.contains_tool_name(tool_class.get_name_from_cls())

    def without_editing_tools(self) -> "AvailableTools":
        """Returns a copy without tools that can mutate project state."""
        return AvailableTools([tool for tool in self.tools if not tool.can_edit()])


class ProjectPromptStatus:
    """Tracks project activation-message provision per MCP session."""

    def __init__(self) -> None:
        self._provided_session_ids: set[str] = set()

    def mark_project_activation_message_as_provided(self, session_id: str) -> None:
        """Marks the activation message as provided for ``session_id``."""
        self._provided_session_ids.add(session_id)

    def is_project_activation_message_already_provided(self, session_id: str) -> bool:
        """Returns whether the activation message was already provided for ``session_id``."""
        return session_id in self._provided_session_ids


@dataclass
class ProjectRuntime:
    """Owns the mutable execution state associated with one loaded project."""

    project: Project
    active_tools: AvailableTools
    git_metrics: GitProjectMetrics
    execution_coordinator: ProjectExecutionCoordinator = field(default_factory=ProjectExecutionCoordinator)
    readiness: RuntimeReadiness = field(default_factory=RuntimeReadiness)
    prompt_status: ProjectPromptStatus = field(default_factory=ProjectPromptStatus)

    @property
    def language_server_manager(self) -> LanguageServerManager | None:
        """Returns the project's lazily initialised language-server manager."""
        return self.project.language_server_manager

    @property
    def memory_manager(self) -> MemoryManager:
        """Returns the project's memory manager."""
        return self.project.memory_manager


class SessionRegistry:
    """Binds every Serena session, including the global startup scope, to cached project runtimes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtimes_by_root: dict[str, ProjectRuntime] = {}
        self._project_root_by_session: dict[str, str] = {}

    def get_runtime_for_session(self, session_id: str) -> ProjectRuntime | None:
        """Returns the runtime currently bound to ``session_id``."""
        with self._lock:
            project_root = self._project_root_by_session.get(session_id)
            if project_root is None:
                return None
            return self._runtimes_by_root.get(project_root)

    def bind(
        self,
        session_id: str,
        project: Project,
        runtime_factory: Callable[[Project], ProjectRuntime],
    ) -> tuple[ProjectRuntime, bool, bool]:
        """Binds ``session_id`` to ``project`` and returns runtime/create/change state."""
        project_root = project.project_root
        with self._lock:
            previous_root = self._project_root_by_session.get(session_id)
            if previous_root == project_root:
                runtime = self._runtimes_by_root[project_root]
                return runtime, False, False

            runtime = self._runtimes_by_root.get(project_root)
            runtime_created = runtime is None
            if runtime is None:
                runtime = runtime_factory(project)
                self._runtimes_by_root[project_root] = runtime
            self._project_root_by_session[session_id] = project_root
            return runtime, runtime_created, True

    def get_runtimes(self) -> list[ProjectRuntime]:
        """Returns a stable snapshot of all cached project runtimes."""
        with self._lock:
            return list(self._runtimes_by_root.values())

    def clear(self) -> None:
        """Drops all session bindings and cached runtimes after their services have shut down."""
        with self._lock:
            self._project_root_by_session.clear()
            self._runtimes_by_root.clear()
