"""
Configuration objects for language servers
"""

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from solidlsp import SolidLanguageServer

log = logging.getLogger(__name__)


class FilenameMatcher:
    def __init__(self, *file_extensions: str, case_sensitive: bool = True) -> None:
        """
        :param file_extensions: file extensions, e.g., `.py, .yml`
        :param case_sensitive: whether the file extensions are case-sensitive.
        """
        self._file_extensions = list(set(file_extensions)) if case_sensitive else list(set(ext.lower() for ext in file_extensions))
        self._case_sensitive = case_sensitive
        # Snapshot of the initial configuration, used by ``reset``. Relevant for matchers that are
        # per-language singletons (``Language.get_source_fn_matcher`` is ``@cache``d): extensions added
        # via ``add_extensions`` for one project must not leak into the next, so the singleton is reset
        # to this snapshot at every language server initialisation.
        self._initial_file_extensions = list(self._file_extensions)

    def reset(self) -> None:
        """
        Restore the matcher to its initial set of extensions (as provided at construction).

        Undoes any extensions added via :meth:`add_extensions`. Intended for the per-language
        singleton matchers, which are reset at the start of every language server initialisation so
        that a previous project's reconfiguration does not leak into a newly activated one.
        """
        self._file_extensions = list(self._initial_file_extensions)

    @property
    def file_extensions(self) -> list[str]:
        """The file extensions currently registered with this matcher, including any added via
        :meth:`add_extensions`. Returned as a copy.
        """
        return list(self._file_extensions)

    def add_extensions(self, *file_extensions: str) -> None:
        """
        Add further file extensions to this matcher (idempotent).

        This is intended for matchers that are per-language singletons, i.e. those returned by
        :meth:`Language.get_source_fn_matcher` (which is ``@cache``d): extensions that a user
        configures for a language server (e.g. ``.cgi`` for Perl) can be added here so that every
        consumer of the matcher — symbol index traversal, ignore checks, language composition —
        treats the same set of files as sources, staying in sync with the language server.

        :param file_extensions: the additional file extensions, e.g. ``.cgi``
        """
        for ext in file_extensions:
            norm = ext if self._case_sensitive else ext.lower()
            if norm not in self._file_extensions:
                self._file_extensions.append(norm)

    def is_relevant_filename(self, fn: str) -> bool:
        if not self._case_sensitive:
            fn = fn.lower()
        for ext in self._file_extensions:
            if fn.endswith(ext):
                return True
        return False

    def string_contains_relevant_filename(self, string: str) -> bool:
        """:return: whether ``string`` contains an occurrence of any registered extension as
        a *complete* extension — i.e. the extension must either end the string or be followed
        by a non-extension-character (anything other than a letter, digit, or underscore).
        """
        if not self._case_sensitive:
            string = string.lower()
        for ext in self._file_extensions:
            if re.search(rf"{re.escape(ext)}(?:\W|$)", string):
                return True
        return False


