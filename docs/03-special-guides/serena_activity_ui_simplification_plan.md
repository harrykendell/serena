# Serena Activity UI Simplification and Dashboard Rebuild Plan

Status: IN PROGRESS — U00–U02 COMPLETE (2026-09-11)

Baseline: `3435c894` (`Show current Git metrics in Serena activity`) plus the original mixed working tree. U00 preserved that pre-existing work as `a0c6a541` (backend performance fixes), `94818381` (dashboard notifications/PWA/deep links), and `04ef9b6e` (validation fixes for the preserved backend work), then recorded the measurable rebuild baseline in `docs/03-special-guides/serena_activity_ui_simplification_u00_baseline.md`.

Scope: Serena's ChatGPT inline activity GUI, the retained Serena/Orchestrator web dashboard, the presentation/read-model layer that supplies them, activity detail rendering, dashboard polling/bootstrap, dashboard notifications/deep links, and tests that define the observable UI/data contract.

Out of scope: observable tool execution semantics, central result presentation/truncation semantics, job execution/storage, session retention policy, Git metric calculation, Orchestrator delegation semantics, and unrelated MCP tool behaviour. Internal execution/activity ownership and wiring may be simplified where doing so removes duplicate lifecycle/presentation responsibilities without changing tool behaviour.

## 1. Goal

Rebuild Serena's activity presentation as a small, direct view over the current canonical backend instead of preserving compatibility machinery accumulated around older dashboard/activity implementations.

The useful product behaviour should remain, but each fact and concern should have one owner:

```text
SerenaFastMCPTool ─────────────► ExecutionStore
                                 SessionRecord
                                 ExecutionRecord
                                 ActivityPanelRun

durable jobs ─────────────────► JobManager / JobRecord
git state ────────────────────► GitMetricsSource

                                      │
                                      ▼
                                 ActivityView
                            ┌──────────┼──────────┐
                            │          │          │
                        for_run   for_session  dashboard_overview
                            │          │          │
                            └────► typed snapshots ◄────┘
                                      │
                                ActivityPanel
                             ┌────────┴────────┐
                         inline host      dashboard host
```

`ExecutionStore` is the sole execution lifecycle owner. `ActivityPanelRun` contains only run identity, supersession and execution membership; it does not mirror job lifecycle or retain copies of job state. A current-turn job is derived from the `durable_job_id` recorded on an execution belonging to that run. `JobManager` remains authoritative for the job itself.

`ActivityView` is a deliberately boring query/presentation component. It joins canonical records, formats renderer-facing values and returns typed immutable snapshots. It owns no persistence, lifecycle, browser revision protocol, polling state or presentation cache.

Inline activity and retained dashboard sessions have deliberately different source semantics:

- `for_run(session_id, run_id)` renders one ChatGPT `ActivityPanelRun`, including supersession and current-turn/background-job rules;
- `for_session(panel_id, expanded_entry_id=None)` renders one retained `SessionRecord` as the complete selected-session document, including bounded detail for the one expanded entry when requested;
- `dashboard_overview()` returns the complete compact summary list for all retained sessions without materialising historical call payloads.

The run and retained-session queries may return the same small renderer-facing snapshot shape, but the backend must not pretend they are the same domain object merely because they share a renderer.

The dashboard must share the **renderer and presentation contract**, not emulate the ChatGPT MCP widget runtime.

The principal invariant is:

> **Persist facts once. Derive presentation on demand. Keep browser state limited to interaction state.** Backend services own canonical state and meaning; `ActivityView` projects it without becoming another state owner; transport hosts own only transport and refresh scheduling.

A second invariant protects dashboard scale:

> **The dashboard has one current server document and one periodic request.** Overview mode returns all compact session summaries in one request; selected-session mode replaces that poll with one complete session document. Neither mode may issue per-session polling, decode unrelated historical results, collect terminal-job telemetry globally or run Git probes once per panel.

## 2. Why a rebuild is preferable to further refactoring

The existing UI is now substantially more complex than the behaviour it provides.

Current source-size baseline:

| File | Current lines | Role |
| --- | ---: | --- |
| `src/serena/activity.py` | 2022 | activity backend plus a large self-contained inline HTML/CSS/JS widget |
| `src/serena/dashboard_widgets.py` | 253 | dashboard-to-MCP widget compatibility adapters |
| `src/serena/custom_dashboard.py` | 819 | dashboard read models/routes plus widget hosting |
| `src/serena/resources/kendell_dashboard/dashboard.js` | 883 | dashboard state, previews, iframe lifecycle, polling and controls |
| `src/serena/resources/kendell_dashboard/styles.css` | 591 | dashboard plus duplicated activity-preview styling |
| `src/serena/resources/kendell_dashboard/index.html` | 156 | dashboard shell |
| `src/orchestrator/activity.py` | 561 | Orchestrator activity widget and backend |

The Serena inline widget alone currently contains roughly 1,100 lines of JavaScript embedded inside a Python string. The dashboard then wraps that widget in another runtime rather than rendering activity directly.

The most important accidental complexities are:

1. **The web dashboard emulates ChatGPT.** `dashboard_widgets.py` creates a fake `window.openai`, maps REST endpoints back into MCP-like calls, tracks dirty/revision state and uses `postMessage` to bridge iframe state.
2. **The dashboard has two renderers.** Before an iframe is mounted it renders its own activity preview; after mounting it displays the full inline widget. Both implementations model the same panel.
3. **There are multiple polling/state machines.** The top-level dashboard polls, each embedded activity widget polls, and the adapter coordinates revisions between them.
4. **Incremental state is reconstructed in the browser.** `summary_only`, `partial`, `changed_since`, bounded summary merging, initial hydration and revision bookkeeping exist mainly to support the iframe/widget reuse strategy.
5. **Backend presentation is duplicated.** `ActivityTracker`, `DashboardActivityArchive` and `DashboardSerenaActivityOverview` independently derive overlapping representations from `ExecutionStore`; call-detail construction is duplicated as well.
6. **Dashboard discovery over-processes history.** Current summary construction first builds full call dictionaries containing persisted arguments/results, parses parameters for semantic summaries, and then discards most of that work to produce bounded first-paint state. The canonical records are already resident; the waste is avoidable parsing/copying/serialisation, not a need for a new persistence layer.
7. **The rich result renderer is too heuristic.** The browser guesses code/diff/language/value types and builds line-number/copy/nesting machinery even though the current backend can supply structured canonical values directly.
8. **The inline widget contains non-essential interaction machinery.** Draggable resizing, keyboard resize control, scroll-aware render deferral, multiple expansion sets, timers and initial-view heuristics add state and failure modes without providing core activity visibility.
9. **Some tests freeze implementation.** Assertions for exact DOM IDs, CSS rules and JavaScript snippets make behaviour-preserving simplification harder and contradict the project's behaviour-focused testing policy.

