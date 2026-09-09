import os
import re
import shutil as _sh
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_TEST_STATE_DIRECTORY = TemporaryDirectory(prefix="serena-test-state-")
_TEST_STATE_ROOT = Path(_TEST_STATE_DIRECTORY.name)
_SERENA_TEST_HOME = _TEST_STATE_ROOT / "serena"
_SOLIDLSP_TEST_HOME = _TEST_STATE_ROOT / "solidlsp"
os.environ["SERENA_HOME"] = str(_SERENA_TEST_HOME)
os.environ["SOLIDLSP_DIR"] = str(_SOLIDLSP_TEST_HOME)

import pytest
from _pytest.mark import Mark, MarkDecorator
from sensai.util import logging

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.constants import SERENA_MANAGED_DIR_NAME
from serena.project import Project
from serena.util.file_system import GitignoreParser
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings

PYTEST_LOG_LEVEL = logging.DEBUG

logging.configure(level=PYTEST_LOG_LEVEL)

log = logging.getLogger(__name__)


def pytest_configure(config: pytest.Config) -> None:
    if os.getenv("PYCHARM_HOSTED") == "1":
        config.option.patch_pycharm_diff = True

    # default local runs exercise unmarked tests only; PYTEST_MARKERS opt selected marked suites back in
    if not config.option.markexpr:
        marker_names = sorted(marker.split(":", 1)[0].strip() for marker in config.getini("markers"))
        marked_expression = " or ".join(marker_names)
        unmarked_expression = f"not ({marked_expression})"
        requested_markers = os.getenv("PYTEST_MARKERS", "").strip()
        config.option.markexpr = f"({unmarked_expression}) or ({requested_markers})" if requested_markers else unmarked_expression


@pytest.fixture(scope="session")
def resources_dir() -> Path:
    """Path to the test resources directory."""
    current_dir = Path(__file__).parent
    return current_dir / "resources"


class LanguageParamRequest:
    param: LanguageServerId


_LANGUAGE_REPO_ALIASES: dict[LanguageServerId, LanguageServerId] = {}

PYTHON_LANGUAGE_BACKENDS = [LanguageServerId.PYTHON]


def get_repo_path(language: LanguageServerId) -> Path:
    repo_language = _LANGUAGE_REPO_ALIASES.get(language, language)
    return Path(__file__).parent / "resources" / "repos" / repo_language / "test_repo"


def _create_ls(
    ls_id: LanguageServerId,
    repo_path: str | None = None,
    ignored_paths: list[str] | None = None,
    trace_lsp_communication: bool = False,
    ls_specific_settings: dict[LanguageServerId, dict[str, Any]] | None = None,
    workspace_folders: list[str] | None = None,
    additional_workspace_folders: list[str] | None = None,
    solidlsp_dir: Path | None = None,
) -> SolidLanguageServer:
    ignored_paths = ignored_paths or []
    if repo_path is None:
        repo_path = str(get_repo_path(ls_id))
    gitignore_parser = GitignoreParser(str(repo_path))
    for spec in gitignore_parser.get_ignore_specs():
        ignored_paths.extend(spec.patterns)
    config = LanguageServerConfig(
        ls_id=ls_id,
        ignored_paths=ignored_paths,
        trace_lsp_communication=trace_lsp_communication,
        workspace_folders=workspace_folders or ["."],
        additional_workspace_folders=additional_workspace_folders or [],
    )
    effective_solidlsp_dir = str(solidlsp_dir) if solidlsp_dir is not None else str(_SOLIDLSP_TEST_HOME)
    project_data_path = os.path.join(repo_path, SERENA_MANAGED_DIR_NAME)
    return SolidLanguageServer.create(
        config,
        repo_path,
        solidlsp_settings=SolidLSPSettings(
            solidlsp_dir=effective_solidlsp_dir,
            project_data_path=project_data_path,
            ls_specific_settings=ls_specific_settings or {},
        ),
    )


@contextmanager
def start_ls_context(
    ls_id: LanguageServerId,
    repo_path: str | None = None,
    ignored_paths: list[str] | None = None,
    trace_lsp_communication: bool = False,
    ls_specific_settings: dict[LanguageServerId, dict[str, Any]] | None = None,
    workspace_folders: list[str] | None = None,
    additional_workspace_folders: list[str] | None = None,
    solidlsp_dir: Path | None = None,
) -> Iterator[SolidLanguageServer]:
    ls = _create_ls(
        ls_id,
        repo_path,
        ignored_paths,
        trace_lsp_communication,
        ls_specific_settings,
        workspace_folders,
        additional_workspace_folders,
        solidlsp_dir,
    )
    log.info(f"Starting language server for {ls_id} {repo_path}")
    with ls.start_server_context():
        yield ls


