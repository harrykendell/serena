# Serena standalone runtime

This repository is the Kendell standalone deployment of Serena, together with the sibling Orchestrator MCP and their shared runtime support. It is maintained as an operated product rather than as a general-purpose upstream-compatible distribution.

The runtime is built for ChatGPT and keeps the parts used by the current deployment: session-scoped project activation, semantic retrieval and refactoring through language servers, guarded file and shell operations, Git tooling, project memories, durable jobs, retained tool output, media/file transfer, the inline activity UI, and the Kendell dashboard.

## Supported environment

- Linux
- Python 3.13
- `uv` for environment and dependency management

The retained language-server catalogue is deliberately small: Python, TypeScript/JavaScript, C/C++, Bash, Nix, MATLAB, Markdown, LaTeX, YAML, JSON, TOML, HTML, and SCSS/Sass/CSS. Additional servers are started lazily when a project or source path requires them.

## Setup

Create or update the local environment:

```bash
uv sync --extra dev --locked
```

The installed entry points are:

```text
serena        Serena MCP/runtime CLI
orchestrator  sibling Orchestrator MCP CLI
```

For a local Streamable HTTP Serena server:

```bash
uv run serena start-mcp-server \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 9121 \
  --streamable-http-path /serena
```

Project activation may be supplied with `--project` or performed later through the MCP tools. Hosted routing, Cloudflare Tunnel configuration, and service supervision are deployment concerns outside this repository.

## Development

The normal checkpoint gates are:

```bash
uv run poe format
uv run poe type-check
uv run poe test
```

Focused language-server tests use the pytest markers declared in `pyproject.toml`.

Useful repository documentation is intentionally limited to the current Kendell design and operation notes under `docs/03-special-guides/`, especially:

- `serena_standalone_simplification_plan.md`
- `chatgpt_orchestrator_plan.md`
- `cpp_setup.md`

## Upstream attribution

This codebase derives from Serena by Oraios AI and retains upstream code under the MIT licence. See `LICENSE` for the copyright and licence terms.
