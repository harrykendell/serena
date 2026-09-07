# Serena Standalone Simplification Plan

Status: planning complete; delegate reviews incorporated; implementation should begin from the F-stage fixes

Baseline checkpoint: `fccc2b6a` (`Isolate CLI project tests from user config`)

This document plans the transition from the current Kendell Serena fork into a deliberately owned product rather than an upstream-compatible general-purpose Serena distribution.

The target is a **Linux-hosted ChatGPT coding MCP** with:

- session-scoped project selection;
- semantic code retrieval/editing through LSP;
- concurrent project access with explicit same-project safety;
- files, memories, Git and shell tools;
- durable background jobs;
- retained tool output and native ChatGPT media/file transfer;
- one Kendell dashboard and one execution/activity model;
- dynamic, lazy language-server lifetime management;
- Orchestrator as a separate peer MCP for delegation/provider lifecycle.

Compatibility with unused Serena clients, backends, GUI applications and historical configuration formats is not a product requirement unless explicitly retained below.

## 1. Architectural invariants

1. **ChatGPT is Serena's client.** The runtime and exposed tool set should be designed around the ChatGPT MCP deployment rather than a matrix of client contexts.
2. **Linux is the deployment platform.** Windows/macOS application, tray and compatibility code is not retained merely for upstream parity.
3. **LSP is the semantic backend.** JetBrains is not a second supported backend.
4. **Orchestrator owns delegation.** Serena does not retain `query_project`/project-server machinery as an alternative delegation path.
5. **One concern has one source of truth.** Tool execution state must not be independently reconstructed by the dashboard, activity widget and task executor.
6. **Different projects are independent.** Reads and writes in different project runtimes may execute concurrently.
7. **Same-project reads should be concurrent where the underlying service is safe.** Same-project mutations are serialized by an explicit project execution policy rather than by an incidental FIFO executor.
8. **Long-running user commands remain jobs.** Normal MCP tool execution should not imitate the durable-job subsystem.
9. **Tests never touch user state.** A test run must not write `~/.serena`, the real project registry, dashboard state, job state or other user-owned paths.
10. **Removal is preferred over compatibility shims.** We control the deployment and project configurations, so migrations should be bounded one-time steps rather than permanent branches in the runtime.
11. **Retries must respect side effects.** A semantic read may be retried after a language-server restart; an editing operation must not be blindly replayed after a partial failure.
12. **A released execution permit means the operation has really stopped.** Timeout/cancellation must never allow a second same-project writer to start while the timed-out writer thread or subprocess continues in the background.
13. **Each implementation step gets a checkpoint.** Do not combine behavioural fixes, architecture replacement and large deletions into one commit.

## 2. Delegate-review conclusions

Three independent read-only reviews were run through Orchestrator before finalising this plan.

### Execution/concurrency review

The current execution path is:

```text
FastMCP async request
    -> asyncio.to_thread(...)
    -> Tool.apply_ex()
    -> SerenaAgent.issue_task()
    -> per-project TaskExecutor FIFO
    -> second worker thread
    -> tool body
```

The review found two correctness problems beyond the already-known unnecessary serialization:

1. `TaskExecutor` cancellation can release the queue while the cancelled worker thread is still running. A second same-project task may therefore start even though the first operation has not actually stopped.
2. project activation currently mutates `_default_project` for non-global sessions. An unbound session can therefore inherit another session's activation. Startup default selection must be immutable or MCP sessions must always bind explicitly.

It also found that `Tool.apply_ex()` retries the complete `apply_fn` after a terminated language server. That is acceptable only for proven read-only operations. A mutating symbolic tool may have partially changed files before the server failure, making replay unsafe.

### Product-surface review

The Kendell dashboard frontend uses only the fork-specific `/dashboard/api/...` surface. The upstream logs/stats/config/language/news/queued-task dashboard routes and old resources are no longer product dependencies.

The same review confirmed that the project-query server, client setup/hooks, non-ChatGPT contexts, desktop viewer/tray paths, Agno integration and analytics/token-estimation path can be removed independently of the semantic/LSP core.

