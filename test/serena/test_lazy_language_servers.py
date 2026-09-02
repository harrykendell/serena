import logging
import threading
from pathlib import Path

from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.ls_manager import LanguageServerManager
from serena.project import Project
from solidlsp.ls_config import LanguageServerId


class _FakeLanguageServer:
    def __init__(self, ls_id: LanguageServerId) -> None:
        self.ls_id = ls_id
        self.running = False
        self.cache_saves = 0
        self.stopped = threading.Event()

    def start(self) -> None:
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def save_cache(self) -> None:
        self.cache_saves += 1

    def stop(self, shutdown_timeout: float = 2.0) -> None:
        self.running = False
        self.stopped.set()


class _FakeFactory:
    def __init__(self) -> None:
        self.created: list[_FakeLanguageServer] = []

    def create_language_server(self, ls_id: LanguageServerId) -> _FakeLanguageServer:
        server = _FakeLanguageServer(ls_id)
        self.created.append(server)
        return server


class _FakeProject:
    def __init__(self, root: Path, candidates: list[LanguageServerId]) -> None:
        self.project_root = str(root)
        self.project_name = "fake"
        self._candidates = candidates

    def determine_language_server_candidates(self) -> list[LanguageServerId]:
        return list(self._candidates)

    def gather_source_files(self) -> list[str]:
        return []


def test_web_languages_are_auto_detected_without_explicit_configuration(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "styles.css").write_text("body {}")
    config = SerenaConfig(log_level=logging.ERROR).with_headless_mode_overrides()
    project = Project(
        project_root=str(tmp_path),
        project_config=ProjectConfig(project_name="web", language_servers=[]),
        serena_config=config,
    )

    candidates = project.determine_language_server_candidates()

    assert LanguageServerId.HTML in candidates
    assert LanguageServerId.SCSS in candidates


def test_explicit_only_project_can_disable_auto_detection(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    config = SerenaConfig(log_level=logging.ERROR).with_headless_mode_overrides()
    project = Project(
        project_root=str(tmp_path),
        project_config=ProjectConfig(project_name="web", language_servers=[], auto_detect_language_servers=False),
        serena_config=config,
    )

    assert project.determine_language_server_candidates() == []


def test_language_server_starts_lazily_and_is_reused(tmp_path: Path) -> None:
    factory = _FakeFactory()
    project = _FakeProject(tmp_path, [LanguageServerId.PYTHON])
    manager = LanguageServerManager.lazy([LanguageServerId.PYTHON], factory, project, idle_timeout=0)

    try:
        assert factory.created == []

        first = manager.get_language_server("module.py")
        second = manager.get_language_server("other.py")

        assert first is second
        assert len(factory.created) == 1
    finally:
        manager.stop_all()


def test_new_file_language_can_be_detected_without_recreating_manager(tmp_path: Path) -> None:
    factory = _FakeFactory()
    project = _FakeProject(tmp_path, [LanguageServerId.PYTHON])
    manager = LanguageServerManager.lazy([LanguageServerId.PYTHON], factory, project, idle_timeout=0)

    try:
        manager.get_language_server("module.py")
        project._candidates.append(LanguageServerId.HTML)

        html_server = manager.get_language_server("index.html")

        assert html_server.ls_id == LanguageServerId.HTML
        assert [server.ls_id for server in factory.created] == [LanguageServerId.PYTHON, LanguageServerId.HTML]
    finally:
        manager.stop_all()


def test_idle_language_server_is_stopped_and_cached(tmp_path: Path) -> None:
    factory = _FakeFactory()
    project = _FakeProject(tmp_path, [LanguageServerId.PYTHON])
    manager = LanguageServerManager.lazy([LanguageServerId.PYTHON], factory, project, idle_timeout=0.1)

    try:
        server = manager.get_language_server("module.py")
        assert isinstance(server, _FakeLanguageServer)
        assert server.stopped.wait(timeout=1.0)
        assert server.cache_saves == 1
        assert manager.get_active_language_server_ids() == []
    finally:
        manager.stop_all()
