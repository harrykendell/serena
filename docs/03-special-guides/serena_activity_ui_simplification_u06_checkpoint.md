# Serena Activity UI Simplification U06 Checkpoint

Date: 2026-09-11

Plan step: U06 — Remove avoidable transport and renderer work.

## Result

U06 is complete. The dashboard still uses the U02/U03 one-current-document architecture, but unchanged conditional requests now stop before constructing or serialising the route document, the renderer contract has one serializer and one owner for timing/latest-summary semantics, and large selected Serena sessions no longer rebuild their complete row tree for one-row expansion changes.

The measured large-list result does not justify introducing renderer virtualisation: the 2,048-call initial construction remains a one-off roughly half-second operation, while unchanged/detail rerender and expand/collapse interactions are now tens of milliseconds or less. A windowing state machine would add complexity without addressing a remaining interaction bottleneck.

## Conditional transport

`CustomDashboard` now computes route ETags from cheap source revisions before calling the document builder:

- Serena overview revision uses O(1) SQLite execution/job generations (`total_changes` plus `PRAGMA data_version`) plus Orchestrator/runtime metadata;
- selected Serena revision uses the indexed retained-session update revision, the O(1) durable-job store generation and the selected expanded-entry identity;
- selected Orchestrator revision uses metadata-only retained-file revisions pending the dedicated compact Orchestrator query work in U07;
- a matching `If-None-Match` returns `304` before `ActivityView.dashboard_overview()` / `ActivityView.for_session()` and before JSON serialisation;
- the revision is invalidation metadata only; no browser delta protocol or second activity model was introduced.

Regression tests replace the overview/session builders with raising mocks after obtaining the first ETag, then verify that a matching conditional request still returns an empty `304`. This directly proves that the unchanged path does not construct the large document merely to discover that it is unchanged.

## Canonical activity payload ownership

`ActivityView` now owns:

- the exact latest-activity rule: most recently submitted/started call or job;
- first-to-latest tool submission span for both active runs and retained sessions.

The shared renderer receives those facts directly and no longer independently sorts calls by finish time or recomputes spans.

A new `serena.activity_transport` module is the single serializer for activity entries, jobs, snapshots and expanded details at both MCP and HTTP boundaries. The obsolete duplicate `structured_arguments` representation was removed; expanded calls carry one structured `arguments` value. The versioned inline resource moved to `activity-v33.html` for the renderer-contract change.

Assembled inline activity HTML and dashboard static/shared assets are cached for the process lifetime instead of being repeatedly read/stat'ed for each resource or dashboard-index request.

## Large selected-session rendering

The shared `ActivityPanel` now:

- builds complete row batches in detached `DocumentFragment`s and attaches each batch once;
- uses one delegated row-click handler instead of one event listener per row;
- patches only the previously expanded and newly expanded rows when selection changes;
- refreshes only the expanded detail container when a response changes detail but not the underlying row document;
- preserves the selected-session row tree while network detail loading is in flight;
- makes dashboard `summaryMode` header-only, so overview cards never construct hidden row/list/job-control DOM;
- applies `content-visibility: auto` with an intrinsic row size to suppress unnecessary offscreen layout/paint work;
- runs the one-second elapsed-time clock only when visible live elapsed text exists and suspends it while the document is hidden.

The dashboard expansion callback no longer performs the redundant full local rerender before requesting the selected-session document.

## Browser measurements

Repeatable command:

```bash
node scripts/smoke_activity_ui.mjs
```

Final measured output from the headless-Chrome smoke harness:

| Case | U03 | U06 |
| --- | ---: | ---: |
| 1,000-card overview DOM rebuild | 321.5 ms | 153.4 ms |
| 2,048-call selected session initial render | — | 518.7 ms |
| 2,048-call unchanged/detail rerender | — | 17.5 ms |
| 2,048-call expand | — | 1.6 ms |
| 2,048-call collapse | — | 0.9 ms |

The 2,048-call benchmark also verifies that expansion preserves the existing scroll position. The complete browser smoke matrix passes, including the one-periodic-request invariant, selected-session expansion, return navigation, notification deep links, push opt-in, Orchestrator rendering, refresh coalescing, idle/hidden refresh behaviour and service-worker notification handling.

No renderer virtualisation was added because the measured interaction path is already cheap and bounded to the affected row/detail.

## Server scale measurement

Repeatable command:

```bash
uv run python scripts/benchmark_dashboard_state.py --json
```

The existing fixture uses four completed calls per retained session, non-trivial historical arguments/results, 100 retained terminal jobs, three running jobs and one shared project.

| Sessions | U03 median | U06 median | U06 response | Git cache reads | Git refreshes | Running-job metadata queries | Terminal telemetry ops |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 2.957 ms | 5.057 ms | 5,904 B | 1 | 0 | 1 | 0 |
| 100 | 14.927 ms | 17.277 ms | 49,644 B | 1 | 0 | 1 | 0 |
| 1,000 | 435.922 ms | 96.165 ms | 487,044 B | 1 | 0 | 1 | 0 |

The 1,000-session current-document construction is about 4.5x faster than the U03 checkpoint while retaining all summaries and the same compact-query invariants. More importantly for U06, unchanged HTTP polls no longer pay even this construction/serialization cost before returning `304`.

## Validation

Final validation on the modified tree:

```bash
node --check src/serena/resources/activity/activity-panel.js
node --check src/serena/resources/activity/inline-host.js
node --check src/serena/resources/kendell_dashboard/dashboard.js
node --check scripts/smoke_activity_ui.mjs
uv run poe format
uv run poe type-check
uv run poe test
node scripts/smoke_activity_ui.mjs
uv run python scripts/benchmark_dashboard_state.py --json
```

Results:

- JavaScript syntax checks: pass;
- formatting/lint: pass;
- type-check: pass;
- focused activity/dashboard tests: `45 passed`;
- full test suite: `819 passed, 265 deselected`;
- expanded headless-browser smoke: pass;
- 1,000-card overview rebuild: `153.4 ms`;
- 2,048-call selected session: `518.7 ms` initial, `17.5 ms` unchanged/detail rerender, `1.6 ms` expand, `0.9 ms` collapse, scroll position preserved;
- 1,000-session server document: `96.165 ms` median, `487,044 B`, one cached Git read, zero Git refreshes, one running-job metadata query and zero terminal telemetry operations.

U00–U06 are complete. U07 is now the next implementation step: give the independent Orchestrator path equivalent compact-query discipline and then perform the final cutover audit/legacy cleanup.
