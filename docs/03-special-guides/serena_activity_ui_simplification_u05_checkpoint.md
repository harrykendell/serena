# Serena Activity UI Simplification U05 Checkpoint

Date: 2026-09-11

U05 replaces normal-operation whole-history JSON persistence with independent indexed SQLite stores.

## Implementation

- `ExecutionStore` now owns `state.sqlite3` with normalized `sessions`, `executions`, `activity_runs`, `activity_run_executions`, and explicit `retained_resources` tables.
- SQLite is configured with WAL, `synchronous=NORMAL`, foreign keys and a bounded busy timeout; `batch_updates()` is a real nested transaction boundary.
- Execution/session/run reads use direct indexed SQL and overview summaries use set-based aggregation. Lifecycle mutations touch the affected rows rather than rewriting retained history.
- Retained snapshot/output references are extracted once at execution completion/import and indexed explicitly, removing historical result-body scans from normal retention queries.
- `JobStore` now owns its own `state.sqlite3`, with indexed status/session/finish access paths. Running-job discovery uses `status = running` rather than decoding every terminal record.
- Existing execution JSON/zstd state and per-job JSON files are imported transactionally only when required and renamed with `.migrated` after validation/commit. Execution history treats `durable_job_id` as a reference, so the same job may legitimately appear in multiple sessions (for example when another chat calls `job_status`); canonical unique ownership remains solely in `JobStore.session_id`. There is no normal-operation dual write.
- The job runner continues to receive a stable store locator, now the SQLite database path; job identity is recovered from its private command-file name.

## Validation

Validation after the final legacy-job importer regression test:

- Deployed cutover verification: all 40 legacy sessions, 7,075 legacy executions and 119 legacy activity runs are present in SQLite; 27 executions and 2 runs were then added normally after cutover. Both SQLite databases pass `PRAGMA integrity_check`.
- The previously failing durable job `309b8b487adf46ae8e6b382b740db517` has execution references from two sessions, while its canonical `JobStore` owner remains the session that started it. This is now an explicit regression case rather than a migration error.

- `uv run poe format`: **passed**.
- `uv run poe type-check`: **passed**.
- `uv run poe test`: **817 passed, 265 deselected**. The existing asynchronous logging-at-shutdown noise remained on stderr but did not fail the suite.

Measured on the current workstation with retained executions in one session and 100 lifecycle probes per history size:

| Retained executions | Store startup | Mean start+finish | SQLite + WAL bytes |
| ---: | ---: | ---: | ---: |
| 1,000 | 0.41 ms | 0.336 ms | 4,431,400 |
| 10,000 | 0.52 ms | 0.437 ms | 5,959,184 |

The 10x retained history increases measured start+finish bookkeeping by about 30%, rather than 10x, and startup remains sub-millisecond because retained history is not rebuilt into Python dictionaries.

Indexed zero-running-job query, averaged over 100 queries:

| Terminal jobs | Mean query |
| ---: | ---: |
| 100 | 0.003 ms |
| 1,000 | 0.003 ms |
| 5,000 | 0.003 ms |

These measurements target the U05 gate directly: execution lifecycle persistence is approximately history-independent and running-job discovery is index-backed in the presence of terminal history.