These are architectural residues rather than isolated defects. Incrementally simplifying them risks retaining the same boundaries in a slightly smaller form. This plan therefore uses a clean presentation-layer cutover.

## 3. Product behaviour to preserve

The rebuild must preserve the useful observable behaviour rather than the current implementation structure.

### 3.1 Serena inline activity

Keep:

- one compact activity panel per `show_activity` run;
- Serena logo and compact current/recent activity summary;
- session title where currently available;
- tool name, useful semantic detail and scope;
- running/completed/failed/cancelled/waiting state;
- submitted time and elapsed/runtime display;
- tool count and relevant job count;
- time between first and latest submitted tool;
- current Git additions/deletions and commits ahead of origin;
- current-turn durable jobs represented as activity entries without duplicating the corresponding `start_job` tool row;
- compact indication of other running Serena jobs;
- collapse/expand;
- a compact scrolling history when expanded, approximately five normal rows high;
- on-demand expansion of one tool/job to inspect useful detail;
- structured arguments and canonical presented result;
- media/file result access;
- central truncation metadata and retained `output_id` visibility when the canonical presented result is truncated;
- live progress while a tool or durable job is active;
- retirement of superseded panels once their relevant activity is terminal.

Do not preserve an implementation detail solely because it exists today.

### 3.2 Serena web dashboard

Keep:

- retained Serena sessions with their ChatGPT conversation/session titles;
- retained Orchestrator sessions/delegations;
- desktop and mobile Serena/Orchestrator views;
- active/inactive state and concise panel summaries;
- fast first paint from one compact overview document containing all retained session summaries, with no per-session hydration;
- one fully opened Serena session at a time, with expandable activity history and one entry detail at a time;
- current Git metrics for Serena sessions;
- top-level `jobs n/N` control with a running-jobs view;
- tools, memories, languages, runtime and version metadata that remain useful operationally;
- PWA installability;
- push notification opt-in through the bell control;
- completed-job push notifications;
- job notification deep links to the originating Serena panel/job;
- dark/light colour-scheme support;
- retained media/result inspection;
- ordinary HTTP cache revalidation only where measurements show it remains cheap and does not require rebuilding/rehashing expensive state.

### 3.3 Orchestrator

Keep the current useful Orchestrator activity semantics, including delegation status/detail and launch-prompt actions where applicable.

Do **not** force Serena and Orchestrator storage models or UI semantics to become identical merely for reuse. Rebuild Orchestrator as its own small direct-DOM renderer first; only extract lower-level presentation primitives/CSS once the duplication is concrete and obviously simpler to share.

## 4. Behaviour to remove deliberately

Remove rather than reimplement:

- draggable activity-panel resizing;
- resize pointer capture, keyboard controls and double-click reset;
- scroll-idle/deferred rendering machinery;
- multiple simultaneously expanded tool/job detail rows;
- multiple simultaneously fully opened Serena dashboard sessions;
- eager loading or polling of every retained historical session;
- client-side code-language guessing and decorative line numbering;
- client-side diff detection solely for styling;
- duplicated dashboard activity previews;
- activity iframes inside the dashboard;
- dashboard creation of a fake `window.openai` API;
- dashboard `postMessage` bootstrap/readiness/height/focus protocols needed only by those iframes;
- `SessionWidgetLoader`, blob-document widget caching and iframe lazy-mount queues;
- client-side activity delta merging;
- `summary_only` / `partial` browser merge semantics;
- `changed_since` activity-delta semantics if they no longer serve another external consumer;
- initial-view heuristics based on several overlapping local state flags;
- per-panel dashboard polling loops;
- dashboard routes whose only purpose is serving wrapped activity-widget documents;
- tests that assert exact private DOM/CSS/JavaScript implementation strings.

A replacement should only be added if an observable retained behaviour actually requires it.

## 5. Target backend design

### 5.1 `ExecutionStore` remains the only execution lifecycle owner

The newer backend already records authoritative `ExecutionRecord` state directly from `SerenaFastMCPTool.run()`. Finish that transition completely: activity code must no longer start, finish or mirror executions.

Target rules:

- `SerenaFastMCPTool` creates the execution directly in `ExecutionStore` before dispatch;
- the current `ActivityPanelRun`, if one exists, is associated with that execution ID in the same backend update;
- tool completion updates the `ExecutionRecord` directly in `ExecutionStore`;
- result/media/durable-job metadata extraction used to persist the canonical execution belongs beside execution/result presentation, not under the activity UI layer;
- activity/run code may create or supersede a run and associate execution IDs, but it never independently starts, completes or repairs execution lifecycle;
- no presentation helper may be required for execution correctness.

`ActivityPanelRun` should be reduced to facts that cannot be derived from its member executions:

```text
run_id
session_id
project_name
started_at
superseded
execution_ids
```

Remove `job_ids`, `retained_jobs` and the synchronization machinery around them. A durable job belonging to a run is derived by following `durable_job_id` from that run's member executions into `JobManager`. This deletes a duplicated persisted relationship and the failure mode where an execution and a panel run disagree about job ownership.

If a small run helper survives, name it for what it actually owns, e.g. `ActivityRunManager`. It may create/supersede runs and associate executions. It must not be an execution tracker.

Arguments should also exploit the newer structured backend. `ExecutionRecord.arguments` should hold a bounded JSON-safe structured value rather than a pre-serialized JSON string. `StructuredOutputCompactor` may still bound the value before persistence, but JSON serialization should happen only when the execution-store state file itself is written. Activity summaries/details should therefore consume the structured mapping directly instead of repeatedly serializing and reparsing it.

Keep `ExecutionRecord.result` aligned with the canonical central result-presentation serialization; that representation has a separate transport/persistence contract and should not be casually changed by this UI rebuild.

### 5.2 One Serena query/presentation owner: `ActivityView`

Introduce one query/view component over the existing canonical services, provisionally `ActivityView`.

It owns all derivation of renderer-facing Serena activity data from:

- `ExecutionStore` session, run and execution records;
- `JobManager` durable-job records;
- `GitMetricsSource`;
- `ActivityDetailFormatter` for concise semantic detail/scope;
- canonical persisted result/media metadata.

It does **not** own execution lifecycle, run persistence, retention, result presentation or job lifecycle.

