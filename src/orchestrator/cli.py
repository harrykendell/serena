"""Command-line entry point for the Orchestrator MCP server."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import click

from orchestrator.config import OrchestratorConfig
from orchestrator.mcp import OrchestratorMCPFactory


@click.command(context_settings={"max_content_width": 120})
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse", "streamable-http"]),
    default="stdio",
    show_default=True,
    help="Transport protocol.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Listen address for network transports.")
@click.option("--port", type=click.IntRange(1, 65535), default=8100, show_default=True, help="Listen port for network transports.")
@click.option(
    "--streamable-http-path",
    type=str,
    default="/mcp",
    show_default=True,
    help="HTTP endpoint path for Streamable HTTP transport.",
)
@click.option(
    "--state-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="Override Orchestrator persistent state root (default: ORCHESTRATOR_HOME or ~/.orchestrator).",
)
def main(
    transport: Literal["stdio", "sse", "streamable-http"],
    host: str,
    port: int,
    streamable_http_path: str,
    state_root: Path | None,
) -> None:
    """Starts the independent Orchestrator MCP server."""
    config = OrchestratorConfig.from_environment(state_root)
    factory = OrchestratorMCPFactory(config)
    server = factory.create_mcp_server(host=host, port=port, streamable_http_path=streamable_http_path)
    try:
        server.run(transport=transport)
    finally:
        factory.close()


if __name__ == "__main__":
    main()
