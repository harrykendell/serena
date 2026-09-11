import logging
import os.path
import subprocess
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from sensai.util.logging import LogTime

from serena.config.serena_config import ProjectConfig, SerenaPaths
from serena.errors import UserFacingError
from serena.util.inspection import detect_language_servers_for_files
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.lsp_protocol_handler.lsp_types import DidChangeWatchedFilesParams, FileChangeType, FileEvent
from solidlsp.settings import SolidLSPSettings

if TYPE_CHECKING:
    from .project import Project

log = logging.getLogger(__name__)


class LanguageServerManagerInitialisationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


class LanguageServerFactory:
    def __init__(
        self,
        project_root: str,
        project_config: ProjectConfig,
        project_data_path: str,
        encoding: str,
        ignored_patterns: list[str],
        ls_timeout: float | None = None,
        ls_specific_settings: dict | None = None,
        trace_lsp_communication: bool = False,
    ):
        self.project_root = project_root
        self.project_config = project_config
        self.project_data_path = project_data_path
        self.encoding = encoding
        self.ignored_patterns = ignored_patterns
        self.ls_timeout = ls_timeout
        self.ls_specific_settings = ls_specific_settings
        self.trace_lsp_communication = trace_lsp_communication

    def create_language_server(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        ls_config = LanguageServerConfig(
            workspace_folders=self.project_config.ls_workspace_folders,
            additional_workspace_folders=self.project_config.ls_additional_workspace_folders,
            ls_id=ls_id,
            ignored_paths=self.ignored_patterns,
            trace_lsp_communication=self.trace_lsp_communication,
            encoding=self.encoding,
        )

        log.info(f"Creating language server instance for {self.project_root}, language={ls_id}.")
        return SolidLanguageServer.create(
            ls_config,
            self.project_root,
            timeout=self.ls_timeout,
            solidlsp_settings=SolidLSPSettings(
                solidlsp_dir=SerenaPaths().serena_user_home_dir,
                project_data_path=self.project_data_path,
                ls_specific_settings=self.ls_specific_settings or {},
            ),
        )


class LanguageServerManager:
    """Manages lazily started language servers for a project."""

    def __init__(
        self,
        language_servers: dict[LanguageServerId, SolidLanguageServer],
        language_server_factory: LanguageServerFactory,
        project: "Project",
        candidate_languages: list[LanguageServerId] | None = None,
        idle_timeout: float = 0.0,
    ) -> None:
        """
        Creates a language-server manager.

        :param language_servers: mapping of already-started language servers
        :param language_server_factory: factory used for lazy server creation
        :param project: owning project
        :param candidate_languages: ordered language-server candidates available for lazy startup
        :param idle_timeout: idle seconds after which running servers are stopped; ``0`` disables idle shutdown
        """
        self._language_servers = dict(language_servers)
        self._language_server_factory = language_server_factory
        self._candidate_languages = list(dict.fromkeys(candidate_languages or list(language_servers.keys())))
        self._project = project
        self._idle_timeout = idle_timeout
        self._last_used_at: dict[LanguageServerId, float] = {ls_id: time.monotonic() for ls_id in self._language_servers}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._file_change_notifier = LanguageServerFileChangeNotifier(project, self)
        self._reaper_thread: threading.Thread | None = None

        if idle_timeout > 0:
            self._reaper_thread = threading.Thread(
                target=self._reap_idle_language_servers,
                name=f"LSIdleReaper:{project.project_name}",
                daemon=True,
            )
            self._reaper_thread.start()

    @classmethod
    def lazy(
        cls,
        candidate_languages: list[LanguageServerId],
        factory: LanguageServerFactory,
        project: "Project",
        idle_timeout: float,
    ) -> "LanguageServerManager":
        """Creates a manager whose candidate language servers are started only when requested."""
        return cls({}, factory, project, candidate_languages=candidate_languages, idle_timeout=idle_timeout)

    @staticmethod
    def from_languages(languages: list[LanguageServerId], factory: LanguageServerFactory, project: "Project") -> "LanguageServerManager":
        """
        Creates a manager with language servers for the given languages using the given factory.
        The language servers are started in parallel threads.

        :param languages: the languages for which to spawn language servers
        :param factory: the factory for language server creation
        :param project: the project for which the language servers are created
        :return: the instance
        """

        class StartLSThread(threading.Thread):
            def __init__(self, ls_id: LanguageServerId):
                super().__init__(target=self._start_language_server, name="StartLS:" + ls_id.value)
                self.ls_id = ls_id
                self.language_server: SolidLanguageServer | None = None
                self.exception: Exception | None = None

            def _start_language_server(self) -> None:
                try:
                    with LogTime(f"Language server startup (language={self.ls_id.value})"):
                        self.language_server = factory.create_language_server(self.ls_id)
                        self.language_server.start()
                        if not self.language_server.is_running():
                            raise RuntimeError(f"Failed to start the language server for language {self.ls_id.value}")
                except Exception as e:
                    log.error(f"Error starting language server for language {self.ls_id.value}: {e}", exc_info=e)
                    self.exception = e

        threads = []
        for language in languages:
            thread = StartLSThread(language)
            thread.start()
            threads.append(thread)

        language_servers: dict[LanguageServerId, SolidLanguageServer] = {}
        exceptions: dict[LanguageServerId, Exception] = {}
        for thread in threads:
            thread.join()
            if thread.exception is not None:
                exceptions[thread.ls_id] = thread.exception
            elif thread.language_server is not None:
                language_servers[thread.ls_id] = thread.language_server

        if exceptions:
            for ls in language_servers.values():
                ls.stop()
            failure_messages = "\n".join([f"{lang.value}: {e}" for lang, e in exceptions.items()])
            raise LanguageServerManagerInitialisationError(f"Failed to start {len(exceptions)} language server(s):\n{failure_messages}")

        return LanguageServerManager(language_servers, factory, project, candidate_languages=languages)

    def _refresh_candidates(self) -> None:
        """Refreshes automatically detected candidates from the current project contents."""
        refreshed = self._project.determine_language_server_candidates()
        with self._lock:
            self._candidate_languages = list(dict.fromkeys(refreshed))

    def _candidate_for_file(self, relative_path: str) -> LanguageServerId | None:
        """Returns the preferred candidate language server for ``relative_path``."""
        filename = os.path.basename(relative_path)
        for ls_id in self._candidate_languages:
            if ls_id.get_source_fn_matcher().is_relevant_filename(filename):
                return ls_id
        return None

    def _touch(self, ls_id: LanguageServerId) -> None:
        with self._lock:
            self._last_used_at[ls_id] = time.monotonic()

    def _create_and_start_language_server(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        try:
            language_server = self._language_server_factory.create_language_server(ls_id)
            language_server.start()
        except OSError as error:
            raise UserFacingError(f"Failed to start the language server for language {ls_id.value}: {error.strerror or error}") from None
        if not language_server.is_running():
            raise UserFacingError(f"Failed to start the language server for language {ls_id.value}.")
        self._language_servers[ls_id] = language_server
        self._touch(ls_id)
        return language_server

    def _ensure_language_server(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        with self._lock:
            ls = self._language_servers.get(ls_id)
            if ls is not None and ls.is_running():
                self._touch(ls_id)
                return ls

            if ls is not None:
                self._language_servers.pop(ls_id, None)
                self._last_used_at.pop(ls_id, None)

            with LogTime(f"Lazy language server startup (language={ls_id.value})"):
                return self._create_and_start_language_server(ls_id)

    def _reap_idle_language_servers(self) -> None:
        check_interval = min(max(self._idle_timeout / 4, 0.05), 60.0)
        while not self._stop_event.wait(check_interval):
            now = time.monotonic()
            with self._lock:
                idle_ids = [ls_id for ls_id, last_used_at in self._last_used_at.items() if now - last_used_at >= self._idle_timeout]
                for ls_id in idle_ids:
                    ls = self._language_servers.pop(ls_id, None)
                    self._last_used_at.pop(ls_id, None)
                    if ls is not None:
                        self._stop_language_server(ls, save_cache=True)

    def get_language_server(self, relative_path: str) -> SolidLanguageServer:
        """Returns the language server suitable for one file path."""
        if os.path.isdir(os.path.join(self._project.project_root, relative_path)):
            raise UserFacingError(f"Expected a file path, got a directory: {relative_path}")

        ls_id = self._candidate_for_file(relative_path)
        if ls_id is None:
            self._refresh_candidates()
            ls_id = self._candidate_for_file(relative_path)
        if ls_id is None:
            raise UserFacingError(f"No language server is available for file: {relative_path}")
        return self._ensure_language_server(ls_id)

    def ensure_language_servers_for_path(self, relative_path: str) -> list[SolidLanguageServer]:
        """Starts language servers required for a directory-scoped semantic query.

        :param relative_path: project-relative directory whose source files define the required servers
        :return: running language servers relevant to the requested subtree
        """
        source_files = self._project.gather_source_files(relative_path=relative_path)
        candidate_languages = detect_language_servers_for_files(source_files, self._candidate_languages)

        if not candidate_languages and source_files:
            self._refresh_candidates()
            candidate_languages = detect_language_servers_for_files(source_files, self._candidate_languages)

        return [self._ensure_language_server(ls_id) for ls_id in candidate_languages]

    def ensure_all_language_servers(self) -> list[SolidLanguageServer]:
        """Starts and returns every known candidate server for complete project-wide semantic queries."""
        return [self._ensure_language_server(ls_id) for ls_id in self._candidate_languages]

    def restart_language_server(self, language: LanguageServerId) -> SolidLanguageServer:
        """Forces recreation and restart of the language server for the given language."""
        if language not in self._candidate_languages:
            raise ValueError(f"No language server for language {language.value} configured or detected; cannot restart")
        with self._lock:
            old_ls = self._language_servers.pop(language, None)
            self._last_used_at.pop(language, None)
            if old_ls is not None:
                self._stop_language_server(old_ls)
            return self._create_and_start_language_server(language)

    def add_language_server(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        """Adds a language-server candidate and starts it immediately."""
        with self._lock:
            if ls_id not in self._candidate_languages:
                self._candidate_languages.append(ls_id)
            return self._ensure_language_server(ls_id)

    def remove_language_server(self, language: LanguageServerId, save_cache: bool = False) -> None:
        """Removes a language-server candidate and stops its running instance, if any."""
        with self._lock:
            if language not in self._candidate_languages:
                raise ValueError(f"No language server for language {language.value} present; cannot remove")
            self._candidate_languages.remove(language)
            ls = self._language_servers.pop(language, None)
            self._last_used_at.pop(language, None)
            if ls is not None:
                self._stop_language_server(ls, save_cache=save_cache)

    def get_active_language_server_ids(self) -> list[LanguageServerId]:
        """Returns language servers that currently have a managed process instance."""
        with self._lock:
            return list(self._language_servers.keys())

    def get_candidate_language_server_ids(self) -> list[LanguageServerId]:
        """Returns configured and automatically detected language-server candidates in precedence order."""
        return list(self._candidate_languages)

    @staticmethod
    def _stop_language_server(ls: SolidLanguageServer, save_cache: bool = False, timeout: float = 2.0) -> None:
        if ls.is_running():
            if save_cache:
                ls.save_cache()
            log.info(f"Stopping language server for language {ls.ls_id} ...")
            ls.stop(shutdown_timeout=timeout)

    def iter_language_servers(self) -> Iterator[SolidLanguageServer]:
        """Iterates currently running language servers without starting additional candidates."""
        with self._lock:
            language_servers = list(self._language_servers.items())
        for ls_id, ls in language_servers:
            if ls.is_running():
                self._touch(ls_id)
                yield ls

    def stop_all(self, save_cache: bool = False, timeout: float = 2.0) -> None:
        """Stops all currently running language servers and the idle-reaper thread."""
        self._stop_event.set()
        reaper_thread = self._reaper_thread
        if reaper_thread is not None and reaper_thread is not threading.current_thread():
            reaper_thread.join(timeout=timeout)

        with self._lock:
            language_servers = list(self._language_servers.values())
            self._language_servers.clear()
            self._last_used_at.clear()
            for ls in language_servers:
                self._stop_language_server(ls, save_cache=save_cache, timeout=timeout)

    def save_all_caches(self) -> None:
        """Saves caches of all currently running language servers."""
        with self._lock:
            language_servers = list(self._language_servers.values())
        for ls in language_servers:
            if ls.is_running():
                ls.save_cache()

    def has_suitable_ls_for_file(self, relative_file_path: str) -> bool:
        if self._candidate_for_file(relative_file_path) is not None:
            return True
        self._refresh_candidates()
        return self._candidate_for_file(relative_file_path) is not None

    def sync_file_system_changes(self, relative_paths: Iterable[str] | None = None) -> int:
        """Synchronizes known or project-wide file-system changes with running language servers."""
        if not self.get_active_language_server_ids():
            return 0
        if relative_paths is None:
            log.info("Polling project file system for language-server changes ...")
        num_changes = self._file_change_notifier.poll_and_notify(relative_paths)
        if relative_paths is None:
            log.info("Project file-system polling complete; %s change events sent to language servers.", num_changes)
        return num_changes


class LanguageServerFileChangeNotifier:
    """Detects project source changes and notifies running language servers."""

    def __init__(self, project: "Project", language_server_manager: LanguageServerManager, initial_poll: bool = True) -> None:
        self._project = project
        self._language_server_manager = language_server_manager
        self._freshness_last_seen_mtimes: dict[str, int] | None = None
        self._freshness_lock = threading.Lock()

        if initial_poll:
            with LogTime("Initialising file change notifier (polling for baseline)"):
                self.poll_and_notify()

    def poll_and_notify(self, relative_paths: Iterable[str] | None = None) -> int:
        """Detects and publishes known-path or project-wide source-file changes.

        :param relative_paths: exact project-relative paths known to have been touched, or ``None``
            when the mutating operation is opaque and the complete project must be checked
        :return: number of change events sent
        """
        if relative_paths is None:
            events = self._poll_project()
        else:
            events = self._poll_paths(relative_paths)
        return self._notify(events)

    def _gather_project_source_files(self) -> list[str]:
        """Returns source files using Git enumeration when available, with filesystem fallback."""
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
            cwd=self._project.project_root,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        if result.returncode != 0:
            return self._project.gather_source_files()

        paths = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        return [rel_path for rel_path in paths if rel_path and not self._project.is_ignored_path(rel_path, ignore_non_source_files=True)]

    def _poll_project(self) -> list[tuple[str, FileChangeType]]:
        """Returns changes discovered by one complete project source-file scan."""
        current: dict[str, int] = {}
        for rel_path in self._gather_project_source_files():
            try:
                current[rel_path] = os.stat(os.path.join(self._project.project_root, rel_path)).st_mtime_ns
            except OSError:
                continue

        with self._freshness_lock:
            previous = self._freshness_last_seen_mtimes
            self._freshness_last_seen_mtimes = current
            if previous is None:
                return []

            events: list[tuple[str, FileChangeType]] = []
            for rel_path, mtime_ns in current.items():
                previous_mtime_ns = previous.get(rel_path)
                if previous_mtime_ns is None:
                    events.append((rel_path, FileChangeType.Created))
                elif mtime_ns != previous_mtime_ns:
                    events.append((rel_path, FileChangeType.Changed))
            events.extend((rel_path, FileChangeType.Deleted) for rel_path in previous if rel_path not in current)
            return events

    def _poll_paths(self, relative_paths: Iterable[str]) -> list[tuple[str, FileChangeType]]:
        """Returns changes for exact paths without walking the project."""
        paths = tuple(dict.fromkeys(relative_paths))
        current: dict[str, int | None] = {}
        for rel_path in paths:
            self._project.validate_relative_path(rel_path)
            absolute_path = os.path.join(self._project.project_root, rel_path)
            try:
                if self._project.is_ignored_path(absolute_path, ignore_non_source_files=True):
                    current[rel_path] = None
                else:
                    current[rel_path] = os.stat(absolute_path).st_mtime_ns
            except (FileNotFoundError, OSError):
                current[rel_path] = None

        with self._freshness_lock:
            if self._freshness_last_seen_mtimes is None:
                self._freshness_last_seen_mtimes = {}
            previous = self._freshness_last_seen_mtimes
            events: list[tuple[str, FileChangeType]] = []
            for rel_path, mtime_ns in current.items():
                previous_mtime_ns = previous.get(rel_path)
                if mtime_ns is None:
                    if previous_mtime_ns is not None:
                        previous.pop(rel_path, None)
                        events.append((rel_path, FileChangeType.Deleted))
                    continue
                previous[rel_path] = mtime_ns
                if previous_mtime_ns is None:
                    events.append((rel_path, FileChangeType.Created))
                elif mtime_ns != previous_mtime_ns:
                    events.append((rel_path, FileChangeType.Changed))
            return events

    def _notify(self, events: list[tuple[str, FileChangeType]]) -> int:
        """Publishes one batch of change events to every running language server."""
        if not events:
            return 0

        changes: list[FileEvent] = [
            {"uri": Path(self._project.project_root, rel_path).resolve().as_uri(), "type": change_type} for rel_path, change_type in events
        ]
        params: DidChangeWatchedFilesParams = {"changes": changes}
        created_paths = [rel_path for rel_path, change_type in events if change_type == FileChangeType.Created]

        for ls in self._language_server_manager.iter_language_servers():
            try:
                ls.server.notify.did_change_watched_files(params)
            except Exception as error:
                log.error("Failed to notify language server of watched file changes", exc_info=error)

            for rel_path in created_paths:
                if ls.is_ignored_path(rel_path, ignore_unsupported_files=True):
                    continue
                try:
                    with ls.open_file(rel_path):
                        pass
                except Exception as error:
                    log.error("Failed to refresh newly created file %r in language server", rel_path, exc_info=error)

        return len(events)