Use small typed dataclasses rather than passing loosely related dictionaries through internal layers. The exact names are secondary, but the contract should distinguish source semantics explicitly, for example:

```text
ActivitySnapshot
ActivityEntrySummary
ActivityCallDetail
ActivityJobDetail
```

Expose explicit queries rather than a generic ambiguous panel lookup:

```text
dashboard_overview()
for_run(session_id, run_id)
for_session(panel_id, expanded_entry_id=None)
call_detail(...)      # reusable helper / inline MCP use
job_detail(...)       # reusable helper / inline MCP use
```

`for_run(...)` applies ChatGPT run semantics such as supersession, current-turn jobs and unrelated running-job visibility. `for_session(...)` applies retained-session semantics and folds the one requested expanded entry detail into the complete selected-session snapshot. `dashboard_overview()` derives compact summaries directly from the lightweight execution/session index and must never be implemented by calling `for_session(...)` repeatedly.

`ActivityView` must not add a synthetic browser-facing revision system, duplicate job cache or retained-session cache. If a backend query index is needed for scale, it belongs inside the canonical store that owns the indexed facts and is rebuildable from those facts.

JSON conversion belongs only at the MCP/HTTP boundaries.

### 5.3 Dashboard data model: one overview document, one selected-session document

Use the same simple shape as the `artiq-tool` viewer: one compact top-level document describes the available work, and opening one item switches the UI to one complete detail document for that item. Do not model the dashboard as hundreds of independently hydrated/polled panels.

The dashboard has exactly two Serena data modes:

```text
overview mode
    GET /dashboard/api/state
    -> compact summary for every retained Serena/Orchestrator session
    -> dashboard metadata + running-job counts

session mode
    GET /dashboard/api/serena/sessions/<panel_id>?expanded=<entry_id?>
    -> complete renderer-facing snapshot for that one session
    -> all lightweight activity rows
    -> full detail only for the currently expanded entry, when any
```

Only the currently displayed mode is polled. While the overview is visible, poll `/state` and make no session requests. While one Serena session is open, stop polling `/state` and poll only that session document. Returning to the overview immediately refetches `/state`. Therefore the normal dashboard has exactly **one periodic HTTP request total**, regardless of whether 10, 100 or 1,000 sessions are retained.

The overview document returns **all retained session summaries in one response**, as `artiq-tool` does for its top-level status data. There is no entry limit, cursor pagination, lazy panel hydration or per-session fetch path for the overview. Retention determines which sessions exist; the dashboard shows all of them. A summary must therefore remain deliberately tiny:

- panel/session identity and display title;
- project name;
- created/last-activity timestamps and active state;
- lightweight tool/job counts where useful;
- latest tool/job name/status where cheap;
- cached Git metrics for the represented project;
- no argument bodies, results, media bodies, job journals or terminal-job telemetry.

Hundreds of such records are expected to be cheap to serialize, transfer and rebuild as ordinary DOM. Keep a 10/100/1,000-session benchmark as the guardrail. If the 1,000-session case exposes a problem, first reduce summary size, eliminate hidden backend work or improve in-place rendering; do not solve it by silently limiting or paging retained sessions out of the overview.

The backend must still avoid hidden history-proportional work when constructing the overview. `ExecutionStore` should expose one lightweight summary query backed by a small rebuildable in-memory index over canonical records. The index may hold session recency/order, execution count, running-execution count and latest execution ID/cheap summary facts. It must not copy arguments/results or become a second persisted semantic model. Rebuild it once from canonical records at startup and update it under the same store lock as execution lifecycle changes.

Ordinary overview construction therefore scales approximately as:

```text
O(number of retained session summaries + currently running jobs)
```

with a very small constant, rather than scanning/parsing all retained executions/results. This is acceptable for hundreds of sessions and intentionally simpler than maintaining a browser paging protocol.

Git state must not make overview polling expensive. `/state` reads the already-maintained `GitMetricsSource.get_project_git_metrics(...)` cache and performs **no Git subprocesses**. The same cached project value is reused for every matching summary.

Running-job metadata should likewise come from a cheap running-job index/query. `/state` must not reconcile every retained terminal job or collect terminal-job telemetry simply to show the overview.

### 5.4 Session documents are complete renderer snapshots, not incremental patches

Opening a Serena session fetches one complete renderer-facing session snapshot. It contains all lightweight activity rows required to rebuild that session UI in one pass. The browser does not merge deltas, hydrate neighbouring sessions or reconstruct state from several endpoints.

Keep potentially bulky detail scoped to the one expanded entry. Pass its identifier as an optional query parameter on the same session request and return that entry's structured arguments, canonical presented result/error, media metadata or bounded live job output alongside the session snapshot. This preserves the **one periodic request** invariant even when a call/job detail is open. Media/file bytes and retained full-output resources remain separate resource requests only when the user explicitly opens them.

A session response should therefore resemble a small manifest: complete panel state plus references/bounded detail, not every historical file/output body embedded inline.

Job-to-session ownership should use canonical `JobRecord.session_id` where available. Notification/deep-link resolution maps `job_id -> session_id -> panel_id` directly rather than scanning retained session summaries.

The browser must not parse Python parameter representations, reinterpret canonical result serialization, reconstruct job identity or independently determine which `start_job` execution corresponds to a durable job.

### 5.5 Delete the duplicate dashboard interpretation layers

The new query/view component should **replace**, not sit underneath, the presentation responsibilities currently split across `DashboardActivityArchive` and `DashboardSerenaActivityOverview`.

Do not retain an architecture such as:

```text
DashboardActivityArchive -> DashboardSerenaActivityOverview -> ActivityView
```

when all three interpret the same canonical execution state.

After cutover, preserve only genuinely dashboard-specific concerns in `CustomDashboard`, such as:

- HTTP routing and ordinary response/cache handling where it remains measurably useful;
- HTTP media adaptation;
- dashboard-wide metadata composition;
- notification routing.

If a tiny dashboard facade remains useful, it should be a transport facade over `ActivityView`, not another semantic model.

### 5.6 Keep job discovery cheap and single-pass

The current activity and dashboard paths maintain separate short-lived job caches and can ask `JobManager` for more information than collapsed activity actually needs. Remove those presentation-layer caches.

For one `ActivityView` query/refresh operation, obtain the minimum required job metadata once and reuse it while constructing the current document:

- `/state`: running-job identity/status/session ownership only, plus the global running/max count;
- selected-session document: lightweight metadata for jobs referenced by that session's executions;
- selected-session document with `expanded=<job_id>`: include that one job's bounded runtime telemetry/journal in the same response.