@contextmanager
def start_default_ls_context(ls_id: LanguageServerId) -> Iterator[SolidLanguageServer]:
    with start_ls_context(ls_id) as ls:
        yield ls


def create_default_serena_config():
    return SerenaConfig(log_level=PYTEST_LOG_LEVEL).with_headless_mode_overrides()


def _create_default_project(ls_id: LanguageServerId, repo_root_override: str | None = None) -> Project:
    repo_path = str(get_repo_path(ls_id)) if repo_root_override is None else repo_root_override
    return Project.load(repo_path, serena_config=create_default_serena_config())


@pytest.fixture(scope="session")
def repo_path(request: LanguageParamRequest) -> Path:
    """Get the repository path for a specific language.

    This fixture requires a language parameter via pytest.mark.parametrize:

    Example:
    ```
    @pytest.mark.parametrize("repo_path", [Language.PYTHON], indirect=True)
    def test_python_repo(repo_path):
        assert (repo_path / "src").exists()
    ```

    """
    if not hasattr(request, "param"):
        raise ValueError("Language parameter must be provided via pytest.mark.parametrize")

    language = request.param
    return get_repo_path(language)


# Note: using module scope here to avoid restarting LS for each test function but still terminate between test modules
@pytest.fixture(scope="module")
def language_server(request: LanguageParamRequest):
    """Create a language server instance configured for the specified language.

    This fixture requires a language parameter via pytest.mark.parametrize:

    Example:
    ```
    @pytest.mark.parametrize("language_server", [Language.PYTHON], indirect=True)
    def test_python_server(language_server: SyncLanguageServer) -> None:
        # Use the Python language server
        pass
    ```

    You can also test multiple languages in a single test:
    ```
    @pytest.mark.parametrize("language_server", [Language.PYTHON, Language.TYPESCRIPT], indirect=True)
    def test_multiple_languages(language_server: SyncLanguageServer) -> None:
        # This test will run once for each language
        pass
    ```

    """
    if not hasattr(request, "param"):
        raise ValueError("Language parameter must be provided via pytest.mark.parametrize")

    language = request.param
    with start_default_ls_context(language) as ls:
        yield ls


@contextmanager
def project_context(ls_id: LanguageServerId, repo_root_override: str | None = None) -> Iterator[Project]:
    """Context manager that creates a Project for the specified language and ensures proper cleanup."""
    project = _create_default_project(ls_id, repo_root_override)
    try:
        yield project
    finally:
        project.shutdown(timeout=5)


@pytest.fixture(scope="module")
def project(request: LanguageParamRequest, repo_root_override: str | None = None) -> Iterator[Project]:
    """Create a Project for the specified language.

    This fixture requires a language parameter via pytest.mark.parametrize:

    Example:
    ```
    @pytest.mark.parametrize("project", [Language.PYTHON], indirect=True)
    def test_python_project(project: Project) -> None:
        # Use the Python project to test something
        pass
    ```

    You can also test multiple languages in a single test:
    ```
    @pytest.mark.parametrize("project", [Language.PYTHON, Language.TYPESCRIPT], indirect=True)
    def test_multiple_languages(project: SyncLanguageServer) -> None:
        # This test will run once for each language
        pass
    ```

    """
    if not hasattr(request, "param"):
        raise ValueError("Language parameter must be provided via pytest.mark.parametrize")
    language = request.param
    with project_context(language, repo_root_override) as project:
        yield project


@contextmanager
def project_with_ls_context(ls_id: LanguageServerId, repo_root_override: str | None = None) -> Iterator[Project]:
    """Context manager that creates a Project with an active language server for the specified language."""
    with project_context(ls_id, repo_root_override) as project:
        project.create_language_server_manager()
        yield project


@contextmanager
def agent_for_project_context(ls_id: LanguageServerId, repo_root_override: str | None = None) -> Iterator[SerenaAgent]:
    project_root = str(get_repo_path(ls_id)) if repo_root_override is None else repo_root_override
    agent = SerenaAgent(project=project_root, serena_config=create_default_serena_config())

    # wait for agent to be ready
    agent.execute_task(lambda: None)

    try:
        yield agent
    finally:
        agent.on_shutdown()


@pytest.fixture(scope="module")
def project_with_ls(request: LanguageParamRequest) -> Iterator[Project]:
    if not hasattr(request, "param"):
        raise ValueError("Language parameter must be provided via pytest.mark.parametrize")
    language = request.param
    with project_with_ls_context(language) as project:
        yield project


is_ci = os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true"
"""
Flag indicating whether the tests are running in the GitHub CI environment.
"""