### Language-support review

The registered Kendell repositories were inspected to establish an evidence-based language set.

**Core semantic languages:**

- Python;
- C/C++;
- TypeScript/JavaScript;
- LaTeX.

**Conservative optional retained set:**

- Bash;
- Nix;
- HTML;
- SCSS/CSS;
- MATLAB;
- JSON;
- YAML;
- TOML;
- Markdown.

All registered project configs currently use `language_servers: []` with automatic detection, so there is no persisted requirement to preserve removed explicit language IDs.

The current SolidLSP catalogue contains roughly 73 language IDs and ~26.6k adapter LOC. Keeping the generic protocol/process layer plus the conservative retained adapters is estimated to remove ~22.3k adapter LOC (~84%) and ~18.9k SolidLSP test LOC (~74%), together with very large unused fixture trees.

## 3. Correctness fixes before broad deletion

### F01 — Complete test isolation from user Serena state

**Already landed:** `fccc2b6a` isolates `test/serena/test_cli_project_commands.py` from the real Serena config. This fixed the concrete source of the `/tmp/tmp...` project-registry pollution that had been observed.

**Remaining work**

- Generalise or audit the isolation so other tests cannot write user-owned Serena state through `SerenaConfig.from_config_file()`, `SerenaPaths()`, job storage, media snapshots or other persistent roots.
- Prefer a suite-level isolation mechanism where practical rather than fixing future leaks one test module at a time.
- Add a behaviour-level guard that a representative config/CLI test run leaves the real `~/.serena` tree unchanged.
- Keep removal of stale real-user `/tmp/...` registrations as operational cleanup, not test behaviour.

**Checkpoint**: `F01 complete Serena test-state isolation`.

### F02 — Fix session/default-project leakage

The startup/default project and a session's explicitly activated project must be distinct concepts.

**Target behaviour**

- a project supplied at Serena process startup may act as an immutable startup default if we still need that feature;
- `activate_project()` binds only the calling MCP session;
- activating project A in session A must not change what a previously unbound session B resolves to;
- switching one session must not redirect or shut down another session's runtime;
- submitted calls remain pinned to the runtime selected at submission.

Update tests that currently encode `_default_project` mutation as expected behaviour.

**Checkpoint**: `F02 isolate session project activation`.

### F03 — State and test the execution/concurrency contract

Before replacing `TaskExecutor`, encode the desired externally observable behaviour:

| Operation | Same project | Different projects |
| --- | --- | --- |
| Plain file/memory read | concurrent | concurrent |
| Symbol/LSP read | concurrent if SolidLSP request path is thread-safe; otherwise serialize only the affected LSP service | concurrent |
| File/symbol/memory mutation | serialized | concurrent |
| Shell command | conservative write-class operation | concurrent across projects |
| `git status/log/diff` | read-class | concurrent |
| `git fetch` | repository-metadata mutation; write-class initially | concurrent across projects |
| branch create/switch/delete, commit, pull, push | write-class | concurrent across projects |
| project activation/session rebinding | session-control operation | independent |
| durable jobs | JobManager's existing semantics | JobManager's existing semantics |

Add behaviour tests for at least:

- two ordinary reads on the same project overlapping;
- same-project writes remaining serialized;
- the selected read/write interaction policy;
- different-project reads/writes remaining independent;
- an already-submitted call staying pinned to the project selected at submission;
- Git operations receiving the intended access class;
- timeout/cancellation never releasing a write permit while the operation is still executing.

Do not force concurrent LSP reads until the SolidLSP request implementation has been checked for thread safety.

Use a writer-preferring reader/writer policy so a continuous stream of reads cannot starve an edit.

**Checkpoint**: `F03 define Serena execution semantics`.

### F04 — Replace `TaskExecutor` as the normal MCP execution primitive

Replace the project FIFO with an explicit `ProjectExecutionCoordinator` owned by each `ProjectRuntime`.

Recommended shape:

```text
ProjectExecutionCoordinator
    execute(access_mode, call)

ExecutionAccess
    READ
    WRITE
    SESSION_CONTROL   # if useful structurally; not a project writer lock
```

