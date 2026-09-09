# Task Completion Checklist

After any code change in `src/` or `test/`, run:

1. `uv run poe format` — applies ruff fixes + formatting (mutates files).
2. `uv run poe type-check` — ty on `src/serena`, `src/solidlsp`, `test/`.
3. `uv run poe test` — pytest on affected files or affected languages, using `-m` markers.

If memories were edited/renamed/split: run `uv run --no-sync serena memories check` from the project root to find broken `mem:` references.