If current `JobManager` APIs force `/state` to reconcile or inspect every retained terminal job, add a narrow running-job metadata query there rather than compensating with another activity cache. Prefer a small in-memory running-job index owned by `JobManager`/its canonical store, rebuilt once from persisted job metadata at startup and updated as jobs start/reconcile/finish. Ordinary `/state` should reconcile only currently running jobs and return their lightweight identity/status/session ownership; it must not walk terminal-job history every scheduler tick.

The dashboard jobs dialog may fetch a bounded recent terminal-job page when the user opens it; runtime telemetry/output remains detail-on-demand. Any cache/index retained for measured backend cost must have one canonical owner beneath both UI transports and must not become a second job-state model.

### 5.7 Keep Orchestrator semantically independent

Keep Orchestrator's storage/read semantics independent. Its delegates have meaningful behaviour that Serena entries do not, including waiting/launch-prompt actions.

Build the Serena renderer contract first and keep Orchestrator independently small by default. Do not design `ActivityPanel` around hypothetical Orchestrator reuse. Once both direct-DOM paths are clear, extract only obviously identical entry/layout primitives or CSS. Reuse the complete Serena panel only if the adaptor is trivially thin and introduces no optional-action matrix or cross-domain conditionals.

Do not introduce a cross-system persistence abstraction.

## 6. Target frontend design

### 6.1 One small Serena renderer, shared where it remains simple

Move Serena activity rendering out of Python string literals into normal resource files, provisionally:

```text
src/serena/resources/activity/
    activity-panel.js
    activity-panel.css
```

The renderer should be small vanilla JavaScript with no frontend framework or build pipeline.

Prefer one encapsulated `ActivityPanel` class/component that owns:

- one panel root;
- collapsed state;
- currently expanded entry ID or `null`;
- rendering a supplied immutable snapshot;
- requesting detail through injected callbacks;
- updating visible elapsed-time text when its host explicitly supplies `tick(now)`.

`ActivityPanel` itself must be timerless. The inline host may own one clock because it renders one panel; the dashboard host owns one shared clock for all visible panels. This prevents one timer/state machine per retained session.

It must not own persistence, polling, revision merging, network transport or dashboard navigation.

The same complete component should serve ChatGPT inline and the Serena dashboard. Keep Orchestrator separate by default; extract only lower-level row/detail/layout primitives or CSS once that reuse is clearly simpler than two small renderers.

### 6.2 Thin ChatGPT inline adapter

The MCP resource should assemble a self-contained document from the shared JS/CSS at runtime so ChatGPT still receives a single `text/html;profile=mcp-app` resource.

The inline-only adapter owns:

- reading initial `window.openai.toolOutput`;
- calling private MCP activity/detail/media/job-detail tools;
- one straightforward poll loop;
- receiving `openai:set_globals` updates if required by the MCP app contract;
- calling the intrinsic-height API after actual height changes;
- retirement when the panel is superseded and terminal.

Do not make the shared renderer understand `window.openai`.

A simple polling policy is preferable to adaptive state machinery. For example, poll quickly while a tool is running, less frequently while only a durable job is running, and slowly when idle. The exact intervals can remain similar to today's behaviour, but there should be one timer and no client-side delta state.

### 6.3 Dashboard host: one route, one document, full in-place rebuild

The dashboard should behave as a small route-driven viewer, closely following the useful `artiq-tool` pattern. It renders ordinary DOM directly; there are no activity iframes and no independently live panel objects.

The host owns only:

- the current route: overview, one Serena session or one Orchestrator session;
- one current immutable server document;
- one optional expanded-entry ID within the selected session;
- one polling scheduler;
- one local elapsed-time clock;
- notification/deep-link routing, PWA controls and top-level dialogs.

For each poll, request exactly the document corresponding to the current route. If the response is unchanged, do nothing. If it changed, replace/rebuild the relevant GUI in place from the new complete snapshot while preserving only intentional local interaction state such as the selected/expanded entry and sensible scroll position.

Do not incrementally merge activity rows, maintain per-panel dirty/revision state, hydrate individual collapsed sessions, or reconcile several independently fetched panel models. Rebuilding the overview list or the one selected panel from canonical data is the default; introduce finer DOM reconciliation only if profiling shows the simple rebuild is materially inadequate.

The scheduler must coalesce triggers and never overlap requests. Do not retain the current 500 ms dashboard-wide poll merely to animate elapsed time; the local clock handles that. Start around **2 s while the current document contains active work, 10 s when idle and visible, and 60 s while hidden**, with immediate refresh on focus/navigation/user actions. These are simple constants, not separate polling state machines.

The request budget is therefore:

```text
overview visible
    1 × /state per poll

one Serena session visible
    1 × /serena/sessions/<id>?expanded=<entry?> per poll

one Orchestrator session visible
    1 × /orchestrator/sessions/<id>?expanded=<delegate?> per poll

media / retained output / explicit actions
    additional requests only when the user asks for them
```

There is never `1 + N` polling for N sessions. A dashboard with hundreds of retained sessions still has one periodic request and one periodic render decision.

For efficient unchanged polls, prefer a cheap server-side generation/ETag tied to the current document rather than hashing/materialising a large response on every request. The overview generation changes only when summary-visible facts change; each selected-session generation changes only when that session's renderer-visible facts change. These generations are transport/cache metadata over canonical state, not a browser-side semantic revision protocol. If a running expanded job's bounded output changes, that selected-session document naturally changes as well.

### 6.4 Generic detail rendering

Replace the current heuristic rich-value renderer with a small deterministic renderer.

Rules:

- `null`, booleans and numbers: ordinary scalar text;
- short strings: ordinary text/inline code only where the backend explicitly identifies semantic identity if needed;
- multiline strings: `<pre>` with wrapping and a copy action if useful;
- arrays/objects: recursive key/value structure with native `<details>` for nested containers;
- media: image/audio/file controls supplied by the appropriate transport;
- no language guessing;
- no line-number generation;
- no special diff parser;
- no alternate content truncation.

If the canonical result is a structured truncation envelope, render it as ordinary structured data plus a concise retained-output affordance. Do not invent a dashboard-specific preview.

### 6.5 CSS/layout simplification

Use CSS to solve layout rather than JavaScript state.

Target expanded panel behaviour:

- fixed natural header height;
- activity list `max-height` roughly equivalent to five standard rows;
- `overflow-y: auto`;
- details expand within the list/body naturally;
- long code/text wraps or horizontally scrolls only where preserving exact text layout is useful;
- no user-resize handle;
- mobile layout from media queries only.

