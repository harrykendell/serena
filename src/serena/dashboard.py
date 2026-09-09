import socket
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

from flask import Flask, Response, redirect, request, send_from_directory
from sensai.util import logging

from serena.constants import SerenaPorts
from serena.custom_dashboard import CustomDashboard

if TYPE_CHECKING:
    from serena.agent import SerenaAgent

log = logging.getLogger(__name__)

# disable Werkzeug's logging to avoid cluttering the output
logging.getLogger("werkzeug").setLevel(logging.WARNING)


class DashboardServer:
    """Hosts the Kendell Serena/Orchestrator dashboard."""

    BASE_PORT = SerenaPorts.DASHBOARD_API_BASE_PORT

    log = logging.getLogger(__qualname__)

    def __init__(
        self,
        agent: "SerenaAgent",
        host: str = "127.0.0.1",
        trusted_hosts: list[str] | None = None,
        port: int | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._app = Flask(self.__class__.__name__)
        if trusted_hosts:
            self._app.config["TRUSTED_HOSTS"] = trusted_hosts
        self._custom_dashboard = CustomDashboard(self._app, agent)
        self._setup_routes()

    def set_serena_session_name(self, session_id: str, display_name: str) -> str:
        """Sets the retained dashboard name for one ChatGPT conversation."""
        return self._custom_dashboard.set_serena_session_name(session_id, display_name)

    def _setup_routes(self) -> None:
        """Registers the dashboard shell and static assets."""

        @self._app.route("/")
        def redirect_to_dashboard() -> Response:
            return redirect("/dashboard/")  # type: ignore[return-value]

        @self._app.route("/dashboard")
        def redirect_dashboard_slash() -> Response:
            return redirect("/dashboard/")  # type: ignore[return-value]

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

    @staticmethod
    def _find_first_free_port(start_port: int, host: str) -> int:
        """Returns the first bindable TCP port at or above ``start_port``."""
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
        """Runs the dashboard on ``port`` and returns the bound port."""
        from flask import cli

        # suppress Werkzeug's startup banner, which can corrupt stdio MCP traffic
        cli.show_server_banner = lambda *args, **kwargs: None  # ty: ignore[invalid-assignment]
        self._app.run(host=self._host, port=port, debug=False, use_reloader=False, threaded=True)
        return port

    def run_in_thread(self) -> tuple[threading.Thread, int]:
        """Starts the dashboard in a daemon thread and returns the thread and port."""
        if self._port is None:
            # reserve 24282 for the externally exposed MCP service dashboard
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
