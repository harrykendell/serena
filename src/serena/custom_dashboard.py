"""Fork-specific Serena dashboard kept separate from the upstream dashboard implementation."""

from __future__ import annotations

import base64
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, abort, request
from mcp.server.fastmcp import Audio, Image

from serena.jobs import JobManager, JobStatus
from serena.task_executor import TaskExecutor
from serena.tools.media_tools import get_result_file_link, get_result_media, read_result_file_link
from serena.util.logging import MemoryLogHandler

if TYPE_CHECKING:
    from serena.agent import SerenaAgent

CUSTOM_DASHBOARD_DIR = Path(__file__).parent / "resources" / "kendell_dashboard"
_CUSTOM_DASHBOARD_JOB_LIMIT = 1000
_EXECUTION_FIELD_LIMIT = 12_000
_TASK_THREAD_PATTERN = r"Task-\d+:[^\]]+Tool"
_TOOL_START_RE = re.compile(rf"\[(?P<task>{_TASK_THREAD_PATTERN})\].*? - [a-z0-9_]+: (?P<parameters>.*?); session_id: [^\s]+$", re.DOTALL)
_TOOL_RESULT_RE = re.compile(rf"\[(?P<task>{_TASK_THREAD_PATTERN})\].*? - Result: (?P<result>.*)$", re.DOTALL)
_TOOL_ERROR_RE = re.compile(rf"^ERROR.*?\[(?P<task>{_TASK_THREAD_PATTERN})\].*? - (?P<error>.*)$", re.DOTALL)
_INTERNAL_SESSION_PARAM_RE = re.compile(r",?\s*session_id=(?:'[^']*'|\"[^\"]*\")\s*$")


def _bounded(value: str) -> str:
    """Bounds one dashboard field while retaining the most recent tail of oversized output."""
    if len(value) <= _EXECUTION_FIELD_LIMIT:
        return value
    omitted = len(value) - _EXECUTION_FIELD_LIMIT
    return f"… {omitted} earlier characters omitted …\n{value[-_EXECUTION_FIELD_LIMIT:]}"


@dataclass(frozen=True)
class DashboardMediaContent:
    """Binary media or file content returned by one completed Serena tool execution."""

    data: bytes
    mime_type: str
    media_type: str
    file_name: str | None = None


