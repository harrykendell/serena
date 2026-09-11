# Serena Activity UI Simplification U01 Checkpoint

Date: 2026-09-11

Plan step: U01 — Finish canonical backend ownership and build the bounded read model.

## Result

U01 cuts activity reads over to canonical execution/job/Git state and introduces the bounded, stateless `ActivityView` required by U02.

Key ownership changes:

- `SerenaFastMCPTool` creates and finalises `ExecutionRecord` directly.
- `ActivityRunManager` owns only run creation, supersession, project attribution and execution membership.
- `ActivityPanelRun` persists only run facts plus `execution_ids`; persisted `job_ids` and `retained_jobs` are removed.
- `ExecutionStore` schema v3 stores bounded structured argument mappings. A v2 load migrates legacy argument strings once, removes legacy run job mirrors and immediately writes v3 state.
- result/media/durable-job metadata extraction lives in `execution_metadata.py`, outside the UI layer.
- `ActivityView` is a stateless typed projection over `ExecutionStore`, `JobManager` metadata and cached Git metrics.

## Bounded read paths

`ExecutionStore` now maintains rebuildable indexes for retained-session execution membership/counts/latest execution and durable-job-to-session ownership. It exposes lightweight execution summary records that exclude result/error/media bodies.

The three activity query paths are:

- `dashboard_overview()`: all retained sessions, active first then recency, with only the latest bounded call summary per session;
- `for_run(session_id, run_id)`: one inline activity run using lightweight execution summaries;
- `for_session(panel_id, expanded_entry_id=None)`: one complete retained-session row document, fetching a full execution or job snapshot only when that entry is explicitly expanded.

Ordinary dashboard overview construction uses one `ActivityView.dashboard_overview()` call for both Serena panel discovery and global running-job metadata. Cached Git metrics are reused once per project for that query. Job overview reads use running-job metadata only; terminal runtime telemetry is not collected.

A direct rebuildable `job_id -> session_id -> panel_id` lookup preserves notification/deep-link routing without scanning retained panels.

## Structural scale guard

`test/serena/test_activity_view.py::test_dashboard_overview_scales_from_lightweight_indexes_without_history_expansion` constructs 1,000 retained sessions and asserts that overview construction:

- returns all 1,000 sessions with no cap;
- orders an active session before newer inactive sessions;
- does not call full-session execution expansion;
- does not fetch/copy a full `ExecutionRecord`;
- does not decode historical result bodies;
- performs one running-job metadata query and no terminal-job detail/telemetry query;
- reads cached Git metrics once for the shared project and never refreshes them;
- keeps the unexpanded selected-session path on lightweight execution summaries;
- fetches/decodes the full result only after an explicit expanded-entry request.

## Repeatable dashboard benchmark

Command:

```bash
uv run python scripts/benchmark_dashboard_state.py --sessions 10 100 1000 --samples 3 --json
```

The fixture remains four completed executions per session, up to 100 retained terminal jobs, multi-kilobyte result bodies and one shared `serena` project.

| Sessions | U00 median | U01 median | Speed-up | Returned panels | Git cache reads | Git refreshes | Running-job metadata queries | Terminal telemetry ops |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 12.855 ms | 1.242 ms | 10.4x | 10 | 1 | 0 | 1 | 0 |
| 100 | 601.770 ms | 5.561 ms | 108.2x | 100 | 1 | 0 | 1 | 0 |
| 1,000 | 58,743.949 ms | 190.198 ms | 308.9x | 1,000 | 1 | 0 | 1 | 0 |

The 1,000-session response remains about 223 KiB; the improvement comes from eliminating history-proportional server construction rather than hiding sessions or paging the overview.

## Validation

The U01 checkpoint is validated with:

```bash
uv run poe format
uv run poe type-check
uv run poe test
uv run python scripts/benchmark_dashboard_state.py --sessions 10 100 1000 --samples 3 --json
```

The final validation pass is recorded in the U01 checkpoint commit.