The dashboard should use the same activity-panel CSS rather than duplicating preview styles.

## 7. Target HTTP/MCP contracts

### 7.1 Dashboard API

Keep the dashboard surface deliberately small and route-oriented:

```text
GET /dashboard/api/state
GET /dashboard/api/serena/sessions/<panel_id>?expanded=<entry_id?>
GET /dashboard/api/serena/sessions/<panel_id>/media/<entry_id>
GET /dashboard/api/orchestrator/sessions/<panel_id>?expanded=<delegate_id?>
GET /dashboard/api/memory?name=...
GET /dashboard/api/push/config
POST /dashboard/api/push/subscribe
GET /dashboard/job/<job_id>
```

`/state` returns the complete compact overview document for the dashboard: dashboard metadata, running/max job counts, all retained Serena session summaries and all retained Orchestrator session summaries. It is the only periodic request while the overview route is visible.

A selected Serena/Orchestrator route polls only its corresponding session document. The selected-session response contains the complete lightweight renderer snapshot and, when `expanded` is supplied, the bounded detail needed for that one expanded row/delegate. Do not expose separate polling endpoints for panel summaries, call details or job details unless a later measured requirement proves the single-document contract inadequate.

Use conditional requests if they are cheap: the server should be able to decide that a document is unchanged from a lightweight generation/last-change marker before serialising the complete body. Avoid body hashing, browser delta protocols, partial-response merging or per-panel revision bookkeeping.

Media/file/full retained-output bytes remain separate resource requests because they are potentially large and user-triggered.

Delete once no code relies on them:

```text
/dashboard/widget/serena*
/dashboard/widget/orchestrator*
/dashboard/api/serena/panels*
/dashboard/api/serena/jobs/*      # if used only by the old panel polling path
/dashboard/api/orchestrator/panels*
```

The goal is one overview document and one detail document per selected route, not a REST graph that forces the browser to assemble the dashboard itself.

### 7.2 Inline MCP tools

Keep the private MCP surface conceptually small:

```text
get_activity(run_id)
get_activity_detail(run_id, call_id)
get_activity_media(run_id, call_id)
get_activity_job_detail(run_id, job_id)
```

They should delegate to the same Serena `ActivityView` used by the dashboard wherever session-ownership/security semantics allow.

`get_activity` should return a complete renderable lightweight snapshot for that run. Do not require the inline browser to merge partial snapshots.

## 8. Notification/PWA boundary

The recent notification work is useful and should survive the rebuild, but it should remain independent of activity rendering.

Preserve:

- `push_notifications.py` ownership of VAPID/subscriptions/send behaviour;
- `service-worker.js` ownership of receiving notifications and opening/focusing the dashboard;
- `manifest.webmanifest`;
- bell opt-in state in the dashboard shell;
- `/dashboard/job/<job_id>` resolving job -> originating panel;
- `?panel=<panel_id>&job=<job_id>` deep-link semantics.

After the iframe layer is gone, deep-link handling becomes simpler:

1. parse the target once on dashboard load/focus;
2. select the Serena view;
3. locate/render the target panel directly;
4. expand the target job directly in that panel;
5. consume the target and do not repeatedly renavigate/reapply it on later refreshes.

No `postMessage` focus handoff should remain.

## 9. File-level migration map

Expected direction; names may move slightly if implementation reveals a simpler boundary, but the ownership/deletion requirements are deliberate.

### `src/serena/execution_store.py` / `src/serena/mcp.py`

Complete the canonical-backend cutover before rebuilding presentation:

- `SerenaFastMCPTool` starts and finishes executions directly through `ExecutionStore`;
- run association is a small grouping operation, never an execution-lifecycle callback;
- reduce `ActivityPanelRun` to run facts plus `execution_ids`; remove persisted `job_ids` / `retained_jobs` and their synchronization methods;
- store bounded structured JSON-safe execution arguments rather than a JSON string that must be reparsed by every presentation path;
- add only the lightweight, rebuildable query index needed to build all compact session summaries without scanning/parsing total retained execution history on every poll;
- ensure those indexes live inside `ExecutionStore`, are non-authoritative, and are rebuilt from canonical persisted records on startup.

### `src/serena/activity.py`

Reduce this module to Serena activity-run, semantic summary formatting and thin MCP resource concerns.

Retain/move:

- semantic activity detail formatting where it remains Serena-specific;
- MCP resource registration;
- thin inline adapter/template assembly;
- run creation/supersession/association needed by `show_activity`;
- direct delegation to `ActivityView` for run snapshots/details.

Remove:

- the monolithic embedded 1,100-line JS renderer;
- resize/deferred-render/rich-format heuristics;
- all execution start/finish ownership;
- duplicated persisted run-job state and retained-job snapshots;
- duplicate job caches and presentation derivation once `ActivityView` owns those queries.

Prefer deleting `ActivityTracker` as a concept. If a helper remains, it should be an obviously small `ActivityRunManager` whose public surface is limited to run creation/supersession/association/lookup.

### New Serena activity view module

Create one small module for `ActivityView` and its typed renderer-facing dataclasses. This becomes the only Serena owner of:

- complete compact `dashboard_overview()` projection;
- `for_run(...)` projection;
- `for_session(..., expanded_entry_id=...)` projection;
- reusable call/job detail formatting used by the selected-session document and inline MCP tools.

It must query canonical services directly and must not own persistent state, polling state, browser revision state or its own long-lived semantic caches. Overview construction must use the lightweight store index rather than repeatedly expanding retained sessions.

### `src/serena/dashboard_activity.py`

Delete the presentation/archive layer once `ActivityView` directly supplies retained-session summaries and snapshots. Do not retain `DashboardActivityArchive` merely as a delegate wrapper around the new view.

If any non-presentation utility remains genuinely necessary, move that utility to the canonical owner rather than preserving the class/module as an architectural layer.

### `src/serena/custom_dashboard.py`

Reduce to:

- dashboard-wide metadata composition;
- thin `ActivityView` HTTP adaptation;
- the small overview + selected-session REST surface;
- cheap generation/ETag revalidation that can reject unchanged polls before serialising the complete document;
- media HTTP adaptation;
- notification routes;
- static shell serving.

Remove `DashboardSerenaActivityOverview` once its semantic projection responsibilities have moved into `ActivityView`. Do not replace it with another dashboard-specific activity model.

### `src/serena/dashboard_widgets.py`

Delete completely once direct dashboard rendering is live.

### `src/serena/resources/activity/`

