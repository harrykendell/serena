"""
The Serena Model Context Protocol (MCP) Server
"""

import dataclasses
import os
import re
import shutil
import threading
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Self, TypeVar

from ruamel.yaml.comments import CommentedMap
from sensai.util import logging
from sensai.util.logging import LogTime, datetime_tag
from sensai.util.string import ToStringMixin

from serena.constants import (
    DEFAULT_SOURCE_FILE_ENCODING,
    PROJECT_LOCAL_TEMPLATE_FILE,
    PROJECT_TEMPLATE_FILE,
    RESOURCES_DIR,
    SERENA_CONFIG_TEMPLATE_FILE,
    SERENA_MANAGED_DIR_NAME,
)
from serena.util.yaml import YamlCommentNormalisation, load_yaml, normalise_yaml_comments, save_yaml, transfer_yaml_comments
from solidlsp.ls_config import LanguageServerId

from ..util.class_decorators import singleton
from ..util.dataclass import get_dataclass_default

if TYPE_CHECKING:
    from ..project import Project

log = logging.getLogger(__name__)
T = TypeVar("T")
DEFAULT_TOOL_TIMEOUT: float = 240
DictType = dict | CommentedMap
TDict = TypeVar("TDict", bound=DictType)


@singleton
class SerenaPaths:
    """
    Provides paths to various Serena-related directories and files.
    """

    def __init__(self) -> None:
        home_dir = os.getenv("SERENA_HOME")
        if home_dir is None or home_dir.strip() == "":
            home_dir = str(Path.home() / SERENA_MANAGED_DIR_NAME)
        else:
            home_dir = home_dir.strip()
        self.resources_dir: str = RESOURCES_DIR
        self.serena_user_home_dir: str = home_dir
        global_memories_path = Path(os.path.join(self.serena_user_home_dir, "memories", "global"))
        global_memories_path.mkdir(parents=True, exist_ok=True)
        self.global_memories_path = global_memories_path
        self.last_returned_log_file_path: str | None = None

    def get_next_log_file_path(self, prefix: str) -> str:
        """
        :param prefix: the filename prefix indicating the type of the log file
        :return: the full path to the log file to use
        """
        log_dir = os.path.join(self.serena_user_home_dir, "logs", datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(log_dir, exist_ok=True)
        self.last_returned_log_file_path = os.path.join(log_dir, prefix + "_" + datetime_tag() + f"_{os.getpid()}" + ".txt")
        return self.last_returned_log_file_path

    def get_resource_path(self, *path_elems: str) -> Path:
        return Path(os.path.join(self.resources_dir, *path_elems))

    # TODO: Paths from constants.py should be moved here


class LineEnding(Enum):
    """Line ending convention for file writes."""

    LF = "lf"
    CRLF = "crlf"
    NATIVE = "native"

    @property
    def newline_str(self) -> str | None:
        """The newline parameter value for :func:`open` and :meth:`Path.write_text`.

        Returns ``None`` for native mode (platform default).
        """
        if self is LineEnding.LF:
            return "\n"
        elif self is LineEnding.CRLF:
            return "\r\n"
        return None

    @classmethod
    def from_str(cls, value: str) -> "LineEnding":
        """Parse a string value into a :class:`LineEnding`."""
        try:
            return cls(value.lower())
        except ValueError as e:
            valid = [le.value for le in cls]
            raise ValueError(f"Invalid line_ending: {value!r}. Valid values are: {valid}") from e


@dataclass
class SharedConfig(ToStringMixin):
    """Settings shared by global and project configuration.

    Project-level ``None`` values inherit the corresponding global setting.
    """

    symbol_info_budget: float | None = None
    line_ending: LineEnding | None = None
    read_only_memory_patterns: list[str] = field(default_factory=list)
    ignored_memory_patterns: list[str] = field(default_factory=list)
    ls_specific_settings: dict = field(default_factory=dict)
    """Advanced language-server implementation settings passed to SolidLSP."""


class SerenaConfigError(Exception):
    pass


DEFAULT_PROJECT_SERENA_FOLDER_LOCATION = "$projectDir/" + SERENA_MANAGED_DIR_NAME
"""
The default template for the project Serena folder location.
Uses $projectDir and $projectFolderName as placeholders.
"""


@dataclass(kw_only=True)
class ProjectConfig(SharedConfig):
    project_name: str
    language_servers: list[LanguageServerId]
    auto_detect_language_servers: bool = True
    ignored_paths: list[str] = field(default_factory=list)
    ls_workspace_folders: list[str] = field(default_factory=lambda: ["."])
    ls_additional_workspace_folders: list[str] = field(default_factory=list)
    read_only: bool = False
    ignore_all_files_in_gitignore: bool = True
    initial_prompt: str = ""
    encoding: str = DEFAULT_SOURCE_FILE_ENCODING
    activation_command: str | None = None
    activation_command_timeout: float = 180.0

    # internal fields which are not mapped to/from the configuration file (must start with "_")
    _local_override_keys: list[str] = field(default_factory=list)

    # class-level members
    SERENA_PROJECT_FILE = "project.yml"
    SERENA_LOCAL_PROJECT_FILE = "project.local.yml"
    FIELDS_WITHOUT_DEFAULTS = {"project_name", "language_servers"}
    YAML_COMMENT_NORMALISATION = YamlCommentNormalisation.LEADING
    """
    the comment normalisation strategy to use when loading/saving project configuration files.
    The template file must match this configuration (i.e. it must use leading comments if this is set to LEADING).
    """
    _save_lock = threading.Lock()

    def _tostring_includes(self) -> list[str]:
        return ["project_name"]

    @classmethod
    def autogenerate(
        cls,
        project_root: str | Path,
        serena_config: "SerenaConfig",
        project_name: str | None = None,
        languages: list[LanguageServerId] | None = None,
        save_to_disk: bool = True,
    ) -> Self:
        """Creates the canonical project configuration for a project root.

        Runtime language-server detection is the default. Explicit ``languages`` are persisted
        as preferences while automatically detected servers remain runtime state.
        """
        project_root = Path(project_root).resolve()
        if not project_root.exists():
            raise FileNotFoundError(f"Project root not found: {project_root}")

        with LogTime("Project configuration auto-generation", logger=log):
            project_name = project_name or project_root.name
            languages_to_use = [] if languages is None else [language.value for language in languages]

            config_with_comments, _ = cls._load_yaml_dict(PROJECT_TEMPLATE_FILE)
            config_with_comments["project_name"] = project_name
            config_with_comments["language_servers"] = languages_to_use
            config_with_comments["auto_detect_language_servers"] = True

            project_yml_path = serena_config.get_project_yml_location(str(project_root))
            if save_to_disk:
                log.info("Saving project configuration to %s", project_yml_path)
                with cls._save_lock:
                    save_yaml(project_yml_path, config_with_comments)
                project_local_yml_path = os.path.join(os.path.dirname(project_yml_path), cls.SERENA_LOCAL_PROJECT_FILE)
                shutil.copy(PROJECT_LOCAL_TEMPLATE_FILE, project_local_yml_path)

            return cls._from_dict(config_with_comments, local_override_keys=[])

    @classmethod
    def _load_yaml_dict(
        cls,
        yml_path: str,
        comment_normalisation: YamlCommentNormalisation = YamlCommentNormalisation.NONE,
        apply_defaults: bool = True,
    ) -> tuple[CommentedMap, bool]:
        """Loads a current-schema project configuration while preserving YAML comments."""
        data = load_yaml(yml_path, comment_normalisation=comment_normalisation)
        was_complete = True

        if apply_defaults:
            for field_info in dataclasses.fields(cls):
                key = field_info.name
                if key.startswith("_") or key in cls.FIELDS_WITHOUT_DEFAULTS:
                    continue
                if key not in data:
                    was_complete = False
                    data[key] = get_dataclass_default(cls, key)

        return data, was_complete

    @classmethod
    def _from_dict(cls, data: dict[str, Any], local_override_keys: list[str]) -> Self:
        """
        Create a ProjectConfig instance from a (full) configuration dictionary

        :param data: the configuration dictionary; must contain all required fields and use the same field names as
            the ProjectConfig dataclass
        :param local_override_keys: the list of keys that have been overridden from project.local.yml
        """
        # map languages to list of enum items, checking for errors
        ls_ids: list[LanguageServerId] = []
        for ls_str in data["language_servers"]:
            orig_language_str = ls_str
            try:
                ls_str = ls_str.lower()
                ls_id = LanguageServerId(ls_str)
                ls_ids.append(ls_id)
            except ValueError as e:
                raise ValueError(
                    f"Invalid language server: '{orig_language_str}'.\nValid values are: {[l.value for l in LanguageServerId]}"
                ) from e

        # Validate activation_command_timeout
        activation_command_timeout_raw = data.get("activation_command_timeout", 180.0)
        try:
            activation_command_timeout = float(activation_command_timeout_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"activation_command_timeout must be a number, got: {activation_command_timeout_raw}") from e
        if activation_command_timeout <= 0:
            raise ValueError(f"activation_command_timeout must be positive, got: {activation_command_timeout}")

        # Validate symbol_info_budget
        symbol_info_budget_raw = data["symbol_info_budget"]
        symbol_info_budget = symbol_info_budget_raw
        if symbol_info_budget is not None:
            try:
                symbol_info_budget = float(symbol_info_budget_raw)
            except (TypeError, ValueError) as e:
                raise ValueError(f"symbol_info_budget must be a number or null, got: {symbol_info_budget_raw}") from e
            if symbol_info_budget < 0:
                raise ValueError(f"symbol_info_budget cannot be negative, got: {symbol_info_budget}")

        line_ending_value = data.get("line_ending")
        line_ending = LineEnding.from_str(line_ending_value) if line_ending_value else None

        # normalize list-valued project settings
        ignored_paths = data["ignored_paths"] or []
        additional_workspace_folders = data.get("ls_additional_workspace_folders") or []

        return cls(
            project_name=data["project_name"],
            language_servers=ls_ids,
            auto_detect_language_servers=data["auto_detect_language_servers"],
            ignored_paths=ignored_paths,
            ls_workspace_folders=data["ls_workspace_folders"],
            ls_additional_workspace_folders=additional_workspace_folders,
            read_only=data["read_only"],
            read_only_memory_patterns=data.get("read_only_memory_patterns", []),
            ignored_memory_patterns=data.get("ignored_memory_patterns", []),
            ignore_all_files_in_gitignore=data["ignore_all_files_in_gitignore"],
            initial_prompt=data["initial_prompt"],
            encoding=data["encoding"],
            line_ending=line_ending,
            symbol_info_budget=symbol_info_budget,
            ls_specific_settings=data.get("ls_specific_settings", {}),
            activation_command=data.get("activation_command"),
            activation_command_timeout=activation_command_timeout,
            _local_override_keys=local_override_keys,
        )

    def _to_yaml_dict(self) -> dict:
        """
        :return: a yaml-serializable dictionary representation of this configuration
        """
        d = dataclasses.asdict(self)

        # drop internal fields starting with underscore
        keys = list(d.keys())
        for k in keys:
            if k.startswith("_"):
                del d[k]

        # map fields using non-primitive types to a YAML-compatible representation
        d["language_servers"] = [lang.value for lang in self.language_servers]
        d["line_ending"] = self.line_ending.value if self.line_ending is not None else None

        return d

    @classmethod
    def _project_local_yml_path(cls, project_yml_path: str) -> str:
        return os.path.join(os.path.dirname(project_yml_path), cls.SERENA_LOCAL_PROJECT_FILE)

    @classmethod
    def load(
        cls,
        project_root: Path | str,
        serena_config: "SerenaConfig",
        autogenerate: bool = False,
    ) -> Self:
        """Loads the canonical project configuration for a project root.

        :param project_root: the path to the project root
        :param serena_config: the global Serena configuration
        :param autogenerate: whether to create the configuration when it does not exist
        """
        project_root = Path(project_root)
        yaml_path = serena_config.get_project_yml_location(project_root)
        log.debug("Loading project configuration from %s", yaml_path)

        if not os.path.exists(yaml_path):
            if autogenerate:
                return cls.autogenerate(project_root, serena_config)
            raise FileNotFoundError(f"Project configuration file not found: {yaml_path}")

        yaml_data, was_complete = cls._load_yaml_dict(str(yaml_path))
        if "project_name" not in yaml_data:
            yaml_data["project_name"] = project_root.name

        local_yaml_path = cls._project_local_yml_path(str(yaml_path))
        local_override_keys = []
        if os.path.exists(local_yaml_path):
            local_yaml_data, _ = cls._load_yaml_dict(local_yaml_path, apply_defaults=False)
            if local_yaml_data:
                local_override_keys = list(local_yaml_data.keys())
                log.debug("Applying project configuration overrides from %s with keys %s", local_yaml_path, local_override_keys)
                yaml_data.update(local_yaml_data)

        project_config = cls._from_dict(yaml_data, local_override_keys=local_override_keys)
        if not was_complete:
            log.info("Project configuration in %s was incomplete, re-saving with current defaults", yaml_path)
            project_config.save(str(yaml_path), save_project_local_yml=False)
        return project_config

    def save(self, project_yml_path: str, save_project_local_yml: bool = True) -> None:
        """
        Saves the project configuration to disk, updating both the project.yml file and, optionally,
        the project.local.yml file to reflect overridden keys.

        Keys that are overridden by project.local.yml are not updated in project.yml.
        Only keys that are overridden are updated in project.local.yml.

        :param project_yml_path: the path to the project.yml file
        :param save_project_local_yml: whether to also update the project.local.yml file to reflect overridden keys
        """
        config_path = project_yml_path
        log.info("Saving updated project configuration to %s", config_path)

        with self._save_lock:
            # get the current configuration as a dictionary
            cur_dict = self._to_yaml_dict()

            # load commented map from the original file and update all non-overridden keys
            config_with_comments, _ = self._load_yaml_dict(config_path, self.YAML_COMMENT_NORMALISATION)
            for key in cur_dict:
                if key not in self._local_override_keys:
                    config_with_comments[key] = cur_dict[key]

            # transfer missing comments from the template file
            template_config, _ = self._load_yaml_dict(PROJECT_TEMPLATE_FILE, self.YAML_COMMENT_NORMALISATION)
            transfer_yaml_comments(template_config, config_with_comments, self.YAML_COMMENT_NORMALISATION, force_update_all=True)

            # save project.yml
            save_yaml(config_path, config_with_comments)

            # update project.local.yml to reflect overridden keys if necessary
            if save_project_local_yml:
                project_local_yml_path = self._project_local_yml_path(project_yml_path)
                if self._local_override_keys and os.path.exists(project_local_yml_path):
                    log.info("Saving updated local project configuration to %s", project_local_yml_path)
                    local_config_with_comments, _ = self._load_yaml_dict(
                        project_local_yml_path, comment_normalisation=YamlCommentNormalisation.NONE, apply_defaults=False
                    )
                    for key in self._local_override_keys:
                        if key in cur_dict:
                            local_config_with_comments[key] = cur_dict[key]
                    save_yaml(project_local_yml_path, local_config_with_comments)


class RegisteredProject(ToStringMixin):
    def __init__(
        self,
        project_root: str,
        project_config: "ProjectConfig",
        project_instance: Optional["Project"] = None,
    ) -> None:
        """
        Represents a registered project in the Serena configuration.

        :param project_root: the root directory of the project
        :param project_config: the configuration of the project
        :param project_instance: an existing project instance (if already loaded)
        """
        self.project_root = Path(project_root).resolve()
        self.project_config = project_config
        self._project_instance = project_instance

    def _tostring_exclude_private(self) -> bool:
        return True

    @property
    def project_name(self) -> str:
        return self.project_config.project_name

    @classmethod
    def from_project_instance(cls, project_instance: "Project") -> "RegisteredProject":
        return RegisteredProject(
            project_root=project_instance.project_root,
            project_config=project_instance.project_config,
            project_instance=project_instance,
        )

    @classmethod
    def from_project_root(
        cls,
        project_root: str | Path,
        serena_config: "SerenaConfig",
        autogenerate: bool = False,
    ) -> "RegisteredProject":
        """Creates a registered project from an existing project root."""
        project_config = ProjectConfig.load(project_root, serena_config=serena_config, autogenerate=autogenerate)
        return RegisteredProject(project_root=str(project_root), project_config=project_config)

    def matches_root_path(self, path: str | Path) -> bool:
        """
        Check if the given path matches the project root path.

        :param path: the path to check
        :return: True if the path matches the project root, False otherwise (including the case
            where this project's root directory no longer exists, e.g. a removed git worktree)
        """
        try:
            return self.project_root.samefile(Path(path).resolve())
        except OSError:
            # typically raised if the path does not exist (e.g., a removed git worktree)
            return False

    def get_project_instance(self, serena_config: "SerenaConfig") -> "Project":
        """
        Returns the project instance for this registered project, loading it if necessary.
        """
        if self._project_instance is None:
            from ..project import Project

            with LogTime(f"Loading project instance for {self}", logger=log):
                self._project_instance = Project(
                    project_root=str(self.project_root),
                    project_config=self.project_config,
                    serena_config=serena_config,
                )
        return self._project_instance


@dataclass(kw_only=True)
class SerenaConfig(SharedConfig):
    """
    Holds the Serena agent configuration, which is typically loaded from a YAML configuration file
    (when instantiated via :method:`from_config_file`), which is updated when projects are added or removed.
    For testing purposes, it can also be instantiated directly with the desired parameters.
    """

    # *** fields that are mapped directly to/from the configuration file (DO NOT RENAME) ***

    projects: list[RegisteredProject] = field(default_factory=list)
    log_level: int = logging.INFO
    trace_lsp_communication: bool = False
    web_dashboard: bool = True
    web_dashboard_open_on_launch: bool = True
    web_dashboard_listen_address: str = "127.0.0.1"
    web_dashboard_trusted_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT
    """
    timeout for tool calls in seconds; if a tool takes longer than this, it is aborted and an error is returned.
    """

    language_server_idle_timeout: float = 900.0
    """Idle seconds after which lazily managed language servers are stopped and cached."""

    max_memory_chars: int = 150_000
    """Default maximum content length accepted by ``write_memory``."""
    default_max_tool_answer_tokens: int = 4_000
    """Approximate default response budget when retained output paging is available.

    The budget uses a deterministic four-characters-per-token approximation and only constrains implicit defaults. An explicit
    ``max_answer_chars`` supplied by a caller remains an exact character override.
    """

    ignored_paths: list[str] = field(default_factory=list)
    """List of paths to ignore across all projects. Same syntax as gitignore, so you can use * and **.
    These patterns are merged additively with each project's own ignored_paths."""

    project_serena_folder_location: str = DEFAULT_PROJECT_SERENA_FOLDER_LOCATION
    """
    Template for the location of the per-project .serena data folder (memories, caches, etc.).
    Supports the following placeholders:
      - $projectDir: the absolute path to the project root directory
      - $projectFolderName: the name of the project folder
    Examples:
      - "$projectDir/.serena" (default, stores data inside the project)
      - "/projects-metadata/$projectFolderName/.serena" (stores data in a central location)
    """

    trusted_project_path_patterns: list[str] = field(default_factory=lambda: ["**"])
    """
    list of glob patterns for project root directories that are considered trusted.
    The dataclass default "**" trusts all roots when no persisted setting is available; generated
    configuration files use the explicit value from the template.
    """

    ls_priorities: dict[str, int] | None = None
    """
    mapping from language server keys to their priority (higher number = higher priority).
    """

    # settings with overridden defaults

    line_ending: LineEnding = LineEnding.NATIVE
    symbol_info_budget: float = 10.0
    """
    Time budget (seconds) for requests when tools request include_info (currently
    only supported for LSP-based tools).

    If the budget is exceeded, Serena stops issuing further requests and returns partial info results.
    0 disables the budget (no early stopping). Negative values are invalid.
    """

    # *** fields that are NOT mapped to/from the configuration file ***

    _loaded_commented_yaml: CommentedMap | None = None
    _config_file_path: str | None = None
    """
    the path to the configuration file to which updates of the configuration shall be saved;
    if None, the configuration is not saved to disk
    """

    # *** static members ***

    CONFIG_FILE = "serena_config.yml"
    CONFIG_FIELDS_WITH_TYPE_CONVERSION = {"projects", "line_ending"}

    # *** methods ***
    @property
    def config_file_path(self) -> str | None:
        return self._config_file_path

    def _iter_config_file_mapped_fields_without_type_conversion(self) -> Iterator[str]:
        for field_info in dataclasses.fields(self):
            field_name = field_info.name
            if field_name.startswith("_"):
                continue
            if field_name in self.CONFIG_FIELDS_WITH_TYPE_CONVERSION:
                continue
            yield field_name

    def _tostring_includes(self) -> list[str]:
        return ["config_file_path"]

    @classmethod
    def _generate_config_file(cls, config_file_path: str) -> None:
        """
        Generates a Serena configuration file at the specified path from the template file.

        :param config_file_path: the path where the configuration file should be generated
        """
        log.info(f"Auto-generating Serena configuration file in {config_file_path}")
        loaded_commented_yaml = load_yaml(SERENA_CONFIG_TEMPLATE_FILE)
        save_yaml(config_file_path, loaded_commented_yaml)

    @classmethod
    def _determine_config_file_path(cls) -> str:
        """Returns the owned global configuration path."""
        return os.path.join(SerenaPaths().serena_user_home_dir, cls.CONFIG_FILE)

    @classmethod
    def from_config_file(cls, generate_if_missing: bool = True) -> "SerenaConfig":
        """Loads Serena's current global configuration schema."""
        config_file_path = cls._determine_config_file_path()
        if not os.path.exists(config_file_path):
            if not generate_if_missing:
                raise FileNotFoundError(f"Serena configuration file not found: {config_file_path}")
            log.info("Serena configuration file not found at %s, autogenerating", config_file_path)
            cls._generate_config_file(config_file_path)

        try:
            loaded_commented_yaml = load_yaml(config_file_path)
        except Exception as e:
            raise ValueError(f"Error loading Serena configuration from {config_file_path}: {e}") from e

        instance = cls(_loaded_commented_yaml=loaded_commented_yaml, _config_file_path=config_file_path)
        for field_name in instance._iter_config_file_mapped_fields_without_type_conversion():
            setattr(instance, field_name, loaded_commented_yaml.get(field_name, get_dataclass_default(cls, field_name)))

        if "projects" not in loaded_commented_yaml:
            raise SerenaConfigError("`projects` key not found in Serena configuration. Please update your `serena_config.yml` file.")
        instance.projects = []
        for configured_path in loaded_commented_yaml["projects"] or []:
            path = Path(configured_path).resolve()
            try:
                valid_project = path.is_dir() and os.path.isfile(instance.get_project_yml_location(path))
            except OSError as e:
                log.warning("Project path %s is not accessible (%s), skipping", path, e)
                continue
            if not valid_project:
                log.warning("Project path %s does not contain a current Serena project configuration, skipping", path)
                continue
            try:
                project_config = ProjectConfig.load(path, serena_config=instance)
            except Exception as e:
                log.error("Failed to load project configuration for %s: %s", path, e)
                continue
            instance.projects.append(RegisteredProject(project_root=str(path), project_config=project_config))

        line_ending_value = loaded_commented_yaml.get("line_ending")
        instance.line_ending = LineEnding.from_str(line_ending_value) if line_ending_value else get_dataclass_default(cls, "line_ending")
        return instance

    @classmethod
    def init(cls) -> "SerenaConfig":
        """Initialises Serena's global configuration file and returns the loaded configuration."""
        return cls.from_config_file()

    def with_headless_mode_overrides(self) -> "SerenaConfig":
        """
        Modifies this instance to apply overrides for headless mode, where any GUI/user interaction-based features are disabled.
        This is intended to be applied for cases where a `SerenaConfig` instance is needed to instantiate a `SerenaAgent` instance
        while the user is not expected to interact with the system (e.g. a CLI command or a test).

        :return: the instance with overrides applied for headless mode
        """
        self.web_dashboard = False
        return self

    @cached_property
    def project_paths(self) -> list[str]:
        return sorted(str(project.project_root) for project in self.projects)

    @cached_property
    def project_names(self) -> list[str]:
        return sorted(project.project_config.project_name for project in self.projects)

    def get_registered_project(self, project_root_or_name: str, autoregister: bool = False) -> Optional[RegisteredProject]:
        """
        :param project_root_or_name: path to the project root or the name of the project
        :param autoregister: whether to auto-register projects that are not yet registered in Serena's global configuration
            but have an existing project configuration file. Project configuration files are never auto-generated.
        :return: the registered project, or None if not found
        """
        # look for project by name
        project_candidates = []
        for project in self.projects:
            if project.project_config.project_name == project_root_or_name:
                project_candidates.append(project)
        if len(project_candidates) == 1:
            return project_candidates[0]
        elif len(project_candidates) > 1:
            raise ValueError(
                f"Multiple projects found with name '{project_root_or_name}'. Please reference it by location instead. "
                f"Locations: {[p.project_root for p in project_candidates]}"
            )
        # no project found by name; check if it's a path
        if os.path.isdir(project_root_or_name):
            for project in self.projects:
                if project.matches_root_path(project_root_or_name):
                    return project
        # no registered project found; optionally auto-register if a project configuration already exists
        if autoregister:
            config_path = self.get_project_yml_location(project_root_or_name)
            if os.path.isfile(config_path):
                registered_project = RegisteredProject.from_project_root(project_root_or_name, serena_config=self)
                self.add_registered_project(registered_project)
                return registered_project
        # nothing found
        return None

    def get_project(self, project_root_or_name: str) -> Optional["Project"]:
        registered_project = self.get_registered_project(project_root_or_name)
        if registered_project is None:
            return None
        else:
            return registered_project.get_project_instance(serena_config=self)

    def add_registered_project(self, registered_project: RegisteredProject) -> None:
        """
        Adds a registered project, persisting the updated project list
        """
        self.projects.append(registered_project)
        self._persist_projects()

    def add_project_from_path(self, project_root: Path | str) -> "Project":
        """Adds a project, creating its canonical project configuration when needed."""
        from ..project import Project

        project_root = Path(project_root).resolve()
        if not project_root.exists() or not project_root.is_dir():
            raise FileNotFoundError(f"Error: Project directory does not exist: {project_root}")

        for already_registered_project in self.projects:
            if str(already_registered_project.project_root) == str(project_root):
                raise FileExistsError(
                    f"Project with path {project_root} was already added with name '{already_registered_project.project_name}'."
                )

        project_config = ProjectConfig.load(project_root, serena_config=self, autogenerate=True)
        new_project = Project(
            project_root=str(project_root),
            project_config=project_config,
            is_newly_created=True,
            serena_config=self,
        )
        self.add_registered_project(RegisteredProject.from_project_instance(new_project))
        return new_project

    def _persist_projects(self) -> None:
        """
        Persists the list of registered projects, merging it with the list currently found on disk
        (parallel agent instances may have added or removed projects in the meantime).
        """
        if self.config_file_path is None:
            return
        persisted = SerenaConfig.from_config_file()
        combined_projects = []
        handled_project_paths = set()
        for p in persisted.projects + self.projects:
            str_path = str(p.project_root)
            if str_path not in handled_project_paths:
                combined_projects.append(p)
                handled_project_paths.add(str_path)
        persisted.projects = combined_projects
        persisted._save()

    def _save(self) -> None:
        """
        Saves the full configuration to the file from which it was loaded (if any)

        NOTE: This method is private, because it is not usually safe to save a configuration instance used
          at runtime, because it often contains transient overrides (e.g. specified through the CLI)
          that should never be persisted back to the configuration file.
        """
        if self.config_file_path is None:
            return

        assert self._loaded_commented_yaml is not None, "Cannot save configuration without loaded YAML"

        commented_yaml = deepcopy(self._loaded_commented_yaml)

        # update fields with current values
        for field_name in self._iter_config_file_mapped_fields_without_type_conversion():
            commented_yaml[field_name] = getattr(self, field_name)

        # convert project objects into list of paths
        commented_yaml["projects"] = sorted({str(project.project_root) for project in self.projects})

        # convert line ending to string
        commented_yaml["line_ending"] = self.line_ending.value

        # transfer comments from the template file
        normalise_yaml_comments(commented_yaml, YamlCommentNormalisation.LEADING)
        template_yaml = load_yaml(SERENA_CONFIG_TEMPLATE_FILE, comment_normalisation=YamlCommentNormalisation.LEADING)
        transfer_yaml_comments(template_yaml, commented_yaml, YamlCommentNormalisation.LEADING, force_update_all=True)

        save_yaml(self.config_file_path, commented_yaml)

    @staticmethod
    def _resolve_serena_folder_location(template: str, placeholders: dict[str, str]) -> str:
        """
        Resolves a folder location template by replacing known ``$placeholder`` tokens
        and raising on any unrecognised ones.

        :param template: the template string (e.g. ``"$projectDir/.serena"``)
        :param placeholders: mapping from placeholder name (without ``$``) to replacement value
        :return: the resolved absolute path
        :raises SerenaConfigError: if the template contains an unknown ``$placeholder``
        """

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in placeholders:
                raise SerenaConfigError(
                    f"Unknown placeholder '${name}' in project_serena_folder_location. "
                    f"Supported placeholders: {', '.join('$' + k for k in placeholders)}"
                )
            return placeholders[name]

        result = re.sub(r"\$([A-Za-z_]\w*)", _replace, template)
        return os.path.abspath(result)

    def get_configured_project_serena_folder(self, project_root: str | Path) -> str:
        """
        Returns the resolved absolute path to the .serena data folder for a project,
        applying placeholder substitution to ``project_serena_folder_location``
        without any fallback logic.

        :param project_root: the absolute path to the project root directory
        :return: the resolved absolute path to the project's .serena folder
        :raises SerenaConfigError: if the template contains an unknown placeholder
        """
        project_folder_name = Path(project_root).name
        placeholders = {
            "projectDir": str(project_root),
            "projectFolderName": project_folder_name,
        }
        return self._resolve_serena_folder_location(self.project_serena_folder_location, placeholders)

    def get_project_serena_folder(self, project_root: str | Path) -> str:
        """
        Resolves the location of the project's .serena data folder using fallback logic:

        1. If the folder exists at the configured path (``project_serena_folder_location``), use it.
        2. Otherwise, if it exists at the default location inside the project root, use that.
        3. If neither exists, return the configured path (for creation).

        :param project_root: the absolute path to the project root directory
        :return: the resolved absolute path to the .serena data folder
        :raises SerenaConfigError: if the configured template contains an unknown placeholder
        """
        configured_path = self.get_configured_project_serena_folder(project_root)
        if os.path.isdir(configured_path):
            return configured_path
        default_path = os.path.join(str(project_root), SERENA_MANAGED_DIR_NAME)
        if configured_path != default_path and os.path.isdir(default_path):
            return default_path
        return configured_path

    def get_project_yml_location(self, project_root: str | Path) -> str:
        """
        Returns the resolved absolute path to the project.yml configuration file,
        based on the resolved .serena data folder (with fallback logic).

        :param project_root: the absolute path to the project root directory
        :return: the resolved absolute path to the project's project.yml file
        """
        serena_folder = self.get_project_serena_folder(project_root)
        return os.path.join(serena_folder, ProjectConfig.SERENA_PROJECT_FILE)

    def is_trusted_project_path(self, project_root: str | Path) -> bool:
        """
        Checks if the given project root path matches any of the trusted project root patterns.

        :param project_root: the path to the project root directory
        :return: True if the project root is trusted, False otherwise
        """
        from serena.util.text_utils import GlobMatcher

        project_root_str = str(project_root)
        for pattern in self.trusted_project_path_patterns:
            if GlobMatcher(pattern).matches(project_root_str):
                return True
        return False

    def get_ls_priority(self, ls_id: LanguageServerId) -> int:
        """
        Gets the priority value associated with a language server

        :param ls_id: identifies the language server
        :return: the integer priority
        """
        if self.ls_priorities is not None:
            try:
                configured_value = self.ls_priorities.get(ls_id.value)
                if configured_value is not None:
                    return int(configured_value)
            except Exception as e:
                log.error("Error reading language priority for %s: %s. Using default priority.", ls_id.value, e)
        return ls_id.get_priority()