class DashboardExecutionHistory:
    """Session-local history of Serena tool calls with parameters and bounded results."""

    def __init__(self, agent: SerenaAgent, memory_log_handler: MemoryLogHandler):
        self._agent = agent
        self._lock = threading.Lock()
        self._completed: list[TaskExecutor.TaskInfo] = []
        self._last_captured_future: object | None = None
        self._metadata_by_task_name: dict[str, dict[str, str]] = {}
        self._agent.register_config_changed_callback(self._capture_last_execution)
        add_emit_callback = getattr(memory_log_handler, "add_emit_callback", None)
        if callable(add_emit_callback):
            add_emit_callback(self._observe_log)

    @staticmethod
    def _status(task_info: TaskExecutor.TaskInfo) -> str:
        """Returns the externally visible state of a task execution."""
        if task_info.is_running:
            return "running"
        if not task_info.future.done():
            return "queued"
        if task_info.future.cancelled():
            return "cancelled"
        if task_info.future.exception() is not None:
            return "failed"
        return "completed"

    @staticmethod
    def _is_tool_execution(task_info: TaskExecutor.TaskInfo) -> bool:
        """Returns whether a task represents an externally invoked Serena tool."""
        _, separator, task_name = task_info.name.partition(":")
        return bool(separator) and task_name.endswith("Tool")

    @staticmethod
    def _media_descriptor(task_info: TaskExecutor.TaskInfo) -> dict[str, str] | None:
        """Returns lightweight artifact metadata without materialising binary content."""
        if not task_info.finished_successfully():
            return None

        result = task_info.future.result()
        media = get_result_media(result)
        file_link = get_result_file_link(result)
        if isinstance(media, Image):
            descriptor = {"type": "image"}
        elif isinstance(media, Audio):
            descriptor = {"type": "audio"}
        elif file_link is not None:
            mime_type = file_link.mimeType or "application/octet-stream"
            if mime_type.startswith("image/"):
                descriptor = {"type": "image"}
            elif mime_type.startswith("audio/"):
                descriptor = {"type": "audio"}
            else:
                descriptor = {"type": "file"}
        else:
            return None

        if file_link is not None:
            descriptor["name"] = file_link.name
            descriptor["mime_type"] = file_link.mimeType or "application/octet-stream"
        return descriptor

    def _find_task_info(self, task_id: int) -> TaskExecutor.TaskInfo:
        """Returns one retained task execution by its session-local identifier."""
        current = list(self._agent.get_current_tasks())
        with self._lock:
            completed = list(self._completed)
        for task_info in [*current, *completed]:
            if task_info.task_id == task_id:
                return task_info
        raise KeyError(f"Unknown tool execution {task_id}")

    def _observe_log(self, message: str) -> None:
        """Captures parameters, results and errors from Serena's existing tool execution logs."""
        start_match = _TOOL_START_RE.search(message)
        if start_match is not None:
            with self._lock:
                metadata = self._metadata_by_task_name.setdefault(start_match.group("task"), {})
                parameters = _INTERNAL_SESSION_PARAM_RE.sub("", start_match.group("parameters").strip())
                metadata["parameters"] = _bounded(parameters)
            return

        result_match = _TOOL_RESULT_RE.search(message)
        if result_match is not None:
            with self._lock:
                metadata = self._metadata_by_task_name.setdefault(result_match.group("task"), {})
                metadata["result"] = _bounded(result_match.group("result").strip())
            return

        error_match = _TOOL_ERROR_RE.search(message)
        if error_match is not None:
            with self._lock:
                metadata = self._metadata_by_task_name.setdefault(error_match.group("task"), {})
                metadata["error"] = _bounded(error_match.group("error").strip())

    def _serialize(self, task_info: TaskExecutor.TaskInfo) -> dict[str, Any]:
        """Serializes a task execution and its captured call metadata."""
        with self._lock:
            metadata = dict(self._metadata_by_task_name.get(task_info.name, {}))
        media = self._media_descriptor(task_info)

        describe_output = getattr(self._agent, "describe_tool_execution_output", None)
        descriptor = describe_output(task_info.name) if callable(describe_output) else None
        stream_output_id = getattr(descriptor, "output_id", None)
        if not isinstance(stream_output_id, str):
            stream_output_id = None
        stream_output_chars = getattr(descriptor, "total_chars", None)
        if not isinstance(stream_output_chars, int):
            stream_output_chars = None

        return {
            "task_id": task_info.task_id,
            "name": task_info.name,
            "status": self._status(task_info),
            "finished_successfully": task_info.finished_successfully(),
            "parameters": metadata.get("parameters"),
            "result": None if media is not None else metadata.get("result"),
            "error": metadata.get("error"),
            "media": media,
            "stream_output_id": stream_output_id,
            "stream_output_chars": stream_output_chars,
        }

    def _capture_last_execution(self) -> None:
        """Adds the last completed tool task to the session history once."""
        task_info = self._agent.get_last_executed_task()
        if task_info is None or not task_info.logged or not self._is_tool_execution(task_info):
            return

        with self._lock:
            if task_info.future is self._last_captured_future:
                return
            self._last_captured_future = task_info.future
            self._completed.append(task_info)

    def get_media(self, task_id: int) -> DashboardMediaContent:
        """Returns media or transferable file content from one successful retained tool execution."""
        task_info = self._find_task_info(task_id)
        if not task_info.finished_successfully():
            raise ValueError(f"Tool execution {task_id} has no completed media result")

        result = task_info.future.result()
        media = get_result_media(result)
        file_link = get_result_file_link(result)
        if isinstance(media, Image):
            content = media.to_image_content()
            return DashboardMediaContent(
                data=base64.b64decode(content.data, validate=True),
                mime_type=content.mimeType,
                media_type=content.type,
                file_name=file_link.name if file_link is not None else None,
            )
        if isinstance(media, Audio):
            content = media.to_audio_content()
            return DashboardMediaContent(
                data=base64.b64decode(content.data, validate=True),
                mime_type=content.mimeType,
                media_type=content.type,
                file_name=file_link.name if file_link is not None else None,
            )
        if file_link is None:
            raise ValueError(f"Tool execution {task_id} did not return media or a file")

        mime_type = file_link.mimeType or "application/octet-stream"
        if mime_type.startswith("image/"):
            media_type = "image"
        elif mime_type.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "file"
        return DashboardMediaContent(
            data=read_result_file_link(file_link),
            mime_type=mime_type,
            media_type=media_type,
            file_name=file_link.name,
        )

    def get_output(self, task_id: int) -> dict[str, Any]:
        """Return the newest bounded retained-output tail for one exact tool execution."""
        task_info = self._find_task_info(task_id)
        read_output = getattr(self._agent, "read_tool_execution_tail", None)
        if not callable(read_output):
            raise ValueError(f"Tool execution {task_id} has no retained output")

        page = read_output(task_info.name, _EXECUTION_FIELD_LIMIT)
        if page is None:
            raise ValueError(f"Tool execution {task_id} has no retained output")
        return {
            "status": "success",
            "task_id": task_id,
            "output_id": page.output_id,
            "offset": page.offset,
            "end_offset": page.offset + len(page.content),
            "total_chars": page.total_chars,
            "output": page.content,
        }

    def get_executions(self) -> dict[str, Any]:
        """Returns current tool calls followed by completed session history, newest first."""
        self._capture_last_execution()

        current_task_infos = [task for task in self._agent.get_current_tasks() if task.logged and self._is_tool_execution(task)]
        current = [self._serialize(task) for task in current_task_infos]
        with self._lock:
            completed_task_infos = list(reversed(self._completed))
        completed = [self._serialize(task) for task in completed_task_infos]

        return {
            "status": "success",
            "executions": [*current, *completed],
            "running": sum(item["status"] == "running" for item in current),
            "queued": sum(item["status"] == "queued" for item in current),
            "done": len(completed),
        }


