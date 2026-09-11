from functools import lru_cache
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from serena.execution_store import ActivityPanelRun, ExecutionStore

ACTIVITY_RESOURCE_URI = "ui://serena/activity-v33.html"
_ACTIVITY_RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"
ACTIVITY_RESOURCE_DIR = Path(__file__).parent / "resources" / "activity"


class ActivityRunManager:
    """Owns only activity-run creation, supersession and execution association."""

    def __init__(self, execution_store: ExecutionStore) -> None:
        self._execution_store = execution_store

    @property
    def execution_store(self) -> ExecutionStore:
        """Returns the canonical execution store used for run grouping."""
        return self._execution_store

    def start_run(self, session_id: str, project_name: str) -> ActivityPanelRun:
        """Starts one activity run without creating or completing executions."""
        return self._execution_store.start_activity_run(session_id, project_name)

    def update_project(self, session_id: str, project_name: str) -> None:
        """Updates project attribution for the current run."""
        self._execution_store.update_activity_run_project(session_id, project_name)

    def associate_execution(self, session_id: str, execution_id: str, *, project_name: str = "") -> None:
        """Associates an already-created execution with the current run, if any."""
        self._execution_store.append_execution_to_current_run(session_id, execution_id, project_name=project_name)


def register_activity_resource(mcp: FastMCP) -> None:
    """Registers the compact ChatGPT activity-panel resource."""

    @mcp.resource(
        ACTIVITY_RESOURCE_URI,
        name="Serena activity",
        description="Compact live view of Serena tool calls with lightweight durable-job visibility.",
        mime_type=_ACTIVITY_RESOURCE_MIME_TYPE,
        meta={
            "openai/widgetDescription": "Shows Serena tool calls, current-turn jobs, and a compact indicator for other running jobs.",
        },
    )
    def activity_resource() -> str:
        return activity_widget_html()


@lru_cache(maxsize=1)
def activity_widget_html() -> str:
    """Returns the self-contained activity widget HTML assembled from shared assets."""
    styles = (ACTIVITY_RESOURCE_DIR / "activity-panel.css").read_text(encoding="utf-8")
    renderer = (ACTIVITY_RESOURCE_DIR / "activity-panel.js").read_text(encoding="utf-8")
    host = (ACTIVITY_RESOURCE_DIR / "inline-host.js").read_text(encoding="utf-8")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<style>{styles}</style></head>"
        '<body style="margin:0;background:transparent">'
        '<div id="serena-activity-root"></div>'
        f"<script>{renderer}</script>"
        f"<script>{host}</script>"
        "</body></html>"
    )
