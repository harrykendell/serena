# Serena Activity UI Simplification U07 Checkpoint

Date: 2026-09-11

Plan step: U07 — Give Orchestrator the same compact-query discipline and close the programme.

## Result

U07 is complete. The retained Orchestrator dashboard now follows the same one-document/query discipline as Serena without merging the two domains or their persistence owners. Overview polling reads compact aggregate rows only, opening one Orchestrator session performs one direct selected-session lookup, and the old 128-session/50-delegate visibility caps are gone.

The post-SQLite deployment cutover has also been closed: the obsolete execution-state and durable-job JSON import paths were removed only after the deployed stores were confirmed migrated, while Zstandard compression remains for retained tool outputs and file snapshots where it still has a live purpose.

## Compact Orchestrator dashboard queries

`DelegateStore` keeps canonical delegate JSON records unchanged and owns a small rebuildable SQLite dashboard projection in `delegates/.dashboard-index.sqlite3`.

The projection:

- is rebuilt once from canonical delegate records when a store process starts;
- is updated row-locally whenever a canonical delegate record changes;
- exposes one grouped summary per parent session containing panel/session identity, timestamps, active state, delegate count and active count;
- supports direct indexed selected-session lookup by stable `panel_id` and returns the complete compact delegate-status history for that session;
- exposes generation-based overview/selected-session revision tokens so conditional requests do not scan retained record files;
- remains derived implementation state rather than a second semantic owner.

`OrchestratorDashboardSessionArchive` now provides direct `panel_id` lookup and cheap revision state for retained conversation names. `DashboardOrchestratorOverview` merges those metadata records with compact delegate summaries without materialising delegate histories for overview cards.

The dashboard overview payload no longer contains a `delegates` array for each Orchestrator card. The renderer consumes `delegate_count` and `active_count`; the selected-session document still contains the complete delegate list and optional expanded detail.

Observable regressions cover 130 retained Orchestrator sessions, 55 delegates in one selected session, unchanged selected-session `304` responses before document construction, and visibility of delegate changes written through another `DelegateStore` instance.

## Hard-cutover cleanup

Before deleting migration code, the deployed state was inspected:

- execution persistence had `state.sqlite3` and only `state.json.migrated`, with no live legacy execution JSON;
- durable-job persistence had `state.sqlite3`, 205 `.json.migrated` files and zero live job JSON files.

The one-time execution JSON/zstd importer, old schema constants/argument migration helpers, legacy retained-resource fallbacks, and per-job JSON importer were then removed. Their migration-specific tests were deleted with the retired functionality.

`RetainedTextCompression` remains because retained pageable tool outputs and exported text snapshots still use it independently of the removed persistence migration path.

## Server scale measurements

Repeatable Serena overview command:

```bash
uv run python scripts/benchmark_dashboard_state.py --samples 5 --json
```

The fixture now builds current SQLite state through `ExecutionStore`, including a real activity run for every retained session, four completed calls per session, 100 terminal jobs and three running jobs.

| Serena sessions | Median `/state` | Response | Git cache reads | Git refreshes | Running-job queries | Terminal telemetry ops |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 4.533 ms | 5,923 B | 1 | 0 | 1 | 0 |
| 100 | 24.116 ms | 49,840 B | 1 | 0 | 1 | 0 |
| 1,000 | 124.281 ms | 489,057 B | 1 | 0 | 1 | 0 |

The 100→1,000 increase is about 5.2× for 10× more retained sessions and the 1,000-session result is within the programme's measured indexed target range. No per-session Git refresh, terminal-job telemetry scan or per-panel request appears.

Repeatable Orchestrator query command:

```bash
uv run python scripts/benchmark_orchestrator_dashboard.py --json
```

Each case contains one delegate in every retained session plus 64 delegates in the selected session.

| Orchestrator sessions | Startup rebuild | Overview median | Overview bytes | Selected 64 delegates | Selected bytes | Derived SQLite + WAL/SHM |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 38.034 ms | 2.055 ms | 19,200 B | 3.069 ms | 19,969 B | 3,588,336 B |
| 1,000 | 185.090 ms | 10.628 ms | 191,669 B | 2.456 ms | 19,970 B | 4,406,896 B |