Add the small Serena `ActivityPanel` renderer and shared activity CSS as ordinary resource files. Keep transport/polling/PWA/navigation concerns out of this component.

### `src/serena/resources/kendell_dashboard/dashboard.js`

Rewrite rather than incrementally simplify.

Keep only dashboard concerns: one refresh loop, one shared elapsed-time clock, bounded panel-page reconciliation, at most one opened Serena panel, direct renderer instances, dialogs, PWA/push, tabs and deep links.

Delete `SessionWidgetLoader`, iframe/blob loading, preview renderer, bootstrap messaging, widget focus messaging and client-side activity delta merging.

### `src/serena/resources/kendell_dashboard/styles.css`

Rewrite/trim around the dashboard shell. Move reusable activity-panel styling into the activity CSS and remove preview duplication.

### `src/serena/resources/kendell_dashboard/index.html`

Retain a small semantic shell. Remove markup/hooks that exist only for iframe widgets/previews.

### `src/orchestrator/activity.py`

Separate Orchestrator semantics from its embedded renderer where useful. Build a small direct-DOM Orchestrator path independently first; only extract shared entry/layout primitives or CSS once identical behaviour is obvious. Reuse the complete Serena `ActivityPanel` only if doing so is simpler than keeping the two small renderers separate.

The goal is less maintained UI code, not abstraction purity.

### Tests

Refactor `test/serena/test_activity.py`, `test/serena/test_dashboard_activity.py` and `test/serena/test_custom_dashboard.py` around observable contracts. Delete assertions whose only purpose is to freeze exact HTML IDs, CSS declarations or JS source text.

Add a deliberately small browser smoke layer for the browser-state regressions that backend tests cannot detect: live-created row/layout equivalence, live duration updates, reload equivalence, single-row expansion and one-shot notification/deep-link consumption.

## 10. Implementation programme

Implementation should proceed in four bounded checkpoints. The sequence is deliberately short: a simplification project should not build transitional architecture merely to keep the legacy iframe frontend alive between adjacent steps.

### U00 — Preserve useful work and establish measurable guardrails

Before structural work:

1. inventory the current mixed working tree;
2. preserve/checkpoint the useful notification/PWA/service-worker/deep-link work separately where practical;
3. preserve unrelated modifications without folding them accidentally into this programme;
4. record source LOC and current dashboard request behaviour;
5. capture `/dashboard/api/state` latency/payload behaviour with representative small and large retained histories;
6. create a repeatable synthetic scale fixture or benchmark with at least roughly 10, 100 and 1,000 retained sessions, including historical calls with non-trivial arguments/results;
7. record how many JobManager telemetry operations and Git metric refreshes one `/state` construction performs;
8. make Sections 3 and 4 the retained/removal acceptance contract.

Do not perfect an iframe/postMessage-specific bug that U02 deletes. Preserve the intended behaviour and prove it on the final direct-DOM path.

The benchmark is primarily a regression guard, not a micro-optimisation contest. The final design must demonstrate that one compact overview containing all retained session summaries remains fast enough to build, transfer and render, while browser request count stays exactly one per poll. The overview must not impose an entry cap or paging boundary; if the 1,000-session case is too slow, optimise the summary/query/render path rather than hiding retained sessions.

Checkpoint: an attributable baseline exists and dashboard scaling can be measured rather than inferred.

### U01 — Finish canonical backend ownership and build the bounded read model

Complete the backend simplification as one coherent change:

- `SerenaFastMCPTool` starts/finishes `ExecutionRecord` directly;
- replace `ActivityTracker` execution callbacks with minimal run association;
- reduce `ActivityPanelRun` to run facts plus `execution_ids` and remove persisted job mirrors;
- store bounded structured execution arguments rather than reparsed JSON strings;
- make the execution-store format cut over cleanly: on load, migrate retained legacy string arguments to the bounded structured representation once and drop legacy run `job_ids`/`retained_jobs`; do not keep dual read/write formats after the migration;
- move canonical result/media/job metadata extraction out of the UI layer where necessary;
- add the minimal rebuildable internal `ExecutionStore` query index needed to construct all lightweight retained-session summaries without scanning/parsing retained execution history per poll;
- create the typed, stateless `ActivityView`;
- implement one complete compact overview projection plus `for_run(...)` and one complete selected-session snapshot projection with optional expanded-entry detail;
- order the overview active-first then by recency and keep each summary deliberately small enough that hundreds can be returned in one document;
- provide a narrow JobManager metadata path if `/state` would otherwise reconcile/collect telemetry for retained terminal jobs;
- make `/state` consume cached Git metrics only, with no Git subprocesses during ordinary dashboard refresh;
- resolve `job_id -> session_id -> panel_id` directly from canonical ownership.

Scale tests at this checkpoint must demonstrate structurally that discovery does not call a full-session expansion once per panel, does not decode historical result bodies, does not collect terminal-job telemetry globally, and executes no Git subprocesses during ordinary `/state` construction.

Do not retrofit the old iframe frontend to this API merely for temporary compatibility.

Checkpoint (complete 2026-09-11): the backend supplies bounded activity discovery and on-demand snapshots/details without history-proportional polling cost. Evidence: `docs/03-special-guides/serena_activity_ui_simplification_u01_checkpoint.md`.

### U02 — Build the small renderer and cut over both Serena surfaces

Create ordinary JS/CSS activity resources and implement a timerless `ActivityPanel` against immutable snapshots.

Implement only retained Serena behaviour:

- compact header/summary metadata;
- collapse/expand;
- approximately five-row scrolling activity history;
- status/detail/scope/time display;
- one expanded entry at a time;
- deterministic structured/text/media detail rendering;
- Git/count/submission-span metadata;
- host-driven running-duration updates.

Cut over ChatGPT inline to:

- runtime-inlined shared renderer assets;
- a thin MCP transport host;
- one simple snapshot poll loop;
- on-demand details/media;
- superseded-terminal retirement.

In the same checkpoint, rewrite the Serena dashboard around the two-route document model:

- overview route polls exactly one `/state` document containing all compact retained-session summaries;
- opening one Serena session switches the poller to exactly one selected-session document;
- returning to overview refetches `/state` and stops the session poll;
- at most one Serena session is open and at most one entry is expanded;
- the expanded entry ID is folded into the selected-session request so live call/job detail does not create a second poller;
- one shared dashboard clock updates elapsed-time text locally;
- when the current server document changes, rebuild that route's GUI in place from the complete snapshot;
- no per-panel timers, observers, hydration requests, delta merging or session paging state.

