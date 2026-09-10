# Serena MCP P05 full catalogue audit

P05 reviewed the 46 active ChatGPT tools in the public coverage ledger and followed their active call graph through `src/serena` and `src/solidlsp`. The classification below records deliberate generic exception sites that remain relevant after P01-P04 and the P05 conversions. CLI-only and dashboard-only code is excluded unless it is reached through a public MCP tool.

## Converted expected failures

The following supported operational failures now use the user-facing/tool-operation path rather than the unexpected-internal-error path:

- `SerenaAgent.get_active_project_or_raise`: no active project.
- `SerenaAgent.activate_project_from_path_or_name`: unavailable registered project directory.
- `SerenaConfig.get_registered_project`: ambiguous registered project name.
- `SerenaConfig.add_project_from_path`: invalid/missing project path and invalid newly loaded project configuration; package/template failures remain internal.
- `Project._ignore_spec` / `Project._ignored_patterns`: unavailable computed ignore state.
- `Project.create_language_server_manager`: invalid tool/idle timeout configuration.
- `Project.get_language_server_manager_or_raise`: known unavailable manager state.
- `RuntimeReadiness.wait_until_ready`: preserves an existing `UserFacingError`; arbitrary initialization failures remain internal.
- `Tool._apply_with_lsp_recovery`: typed `LanguageServerOperationError` failures are user-facing; plain `SolidLSPException` remains internal, while termination still follows the existing read-retry/write-no-replay recovery path.
- `Request.get_result` and `LanguageServerInterface.send_request`: request timeouts and server-reported request failures use `LanguageServerOperationError` rather than generic exceptions or payload-heavy request diagnostics.
- `SolidLanguageServer.PathWorkspaceStatus.check_within_workspace_or_raise`, `request_full_symbol_tree`, `request_overview`, and `FileUtils.read_file`: explicit missing, ignored, outside-workspace, or concurrently removed files use the LSP operational exception path.
- `LanguageServerManager._create_and_start_language_server`: OS-level startup failures are user-facing.
- `LanguageServerUnavailableError`: explicit runtime/dependency/configuration availability failures from retained language-server implementations and dependency utilities, including missing executables/dependencies, unsupported runtime platforms, invalid Nix-specific configuration, failed downloads/integrity checks, and failed runtime installation/verification.

`SolidLSPException.user_message()` exposes the concise server/operational cause to the tool boundary instead of echoing full request parameters. Diagnostic `__str__` output remains unchanged for logs.

## Intentionally normal at a higher layer or dedicated control flow

- `serena.util.shell.execute_shell_command` may raise `TimeoutError`; `ExecuteShellCommandTool` catches it and returns a concise user-facing timeout. Ordinary command non-zero exits remain successful structured results.
- `LanguageServerManager.restart_language_server` can reject a language that is no longer a candidate. It is reached only from terminated-LSP recovery: mutating tools convert recovery failure into the existing safe no-replay user-facing result, while an impossible read-side mismatch remains an internal recovery defect.
- Low-level diagnostic range/severity `ValueError`s in `SolidLanguageServer` are preconditions already validated by `GetDiagnosticsForFileTool` before the LSP call.
- Search/symbol zero-match results, safe-delete reference blocking, and already-terminal job cancellation remain their documented normal-result paths rather than exceptions.

## Intentionally internal invariants or defects

The following generic raises remain deliberately internal when reachable indirectly from tool execution:

- `ToolOutputWriter` / `ToolOutputStore`: closed writer/store, append-after-finish, constructor limits, internal missing-record lookup, and private tail-helper preconditions. Public `read_tool_output` validation is already user-facing.
- `JobStore` / `JobManager`: duplicate generated job ID, corrupt persisted job state, impossible internal cursor combination, constructor limits, and internal listing limit.
- `SymbolDictGrouper`: invalid fixed `TypedDict`/group-key construction.
- `LanguageServerCodeEditor`: unhandled LSP workspace-edit kind/format, which is a protocol/implementation defect.
- `ProjectExecutionCoordinator` and `RuntimeReadiness`: unsupported internal access mode and arbitrary runtime-initialization defects.
- tool registry/base-tool construction: unimplemented `apply`, duplicate tool names, and lookup of a non-existent registered tool.
- `FileSnapshotStore`: private-store permission invariant, resource-token parser helpers, legacy snapshot internals, and storage corruption/expiry reached through the MCP resource endpoint; public media/file tools translate their expected failures.
- media temporary upload allocation after exhausting collision retries.
- plain `SolidLSPException` lifecycle invariants such as calling protocol operations before server startup, plus unknown internally supplied archive types.
- SolidLSP enum/exhaustiveness checks, protocol `Content-Length` parsing, and unsupported server-emitted URI schemes.
- retained runtime dependency table inconsistencies (`RuntimeDependencyCollection`) and the missing shipped Taplo checksum invariant.

## Not in the public tool call graph

Generic exceptions in `serena.cli`, direct dashboard HTTP/UI handlers, and dashboard/activity lookup helpers are not converted solely for P05. `open_dashboard` keeps its separate public-tool handling from P04; unexpected dashboard server defects remain internal by design.

## Completion interpretation

Every active tool row in sections 6.1-6.7 of the plan was reviewed. Remaining generic exception sites on the public call graph are therefore either guarded by a higher-level expected-failure path, dedicated control flow, or deliberately retained as internal defects/invariants. P06 can focus on successful-response payload efficiency rather than reopening exception classification.