The coordinator should:

- execute the tool body in the existing FastMCP worker thread instead of spawning an additional worker thread;
- use a writer-preferring per-project reader/writer gate;
- never introduce a process-global project lock;
- capture/pin `ProjectRuntime` before entering the worker/gate;
- keep a permit until the underlying operation has really returned;
- provide synchronization only, not execution-history storage.

Tool access class should be explicit. `ToolMarkerCanEdit` can provide a default WRITE classification, but mixed tools such as Git must override it deliberately.

**Timeout/cancellation semantics**

Python worker threads cannot be force-terminated. Therefore a timed-out in-process call may stop waiting at the MCP boundary, but the project WRITE permit must stay held until its worker really returns.

For shell execution, if cancellation/timeout is intended to terminate the command, the subprocess/process group must be terminated and reaped before the WRITE permit is released. Durable commands should continue to use the existing JobManager path instead.

Delete `TaskExecutor` and tests that describe only its internal FIFO/cancellation implementation after all ordinary tools have migrated.

**Checkpoint**: `F04 replace tool FIFO with project execution coordinator`.

### F05 — Make language-server recovery side-effect aware

`Tool.apply_ex()` currently retries the complete tool `apply_fn` after a `SolidLSPException` reports a terminated server.

Replace this with a policy that distinguishes safe reads from mutations:

- **READ:** restart the affected server and retry once when the failure mode indicates termination and replay is safe;
- **WRITE:** restart/recover the server for subsequent requests, but fail the current operation closed. Do not replay an edit whose partial side effects are unknown;
- return an error instructing the caller to re-inspect state before attempting the edit again.

Where possible, keep this policy at the execution/tool abstraction boundary rather than duplicating retry rules across individual tools.

**Checkpoint**: `F05 make LSP recovery mutation-safe`.

### F06 — Establish one execution record/store

Tool lifecycle is currently represented by overlapping mechanisms: MCP activity tracking, `TaskExecutor.TaskInfo`, dashboard execution history and log parsing.

Create one `ExecutionStore` containing externally useful tool-call state:

```text
ExecutionRecord
    id
    session_id
    project
    tool_name
    bounded/display-safe arguments
    started_at
    finished_at
    status
    bounded result/error descriptor
    retained-output id if applicable
    media/file descriptor if applicable
    durable-job identity if a start_job call created one
```

The MCP wrapper records start/finish directly because it has authoritative request lifecycle information. The ChatGPT inline activity resource and web dashboard both read this store.

Do not make the execution store responsible for project locking or durable jobs.

**Checkpoint**: `F06 unify Serena execution state`.

## 4. Standalone product simplification

Only begin broad deletion after F01-F06 pass and the resulting behaviour is stable.

### S01 — Remove superseded delegation/project-query machinery

Delete the old Serena-side project-query path:

- `src/serena/project_server.py`;
- `src/serena/tools/query_project_tools.py`;
- `query-projects` mode/config;
- associated CLI `start-project-server` path, tests, docs and imports.

Orchestrator is the supported delegation/provider mechanism. Serena retains only project activation/routing for its own coding tools.

**Checkpoint**: `S01 remove legacy project query service`.

### S02 — Canonicalise live configuration before deleting compatibility paths

Before removing JetBrains, generic modes/contexts and old config migrations, rewrite the live Serena configuration and all registered project configs into a known canonical current form.

- inventory every active global/project field and whether the Kendell deployment uses it;
- explicitly materialise values that would otherwise depend on legacy defaults being deleted;
- verify every registered project still activates and detects its intended language servers;
- commit any repository-owned project config updates separately from runtime deletion;
- if a one-time migration helper is required, make it intentionally temporary and remove it after S08.

This prevents feature-removal commits from also having to preserve old configuration semantics indefinitely.

**Checkpoint**: `S02 canonicalise Kendell Serena configuration`.

### S03 — Remove the JetBrains backend

Delete JetBrains as a supported semantic backend:

