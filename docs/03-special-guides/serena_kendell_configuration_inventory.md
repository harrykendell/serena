# Kendell Serena configuration inventory (S02)

This inventory records the canonical live configuration used by the Kendell Serena deployment before the standalone simplification removes compatibility surfaces. It is the reference for S03-S08: later deletion stages may remove fields, but must not silently change the behaviour recorded here.

Inventory date: 2026-09-09.

## Live global configuration

The live global file is `~/.serena/serena_config.yml`. S02 rewrote it through the current `SerenaConfig` serializer so every current field is explicit and removed three stale registrations whose project directories no longer exist (`seahorse-cpu`, `seahorse-cpu-evidence-20260907`, `seahorse-gpu`). Nine projects remain registered.

| Field | Canonical value | Kendell deployment use / disposition |
| --- | --- | --- |
| `default_modes` | `null` | No additional default modes. Remove with generic mode configuration in S06. |
| `base_modes` | `interactive`, `editing` | Used by the current ChatGPT policy. Preserve the effective instructions when S06 removes mode configuration. |
| `excluded_tools` | `[]` | No global custom exclusions. Generic tool-composition field is unused and removable in S06/S08. |
| `included_optional_tools` | `[]` | No global optional-tool additions. Generic tool-composition field is unused and removable in S06/S08. |
| `fixed_tools` | `[]` | No fixed custom tool set. Generic tool-composition field is unused and removable in S06/S08. |
| `symbol_info_budget` | `10` | Used as the inherited hover/info request budget. |
| `language_backend` | `LSP` | Used and fixed to LSP. Remove the choice, not the behaviour, in S03. |
| `line_ending` | `native` | Used as the inherited project write convention. |
| `read_only_memory_patterns` | `[]` | No global memory restrictions. |
| `ignored_memory_patterns` | `[]` | No global ignored memories. |
| `ls_specific_settings` | `{}` | No global language-server implementation overrides. Capability is currently unused. |
| `projects` | 9 live roots | Used for project activation/routing. Stale roots were removed in S02. |
| `gui_log_window` | `false` | Unused GUI surface; remove in S04. |
| `log_level` | `20` | Used (`INFO`). |
| `trace_lsp_communication` | `false` | Explicitly disabled; retained as a diagnostic setting unless later simplification removes it. |
| `web_dashboard` | `true` | Used by the retained Kendell dashboard. |
| `web_dashboard_open_on_launch` | `true` | Current local launch behaviour; not part of dashboard state semantics and may be simplified with S04/S05. |
| `web_dashboard_interface` | `null` | No explicit desktop/tray interface. Desktop/tray selection is unused and removable in S04. |
| `web_dashboard_listen_address` | `127.0.0.1` | Used: dashboard binds locally and is exposed by the deployment infrastructure. |
| `web_dashboard_trusted_hosts` | `127.0.0.1`, `localhost` | Used as the current dashboard host restriction. |
| `jetbrains_plugin_server_address` | `127.0.0.1` | Unused because the backend is LSP; remove in S03. |
| `jetbrains_launch_command` | `null` | Unused; remove in S03. |
| `tool_timeout` | `240` | Used as the tool execution timeout. |
| `language_server_idle_timeout` | `900` | Used by lazy language-server shutdown/cache behaviour. |
| `token_count_estimator` | `CHAR_COUNT` | Used only by current tool-usage statistics; no Kendell-specific requirement. Candidate for removal with analytics in S04. |
| `default_max_tool_answer_chars` | `150000` | Retained legacy ceiling/fallback for bounded tool answers. |
| `default_max_tool_answer_tokens` | `8000` | Used by retained-output default budgeting in the ChatGPT deployment. |
| `ignored_paths` | `[]` | No global path exclusions beyond project/gitignore behaviour. |
| `project_serena_folder_location` | `$projectDir/.serena` | Used for project-local memories, caches, logs and configuration. |
| `trusted_project_path_patterns` | `[]` | No project roots are globally trusted for project-supplied commands/settings. Safe currently because all activation commands and project LSP overrides are empty. |
| `ls_priorities` | `null` | Uses Serena's built-in language-server priorities for additional auto-detection. |

