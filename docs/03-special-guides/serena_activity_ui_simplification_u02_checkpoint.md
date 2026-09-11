# Serena Activity UI Simplification U02 Checkpoint

Date: 2026-09-11

Plan step: U02 — Build the small renderer and cut over both Serena surfaces.

## Result

U02 is complete. The ChatGPT inline activity app and retained Serena web dashboard now share one ordinary timerless activity renderer over immutable snapshots, while each host owns only its transport and scheduling responsibilities.

The old iframe/widget compatibility architecture is physically removed rather than retained as a fallback.

## Shared renderer

The shared renderer lives in:

- `src/serena/resources/activity/activity-panel.js`;
- `src/serena/resources/activity/activity-panel.css`.

`ActivityPanel` owns only presentation state and DOM rendering. It has no polling, HTTP/MCP transport, `window.openai` dependency, timers, iframes, `postMessage` bridge, resize observers or intersection observers.

It retains the intended Serena activity behaviour:

- compact session/tool/job/Git summary metadata;
- collapse and expand;
- bounded-height scrolling activity history with follow-latest behaviour until the user scrolls away;
- status, semantic detail, scope and timing rows;
- at most one expanded entry;
- deterministic structured, text, error and media presentation;
- Git additions/deletions/ahead metadata;
- submission-span metadata;
- host-driven running-duration updates through `tick(now)`.

## ChatGPT inline host

`src/serena/activity.py` is reduced to the activity-run boundary plus assembly of the self-contained MCP app resource. It runtime-inlines the shared CSS/renderer and the thin transport host in `src/serena/resources/activity/inline-host.js`.

The inline host owns:

- one activity snapshot polling loop;
- MCP `window.openai` transport;
- on-demand call/job details and media;
- host-driven elapsed-time ticks;
- intrinsic-height notification;
- superseded-terminal retirement.

The shared renderer itself remains transport- and timer-free.

## Direct dashboard route model

The web dashboard now has one current server document and one periodic request for that document.

Overview mode polls only:

```text
/dashboard/api/state
```

That document contains every retained Serena session as a compact summary. A session summary contains counts, activity/Git metadata and one latest-activity summary; it does not hydrate the session's historical call/result bodies.

Opening one Serena session switches the same poller to:

```text
/dashboard/api/serena/sessions/<panel_id>
```

Opening one entry folds expansion into that same document request:

```text
/dashboard/api/serena/sessions/<panel_id>?expanded=<entry_id>
```

Returning to the overview switches the poller back to `/dashboard/api/state`. There is therefore no per-session polling fan-out and no second periodic detail request.

The dashboard owns one shared elapsed-time clock. Route changes rebuild the direct DOM from the current complete document rather than reconciling iframe-local/delta state.

The same route-oriented pattern is used for the small direct-DOM Orchestrator dashboard path:

```text
/dashboard/api/orchestrator/sessions/<panel_id>?expanded=<delegate_id>
```

## Compatibility architecture removed

U02 removes:

- `src/serena/dashboard_widgets.py`;
- `src/serena/dashboard_activity.py` / `DashboardActivityArchive`;
- `DashboardSerenaActivityOverview`;
- `/dashboard/widget/*` routes;
- dashboard fake `window.openai` adapters;
- activity iframe/blob documents;
- `SessionWidgetLoader`;
- duplicate Serena preview renderer/CSS;
- dashboard `postMessage` bootstrap/readiness/height/focus protocols;
- browser-side `summary_only`, `partial` and `changed_since` merge state;
- synthetic browser panel revision/dirty machinery used only by the deleted compatibility path.

Canonical retention/restart/migration tests formerly colocated with the deleted dashboard archive tests were moved to `test/serena/test_execution_store.py`, so deleting the presentation wrapper did not delete canonical `ExecutionStore` coverage.

## Browser/request smoke coverage

`scripts/smoke_activity_ui.mjs` uses the installed headless Chrome directly and introduces no Playwright/Selenium dependency. It exercises the real shared renderer/dashboard JavaScript against deterministic route documents and verifies:

- 10 retained sessions still cause exactly one periodic `/dashboard/api/state` request;
- 1,000 retained sessions still cause exactly one periodic `/dashboard/api/state` request;
- opening a Serena session switches to one selected-session document request;
- expanding one call uses the same selected-session route with `expanded=<call_id>` rather than a second detail poller;
- expanded detail renders;
- elapsed text changes locally from the dashboard's shared clock.

The final browser smoke passes.

## Scale measurement

Repeatable command:

```bash
uv run python scripts/benchmark_dashboard_state.py --json
```

The fixture remains four completed calls per retained session, up to 100 retained terminal jobs and one shared `serena` project.

| Sessions | U00 median | U01 median | U02 median | U02 response | Git cache reads | Git refreshes | Running-job metadata queries | Terminal telemetry ops |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 12.855 ms | 1.242 ms | 3.581 ms | 5,243 B | 1 | 0 | 1 | 0 |
| 100 | 601.770 ms | 5.561 ms | 15.505 ms | 48,983 B | 1 | 0 | 1 | 0 |
| 1,000 | 58,743.949 ms | 190.198 ms | 358.680 ms | 486,383 B | 1 | 0 | 1 | 0 |

The richer U02 compact-summary transport is larger and somewhat slower to serialize than the minimal U01 transition payload, but the 1,000-session path remains roughly 164× faster than U00 and returns all retained sessions without capping or paging. Historical result bodies remain outside ordinary overview construction.

The browser smoke separately proves that periodic request count is invariant with retained-session count.

## Source-size comparison

The U00 relevant source baseline was 5,285 lines. Counting the replacement shared assets rather than hiding moved code, the corresponding U02 activity/dashboard source set is 3,689 lines:

```text
   68 src/serena/activity.py
  501 src/serena/custom_dashboard.py
  562 src/serena/resources/activity/activity-panel.js
  397 src/serena/resources/activity/activity-panel.css
  188 src/serena/resources/activity/inline-host.js
  784 src/serena/resources/kendell_dashboard/dashboard.js
  472 src/serena/resources/kendell_dashboard/styles.css
  156 src/serena/resources/kendell_dashboard/index.html
  561 src/orchestrator/activity.py
 3689 total
```

That is 1,596 fewer lines, about a 30.2% reduction, while replacing iframe-specific compatibility machinery with ordinary shared resources and direct route rendering.

## Validation

Final U02 validation on the exact source/test tree:

```bash
node --check src/serena/resources/activity/activity-panel.js
node --check src/serena/resources/activity/inline-host.js
node --check src/serena/resources/kendell_dashboard/dashboard.js
node --check scripts/smoke_activity_ui.mjs
node scripts/smoke_activity_ui.mjs
uv run poe format
uv run poe type-check
uv run poe test
uv run python scripts/benchmark_dashboard_state.py --json
```

Results:

- JavaScript syntax checks: pass;
- headless browser smoke: pass;
- formatting/lint: pass;
- type-check: pass;
- test suite: `815 passed, 265 deselected`;
- final 1,000-session benchmark: 358.680 ms median, 486,383 B, with one cached Git read, zero Git refreshes, one running-job metadata query and zero terminal telemetry operations.

U03 remains responsible for the final peripheral-behaviour audit, notification/PWA/deep-link validation, broader Orchestrator coverage, final scale/DOM audit and hard-cutover comparison.
