# Serena — Standalone Project Core

Serena is the Kendell-operated ChatGPT MCP coding runtime. This repository also ships the independent sibling Orchestrator MCP and shared runtime support; upstream distribution compatibility is not a product constraint.

## Source map

- `src/serena/` — Serena MCP server, session/project runtime, tools, memories, execution/activity state, jobs, retained output/media, and Kendell dashboard. `runtime.py` owns session-to-project runtime binding; `file_snapshots.py` owns persistent exported-file snapshots.
- `src/orchestrator/` — independent Orchestrator MCP.
- `src/mcp_runtime/` — small shared runtime utilities used by the two MCPs.
- `src/solidlsp/` — generic LSP protocol/process/cache layer plus the retained language adapters.
- `test/serena/`, `test/orchestrator/`, `test/solidlsp/` — pytest suites; language suites use markers declared in `pyproject.toml`.
- `test/resources/repos/` — fixture projects only for retained languages.
- `scripts/` — small maintenance utilities: memory graph, retained language list, and tool overview.
- `docs/03-special-guides/` — hand-maintained Kendell design/operation notes. There is no generated Sphinx/JupyterBook documentation pipeline.

## Core runtime architecture

- `SessionRegistry` is the only session/project binding path, including the `global` startup scope. It caches one `ProjectRuntime` per project root; sessions switch bindings without shutting down runtimes used elsewhere.
- `ProjectRuntime` groups one `Project`, runtime readiness, project execution coordination, active tools, prompt status, and access to the project's language-server and memory managers.
- Process services are singular: `ExecutionStore` (including activity-panel runs), `JobManager`, `ToolOutputStore`, and `FileSnapshotStore`. Activity/dashboard components are read/presentation views over those services rather than independent state owners.
- The Kendell dashboard remains Serena-hosted because it already composes Serena's read model with Orchestrator's independently persisted state cleanly; `mcp_runtime` remains shared utilities rather than shared runtime state.
- `SerenaAgent` remains the MCP-facing application facade for tool catalogue, prompts, configuration, service wiring and execution routing. Project-runtime ownership no longer lives there, so renaming/splitting it would currently add churn without a clearer boundary.

## Runtime/deployment invariants

- Supported host environment: Linux with Python 3.13.
- `uv.lock` is the authoritative dependency resolution; runtime dependencies in `pyproject.toml` are direct imports used by the shipped packages.
- Wheel contents: `serena`, `orchestrator`, `mcp_runtime`, `solidlsp`.
- Entry points: `serena` -> `serena.cli:top_level`; `orchestrator` -> `orchestrator.cli:main`.
- CI is a single Linux/Python-3.13 workflow; no PyPI/TestPyPI, release, Docker, Nix packaging, devcontainer, generated-docs, or cross-platform release machinery is maintained.
- Upstream Serena attribution remains under the MIT `LICENSE`.

## Retained language catalogue

Python, TypeScript/JavaScript, C/C++, Bash, Nix, MATLAB, Markdown, LaTeX, YAML, JSON, TOML, HTML, and SCSS/Sass/CSS. Language servers retain lazy detection/startup, cache persistence, and idle shutdown.