## Canonical registered projects

All registered projects keep `auto_detect_language_servers: true`, but S02 now explicitly records each intended semantic language set. This is important for languages deliberately excluded from automatic detection, such as LaTeX and JSON.

| Project | Canonical explicit language servers | Basis |
| --- | --- | --- |
| `artiq` | `python`, `json`, `yaml` | Project `tech_stack` memory; Nix intentionally remains plain text on this machine. |
| `artiq-tool` | `python`, `typescript`, `html`, `scss`, `json` | Project `tech_stack` memory. |
| `ndscan` | `python`, `yaml`, `toml` | Project `tech_stack` memory; Nix intentionally remains plain text. |
| `kendell.uk` | `html`, `scss` | Current retained source types; project has no onboarding memory. |
| `qengine` | `cpp`, `python` | Project `tech_stack` memory; JSON is intentionally omitted there. |
| `rowing` | `python` | Current retained source code; project has no onboarding memory. |
| `seahorse` | `python`, `cpp`, `json` | Project `tech_stack` memory. |
| `serena` | `python`, `typescript`, `html`, `scss`, `yaml` | Retained Serena/Orchestrator runtime and dashboard source types. |
| `thesis` | `latex`, `python`, `markdown`, `json` | Project `tech_stack` memory. LaTeX must be explicit because texlab is not auto-detected by default. |

A fresh-process `serena project health-check` was run sequentially for all nine roots after canonicalisation and all nine passed. The thesis check selected `thesis.tex`, loaded the explicit `latex` candidate and successfully started texlab, directly verifying the previously implicit/non-auto-detected case.

## Per-project field inventory

All nine project files use the same current schema. Unless noted below, values are identical across the projects.

| Field | Canonical deployment value | Kendell deployment use / disposition |
| --- | --- | --- |
| `default_modes` | `null` | No project overrides; remove with modes in S06. |
| `added_modes` | `null` | No project-added modes; remove with modes in S06. |
| `excluded_tools` | `[]` | No project-specific exclusions. Generic composition is unused. |
| `included_optional_tools` | `[]` | No project-specific optional inclusions. Generic composition is unused. |
| `fixed_tools` | `[]` | No project-specific fixed tool set. Generic composition is unused. |
| `symbol_info_budget` | `null` | Inherits the explicit global value `10`. |
| `language_backend` | `null` | Inherits global LSP. The field disappears in S03 when LSP becomes structural. |
| `line_ending` | `null` | Inherits the explicit global `native` convention. |
| `read_only_memory_patterns` | `[]` | No project memory restrictions. |
| `ignored_memory_patterns` | `[]` | No project ignored-memory patterns. |
| `ls_specific_settings` | `{}` | No project-specific LSP implementation overrides. |
| `project_name` | project-specific | Used for registered project activation/routing. |
| `language_servers` | project-specific explicit list above | Used to preserve intended semantic backends, including non-auto-detected ones. |
| `auto_detect_language_servers` | `true` | Used to lazily add applicable normal-priority servers beyond the explicit set. |
| `ignored_paths` | `[]` | No project-specific additional path exclusions. |
| `ls_workspace_folders` | `["."]` | Used: whole project root is the semantic workspace. |
| `ls_additional_workspace_folders` | `[]` | No cross-project LSP workspace additions. |
| `read_only` | `false` | All registered projects remain writable. |
| `ignore_all_files_in_gitignore` | `true` | Used for source/file filtering. |
| `initial_prompt` | empty except Serena | Serena embeds `critical_info`; all other projects have no project prompt. |
| `encoding` | `utf-8` | Used for project text files. |
| `activation_command` | `null` | Unused on every registered project. |
| `activation_command_timeout` | `180` | Latent only because no activation command is configured. |

The former Serena-only `base_modes` project key was removed during S02 because it is not part of the current `ProjectConfig` schema.

## S02 boundary

S02 changes configuration data only. It does not delete compatibility code. S03 and later stages may now remove JetBrains, modes/contexts and old migrations without preserving missing-field or stale-project behaviour for the Kendell deployment. If a later stage changes one of the behaviours marked as used above, that change is a product decision rather than a compatibility cleanup.
