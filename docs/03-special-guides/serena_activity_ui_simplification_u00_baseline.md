# Serena Activity UI Simplification — U00 Baseline

Date: 2026-09-11

Plan: `docs/03-special-guides/serena_activity_ui_simplification_plan.md`

## Purpose

This document records the attributable pre-rebuild baseline required by U00. It is a regression guard for the simplification programme, not a performance target for preserving the legacy architecture.

Sections 3 and 4 of the plan are the retained/removal acceptance contract for U01–U03.

## Working-tree preservation

The plan began from commit `3435c894` plus a mixed working tree. U00 separated the useful pre-existing work into two commits before structural UI work:

- `a0c6a541` — `Preserve pending Serena backend performance fixes`
- `94818381` — `Preserve dashboard notifications and deep links`
- `04ef9b6e` — `Validate preserved backend performance fixes`

The first commit contains the unrelated execution-store batching/language-server freshness/job scheduling performance work. The second contains the dashboard notification/PWA/deep-link work, including the top-level running-jobs metadata changes needed by that UX. The third restores source-discovery auto-detection and updates shell-test fixtures after the validation suite exposed regressions in the preserved backend work. This leaves the simplification plan and its U00 benchmark as the only new programme work.

## Source-size baseline

Measured with:

```bash
wc -l \
  src/serena/activity.py \
  src/serena/dashboard_widgets.py \
  src/serena/custom_dashboard.py \
  src/serena/resources/kendell_dashboard/dashboard.js \
  src/serena/resources/kendell_dashboard/styles.css \
  src/serena/resources/kendell_dashboard/index.html \
  src/orchestrator/activity.py
```

| File | Lines |
| --- | ---: |
| `src/serena/activity.py` | 2,022 |
| `src/serena/dashboard_widgets.py` | 253 |
| `src/serena/custom_dashboard.py` | 819 |
| `src/serena/resources/kendell_dashboard/dashboard.js` | 883 |
| `src/serena/resources/kendell_dashboard/styles.css` | 591 |
| `src/serena/resources/kendell_dashboard/index.html` | 156 |
| `src/orchestrator/activity.py` | 561 |
| **Total** | **5,285** |

The final comparison should report both this same file set and the replacement files introduced by the rebuild so deleted code is not hidden by moving it.

## Current browser request model

The outer dashboard has one scheduler, but the retained activity implementation adds independent iframe/widget polling state beneath it.

The top-level dashboard currently requests:

- first refresh: `/dashboard/api/state?include_state=1` unless equivalent bootstrap data was embedded in the page;
- subsequent refreshes: `/dashboard/api/state`;
- cadence: 500 ms while any Serena/Orchestrator panel is active, 5 s while visible and idle, 60 s while hidden.

Each mounted retained Serena iframe embeds the inline activity widget and runs its own activity timer. That timer executes every 500 ms while a tool is running, every 3 s while a job is running, and every 5 s otherwise. The dashboard adaptor normally serves that timer from its local copy, but when the parent `/state` response announces a changed revision it marks the iframe dirty and the iframe's next timer requests `/dashboard/api/serena/panels/<panel_id>` (with `changed_since` when available). Orchestrator retained iframes use the same revision/dirty arrangement and fetch their panel endpoint when dirty.

Therefore browser network request count is not structurally fixed at one request per refresh. A changing set of `N` mounted panels can produce the top-level `/state` request plus up to `N` independent panel refreshes, with separate iframe timers, state machines and scheduling. Initial widget documents are shared through the `SessionWidgetLoader` blob-document cache, but every mounted panel still owns an iframe runtime and activity timer.

U02 must replace this with exactly one current server document and one periodic request: overview mode polls only `/state`; opening one session replaces that poll target with one selected-session document.

## Repeatable synthetic scale benchmark

Benchmark:

```bash
uv run python scripts/benchmark_dashboard_state.py --sessions 10 100 1000 --samples 3 --json
```

Fixture characteristics:

- 4 completed historical executions per retained session;
- non-trivial JSON arguments and multi-kilobyte result bodies on every execution;
- up to 100 retained terminal jobs referenced by synthetic calls;
- all sessions use the same `serena` project so repeated Git-cache lookup behaviour is visible;
- each sample forces the legacy presentation-layer job cache cold so work is attributable to one state request;
- timing covers the Flask `/dashboard/api/state` request and JSON serialization, not fixture generation.

Measured baseline:

| Retained sessions | Calls/session | Terminal jobs | Panels returned | Median | Min | Max | Response | Git cache reads | Git refreshes | Job snapshot queries | Job telemetry ops |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 4 | 10 | 10 | 12.855 ms | 11.818 ms | 15.403 ms | 2,663 B | 10 | 0 | 1 | 10 |
| 100 | 4 | 100 | 100 | 601.770 ms | 592.674 ms | 639.310 ms | 23,183 B | 100 | 0 | 2 | 200 |
| 1,000 | 4 | 100 | 1,000 | 58,743.949 ms | 53,105.007 ms | 59,580.908 ms | 227,383 B | 1,000 | 0 | 2 | 200 |

The 1,000-session response is only about 222 KiB, so transfer size is not the dominant baseline failure. Server construction is the catastrophic scaling problem.

## Baseline diagnosis and guards

The current overview path expands every retained session through `DashboardActivityArchive._session_payload()`. For each session it obtains that session's executions by filtering the complete execution list, parses stored argument strings, formats details and carries historical result bodies through intermediate dictionaries. `DashboardActivitySessionSummary.from_session()` then discards most of that expanded data. With `S` sessions and `E` retained executions, repeated full execution filtering makes overview discovery approximately `O(S * E)` before the avoidable body parsing/copying cost; with a fixed number of calls per session this trends quadratically in session count.

The benchmark also demonstrates two secondary costs:

- `DashboardSerenaActivityOverview._git_metrics()` performs one cached Git lookup per session even when all sessions share one project. It performs zero Git refreshes/subprocesses, which is good, but U01 should reuse each project's cached value once per overview query.
- `DashboardSerenaActivityOverview._jobs_by_id()` asks `DashboardJobOverview.get_jobs()` for retained terminal jobs with runtime telemetry. Once overview construction itself exceeds the 0.5 s presentation cache lifetime, `dashboard_state()` can perform that expensive operation twice in one request: once while constructing Serena panels and again for the top-level jobs summary. The 100- and 1,000-session cases therefore perform 200 synthetic telemetry operations for 100 terminal jobs.

The final architecture must make the following properties structural, and the scale tests introduced during implementation should assert them as observable/query-call behaviour rather than merely comparing wall-clock timings:

1. one compact `/state` overview document returns every retained session summary;
2. overview discovery does not expand each session's historical execution bodies;
3. ordinary overview construction does not parse/copy historical result bodies;
4. execution discovery is indexed/single-pass rather than filtering all executions once per session;
5. Git metrics are cached by project within one view construction and no Git refresh/subprocess occurs;
6. overview job discovery uses running-job metadata only and does not collect terminal-job runtime telemetry;
7. browser request count remains exactly one periodic request for the current route, independent of retained-session count;
8. no entry cap or pagination may be used to disguise poor 1,000-session scaling.

These are the U00 guardrails for U01–U03.
