# Codex Desktop integration

Serena owns its Codex Desktop integration in this directory.

Tracked files:

- `bootstrap.sh` — clones the upstream Linux wrapper, builds the feature overlay, and runs its native bootstrap.
- `features/serena-approval-notifications/` — Serena-owned ChatGPT approval forwarding feature.

Generated local state:

- `upstream/` — unmodified clone of `https://github.com/ilysenko/codex-desktop-linux.git`.
- `overlay/` — generated Linux feature root containing upstream `tray-usage` plus Serena's approval feature.

Both generated directories are gitignored.

From this directory:

```bash
./install.sh
```

Set `SERENA_CODEX_BOOTSTRAP_PREPARE_ONLY=1` to stop after cloning and generating the overlay without installing the native package.
