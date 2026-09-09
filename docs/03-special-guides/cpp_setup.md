# C/C++ semantic setup

Serena's retained C/C++ language server is clangd. Configure a project with `cpp` in `language_servers` when C/C++ semantic tools are required.

## Compilation database

For reliable cross-file symbols and references, generate `compile_commands.json` at the project root with the real compiler flags, include paths, and language standard used by the build.

Clangd needs usable absolute directory paths in the compilation database. Serena reads the root `compile_commands.json`, normalises relative directories when necessary, and writes the derived database to:

```text
.serena/compile_commands.json
```

The original build-generated file is not modified. Serena then launches clangd with the derived directory via `--compile-commands-dir`.

The output directory can be changed for a trusted project through language-server settings:

```yaml
ls_specific_settings:
  cpp:
    compile_commands_dir: ".serena"
```

## Clangd runtime

On the supported Linux x86-64 deployment, Serena manages the pinned clangd runtime automatically when it is not already available. The retained default is clangd 19.1.2.

A trusted project can override that version:

```yaml
ls_specific_settings:
  cpp:
    clangd_version: "19.1.2"
```

For normal Kendell projects, leave the default unchanged unless a project has a concrete compatibility requirement.

## Practical checks

If C/C++ navigation is incomplete, first verify that:

- the root `compile_commands.json` exists and is current;
- every relevant translation unit is present in it;
- compiler include paths and `-std=` flags match the real build;
- generated headers or source files needed by clangd exist before semantic queries run.

Clangd uses background indexing, so a correct compilation database is the main prerequisite for reliable project-wide references.