Unchanged overview construction grows approximately linearly while selected-session cost is independent of unrelated retained sessions. The first 1,000-session overview after archive invalidation was 58.011 ms; subsequent unchanged samples were 10.6 ms because conversation metadata is revision-cached rather than reopening 1,000 JSON files per poll. The selected document contains all 64 delegates rather than the former 50-entry truncation.

## Storage scale measurements

Repeatable command:

```bash
uv run python scripts/benchmark_activity_storage.py --json
```

Execution lifecycle results use 100 measured start+finish probes after constructing each retained-history size:

| Retained executions | Store startup | Mean start+finish | Median start+finish | SQLite + WAL/SHM |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 1.421 ms | 0.644 ms | 0.362 ms | 4,558,304 B |
| 10,000 | 1.419 ms | 1.070 ms | 0.352 ms | 7,343,752 B |

The median lifecycle cost is effectively unchanged at 10× retained history and startup remains history-independent.

Zero-running-job discovery uses 100 queries per history size:

| Terminal jobs | Mean query | Median query | SQLite + WAL/SHM |
| ---: | ---: | ---: | ---: |
| 100 | 0.0144 ms | 0.0131 ms | 2,224,616 B |
| 1,000 | 0.0121 ms | 0.0109 ms | 4,685,376 B |
| 5,000 | 0.0155 ms | 0.0142 ms | 6,610,544 B |

The 5,000-terminal-job case remains an indexed constant-cost query rather than a catalogue scan.

## Browser measurements and request invariants

Repeatable command:

```bash
node scripts/smoke_activity_ui.mjs
```

Final measured output:

| Case | U07 measurement |
| --- | ---: |
| 1,000-card overview DOM rebuild | 134.5 ms |
| 2,048-call selected session initial render | 350.4 ms |
| 2,048-call unchanged/detail rerender | 19.0 ms |
| 2,048-call expand | 1.9 ms |
| 2,048-call collapse | 1.0 ms |

The complete smoke matrix passes, including one-periodic-request routing, selected-session expansion, return navigation, notification/deep-link behaviour, push opt-in, compact Orchestrator overview plus selected-detail rendering, refresh coalescing, idle/hidden cadence and service-worker notification handling.

## Validation and deployed restart

Final validation on the modified tree:

```bash
uv run poe format
uv run poe type-check
uv run poe test
node --check src/serena/resources/activity/activity-panel.js
node --check src/serena/resources/activity/inline-host.js
node --check src/serena/resources/kendell_dashboard/dashboard.js
node --check scripts/smoke_activity_ui.mjs
node scripts/smoke_activity_ui.mjs
uv run python scripts/benchmark_dashboard_state.py --samples 5 --json
uv run python scripts/benchmark_orchestrator_dashboard.py --json
uv run python scripts/benchmark_activity_storage.py --json
```

Results:

- formatting/lint: pass;
- type-check: pass;
- JavaScript syntax checks: pass;
- full test suite: `818 passed, 265 deselected`;
- browser smoke matrix: pass;
- compact Orchestrator >128-session/>50-delegate regression: pass;
- unchanged Serena/Orchestrator conditional-request regressions: pass.

The deployed `orchestrator-mcp.service` and `serena-mcp.service` were restarted after migration cleanup. Before restart the persisted stores contained 42 sessions, 7,608 executions, 123 activity runs and 224 jobs with one running job. After restart they contained the same 42 sessions, 123 activity runs and 224 jobs; executions had advanced to 7,614 solely from the verification tool calls. The dashboard returned the same 42 Serena panels, two Orchestrator panels and one running job. Both SQLite databases returned `ok` from `PRAGMA integrity_check`.

## Final audit

The final source audit found no retained Serena dashboard compatibility adapter, iframe/window-openai emulation, duplicate `structured_arguments` renderer field or browser delta/hydration protocol. Canonical execution/job facts remain in independent SQLite stores, canonical Orchestrator delegate facts remain in Orchestrator's independent store, and the Orchestrator SQLite dashboard index is explicitly rebuildable derived state.

U00–U07 are complete. The rebuilt activity/dashboard path now satisfies the programme invariants: facts are persisted once, presentation is derived on demand, overview reads are compact and approximately linear, selected detail comes from one direct document, unchanged polling stops before document construction, and browser work is limited to visible interaction state.
