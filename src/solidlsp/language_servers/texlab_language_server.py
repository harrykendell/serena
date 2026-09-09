"""
Provides LaTeX specific instantiation of the LanguageServer class using texlab.
texlab is downloaded as a prebuilt binary from the latex-lsp/texlab GitHub releases.
"""

import logging
import os

from overrides import override

from solidlsp.dependency_provider import LanguageServerDependencyProviderSinglePath
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.settings import SolidLSPSettings

from .common import RuntimeDependency, RuntimeDependencyCollection

log = logging.getLogger(__name__)

TEXLAB_VERSION = "5.25.1"
TEXLAB_BASE_URL = f"https://github.com/latex-lsp/texlab/releases/download/v{TEXLAB_VERSION}"
TEXLAB_ALLOWED_HOSTS = (
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
)
TEXLAB_SHA256_BY_PLATFORM = {
    "linux-arm64": "e0d8e0b27b2e6e3526fa5019323bb3fddb1202a0f0049e527672b5ff323cc15e",
    "linux-x64": "c8260b2fd2849cbad7d1f54c4ffa0389f34664b049392107bc4f7f9c8ec542ba",
}


class TexlabLanguageServer(SolidLanguageServer):
    """
    Provides LaTeX specific instantiation of the LanguageServer class using texlab.

    Symbol navigation maps onto LaTeX structure: document symbols are the sectioning
    hierarchy, definitions/references cover labels and citations.
    """

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        return super().is_ignored_dirname(dirname) or dirname in ["_minted", "_build", "build", "out", "auto"]

    @staticmethod
    def _setup_runtime_dependencies(ls_resources_dir: str) -> str:
        """Download and install texlab for the supported Linux host."""
        deps = RuntimeDependencyCollection(
            [
                RuntimeDependency(
                    id="Texlab",
                    description="texlab for Linux (ARM64)",
                    url=f"{TEXLAB_BASE_URL}/texlab-aarch64-linux.tar.gz",
                    platform_id="linux-arm64",
                    archive_type="gztar",
                    binary_name="texlab",
                    sha256=TEXLAB_SHA256_BY_PLATFORM["linux-arm64"],
                    allowed_hosts=TEXLAB_ALLOWED_HOSTS,
                ),
                RuntimeDependency(
                    id="Texlab",
                    description="texlab for Linux (x64)",
                    url=f"{TEXLAB_BASE_URL}/texlab-x86_64-linux.tar.gz",
                    platform_id="linux-x64",
                    archive_type="gztar",
                    binary_name="texlab",
                    sha256=TEXLAB_SHA256_BY_PLATFORM["linux-x64"],
                    allowed_hosts=TEXLAB_ALLOWED_HOSTS,
                ),
            ]
        )
        dependency = deps.get_single_dep_for_current_platform()
        install_dir = os.path.join(ls_resources_dir, f"texlab-{TEXLAB_VERSION}")
        texlab_executable_path = deps.binary_path(install_dir)
        if not os.path.exists(texlab_executable_path):
            log.info(f"Downloading texlab from {dependency.url}")
            deps.install(install_dir)

        assert os.path.exists(texlab_executable_path), f"texlab executable not found at {texlab_executable_path}"
        os.chmod(texlab_executable_path, 0o755)
        return texlab_executable_path

    class DependencyProvider(LanguageServerDependencyProviderSinglePath):
        def _get_or_install_core_dependency(self) -> str:
            return TexlabLanguageServer._setup_runtime_dependencies(self._ls_resources_dir)

        def _create_launch_command(self, core_path: str) -> list[str]:
            return [core_path]

    def _create_dependency_provider(self) -> LanguageServerDependencyProviderSinglePath:
        return self.DependencyProvider(self._custom_settings, self._ls_resources_dir)

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        """Create the retained Texlab language server."""
        super().__init__(config, repository_root_path, "latex", solidlsp_settings)

    def _create_base_initialize_params(self) -> dict:
        """Returns the initialize params for the texlab Language Server."""
        result = {
            "locale": "en",
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True, "dynamicRegistration": True},
                    "completion": {"dynamicRegistration": True, "completionItem": {"snippetSupport": True}},
                    "hover": {"dynamicRegistration": True, "contentFormat": ["markdown", "plaintext"]},
                    "definition": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "hierarchicalDocumentSymbolSupport": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                    "rename": {"dynamicRegistration": True},
                },
                "workspace": {"workspaceFolders": True, "didChangeConfiguration": {"dynamicRegistration": True}},
            },
        }
        return result

    def _start_server(self) -> None:
        """Start the texlab server process."""

        def register_capability_handler(params: dict) -> None:
            return

        def window_log_message(msg: dict) -> None:
            log.info(f"LSP: window/logMessage: {msg}")

        def do_nothing(params: dict) -> None:
            return

        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", do_nothing)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        log.info("Starting texlab server process")
        self.server.start()
        initialize_params = self._create_initialize_params()

        log.info("Sending initialize request from LSP client to texlab and awaiting response")
        init_response = self.server.send.initialize(initialize_params)

        assert "textDocumentSync" in init_response["capabilities"]
        assert "documentSymbolProvider" in init_response["capabilities"]
        assert "definitionProvider" in init_response["capabilities"]

        self.server.notify.initialized({})
