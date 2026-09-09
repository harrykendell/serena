import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from html import escape
from typing import TYPE_CHECKING, Any, Self

from flask import Flask, Response, redirect, request, send_from_directory
from pydantic import BaseModel
from sensai.util import logging
from sensai.util.pickle import dump_pickle, load_pickle

from serena.config.serena_config import SerenaConfig, SerenaPaths
from serena.constants import SerenaPorts
from serena.custom_dashboard import CustomDashboard
from serena.execution import ExecutionAccess
from serena.execution_store import ExecutionRecord
from serena.util.logging import MemoryLogHandler
from serena.util.pypi import PyPIPackageInfo
from serena.util.version import Version

if TYPE_CHECKING:
    from serena.agent import SerenaAgent

log = logging.getLogger(__name__)

# disable Werkzeug's logging to avoid cluttering the output
logging.getLogger("werkzeug").setLevel(logging.WARNING)


class RequestLog(BaseModel):
    start_idx: int = 0


class ResponseLog(BaseModel):
    messages: list[str]
    max_idx: int
    active_project: str | None = None


class ResponseToolNames(BaseModel):
    tool_names: list[str]


class ResponseConfigOverview(BaseModel):
    active_project: dict[str, str | None]
    context: dict[str, str]
    modes: list[dict[str, str]]
    active_tools: list[str]
    registered_projects: list[dict[str, str | bool]]
    available_tools: list[dict[str, str | bool]]
    available_modes: list[dict[str, str | bool]]
    available_contexts: list[dict[str, str | bool]]
    available_memories: list[str] | None
    languages: list[str]
    encoding: str | None
    current_client: str | None
    serena_version: str
    newer_serena_version: str | None


class ResponseAvailableLanguages(BaseModel):
    languages: list[str]


class RequestAddLanguage(BaseModel):
    language: str


class RequestRemoveLanguage(BaseModel):
    language: str


class RequestGetMemory(BaseModel):
    memory_name: str


class ResponseGetMemory(BaseModel):
    content: str
    memory_name: str


class RequestSaveMemory(BaseModel):
    memory_name: str
    content: str


class RequestDeleteMemory(BaseModel):
    memory_name: str


class RequestRenameMemory(BaseModel):
    old_name: str
    new_name: str


class ResponseGetSerenaConfig(BaseModel):
    content: str


class RequestSaveSerenaConfig(BaseModel):
    content: str


class RequestCancelTaskExecution(BaseModel):
    task_id: int


class QueuedExecution(BaseModel):
    task_id: int
    is_running: bool
    name: str
    finished_successfully: bool
    logged: bool

    @staticmethod
    def task_id_for_execution(execution_id: str) -> int:
        """Returns a stable legacy dashboard identifier for an execution record."""
        return int(execution_id[:13], 16)

    @classmethod
    def from_execution_record(cls, record: ExecutionRecord) -> Self:
        """Builds the legacy queued-execution view from canonical execution state."""
        return cls(
            task_id=cls.task_id_for_execution(record.execution_id),
            is_running=record.status == "running",
            name=record.tool_name,
            finished_successfully=record.status == "succeeded",
            logged=True,
        )


class ReadNews:
    def __init__(self, read_ids: list[str], legacy_last_read_id: str | None = None):
        self._read_ids = set(read_ids)
        self._legacy_last_read_id = legacy_last_read_id

    @staticmethod
    def load() -> "ReadNews":
        read_news_path = SerenaPaths().news_read_items_file
        legacy_last_read_id_path = SerenaPaths().news_legacy_last_read_id_file

        def load_legacy_last_read_id() -> str | None:
            if not os.path.exists(legacy_last_read_id_path):
                return None
            with open(legacy_last_read_id_path, encoding="utf-8") as f:
                last_read_news_id = f.read().strip()
                if last_read_news_id == "20262103":
                    last_read_news_id = "20260321"  # fix originally misnamed news id
                return last_read_news_id

        if os.path.exists(read_news_path):
            return load_pickle(read_news_path)
        else:
            instance = ReadNews(read_ids=[], legacy_last_read_id=load_legacy_last_read_id())
            instance._save()
            try:
                os.unlink(legacy_last_read_id_path)
            except:
                pass
            return instance

    def _save(self) -> None:
        dump_pickle(self, SerenaPaths().news_read_items_file)

    def is_read(self, identifier: str) -> bool:
        if identifier in self._read_ids:
            return True
        if self._legacy_last_read_id is not None and identifier <= self._legacy_last_read_id:
            return True
        return False

    def mark_read(self, identifier: str) -> None:
        """
        Marks the given news snippet as read, saving the new state to disk
        """
        self._read_ids.add(identifier)
        self._save()


