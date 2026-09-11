# Serena Activity UI Simplification U04 Checkpoint

Date: 2026-09-11

Plan step: U04 — Correct derived counts and make read paths genuinely linear.

## Result

U04 is complete. The post-U03 read-side correctness and scaling issues are fixed before the persistence rewrite in U05.

The execution store now keeps only rebuildable derived indexes for overview queries: panel IDs resolve directly to sessions, durable-job membership is a set of unique job IDs per session, and dangling activity-run references are validated with one set-based pass over the retained indexes. Repeated executions that report the same durable job therefore contribute one job to the session summary.

Ordinary execution/session list and lookup paths no longer invoke retention pruning or persistence writes. Retention remains deterministic on lifecycle mutations and is also available explicitly through `ExecutionStore.maintain_retention()`. Job-retention synchronization updates only lifecycle facts and does not itself run artifact/capacity pruning.

`JobManager.list_running_jobs()` now reads the retained job catalogue once per call. It no longer performs a separate orphan-command cleanup scan followed by another catalogue decode, and it reuses that one decoded catalogue for retention synchronization. Lightweight selected-job reads avoid global retention work unless they discover a lifecycle transition. The remaining O(all retained jobs) JSON catalogue scan is intentionally the U05 storage problem; the benchmark below now exposes it directly rather than hiding it behind a synthetic job source.

## Regression coverage

`test/serena/test_execution_store.py` now verifies that three executions representing `start_job`, `job_status`, `job_status` for one durable job produce `execution_count == 3` and `durable_job_count == 1`. Retention tests also verify that ordinary reads do not opportunistically expire sessions and that explicit maintenance performs the deterministic prune.

`scripts/benchmark_dashboard_state.py` now includes one activity run per synthetic session so dangling-reference validation is exercised during store load. It also uses a real `JobStore`/`JobManager` with retained terminal-job JSON records instead of the previous in-memory fake.

## Scale measurements

Repeatable execution-side/dashboard measurement:

```bash
uv run python scripts/benchmark_dashboard_state.py --sessions 100 1000 --terminal-jobs 100 --samples 3 --json
```

Final medians:

| Sessions | Retained jobs | Median `/state` | Response |
| ---: | ---: | ---: | ---: |
| 100 | 100 + 3 running | 11.161 ms | 49,644 B |
| 1,000 | 100 + 3 running | 40.813 ms | 487,044 B |

A 10x increase in sessions costs about 3.7x in this fixture, comfortably inside the U04 linear-growth guardrail and substantially below both the U03 435.922 ms result and the plan's 110–150 ms execution-side target range. The fixture now includes activity runs and a real retained job catalogue, so it exercises more of the audited path than the U03 synthetic job source.

Real retained-job catalogue measurements with 100 visible sessions:

| Retained jobs | Median `/state` |
| ---: | ---: |
| 100 + 3 running | 11.788 ms |
| 1,000 + 3 running | 26.257 ms |
| 5,000 + 3 running | 131.506 ms |

These results confirm the remaining per-file JSON catalogue scaling that U05 is explicitly intended to replace with indexed SQLite. U04 removes duplicate catalogue decoding and unrelated retention/artifact work; it does not mask the storage-engine cost.

## Validation

Final validation on the modified source tree:

```bash
uv run poe format
uv run poe type-check
uv run poe test
node scripts/smoke_activity_ui.mjs
```

Results:

- formatting/lint: pass;
- type-check: pass;
- test suite: `816 passed, 265 deselected`;
- browser smoke: pass;
- browser 1,000-session DOM rebuild: 152.300 ms.

U04 is complete. U05 is the next programme step: replace execution/activity and durable-job whole-history JSON persistence with independent indexed SQLite stores and row-local transactions.
