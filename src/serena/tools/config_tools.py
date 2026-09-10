from sensai.util.helper import mark_used

from serena.execution import ExecutionAccess
from serena.tools import Tool, ToolMarkerDoesNotRequireActiveProject


class OpenDashboardTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    Opens the Serena web dashboard in the default web browser.
    The dashboard provides logs, session information, and tool usage statistics.
    """

    def apply(self) -> str:
        """Opens the Serena web dashboard in the default web browser."""
        url = self.agent.get_dashboard_url()
        if self.agent.open_dashboard():
            return f"Opened: {url}"
        return f"Open manually: {url}"


class ActivateProjectTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    Activates a project based on the project name or path.
    """

    # noinspection PyIncorrectDocstring
    # (session_id is injected via apply_ex)
    @classmethod
    def get_execution_access(cls) -> ExecutionAccess:
        """Returns session-control access because activation changes only the calling session binding."""
        return ExecutionAccess.SESSION_CONTROL

    def apply(self, project: str, session_id: str) -> str:
        """
        Activates the project with the given name or path.

        :param project: the name of a registered project to activate or a path to a project directory
        """
        is_new_activation = self.agent.activate_project_from_path_or_name(project)
        mark_used(is_new_activation)
        return self.agent.get_project_activation_message(session_id)


class GetCurrentConfigTool(Tool):
    """Prints Serena's current fixed-runtime configuration and project/tool state."""

    def apply(self) -> str:
        """
        Print Serena's current runtime configuration, active project, and tool state.

        :return: the complete current configuration overview
        """
        return self.agent.get_current_config_overview()