class LanguageServerId(str, Enum):
    """
    Enumeration of language servers supported by SolidLSP.
    """

    PYTHON = "python"
    TYPESCRIPT = "typescript"
    CPP = "cpp"
    BASH = "bash"
    NIX = "nix"
    MATLAB = "matlab"
    MARKDOWN = "markdown"
    LATEX = "latex"
    YAML = "yaml"
    JSON = "json"
    TOML = "toml"
    HTML = "html"
    SCSS = "scss"

    @classmethod
    def iter_all(cls, include_experimental: bool = True, include_non_programming_languages: bool = True) -> Iterable[Self]:
        for lang in cls:
            if include_experimental or not lang.is_experimental():
                if include_non_programming_languages or lang.is_programming_language():
                    yield lang

    def is_experimental(self) -> bool:
        """Returns whether the retained server is opt-in rather than auto-detected."""
        return self in {
            self.MARKDOWN,
            self.LATEX,
            self.YAML,
            self.JSON,
            self.TOML,
            self.HTML,
            self.SCSS,
        }

    def is_programming_language(self) -> bool:
        """Returns whether the retained server represents a programming language."""
        return self not in {self.MARKDOWN, self.LATEX, self.JSON, self.TOML, self.YAML, self.HTML, self.SCSS}

    def __str__(self) -> str:
        return self.value

    def get_priority(self) -> int:
        """Returns the auto-detection priority for the retained language server."""
        if self.is_experimental():
            return 1 if self in {self.HTML, self.SCSS} else 0
        return 2

    def supports_implementation_request(self) -> bool:
        """
        Return whether the default language server for this language supports ``textDocument/implementation``.
        """
        return self.get_ls_class().supports_implementation_request()

    # NOTE: Caching results in a singleton per enum item, which is a precondition for persistent configuration of the matcher.
    @cache
    def get_source_fn_matcher(self) -> FilenameMatcher:
        match self:
            case self.PYTHON:
                return FilenameMatcher(".py", ".pyi")
            case self.TYPESCRIPT:
                path_patterns = []
                for prefix in ["c", "m", ""]:
                    for postfix in ["x", ""]:
                        for base_pattern in ["ts", "js"]:
                            path_patterns.append(f".{prefix}{base_pattern}{postfix}")
                return FilenameMatcher(*path_patterns)
            case self.CPP:
                return FilenameMatcher(
                    ".c",
                    ".h",
                    ".c++",
                    ".cc",
                    ".cp",
                    ".cpp",
                    ".cxx",
                    ".hh",
                    ".hpp",
                    ".hxx",
                    ".inl",
                    ".ipp",
                    ".tpp",
                    ".txx",
                    ".m",
                    ".mm",
                    ".c++m",
                    ".cppm",
                    ".cxxm",
                    ".ixx",
                    ".cu",
                    ".hip",
                    ".cl",
                    ".clcpp",
                    ".ino",
                    case_sensitive=False,
                )
            case self.BASH:
                return FilenameMatcher(".sh", ".bash")
            case self.NIX:
                return FilenameMatcher(".nix")
            case self.MATLAB:
                return FilenameMatcher(".m", ".mlx", ".mlapp")
            case self.MARKDOWN:
                return FilenameMatcher(".md", ".markdown")
            case self.LATEX:
                return FilenameMatcher(".tex", ".bib", ".sty", ".cls")
            case self.YAML:
                return FilenameMatcher(".yaml", ".yml")
            case self.JSON:
                return FilenameMatcher(".json", ".jsonc")
            case self.TOML:
                return FilenameMatcher(".toml")
            case self.HTML:
                return FilenameMatcher(".html", ".htm")
            case self.SCSS:
                return FilenameMatcher(".scss", ".sass", ".css")
            case _:
                raise ValueError(f"Unhandled language: {self}")

    def get_ls_class(self) -> type["SolidLanguageServer"]:
        match self:
            case self.PYTHON:
                from solidlsp.language_servers.pyright_server import PyrightServer

                return PyrightServer
            case self.TYPESCRIPT:
                from solidlsp.language_servers.typescript_language_server import TypeScriptLanguageServer

                return TypeScriptLanguageServer
            case self.CPP:
                from solidlsp.language_servers.clangd_language_server import ClangdLanguageServer

                return ClangdLanguageServer
            case self.BASH:
                from solidlsp.language_servers.bash_language_server import BashLanguageServer

                return BashLanguageServer
            case self.NIX:
                from solidlsp.language_servers.nixd_ls import NixLanguageServer

                return NixLanguageServer
            case self.MATLAB:
                from solidlsp.language_servers.matlab_language_server import MatlabLanguageServer

                return MatlabLanguageServer
            case self.MARKDOWN:
                from solidlsp.language_servers.marksman import Marksman

                return Marksman
            case self.LATEX:
                from solidlsp.language_servers.texlab_language_server import TexlabLanguageServer

                return TexlabLanguageServer
            case self.YAML:
                from solidlsp.language_servers.yaml_language_server import YamlLanguageServer

                return YamlLanguageServer
            case self.JSON:
                from solidlsp.language_servers.json_language_server import JsonLanguageServer

                return JsonLanguageServer
            case self.TOML:
                from solidlsp.language_servers.taplo_server import TaploServer

                return TaploServer
            case self.HTML:
                from solidlsp.language_servers.vscode_html_language_server import VsCodeHtmlLanguageServer

                return VsCodeHtmlLanguageServer
            case self.SCSS:
                from solidlsp.language_servers.some_sass_language_server import SomeSassLanguageServer

                return SomeSassLanguageServer
            case _:
                raise ValueError(f"Unhandled language: {self}")

    @classmethod
    def from_ls_class(cls, ls_class: type["SolidLanguageServer"]) -> Self:
        """
        Get the Language enum value from a SolidLanguageServer class.

        :param ls_class: The SolidLanguageServer class to find the corresponding Language for
        :return: The Language enum value
        :raises ValueError: If the language server class is not supported
        """
        for enum_instance in cls:
            if enum_instance.get_ls_class() == ls_class:
                return enum_instance
        raise ValueError(f"Unhandled language server class: {ls_class}")


