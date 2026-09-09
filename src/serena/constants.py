from pathlib import Path

_repo_root_path = Path(__file__).parent.parent.parent.resolve()
_serena_pkg_path = Path(__file__).parent.resolve()
_resources_path = _serena_pkg_path / "resources"

SERENA_MANAGED_DIR_NAME = ".serena"

# TODO: Path-related constants should be moved to SerenaPaths; don't add further constants here.
REPO_ROOT = str(_repo_root_path)
RESOURCES_DIR = str(_resources_path)
SERENA_DASHBOARD_DIR = str(_resources_path / "dashboard")
SERENA_ICON_DIR = str(_resources_path / "icons")

DEFAULT_SOURCE_FILE_ENCODING = "utf-8"
"""The default encoding assumed for project source files."""
SERENA_FILE_ENCODING = "utf-8"
"""The encoding used for Serena's own files, such as configuration files and memories."""

PROJECT_TEMPLATE_FILE = str(_resources_path / "project.template.yml")
PROJECT_LOCAL_TEMPLATE_FILE = str(_resources_path / "project.local.template.yml")
SERENA_CONFIG_TEMPLATE_FILE = str(_resources_path / "serena_config.template.yml")

SERENA_LOG_FORMAT = "%(levelname)-5s %(asctime)-15s [%(threadName)s] %(name)s:%(funcName)s:%(lineno)d - %(message)s"


class SerenaPorts:
    TRAY_MANAGER_PORT = 0x5EA0
    DASHBOARD_API_BASE_PORT = 0x5EDA