Then delete the compatibility implementation rather than retaining a fallback:

- `src/serena/dashboard_widgets.py`;
- `/dashboard/widget/*` routes;
- fake dashboard `window.openai`;
- activity iframes/blob documents;
- `SessionWidgetLoader`;
- duplicate preview renderer/CSS;
- `postMessage` bootstrap/readiness/height/focus protocols;
- browser-side `summary_only` / `partial` / `changed_since` merging;
- synthetic browser panel revision machinery that no longer has a real consumer;
- `DashboardActivityArchive` and `DashboardSerenaActivityOverview` after `ActivityView` owns their useful projection responsibilities.

Checkpoint: inline and dashboard both use the small canonical renderer/read model and the old activity compatibility architecture is physically gone.

### U03 — Reconnect peripheral behaviour, audit scale and hard-cut over

Reconnect PWA/notification behaviour to direct dashboard state:

- bell opt-in/subscription state;
- service-worker receipt;
- completed-job notification;
- direct canonical job-to-panel resolution;
- one-shot `?panel=<panel_id>&job=<job_id>` consumption;
- mobile Serena tab selection;
- direct panel/job expansion with no repeated navigation.

Rebuild Orchestrator as a small direct-DOM path without designing Serena around it. Extract only shared primitives that are obviously simpler than duplication.

Backend/protocol/browser coverage should then verify:

- run versus retained-session semantics;
- lifecycle/status ordering and semantic detail/scope;
- current-turn versus unrelated running jobs;
- canonical call detail/result/media behaviour;
- retained sessions survive restart;
- one compact all-session overview document with no per-session hydration;
- one opened session / one expanded entry behaviour with one periodic request total;
- one-shot notification/deep-link behaviour;
- live-created versus reloaded layout/timing equivalence;
- Orchestrator session/detail/action behaviour;
- MCP resource/tool visibility contract.

Run the scale fixture again and require:

- a 1,000-session retained history still produces exactly one overview `/state` request;
- `/state` returns all 1,000 compact summaries without any per-session HTTP fetches or full-session hydration;
- periodic browser request count remains exactly one per poll whether 10 or 1,000 sessions are retained;
- at the starting policy, ordinary polling is no faster than roughly one request per 2 s while active, one per 10 s while idle and one per 60 s while hidden, with refresh triggers coalesced rather than queued into bursts;
- `/state` latency, serialized size and DOM rebuild time remain acceptable at 1,000 summaries; if not, optimise the compact summary/query/render path rather than capping or paging the overview;
- historical result size/content does not materially affect `/state` payload construction;
- ordinary `/state` performs no Git subprocesses and reuses cached metrics per project;
- global discovery does not collect terminal-job runtime telemetry;
- opening a Serena session switches from overview polling to one session-document poll rather than adding another periodic request;
- expanding one entry keeps the same one session-document poll by including `expanded=<entry_id>`;
- returning to overview stops the session poll and performs one fresh `/state` request.

Finally:

1. audit for superseded compatibility concepts;
2. compare final LOC/payload/request-rate/scale measurements with U00;
3. run the required format/type/test suite for any source/test changes plus the browser smoke checks;
4. inspect the complete Git diff;
5. manually verify the acceptance matrix;
6. checkpoint the coherent rebuild;
7. restart deployed Serena only after the cutover is internally consistent.

Completion condition: canonical facts are stored once, the dashboard has one current server document and one periodic request, overview work is limited to compact session summaries rather than historical bodies, and no retained behaviour depends on the old compatibility architecture.

## 11. Acceptance matrix

Before declaring the programme complete, manually verify at least:

| Surface | Scenario | Expected behaviour |
| --- | --- | --- |
| ChatGPT inline | first Serena tool | panel appears with correct current activity |
| ChatGPT inline | several fast tools | rows/status/times update without duplication |
| ChatGPT inline | live-created row then reload | row width/layout and displayed timing agree before and after reload |
| ChatGPT inline | long-running foreground tool | live status and elapsed time update and remain correct after reload |
| ChatGPT inline | `start_job` | job replaces duplicate start-job row and remains inspectable |
| ChatGPT inline | unrelated job running | compact other-job indicator is shown |
| ChatGPT inline | tool failure | concise canonical error is inspectable |
| ChatGPT inline | large centrally truncated result | presented result matches canonical output and exposes `output_id` |
| ChatGPT inline | image/file result | media/file remains accessible |
| ChatGPT inline | Git mutation | fresh `+/-` and ahead count appear |
| ChatGPT inline | superseded terminal panel | panel retires correctly |
| Dashboard desktop | retained sessions | one compact overview document renders all retained session summaries and one session opens directly |
| Dashboard mobile | Serena/Orchestrator tabs | correct responsive tab behaviour |
| Dashboard scale | 1,000 retained sessions | initial render uses one `/state` request, with no per-session hydration and acceptable payload/render cost |
| Dashboard scale | periodic refresh with 10 vs 1,000 sessions | browser request count remains exactly one per poll |
| Dashboard scale | active/idle/hidden scheduler | roughly 2 s / 10 s / 60 s cadence, no overlapping requests or catch-up bursts |
| Dashboard scale | sessions with Git metadata | ordinary `/state` reads cached metrics and runs no Git subprocesses |
| Dashboard scale | many terminal jobs | overview does not fetch terminal-job runtime telemetry/output |
| Dashboard | open session | overview polling stops and exactly one selected-session document becomes the polling source |
| Dashboard | historical large session | overview is independent of its result bodies; selected-session document contains its complete lightweight row history |
| Dashboard | switch opened session | previous session document is discarded and only the newly selected session is polled |
| Dashboard | return to overview | session polling stops and one fresh `/state` request rebuilds the overview |
| Dashboard | running tool/job | the one current-route document keeps visible state current |
| Dashboard | expanded running job | `expanded=<entry_id>` keeps bounded live detail in the same selected-session polling request |
| Dashboard | details | one row expands; arguments/result/job output render correctly without hydrating any other session |
| Dashboard | notification opt-in | bell registers subscription and reflects enabled state |
| PWA notification | completed Serena job | notification arrives with useful title/body |
| PWA deep link | tap notification | dashboard opens correct panel and job once |
| Dashboard | subsequent refresh after deep link | target is not repeatedly reapplied/navigation is stable |
| Orchestrator | active delegation | status/detail/action behaviour remains available |
| Dark mode | both surfaces | panel/dashboard remain readable without separate JS state |

## 12. Testing strategy

Follow `mem:task_completion` after source/test changes:

