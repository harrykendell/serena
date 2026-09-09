# Adding Retained Language Support

Use this only when the Kendell deployment has a concrete need for another semantic language server. The supported catalogue is intentionally small; do not restore upstream breadth by default.

## 1. Implement the adapter

Add one adapter under `src/solidlsp/language_servers/` and follow the closest retained implementation.

- Reuse the generic SolidLSP protocol/process/cache layer.
- Use the most specific `LanguageServerDependencyProvider` abstraction that fits.
- For Python packages that can run on demand, prefer `LanguageServerDependencyProviderUvx` with a pinned default version.
- For managed executable/archive downloads, use the retained `RuntimeDependency` / `RuntimeDependencyCollection` pattern with an explicit allowed-host set and pinned SHA256 for the supported Linux artifact.
- Use `subprocess_run` rather than calling `subprocess.run` directly from adapter setup code.
- Keep server-specific initialisation in `_create_base_initialize_params`; common LSP fields are owned by `InitializeParamsBuilder`.

## 2. Register the language

Extend `LanguageServerId` in `src/solidlsp/ls_config.py` in all required places:

- enum id;
- source filename matcher;
- auto-detection priority/experimental classification where relevant;
- adapter mapping in `get_ls_class`.

Preserve lazy detection/startup and avoid introducing alternative adapters for one language without a concrete deployment requirement.

## 3. Add behaviour-level tests

Create a minimal fixture under `test/resources/repos/<language>/test_repo/` and tests under `test/solidlsp/<language>/`.

Tests should verify externally useful semantic behaviour, such as:

- symbol discovery;
- within-file references;
- cross-file references;
- declarations/implementations only when the server genuinely supports them;
- diagnostics or editing behaviour when relevant.

Add one pytest marker for the language to `pyproject.toml`. Do not write tests that merely freeze adapter implementation structure.

## 4. Keep the standalone surface current

Update:

- the retained-language list in `README.md`;
- `src/serena/resources/project.template.yml`;
- `.github/workflows/ci.yml` only if the Linux runner needs a system dependency that the adapter cannot manage itself.

Run `uv run python scripts/print_language_list.py` to inspect the canonical enum list when updating the template.

## 5. Verify

Run the language's focused tests, then the normal checkpoint gates from `mem:task_completion`. Confirm the new server works on the supported Linux/Python-3.13 environment before accepting it into the retained catalogue.