- `src/serena/tools/jetbrains_tools.py`;
- `src/serena/jetbrains/`;
- JetBrains internal mode/configuration;
- backend switching/replacement logic in `SerenaAgent`, `Project`, file tools and config;
- JetBrains-specific CLI/config fields and tests.

Make LSP structural rather than configurable. `language_backend`, `jetbrains_plugin_server_address`, `jetbrains_launch_command` and backend migration logic disappear.

**Checkpoint**: `S03 make Serena LSP-only`.

### S04 — Remove unused client, desktop, Agno and analytics surfaces

The supported service is the hosted ChatGPT MCP. Remove general-distribution integrations that are not part of that product:

- non-ChatGPT context YAML files unless a specific currently used client is deliberately retained;
- `config/client_setup.py` client installers;
- `serena-hooks` and `hooks.py` if there is no retained consumer;
- `agno.py`, Agno scripts and optional extras;
- GUI log viewer;
- pywebview/tray-manager infrastructure;
- macOS/Windows dashboard app/tray assets and branches;
- client-specific prompt overrides and tests;
- analytics/token-estimation plumbing once the old dashboard stats API is gone;
- Serena upstream usage telemetry if still present.

Likely dependency removals after final reference verification include:

- `pywebview`;
- `pystray`;
- Windows-only `pythonnet`;
- `tiktoken`;
- `anthropic`;
- Agno extras;
- `python-dotenv` if no remaining runtime reference survives Agno/analytics deletion.

`psutil` remains because durable jobs use it.

Keep the streamable HTTP MCP deployment; ChatGPT-only does not mean hardcoding away the hosted transport.

**Checkpoint**: `S04 remove unused integrations and telemetry`.

### S05 — Make the Kendell dashboard the only dashboard

The Kendell frontend uses the fork-specific endpoints for session, memory, executions, jobs, output and media. The upstream dashboard still exposes old logs/stats/config/language/news/memory-edit/queued-task APIs and desktop resources.

Replace `dashboard.py + custom_dashboard.py` with one `DashboardServer` around the Kendell UI and the authoritative `ExecutionStore`, `JobManager` and session registry.

Retain only endpoints with a current product use case, approximately:

- session/project state;
- execution list/detail/output/media;
- job list/detail/output;
- memory list/read if still exposed;
- health/heartbeat;
- any small deployment-admin endpoint actually in use.

Delete:

- Oraios news fetching/read tracking;
- upstream-version checking;
- old token-statistics APIs;
- old dashboard static assets;
- dashboard language mutation if dynamic language detection makes it unnecessary;
- queued-task APIs tied to `TaskExecutor`;
- regex/log reconstruction of tool starts/results.

The dashboard becomes a view over Serena state, not a second state machine.

**Checkpoint**: `S05 collapse to one dashboard`.

### S06 — Collapse contexts, modes and dynamic tool-set composition

The deployed configuration is effectively ChatGPT + editing + interactive. Replace the generic agent framework with fixed product behaviour:

- one ChatGPT-oriented instruction set;
- one explicit MCP tool registry;
- read-only project policy as a structural restriction;
- optionality only where there is a real deployment reason.

Remove:

- general context loader/registry;
- runtime mode switching;
- `base_modes`, `default_modes`, `added_modes` project/global configuration;
- generic `fixed_tools`/included/excluded composition unless a concrete retained use case remains;
- mode prompt tracking that no longer has multiple modes to track;
- CLI mode/context editors and templates.

Keep the useful instruction content, but it need not remain a configurable agent-personality framework.

**Checkpoint**: `S06 replace agent modes with fixed ChatGPT runtime`.

### S07 — Simplify prompt generation and remove `interprompt` if no longer justified

After S06, reassess the prompt system. If the remaining initial instructions can be represented by a small static/Jinja template local to Serena, remove:

- `src/interprompt`;
- generated prompt-factory indirection;
- prompt override CLI machinery;
- generation scripts and tests that exist only for the generic prompt library.

Retain templating only where live data is genuinely inserted, such as project activation information, exposed tools and memories.

**Checkpoint**: `S07 simplify Serena prompting`.

### S08 — Reduce configuration to the owned current schema