```text
uv run poe format
uv run poe type-check
uv run poe test
```

During implementation, run focused affected tests at each checkpoint, then the normal full standard suite at the final cutover.

Most coverage should remain backend/read-model behavioural tests and small protocol tests. Add scale-focused tests with instrumented fake collaborators so the architectural constraints are deterministic rather than timing-only: assert one overview request regardless of session count, no per-session full-history query, no historical-result decoding during overview construction, no terminal-job telemetry sweep and cached Git lookup reuse by project.

Also keep a deliberately small browser smoke layer because several important regressions are properties of live DOM/state behaviour rather than backend data:

- live-created versus freshly loaded layout equivalence;
- live duration updates versus reload;
- one-opened-session and one-expanded-entry behaviour;
- one-shot notification/deep-link consumption;
- direct route-document rebuild without iframe-specific state;
- fetch-count instrumentation showing that 10 versus 1,000 retained sessions still produces exactly one periodic request;
- opening a session switches the poll target rather than adding a second periodic request;
- expanding a row keeps detail in that same selected-session request.

Keep that browser layer narrow. Do not introduce broad visual snapshots, a general end-to-end suite, or source-string assertions unless a later recurrent failure demonstrates a specific need. Keep the 10/100/1,000-session benchmark as a coarse performance regression check in addition to these deterministic structural tests.

## 13. Complexity and size guardrails

These are design guardrails, not artificial line-count tests.

The finished implementation should satisfy all of the following:

1. one maintained Serena activity renderer for ChatGPT inline + the Serena dashboard;
2. Orchestrator remains independently small unless complete-panel reuse is demonstrably simpler;
3. no dashboard activity iframes or fake `window.openai`;
4. no duplicated activity preview renderer;
5. one Serena `ActivityView` owner for discovery/run/session/call/job presentation queries;
6. no second execution-lifecycle owner outside `ExecutionStore`/the MCP execution path;
7. `ActivityPanelRun` does not persist `job_ids`, retained job snapshots or any other state derivable from member executions/jobs;
8. execution arguments are retained as bounded structured JSON-safe values rather than repeatedly reparsed presentation strings;
9. any discovery performance index is non-authoritative, rebuildable and owned inside `ExecutionStore`;
10. no `DashboardActivityArchive -> DashboardSerenaActivityOverview -> ActivityView` semantic layering;
11. one dashboard periodic refresh scheduler and one dashboard elapsed-time clock; refresh triggers are coalesced and never overlap;
12. exactly one periodic HTTP request exists for the current route: `/state` on overview, one selected-session document on detail;
13. `/state` returns all retained Serena/Orchestrator summaries in one compact document with no entry cap, pagination or per-session hydration requests;
14. opening a session replaces the overview poll target rather than adding another poller, and returning to overview reverses that switch;
15. at most one Serena dashboard session is fully opened at a time and at most one entry detail is expanded in it;
16. an expanded entry is included in the selected-session document so live detail does not create a second periodic request;
17. no per-panel timers, polling loops, observers, iframe runtimes, paging state or hidden hydration requests;
18. ordinary dashboard overview construction performs no Git subprocesses, reuses cached metrics by project and does not globally collect terminal-job runtime telemetry;
19. historical result/argument/media bodies do not participate in overview construction; selected-session responses include only the bounded detail needed for the one expanded entry;
20. CSS, rather than JavaScript, owns ordinary sizing/scrolling/responsive layout;
21. no browser-side semantic parsing of persisted Python parameter representations and no dashboard-specific result truncation/compaction.

A reduction of at least roughly half of the current UI/adapter JavaScript is a reasonable expected consequence of this architecture, but clarity and deleted state machines matter more than compressing source into fewer lines.

## 14. Decisions intentionally deferred until measurements exist

Do not pre-optimise these areas:

- pagination/virtualisation within one opened session's lightweight call history;
- list virtualisation;
- SSE/WebSocket activity streaming;
- a frontend framework;
- a bundler/transpiler;
- a generic shared Serena/Orchestrator persistence abstraction;
- complex per-panel incremental/delta protocols.

Only introduce one if the simplified implementation has a measured problem that the mechanism directly solves.

## 15. Definition of done

This programme is complete when:

- `ExecutionStore` is the sole authoritative execution lifecycle store used by Serena tool execution;
- the surviving activity-run helper only creates/supersedes/groups runs and never starts/finishes executions;
- `ActivityPanelRun` contains no duplicated durable-job state; current-turn jobs are derived from member `ExecutionRecord.durable_job_id` values;
- execution arguments are canonical bounded structured values, with serialization only at persistence/transport boundaries;
- any session-discovery index is internal to `ExecutionStore`, rebuildable and non-authoritative;
- one typed stateless `ActivityView` projects canonical `ExecutionStore`/job/Git state into one complete compact overview document plus explicit run/selected-session snapshots;
- `DashboardActivityArchive` and `DashboardSerenaActivityOverview` no longer form separate semantic presentation layers;
- inline ChatGPT and the Serena dashboard render through the same small timerless Serena `ActivityPanel` implementation;
- Orchestrator remains a small direct-DOM path and shares only abstractions that genuinely reduce code/conditionals;
- the dashboard contains no activity iframe, fake MCP runtime, duplicated preview, delta merge or resize/scroll-reconciliation machinery;
- `dashboard_widgets.py` and widget-serving routes are gone;
- overview mode polls exactly one `/state` document containing all compact retained Serena/Orchestrator summaries with no per-session hydration;
- selected-session mode stops overview polling and polls exactly one complete session document, including the one expanded entry detail when applicable;
- a 1,000-session retained history still uses one overview request per poll, returns every retained summary, and remains within the measured acceptable payload/latency/DOM-rebuild budget without entry caps or paging;
- one dashboard scheduler owns whichever route document is current, and one shared clock owns elapsed-time updates;
- overview construction never serializes/decodes complete historical results merely to list sessions and never fetches terminal-job runtime telemetry globally;
- job deep links resolve through canonical job/session ownership and notification targets are consumed once;
- canonical model/dashboard result semantics remain aligned with the central result-presentation architecture;
- deterministic scale tests and the small browser-state smoke matrix cover the performance/request-count invariants as observable behaviour;
- standard format/type/test and browser-smoke checks pass for the implementation;
- the final diff has been audited for old compatibility concepts and accidental transitional layers;
- final LOC/payload/request/scale measurements demonstrate that the rebuilt UI is materially smaller, uses one periodic request for the current route, and pays only the small linear cost of compact overview summaries rather than history/body-sized or per-session request costs.