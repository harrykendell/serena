# Serena — Project Core

Serena is an MCP-based "IDE for coding agents": semantic code retrieval/editing/refactoring tools driven by language servers.

## Source map

- `src/serena/` — agent, MCP server, tools, project/config layer
  - `agent.py`, `mcp.py`, `cli.py`, `hooks.py` — entrypoints/wiring
  - `tools/` — tool implementations (memory_tools, symbol_tools, file_tools, workflow_tools, config_tools, cmd_tools)
  - `tools/tools_base.py` — base classes for all tools
  - `config/serena_config.py` — global/project configuration; the runtime has no context/mode configuration layer
  - `chatgpt_policy.py` + `tools/MCP_TOOL_CLASSES` — fixed ChatGPT product instructions, tool-description overrides, and explicit MCP tool catalogue
  - `code_editor.py`, `symbol.py`, `ls_manager.py` — symbolic editing / LS lifecycle
  - `dashboard.py`, `custom_dashboard.py` — browser dashboard backend and Kendell dashboard
  - `prompt_factory.py` — fixed ChatGPT prompts and Serena-local sandboxed Jinja rendering
- `src/solidlsp/` — generic LSP client framework; retained adapters are Python, TypeScript/JavaScript, C/C++, LaTeX, Bash, Nix, HTML, SCSS/CSS, MATLAB, JSON, YAML, TOML, and Markdown
- `test/serena/`, `test/solidlsp/<lang>/` — pytest suites; per-language tests gated by pytest markers
- `test/resources/repos/<lang>/` — fixture projects used by language-server tests
- `scripts/` — utilities (tool overview, profiling, maintenance)
- `docs/` — Jupyter Book sources; build via `poe doc-build`

## Project-wide invariants

- Package name (PyPI): `serena-agent`; wheel includes `serena`, `orchestrator`, `mcp_runtime`, `solidlsp`.
- Python: `>=3.11, <3.15`. Dependencies are exact-pinned in `pyproject.toml` (uvx installs from git, lockfile ignored — pin exactly).
- Entry points: `serena` → `serena.cli:top_level`; `orchestrator` → `orchestrator.cli:main`.