class SerenaDashboardAPI:
    BASE_PORT = SerenaPorts.DASHBOARD_API_BASE_PORT

    log = logging.getLogger(__qualname__)

    def __init__(
        self,
        memory_log_handler: MemoryLogHandler,
        tool_names: list[str],
        agent: "SerenaAgent",
        host: str = "127.0.0.1",
        trusted_hosts: list[str] | None = None,
        port: int | None = None,
    ) -> None:
        self._memory_log_handler = memory_log_handler
        self._tool_names = tool_names
        self._agent = agent
        self._host = host
        self._port = port
        self._app = Flask(self.__class__.__name__)
        if trusted_hosts:
            self._app.config["TRUSTED_HOSTS"] = trusted_hosts
        self._custom_dashboard = CustomDashboard(self._app, agent)
        self._loaded_news: dict[str, str] = {}
        self._news_ready = threading.Event()
        self._setup_routes()
        self._read_news = ReadNews.load()
        self._newer_serena_version: str | None = None

        # register callback for config changes
        self._current_config_overview: dict[str, Any] | None = None
        self._agent.register_config_changed_callback(self._on_agent_config_changed)

        # start threads for background computations
        # * fetch remote news in background on startup (non-blocking)
        threading.Thread(target=self._fetch_news, daemon=True).start()
        # * determine if a newer Serena version is available
        threading.Thread(target=self._determine_newer_serena_version, daemon=True).start()

    def set_serena_session_name(self, session_id: str, display_name: str) -> str:
        """Sets the retained dashboard name for one ChatGPT conversation."""
        return self._custom_dashboard.set_serena_session_name(session_id, display_name)

    @property
    def memory_log_handler(self) -> MemoryLogHandler:
        return self._memory_log_handler

    def _determine_newer_serena_version(self) -> str | None:
        """
        Checks for availability of a newer Serena version on PyPI and stores it in self._newer_serena_version if found.
        """
        try:
            # query PyPI for the latest released version
            latest_serena_version = PyPIPackageInfo("serena-agent").get_latest_version(timeout_secs=5)

            # compare against the running version
            latest_version = Version(latest_serena_version)
            current_version = Version(self._agent.version)
            log.debug("Latest available Serena version on PyPI: %s, current version: %s", latest_version, current_version)
            if not current_version.is_at_least(*latest_version.components):
                self._newer_serena_version = latest_serena_version
        except Exception as e:
            log.info("Failed to check for newer Serena version on PyPI: %s", e)

    def _setup_routes(self) -> None:
        @self._app.route("/")
        def redirect_to_dashboard() -> Response:
            return redirect("/dashboard/")  # type: ignore[return-value]

        @self._app.route("/dashboard")
        def redirect_dashboard_slash() -> Response:
            return redirect("/dashboard/")  # type: ignore[return-value]

        # Static files
        @self._app.route("/dashboard/<path:filename>")
        def serve_dashboard(filename: str) -> Response:
            if filename == "index.html":
                response = Response(self._custom_dashboard.render_index_html(), mimetype="text/html")
                response.headers["Cache-Control"] = "private, no-store"
                return response

            response = send_from_directory(self._custom_dashboard.static_dir, filename)
            response.headers["Cache-Control"] = "private, max-age=31536000, immutable" if request.args.get("v") else "private, no-cache"
            return response

        @self._app.route("/dashboard/")
        def serve_dashboard_index() -> Response:
            response = Response(self._custom_dashboard.render_index_html(), mimetype="text/html")
            response.headers["Cache-Control"] = "private, no-store"
            return response

        # API routes

        @self._app.route("/heartbeat", methods=["GET"])
        def get_heartbeat() -> dict[str, Any]:
            return {"status": "alive"}

        @self._app.route("/get_log_messages", methods=["POST"])
        def get_log_messages() -> dict[str, Any]:
            request_data = request.get_json()
            if not request_data:
                request_log = RequestLog()
            else:
                request_log = RequestLog.model_validate(request_data)

            result = self._get_log_messages(request_log)
            return result.model_dump()

        @self._app.route("/get_tool_names", methods=["GET"])
        def get_tool_names() -> dict[str, Any]:
            result = self._get_tool_names()
            return result.model_dump()

        @self._app.route("/clear_logs", methods=["POST"])
        def clear_logs() -> dict[str, str]:
            self._memory_log_handler.clear_log_messages()
            return {"status": "cleared"}

        @self._app.route("/get_config_overview", methods=["GET"])
        def get_config_overview() -> dict[str, Any]:
            result = self._current_config_overview
            if result is None:
                raise ValueError("Config overview not yet available")
            return result

        @self._app.route("/shutdown", methods=["PUT"])
        def shutdown() -> dict[str, str]:
            self._agent.shutdown()
            return {"status": "shutting down"}

        @self._app.route("/get_available_languages", methods=["GET"])
        def get_available_languages() -> dict[str, Any]:
            result = self._get_available_languages()
            return result.model_dump()

        @self._app.route("/add_language", methods=["POST"])
        def add_language() -> dict[str, str]:
            request_data = request.get_json()
            if not request_data:
                return {"status": "error", "message": "No data provided"}
            request_add_language = RequestAddLanguage.model_validate(request_data)
            try:
                self._add_language(request_add_language)
                return {"status": "success", "message": f"Language {request_add_language.language} added successfully"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/remove_language", methods=["POST"])
        def remove_language() -> dict[str, str]:
            request_data = request.get_json()
            if not request_data:
                return {"status": "error", "message": "No data provided"}
            request_remove_language = RequestRemoveLanguage.model_validate(request_data)
            try:
                self._remove_language(request_remove_language)
                return {"status": "success", "message": f"Language {request_remove_language.language} removed successfully"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/get_memory", methods=["POST"])
        def get_memory() -> dict[str, Any]:
            request_data = request.get_json()
            if not request_data:
                return {"status": "error", "message": "No data provided"}
            request_get_memory = RequestGetMemory.model_validate(request_data)
            try:
                result = self._get_memory(request_get_memory)
                return result.model_dump()
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/save_memory", methods=["POST"])
        def save_memory() -> dict[str, str]:
            request_data = request.get_json()
            if not request_data:
                return {"status": "error", "message": "No data provided"}
            request_save_memory = RequestSaveMemory.model_validate(request_data)
            try:
                self._save_memory(request_save_memory)
                return {"status": "success", "message": f"Memory {request_save_memory.memory_name} saved successfully"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/delete_memory", methods=["POST"])
        def delete_memory() -> dict[str, str]:
            request_data = request.get_json()
            if not request_data:
                return {"status": "error", "message": "No data provided"}
            request_delete_memory = RequestDeleteMemory.model_validate(request_data)
            try:
                self._delete_memory(request_delete_memory)
                return {"status": "success", "message": f"Memory {request_delete_memory.memory_name} deleted successfully"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/rename_memory", methods=["POST"])
        def rename_memory() -> dict[str, str]:
            request_data = request.get_json()
            if not request_data:
                return {"status": "error", "message": "No data provided"}
            request_rename_memory = RequestRenameMemory.model_validate(request_data)
            try:
                result_message = self._rename_memory(request_rename_memory)
                return {"status": "success", "message": result_message}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/get_serena_config", methods=["GET"])
        def get_serena_config() -> dict[str, Any]:
            try:
                result = self._get_serena_config()
                return result.model_dump()
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/save_serena_config", methods=["POST"])
        def save_serena_config() -> dict[str, str]:
            request_data = request.get_json()
            if not request_data:
                return {"status": "error", "message": "No data provided"}
            request_save_config = RequestSaveSerenaConfig.model_validate(request_data)
            try:
                self._save_serena_config(request_save_config)
                return {"status": "success", "message": "Serena config saved successfully"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/queued_task_executions", methods=["GET"])
        def get_queued_executions() -> dict[str, Any]:
            try:
                current_executions = [record for record in self._agent.execution_store.list_executions() if record.status == "running"]
                response = [QueuedExecution.from_execution_record(record).model_dump() for record in current_executions]
                return {"queued_executions": response, "status": "success"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/cancel_task_execution", methods=["POST"])
        def cancel_task_execution() -> dict[str, Any]:
            request_data = request.get_json()
            try:
                request_cancel_task = RequestCancelTaskExecution.model_validate(request_data)
                for record in self._agent.execution_store.list_executions():
                    if QueuedExecution.task_id_for_execution(record.execution_id) == request_cancel_task.task_id:
                        return {
                            "status": "success",
                            "was_cancelled": False,
                            "message": "In-process tool execution cannot be force-cancelled safely; the project permit is retained until it stops.",
                        }
                return {
                    "status": "success",
                    "was_cancelled": False,
                    "message": f"Task with id {escape(str(request_data.get('task_id')))} not found, maybe execution was already finished",
                }
            except Exception as e:
                return {"status": "error", "message": str(e), "was_cancelled": False}

        @self._app.route("/last_execution", methods=["GET"])
        def get_last_execution() -> dict[str, Any]:
            try:
                completed = [record for record in self._agent.execution_store.list_executions() if record.status != "running"]
                response = QueuedExecution.from_execution_record(completed[0]).model_dump() if completed else None
                return {"last_execution": response, "status": "success"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/fetch_unread_news", methods=["GET"])
        def fetch_unread_news() -> dict[str, dict[str, str] | str]:
            def _fetch_unread_news() -> dict[str, str]:
                """News ids are strings of format YYYYMMDD (publication dates)"""
                self._news_ready.wait()
                all_news = self._loaded_news

                serena_config_creation_date = SerenaConfig.get_config_file_creation_date()
                if serena_config_creation_date is None:
                    # should not normally happen, since config file should exist when the dashboard is started
                    # We assume a fresh installation in this case
                    log.error("Serena config file not found when starting the dashboard")
                    return {}
                serena_config_creation_date = serena_config_creation_date.strftime("%Y%m%d")

                # filter for news after the installation date
                post_installation_news = {k: v for k, v in all_news.items() if k >= serena_config_creation_date}

                # read unread news
                return {k: v for k, v in post_installation_news.items() if not self._read_news.is_read(k)}

            try:
                unread_news = _fetch_unread_news()
                return {"news": unread_news, "status": "success"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self._app.route("/mark_news_snippet_as_read", methods=["POST"])
        def mark_news_snippet_as_read() -> dict[str, str]:
            try:
                request_data = request.get_json()
                news_snippet_id = escape(str(request_data.get("news_snippet_id")))
                self._read_news.mark_read(news_snippet_id)
                return {"status": "success", "message": f"Marked news snippet {news_snippet_id} as read"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

    def _get_log_messages(self, request_log: RequestLog) -> ResponseLog:
        messages = self._memory_log_handler.get_log_messages(from_idx=request_log.start_idx)
        project = self._agent.get_default_project()
        project_name = project.project_name if project else None
        return ResponseLog(messages=messages.messages, max_idx=messages.max_idx, active_project=project_name)

    def _get_tool_names(self) -> ResponseToolNames:
        return ResponseToolNames(tool_names=self._tool_names)

    def _compute_config_overview(self) -> ResponseConfigOverview:
        from serena.config.context_mode import SerenaAgentContext, SerenaAgentMode
        from serena.tools.tools_base import Tool

        # Get active project info
        project = self._agent.get_default_project()
        active_project_name = project.project_name if project else None
        project_info = {
            "name": active_project_name,
            "language": ", ".join([l.value for l in project.project_config.language_servers]) if project else None,
            "path": str(project.project_root) if project else None,
        }

        # Get context info
        context = self._agent.get_context()
        context_info = {
            "name": context.name,
            "description": context.description,
            "path": SerenaAgentContext.get_path(context.name, instance=context),
        }

        # Get active modes
        modes = self._agent.get_active_modes().get_modes(include_background_base_modes=False)
        modes_info = [
            {"name": mode.name, "description": mode.description, "path": SerenaAgentMode.get_path(mode.name, instance=mode)}
            for mode in modes
        ]
        active_mode_names = [mode.name for mode in modes]

        # Get active tools
        active_tools = self._agent.get_active_tool_names()

        # Get registered projects
        registered_projects: list[dict[str, str | bool]] = []
        for proj in self._agent.serena_config.projects:
            registered_projects.append(
                {
                    "name": proj.project_name,
                    "path": str(proj.project_root),
                    "is_active": proj.project_name == active_project_name,
                }
            )

        # Get all available tools (excluding active ones)
        all_tool_names = sorted([tool.get_name_from_cls() for tool in self._agent._all_tools.values()])
        available_tools: list[dict[str, str | bool]] = []
        for tool_name in all_tool_names:
            if tool_name not in active_tools:
                available_tools.append(
                    {
                        "name": tool_name,
                        "is_active": False,
                    }
                )

        # Get all available modes
        all_mode_names = SerenaAgentMode.list_registered_mode_names()
        available_modes: list[dict[str, str | bool]] = []
        for mode_name in all_mode_names:
            try:
                mode_path = SerenaAgentMode.get_path(mode_name)
            except FileNotFoundError:
                # Skip modes that can't be found (shouldn't happen for registered modes)
                continue
            available_modes.append(
                {
                    "name": mode_name,
                    "is_active": mode_name in active_mode_names,
                    "path": mode_path,
                }
            )

        # Get all available contexts
        all_context_names = SerenaAgentContext.list_registered_context_names()
        available_contexts: list[dict[str, str | bool]] = []
        for context_name in all_context_names:
            try:
                context_path = SerenaAgentContext.get_path(context_name)
            except FileNotFoundError:
                # Skip contexts that can't be found (shouldn't happen for registered contexts)
                continue
            available_contexts.append(
                {
                    "name": context_name,
                    "is_active": context_name == context.name,
                    "path": context_path,
                }
            )

        # Get available memories if ReadMemoryTool is active
        available_memories = None
        if self._agent.tool_is_active("read_memory") and project is not None:
            available_memories = project.memory_manager.list_memories().get_full_list()

        # Get list of languages for the active project
        languages = []
        if project is not None:
            languages = [lang.value for lang in project.project_config.language_servers]

        # Get file encoding for the active project
        encoding = None
        if project is not None:
            encoding = project.project_config.encoding

        return ResponseConfigOverview(
            active_project=project_info,
            context=context_info,
            modes=modes_info,
            active_tools=active_tools,
            registered_projects=registered_projects,
            available_tools=available_tools,
            available_modes=available_modes,
            available_contexts=available_contexts,
            available_memories=available_memories,
            languages=languages,
            encoding=encoding,
            current_client=Tool.get_last_tool_call_client_str(),
            serena_version=self._agent.version,
            newer_serena_version=self._newer_serena_version,
        )

    def _on_agent_config_changed(self) -> None:
        self._current_config_overview = self._compute_config_overview().model_dump()

    def _get_available_languages(self) -> ResponseAvailableLanguages:
        from solidlsp.ls_config import LanguageServerId

        def run() -> ResponseAvailableLanguages:
            all_languages = [lang.value for lang in LanguageServerId.iter_all(include_experimental=True)]

            # Filter out already added languages for the active project
            project = self._agent.get_default_project()
            if project:
                current_languages = [lang.value for lang in project.project_config.language_servers]
                available_languages = [lang for lang in all_languages if lang not in current_languages]
            else:
                available_languages = all_languages

            return ResponseAvailableLanguages(languages=sorted(available_languages))

        return self._agent.execute_task(run, logged=False, access=ExecutionAccess.READ)

    def _get_memory(self, request_get_memory: RequestGetMemory) -> ResponseGetMemory:
        def run() -> ResponseGetMemory:
            project = self._agent.get_default_project()
            if project is None:
                raise ValueError("No active project")

            content = project.memory_manager.load_memory(request_get_memory.memory_name)
            return ResponseGetMemory(content=content, memory_name=request_get_memory.memory_name)

        return self._agent.execute_task(run, logged=False, access=ExecutionAccess.READ)

    def _save_memory(self, request_save_memory: RequestSaveMemory) -> None:
        def run() -> None:
            project = self._agent.get_default_project()
            if project is None:
                raise ValueError("No active project")
            project.memory_manager.save_memory(request_save_memory.memory_name, request_save_memory.content, is_tool_context=False)

        self._agent.execute_task(run, logged=True, name="SaveMemory", access=ExecutionAccess.WRITE)

    def _delete_memory(self, request_delete_memory: RequestDeleteMemory) -> None:
        def run() -> None:
            project = self._agent.get_default_project()
            if project is None:
                raise ValueError("No active project")
            project.memory_manager.delete_memory(request_delete_memory.memory_name, is_tool_context=False)

        self._agent.execute_task(run, logged=True, name="DeleteMemory", access=ExecutionAccess.WRITE)

    def _rename_memory(self, request_rename_memory: RequestRenameMemory) -> str:
        def run() -> str:
            project = self._agent.get_default_project()
            if project is None:
                raise ValueError("No active project")

            return project.memory_manager.move_memory(request_rename_memory.old_name, request_rename_memory.new_name, is_tool_context=False)

        return self._agent.execute_task(run, logged=True, name="RenameMemory", access=ExecutionAccess.WRITE)

    def _get_serena_config(self) -> ResponseGetSerenaConfig:
        config_path = self._agent.serena_config.config_file_path
        if config_path is None or not os.path.exists(config_path):
            raise ValueError("Serena config file not found")

        with open(config_path, encoding="utf-8") as f:
            content = f.read()

        return ResponseGetSerenaConfig(content=content)

    def _save_serena_config(self, request_save_config: RequestSaveSerenaConfig) -> None:
        def run() -> None:
            config_path = self._agent.serena_config.config_file_path
            if config_path is None:
                raise ValueError("Serena config file path not set")

            with open(config_path, "w", encoding="utf-8") as f:
                f.write(request_save_config.content)

        self._agent.execute_task(run, logged=True, name="SaveSerenaConfig", access=ExecutionAccess.WRITE)

    # ===== Remote News Methods =====

    # The branch from which news are fetched. Change to a feature branch for testing.
    _NEWS_JSON_URL = "https://oraios-software.de/serena_news.json"

    def _fetch_news(self) -> None:
        """Fetch news.json from GitHub using ETag-based caching and store in memory. Silently ignores network errors."""
        paths = SerenaPaths()

        headers: dict[str, str] = {}
        # Load stored ETag if available
        if os.path.exists(paths.news_etag_file) and os.path.exists(paths.news_file):
            try:
                with open(paths.news_etag_file, encoding="utf-8") as f:
                    stored_etag = f.read().strip()
                if stored_etag:
                    headers["If-None-Match"] = stored_etag
            except Exception:
                log.warning("Failed to read stored news ETag at %s, proceeding without it", paths.news_etag_file, exc_info=True)

        fetched_news_dict = None
        try:
            req = urllib.request.Request(self._NEWS_JSON_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                etag = response.headers.get("ETag", "")
                body = response.read().decode("utf-8")
                # Validate JSON
                fetched_news_dict = json.loads(body)
                # Store news content and ETag
                with open(paths.news_file, "w", encoding="utf-8") as f:
                    f.write(body)
                if etag:
                    with open(paths.news_etag_file, "w", encoding="utf-8") as f:
                        f.write(etag)
                log.info("Remote news updated from %s", self._NEWS_JSON_URL)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                log.debug("Remote news unchanged (304 Not Modified)")
            else:
                log.warning("Failed to fetch remote news (HTTP %d): %s", e.code, e.reason)
        except Exception as e:
            log.warning("Failed to fetch remote news: %s", e)
        if fetched_news_dict is None:
            fetched_news_dict = self._load_previously_fetched_news_data()
        self._loaded_news = fetched_news_dict
        self._news_ready.set()

    @staticmethod
    def _load_previously_fetched_news_data() -> dict[str, str]:
        """Return the news data dict. Uses local cache if available, otherwise falls back to local news files."""
        paths = SerenaPaths()

        if os.path.exists(paths.news_file):
            try:
                with open(paths.news_file, encoding="utf-8") as f:
                    return json.loads(f.read())
            except Exception:
                log.warning("Failed to read cached news data from %s", paths.news_file)
        return {}

    def _add_language(self, request_add_language: RequestAddLanguage) -> None:
        from solidlsp.ls_config import LanguageServerId

        try:
            language = LanguageServerId(request_add_language.language)
        except ValueError:
            raise ValueError(f"Invalid language server identifier: {request_add_language.language}")
        # add_language is already thread-safe
        self._agent.add_language_server(language)

    def _remove_language(self, request_remove_language: RequestRemoveLanguage) -> None:
        from solidlsp.ls_config import LanguageServerId

        try:
            language = LanguageServerId(request_remove_language.language)
        except ValueError:
            raise ValueError(f"Invalid language server identifier: {request_remove_language.language}")
        # remove_language is already thread-safe
        self._agent.remove_language_server(language)

    @staticmethod
    def _find_first_free_port(start_port: int, host: str) -> int:
        port = start_port
        while port <= 65535:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((host, port))
                    return port
            except OSError:
                port += 1

        raise RuntimeError(f"No free ports found starting from {start_port}")

    def run(self, port: int) -> int:
        """
        Runs the dashboard on the given host and port and returns the port number.
        """
        # patch flask.cli.show_server to avoid printing the server info
        from flask import cli

        # ty cannot model reassigning a third-party module's function attribute (it rejects any
        # replacement, even one with an identical signature), so the monkeypatch is suppressed here
        cli.show_server_banner = lambda *args, **kwargs: None  # ty: ignore[invalid-assignment]
        self._app.run(host=self._host, port=port, debug=False, use_reloader=False, threaded=True)
        return port

    def run_in_thread(self) -> tuple[threading.Thread, int]:
        if self._port is None:
            # Port 24282 is reserved for the externally exposed MCP service dashboard.
            # Other Serena processes must not claim it while that service is stopped.
            port = self._find_first_free_port(self.BASE_PORT + 1, self._host)
        else:
            port = self._port
            first_free_port = self._find_first_free_port(port, self._host)
            if first_free_port != port:
                raise RuntimeError(f"Configured dashboard port {port} is already in use on {self._host}")
        log.info("Starting dashboard (listen_address=%s, port=%d)", self._host, port)
        thread = threading.Thread(target=lambda: self.run(port=port), daemon=True)
        thread.start()
        return thread, port


def open_url_in_browser(url: str, use_subprocess: bool = False) -> None:
    """
    Opens the given URL in the user's default web browser,
    optionally using a subprocess to ensure that no output is written to stdout
    (highly problematic when run within a stdio MCP server context)

    :param url: the URL to open
    :param use_subprocess: whether to use a subprocess to opening the URL, making stdio contamination impossible
    """
    if use_subprocess:
        # Use a subprocess to avoid any output from webbrowser.open being written to stdout
        try:
            p = subprocess.Popen(
                [sys.executable, "-c", f"import webbrowser; webbrowser.open({url!r})"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=False,
            )
            threading.Thread(target=p.wait, daemon=True).start()
        except Exception as e:
            # Subprocess creation can fail in rare cases (e.g. on some Linux systems; possibly subprocess/glibc bug)
            # See #1363
            log.error("Failed to open URL (%s) in subprocess; %s", url, e)
    else:
        import webbrowser

        webbrowser.open(url)