With removed features gone and live configs already canonicalised in S02, delete compatibility-heavy fields and historical migrations.

Likely retained global settings:

- registered project roots;
- logging level;
- dashboard bind/trusted-host settings needed for deployment;
- tool/output budgets;
- global ignored paths;
- project-data location if useful;
- trusted project paths;
- LSP priorities/settings and idle timeout.

Likely retained per-project settings:

- project name;
- explicit language-server overrides plus auto-detection switch;
- ignored paths/gitignore behaviour;
- read-only;
- encoding/line ending only if editing behaviour requires them;
- memory ignore/read-only patterns if used;
- LSP workspace folders/settings;
- activation command/timeout if used by real projects;
- concise initial project instruction/memory embedding if still useful.

Remove configuration dimensions for:

- semantic backend choice;
- JetBrains;
- GUI/tray;
- contexts/modes;
- generic tool composition without a retained use case;
- token-estimator/analytics;
- obsolete async/interactive language-autogeneration compatibility;
- historical migrations whose inputs were canonicalised in S02.

Remove the temporary migration helper after validation rather than carrying it indefinitely.

**Checkpoint**: `S08 reduce Serena configuration schema`.

### S09 — Reduce SolidLSP to Kendell's real language set

Preserve the generic LSP protocol/process/cache layer unless a separate review proves a simplification safe. In particular retain the language-agnostic roles currently provided by modules such as:

- `ls.py`;
- `ls_process.py`;
- `ls_request.py`;
- `ls_types.py`;
- `ls_utils.py`;
- `lsp_protocol_handler/`;
- `dependency_provider.py`;
- `initialize_params.py`;
- settings/filename-matcher/registry mechanics;
- `language_servers/common.py`.

Reduce the concrete adapter catalogue to:

**Core**

- Python;
- C/C++;
- TypeScript/JavaScript;
- LaTeX.

**Retain initially unless a focused usage check shows they are unnecessary**

- Bash;
- Nix;
- HTML;
- SCSS/CSS;
- MATLAB;
- JSON;
- YAML;
- TOML;
- Markdown.

Immediately remove duplicate/experimental alternatives where the primary implementation is sufficient, including candidates such as:

- `python_jedi`;
- `python_ty`;
- `python_pyrefly`;
- `python_basedpyright`;
- `cpp_ccls`;
- `typescript_vts`.

Then remove every other unsupported `LanguageServerId` and its adapter, install/download path, tests, pytest marker and fixture repository as a vertical slice.

Do not replace SolidLSP with Python-specific code; the generic protocol layer is valuable and small relative to the adapter catalogue.

**Expected scale**

The delegate inventory measured approximately:

- 73 current language IDs;
- ~26.6k adapter LOC;
- ~25.6k SolidLSP test LOC;
- 10,549 fixture files.

The conservative retained set is estimated to allow removal of ~22.3k adapter LOC (~84%) and ~18.9k SolidLSP test LOC (~74%). Large Angular/Svelte fixture trees alone account for the majority of the ~186 MB fixture tree.

**Checkpoint**: `S09 reduce supported language catalogue`.

### S10 — Remove obsolete packaging, CI, docs and dependency surface

Once source deletion has settled, remove distribution machinery that serves no Kendell deployment:

- unused PyPI/TestPyPI publishing paths;
- upstream release/news tooling;
- Docker/Nix/devcontainer paths if we do not use them for deployment or development;
- cross-platform CI matrices beyond the supported Linux/Python environment;
- docs describing deleted clients/backends/languages;
- demo/scripts for deleted integrations;
- dependencies left unreferenced by retained code.

Preserve license/attribution requirements from upstream code even though ongoing upstream compatibility is no longer a design goal.

**Checkpoint**: `S10 trim standalone packaging and dependencies`.

### S11 — Consolidate the core runtime after deletion

Only after the major feature branches are gone, reshape the surviving code rather than doing speculative renames before deletion.

Target conceptual structure:

```text
Serena MCP server
    SessionRegistry
        session -> ProjectRuntime

ProjectRuntime
    Project
    LanguageServerManager
    ProjectExecutionCoordinator
    MemoryManager

Shared services
    ExecutionStore
    JobManager
    DashboardServer
    ToolOutputStore
```

At this point reassess whether `SerenaAgent` remains the right abstraction. If it mostly acts as a service container/router, rename or split it then. Do not introduce a new abstraction hierarchy merely to replace the old hierarchy one-for-one.

**Checkpoint**: `S11 consolidate standalone Serena core`.

## 5. Implementation order

```text
fccc2b6a partial test-isolation fix already landed
  -> F01 complete test-state isolation
  -> F02 isolate session project activation
  -> F03 executable concurrency contract
  -> F04 ProjectExecutionCoordinator
  -> F05 mutation-safe LSP recovery
  -> F06 single ExecutionStore
  -> S01 query-project removal
  -> S02 canonicalise live configs
  -> S03 JetBrains removal
  -> S04 client/desktop/Agno/analytics removal
  -> S05 dashboard collapse
  -> S06 contexts/modes/toolset collapse
  -> S07 prompt simplification
  -> S08 final config-schema reduction
  -> S09 language-catalogue reduction
  -> S10 packaging/dependency/docs cleanup
  -> S11 final core consolidation
```

Do not begin S01 while an F-stage behavioural regression remains unresolved.

## 6. Verification and checkpoint policy

For every source/test checkpoint:

1. inspect `git status` and ensure unrelated concurrent work is not included;
2. run `uv run poe format`;
3. run `uv run poe type-check`;
4. run focused tests for the changed subsystem;
5. run the default `uv run poe test` before accepting the checkpoint;
6. inspect the Git diff;
7. commit only that checkpoint's paths when explicitly requested.

Additional gates:

- F01: representative tests leave real `~/.serena` unchanged;
- F02-F04: concurrency tests must demonstrate overlap/serialization using externally observable timing/events rather than implementation internals;
- F04: cancellation tests must prove no second same-project writer enters while the first worker/process is still alive;
- F05: an editing tool is never automatically replayed after a language-server termination;
- S02/S08: every registered project activates successfully using its canonicalised config;
- S09: run tests for every retained language whose shared SolidLSP infrastructure changed.

For large removal stages, measure at each checkpoint:

- source LOC by top-level subsystem;
- number of runtime dependencies;
- number of context/mode files;
- number of exposed/registered tools;
- number of supported language adapters;
- fixture-tree size;
- default test count/runtime where practical.

The goal is not minimum LOC in isolation. The goal is a smaller runtime whose structure directly represents the product we operate.

## 7. Explicitly retained features unless a later review changes the decision

Do not accidentally remove these while deleting upstream generality:

- session-scoped project activation;
- multiple simultaneous project runtimes;
- dynamic lazy language-server startup/idle shutdown;
- semantic symbol retrieval/edit/refactoring;
- project memories and memory maintenance tooling;
- guarded file operations;
- Git tools;
- shell execution with inherited user environment;
- durable background jobs and their persistence/telemetry;
- retained/paged tool output;
- media/PDF/file transfer to ChatGPT;
- ChatGPT inline activity UI;
- Kendell web dashboard;
- path-based hosted MCP deployment;
- Orchestrator as an independent sibling service.

## 8. Decisions intentionally deferred until implementation evidence

Resolve these at their named gates rather than guessing now:

- whether concurrent symbolic reads can share one SolidLSP server safely without a per-server request lock (F03/F04);
- whether memory writes should share the project WRITE gate or use a narrower memory-store lock (F04);
- whether `git fetch` can eventually use a narrower repository-metadata lock rather than the full project WRITE gate (F04);
- exact treatment of an in-process tool call whose MCP caller has timed out but whose worker still runs (F04);
- whether any non-ChatGPT client context is still intentionally used (S04);
- whether project-specific tool exclusions remain useful enough to retain (S06/S08);
- whether `interprompt` remains worthwhile after modes/contexts disappear (S07);
- whether every language in the conservative optional set merits a semantic server rather than plain text/file support (S09);
- which Docker/Nix/release paths remain useful operationally (S10).