class DashboardSessionOverview:
    """Read-only summary of the Serena state needed by the custom dashboard."""

    def __init__(self, agent: SerenaAgent):
        self._agent = agent

    def get_session(self) -> dict[str, Any]:
        """Returns compact session metadata for the custom dashboard."""
        project = self._agent.get_active_project()
        if project is None:
            project_info = {"name": None, "path": None}
            languages: list[str] = []
            memories: list[str] = []
        else:
            project_info = {"name": project.project_name, "path": str(project.project_root)}
            languages = [language.value for language in project.project_config.language_servers]
            memories = project.memory_manager.list_memories().get_full_list()

        active_tools = self._agent.get_active_tool_names()
        modes = self._agent.get_active_modes().get_modes(include_background_base_modes=False)
        return {
            "status": "success",
            "active_project": project_info,
            "languages": languages,
            "context": self._agent.get_context().name,
            "modes": [mode.name for mode in modes],
            "serena_version": self._agent.version,
            "active_tools": active_tools,
            "total_tools": len(self._agent.get_exposed_tool_instances()),
            "available_memories": memories,
        }


class DashboardMemoryOverview:
    """Read-only access to memories for the active project."""

    def __init__(self, agent: SerenaAgent):
        self._agent = agent

    def get_memory(self, memory_name: str) -> dict[str, Any]:
        """Returns one memory from the currently active project."""
        project = self._agent.get_active_project()
        if project is None:
            raise ValueError("No active project")

        content = project.memory_manager.load_memory(memory_name)
        return {
            "status": "success",
            "memory_name": memory_name,
            "content": content,
        }