_LANGUAGE_PYTEST_MARKERS: dict[LanguageServerId, list[MarkDecorator | Mark]] = {
    LanguageServerId.PYTHON: [pytest.mark.python],
    LanguageServerId.TYPESCRIPT: [pytest.mark.typescript],
    LanguageServerId.CPP: [pytest.mark.cpp],
    LanguageServerId.BASH: [pytest.mark.bash],
    LanguageServerId.NIX: [pytest.mark.nix],
    LanguageServerId.MATLAB: [pytest.mark.matlab],
    LanguageServerId.MARKDOWN: [pytest.mark.markdown],
    LanguageServerId.LATEX: [pytest.mark.latex],
    LanguageServerId.YAML: [pytest.mark.yaml],
    LanguageServerId.JSON: [pytest.mark.json],
    LanguageServerId.TOML: [pytest.mark.toml],
    LanguageServerId.HTML: [pytest.mark.html],
    LanguageServerId.SCSS: [pytest.mark.scss],
}


def get_pytest_markers(ls_id: LanguageServerId) -> list[MarkDecorator | Mark]:
    """Pytest markers for a language server.

    Returns the primary language server marker plus the central enablement skip derived from
    ``language_tests_enabled()`` -- so per-language availability/reliability lives in exactly one
    place (``_determine_disabled_languages``) instead of being duplicated per marker or per test file.
    """
    return [
        *_LANGUAGE_PYTEST_MARKERS[ls_id],
        pytest.mark.skipif(not language_server_tests_enabled(ls_id), reason=f"{ls_id.value} tests are disabled in this environment"),
    ]


def _is_matlab_available() -> bool:
    """Whether MATLAB can be located on the supported Linux host."""
    matlab_path = os.environ.get("MATLAB_PATH")
    if matlab_path:
        return Path(matlab_path).is_dir()
    return any(any(Path(base).glob("R*")) for base in ("/usr/local/MATLAB", "/opt/MATLAB", str(Path.home() / "MATLAB")))


def _determine_disabled_language_servers() -> list[LanguageServerId]:
    """Determines retained language-server suites unavailable in this environment."""
    result: list[LanguageServerId] = []
    if _sh.which("clangd") is None:
        result.append(LanguageServerId.CPP)
    if not _is_matlab_available():
        result.append(LanguageServerId.MATLAB)
    if _sh.which("nixd") is None:
        result.append(LanguageServerId.NIX)
    return result


_disabled_language_servers = _determine_disabled_language_servers()


def language_server_tests_enabled(ls_id: LanguageServerId) -> bool:
    """
    Check if tests for the given language server are enabled in the current environment.

    :param ls_id: the language server to check
    :return: True if tests for the language are enabled, False otherwise
    """
    return ls_id not in _disabled_language_servers


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip unavailable language suites consistently, including module-level marker-only tests."""
    del config
    for item in items:
        for marker in item.iter_markers():
            try:
                ls_id = LanguageServerId(marker.name)
            except ValueError:
                continue
            if not language_server_tests_enabled(ls_id):
                item.add_marker(pytest.mark.skip(reason=f"{ls_id.value} tests are disabled in this environment"))
                break


def ls_supports_implementation(language: LanguageServerId) -> bool:
    return language.supports_implementation_request()


def language_servers_supporting_implementation(*languages: LanguageServerId) -> list[LanguageServerId]:
    return [language for language in languages if ls_supports_implementation(language)]


_VERIFIED_IMPLEMENTATION_LANGUAGES = {LanguageServerId.TYPESCRIPT}


def ls_has_verified_implementation_support(language: LanguageServerId) -> bool:
    """
    True only for languages where the server advertises implementation support and
    the repo fixtures contain a verified working go-to-implementation scenario.
    """
    return language in _VERIFIED_IMPLEMENTATION_LANGUAGES and ls_supports_implementation(language)


def find_identifier_position(file_path: Path, identifier: str) -> tuple[int, int] | None:
    pattern = re.compile(r"\b" + re.escape(identifier) + r"\b")
    with file_path.open(encoding="utf-8") as f:
        for line_idx, line in enumerate(f):
            match = pattern.search(line)
            if match:
                return line_idx, match.start()
    return None


def find_identifier_pos(
    file_path: Path,
    identifier: str,
    occurrence_index: int = 0,
    column_offset: int = 0,
) -> tuple[int, int] | None:
    if occurrence_index < 0:
        raise ValueError("occurrence_index must be non-negative")
    if column_offset < 0:
        raise ValueError("column_offset must be non-negative")

    pattern = re.compile(r"\b" + re.escape(identifier) + r"\b")
    current_index = 0
    with file_path.open(encoding="utf-8") as f:
        for line_idx, line in enumerate(f):
            for match in pattern.finditer(line):
                if current_index == occurrence_index:
                    return line_idx, match.start() + column_offset
                current_index += 1
    return None