@dataclass(frozen=True)
class LanguageServerConfig:
    """
    Configuration parameters for a language server instance
    """

    ls_id: LanguageServerId
    """
    defines the language server to use
    """
    workspace_folders: list[str] = field(default_factory=lambda: ["."])
    """
    list of workspace folders to be used by the language server and to be fully indexed by SolidLSP.
    Paths can either be absolute or relative to the project root.
    These folders must be descendants of the project root.
    """
    additional_workspace_folders: list[str] = field(default_factory=list)
    """
    list of additional workspace folders to be passed to the language server, but which are not to be indexed by SolidLSP. 
    Paths can either be absolute or relative to the project root.
    These folders can potentially be outside of the project root, e.g. for cross-package reference support.
    """
    trace_lsp_communication: bool = False
    start_independent_lsp_process: bool = True
    ignored_paths: list[str] = field(default_factory=list)
    """
    list of ordered ignore patterns (same syntax as .gitignore; only forward slashes) to be used by the language server
    for filtering out files and folders from indexing and analysis.
    """
    encoding: str = "utf-8"
    """File encoding to use when reading source files"""

    @classmethod
    def from_dict(cls, env: dict) -> Self:
        import inspect

        return cls(**{k: v for k, v in env.items() if k in inspect.signature(cls).parameters})

    @staticmethod
    def _absolute_workspace_folders(folders: list[str], project_root: str) -> list[str]:
        abs_workspace_folders = []
        for path in folders:
            if os.path.isabs(path):
                abs_path = str(Path(path).resolve())
            else:
                abs_path = os.path.realpath(os.path.join(project_root, path))
            if not os.path.exists(abs_path):
                log.error("Workspace folder does not exist: %s; skipping", abs_path)
                continue
            if abs_path in abs_workspace_folders:
                log.warning("Duplicate workspace folder: %s; skipping", abs_path)
                continue
            abs_workspace_folders.append(abs_path)
        return abs_workspace_folders

    def get_absolute_workspace_folders(self, project_root: str) -> list[str]:
        """
        Get the absolute paths of the workspace folders, resolving relative paths against the project root.

        :param project_root: The root path of the project
        :return: List of absolute workspace folder paths
        """
        return self._absolute_workspace_folders(self.workspace_folders, project_root)

    def get_absolute_additional_workspace_folders(self, project_root: str) -> list[str]:
        """
        Get the absolute paths of the additional workspace folders, resolving relative paths against the project root.

        :param project_root: The root path of the project
        :return: List of absolute additional workspace folder paths
        """
        return self._absolute_workspace_folders(self.additional_workspace_folders, project_root)