class DashboardJobOverview:
    """Read-only overview of retained durable jobs for the custom dashboard."""

    def __init__(self, job_manager: JobManager):
        self._job_manager = job_manager

    def get_jobs(self) -> dict[str, Any]:
        """Returns running jobs followed by retained terminal jobs with lightweight telemetry."""
        snapshots = self._job_manager.list_job_snapshots(limit=_CUSTOM_DASHBOARD_JOB_LIMIT, running_only=False)
        persistence = self._job_manager.persistence_info()

        jobs: list[dict[str, Any]] = []
        running_jobs = 0
        terminal_jobs = 0
        for snapshot in snapshots:
            record = snapshot.record
            runtime = snapshot.runtime
            if record.status is JobStatus.RUNNING:
                running_jobs += 1
            elif record.status.is_terminal:
                terminal_jobs += 1
            jobs.append(
                {
                    "job_id": record.job_id,
                    "label": record.label,
                    "project": record.project_name,
                    "cwd": record.cwd,
                    "status": record.status.value,
                    "created_at": record.created_at,
                    "finished_at": record.finished_at,
                    "return_code": record.return_code,
                    "status_message": record.status_message,
                    "timeout_seconds": record.timeout_seconds,
                    "elapsed_seconds": runtime.elapsed_seconds,
                    "seconds_since_last_output": runtime.seconds_since_last_output,
                    "memory_bytes": runtime.memory_bytes,
                    "cpu_seconds": runtime.cpu_seconds,
                    "process_count": runtime.process_count,
                }
            )

        return {
            "status": "success",
            "jobs": jobs,
            "running_jobs": running_jobs,
            "terminal_jobs": terminal_jobs,
            "max_concurrent_jobs": self._job_manager.max_concurrent_jobs,
            "persistence": {
                "survives_serena_restart": persistence.survives_serena_restart,
                "survives_logout": persistence.survives_logout,
                "survives_reboot": persistence.survives_reboot,
                "linger_enabled": persistence.linger_enabled,
            },
        }

    def get_output(self, job_id: str, mode: str, cursor: str | None) -> dict[str, Any]:
        """Returns one bounded output page for a retained durable job."""
        if mode == "latest":
            if cursor is not None:
                raise ValueError("latest output does not accept a cursor")
            snapshot = self._job_manager.get_job(job_id)
        elif mode == "after":
            if cursor is None:
                raise ValueError("after output requires a cursor")
            snapshot = self._job_manager.get_job(job_id, cursor=cursor)
        elif mode == "before":
            if cursor is None:
                raise ValueError("before output requires a cursor")
            snapshot = self._job_manager.get_job_output_before(job_id, cursor)
        else:
            raise ValueError(f"Unsupported output mode {mode!r}")

        chunk = snapshot.output
        if chunk is None:
            raise RuntimeError(f"No output payload available for job {job_id!r}")
        return {
            "status": "success",
            "job_id": snapshot.record.job_id,
            "job_status": snapshot.record.status.value,
            "output": chunk.output,
            "newest_cursor": chunk.next_cursor,
            "oldest_cursor": chunk.oldest_cursor,
            "has_more_output": chunk.has_more_output,
            "has_earlier_output": chunk.has_earlier_output,
            "output_truncated": chunk.output_truncated,
            "earlier_output_omitted": chunk.earlier_output_omitted,
            "cursor_reset": chunk.cursor_reset,
        }


class CustomDashboard:
    """Fork-specific dashboard integration kept outside Serena's upstream frontend implementation."""

    def __init__(self, app: Flask, agent: SerenaAgent, memory_log_handler: MemoryLogHandler):
        self._session_overview = DashboardSessionOverview(agent)
        self._memory_overview = DashboardMemoryOverview(agent)
        self._execution_history = DashboardExecutionHistory(agent, memory_log_handler)
        self._job_overview = DashboardJobOverview(JobManager())
        self._register_routes(app)

    @property
    def static_dir(self) -> Path:
        """Returns the directory containing the custom dashboard frontend."""
        return CUSTOM_DASHBOARD_DIR

    def _register_routes(self, app: Flask) -> None:
        """Registers all fork-specific APIs under the dashboard URL namespace."""

        @app.route("/dashboard/api/session", methods=["GET"])
        def get_session() -> dict[str, Any]:
            return self._session_overview.get_session()

        @app.route("/dashboard/api/memory", methods=["GET"])
        def get_custom_memory() -> dict[str, Any]:
            try:
                memory_name = request.args.get("name")
                if not memory_name:
                    raise ValueError("Memory name is required")
                return self._memory_overview.get_memory(memory_name)
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @app.route("/dashboard/api/executions", methods=["GET"])
        def get_executions() -> dict[str, Any]:
            return self._execution_history.get_executions()

        @app.route("/dashboard/api/executions/<int:task_id>/media", methods=["GET"])
        def get_execution_media(task_id: int) -> Response:
            try:
                media = self._execution_history.get_media(task_id)
            except (KeyError, ValueError):
                abort(404)

            response = Response(media.data, mimetype=media.mime_type)
            response.headers["Cache-Control"] = "private, max-age=3600"
            return response

        @app.route("/dashboard/api/executions/<int:task_id>/output", methods=["GET"])
        def get_execution_output(task_id: int) -> dict[str, Any]:
            try:
                return self._execution_history.get_output(task_id)
            except (KeyError, ValueError):
                abort(404)

        @app.route("/dashboard/api/jobs", methods=["GET"])
        def get_jobs() -> dict[str, Any]:
            return self._job_overview.get_jobs()

        @app.route("/dashboard/api/jobs/<job_id>/output", methods=["GET"])
        def get_job_output(job_id: str) -> dict[str, Any]:
            mode = request.args.get("mode", "latest")
            cursor = request.args.get("cursor")
            return self._job_overview.get_output(job_id, mode, cursor)
