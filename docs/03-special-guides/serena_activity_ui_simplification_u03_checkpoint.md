# Serena Activity UI Simplification U03 Checkpoint

Date: 2026-09-11

Plan step: U03 — Reconnect peripheral behaviour, audit scale and hard-cut over.

## Result

U03 is complete. The simplification programme now ends on the canonical U02 route/read-model architecture rather than adding another compatibility layer. U03 verifies the preserved PWA/notification/deep-link behaviour on that direct path, exercises the small direct-DOM Orchestrator path, expands the browser state-machine smoke matrix, and performs the final scale/compatibility audit.

The only production-code adjustment in U03 is a scale-oriented rendering optimization: retained Serena overview cards are now built in a detached `DocumentFragment` and attached to the live dashboard once. This preserves the shared `ActivityPanel` renderer while avoiding incremental attachment of up to thousands of summary cards.

## Notification and deep-link path

The preserved Web Push implementation remains directly connected to canonical Serena state:

- `WebPushNotifier.send_job_finished()` sends only after terminal job state is durable;
- the payload links to `/dashboard/job/<job_id>`;
- `CustomDashboard` resolves `job_id -> panel_id` through `ActivityView.panel_id_for_job()` and redirects to `?panel=<panel_id>&job=<job_id>`;
- the dashboard opens the selected Serena session with that job as the one expanded entry;
- once the selected-session document contains the job, the browser removes the one-shot `job=` target from the URL while retaining `panel=` and does not reapply navigation on later polls;
- the responsive activity view is forced to Serena for a Serena deep link;
- the service worker navigates an already-open dashboard client to the canonical job URL and focuses it, or opens that URL when no dashboard client exists.

Notification opt-in remains a bell-driven browser action. The browser smoke now verifies registration/subscription state and the two explicit push API calls without adding polling or dashboard state.

## Orchestrator path

Orchestrator remains intentionally independent and small. U03 does not introduce a generic Serena/Orchestrator presentation abstraction.

The browser smoke verifies that an Orchestrator overview entry:

- switches the single current-route poller to `/dashboard/api/orchestrator/sessions/<panel_id>`;
- expands one delegate by folding `expanded=<delegate_id>` into that same route document;
- renders the bounded delegate detail;
- preserves the manual ChatGPT launch-prompt action for `WAITING_FOR_CHAT` delegates.

## Browser/request smoke coverage

`scripts/smoke_activity_ui.mjs` remains a dependency-free headless-Chrome harness. U03 extends it to cover:

- exactly one overview request per active polling interval with 10 retained sessions;
- exactly one overview request per active polling interval with 1,000 retained sessions;
- one selected Serena session route and one expanded-entry route, with no secondary detail poller;
- return from a selected Serena session to one fresh `/state` request;
- notification deep-link expansion and one-shot URL consumption across a later poll;
- responsive Serena selection for a Serena notification target;
- notification bell opt-in and subscription API flow;
- service-worker push payload rendering and notification-click navigation/focus;
- direct Orchestrator session/detail rendering and manual launch-prompt action;
- refresh-trigger coalescing while a request is already in flight;
- approximately 2 s active, 10 s idle and 60 s hidden refresh policy;
- a real-time 1,000-session DOM rebuild measurement.

The final browser smoke passes.

## Final scale measurement

Repeatable server-side command:

```bash
uv run python scripts/benchmark_dashboard_state.py --json
```

The fixture remains four completed calls per retained session, non-trivial historical arguments/results, up to 100 retained terminal jobs and one shared `serena` project.

| Sessions | U00 median | U03 median | U03 response | Git cache reads | Git refreshes | Running-job metadata queries | Terminal telemetry ops |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 12.855 ms | 2.957 ms | 5,243 B | 1 | 0 | 1 | 0 |
| 100 | 601.770 ms | 14.927 ms | 48,683 B | 1 | 0 | 1 | 0 |
| 1,000 | 58,743.949 ms | 435.922 ms | 484,383 B | 1 | 0 | 1 | 0 |

The final 1,000-session server document is about 473 KiB and constructs roughly 135x faster than U00 while still returning all 1,000 summaries. Historical result bodies remain outside overview construction. Ordinary overview construction performs no Git refresh/subprocess and no terminal-job telemetry sweep.

The browser harness measures a full 1,000-card in-place DOM rebuild at 321.5 ms in the final validation run. Before the U03 detached-fragment optimization the same harness measured 505.2 ms, so the low-risk change reduced that synchronous rebuild by about 36%. Unchanged route documents continue to use HTTP ETag revalidation and therefore do not rebuild the DOM at every poll.

## Source-size comparison

The U00 activity/dashboard source baseline was 5,285 lines. Counting the replacement shared renderer assets, the final equivalent source set is 3,690 lines:

```text
   68 src/serena/activity.py
  501 src/serena/custom_dashboard.py
  562 src/serena/resources/activity/activity-panel.js
  397 src/serena/resources/activity/activity-panel.css
  188 src/serena/resources/activity/inline-host.js
  785 src/serena/resources/kendell_dashboard/dashboard.js
  472 src/serena/resources/kendell_dashboard/styles.css
  156 src/serena/resources/kendell_dashboard/index.html
  561 src/orchestrator/activity.py
 3690 total
```

That is 1,595 fewer lines, a 30.2% reduction, while the surviving source now includes the shared ordinary renderer and direct route hosts rather than hiding moved UI code.

## Hard-cutover audit

A final source search finds no surviving references to the superseded dashboard compatibility concepts:

- `DashboardActivityArchive`;
- `DashboardSerenaActivityOverview`;
- `SessionWidgetLoader`;
- dashboard widget routes;
- activity iframes;
- dashboard `postMessage` activity protocols;
- `summary_only` / `changed_since` browser merge machinery.

`window.openai` remains only in `src/serena/resources/activity/inline-host.js`, where it is the real ChatGPT MCP transport rather than a dashboard emulation.

The final architecture therefore satisfies the central scaling invariant: the dashboard has one current server document and one periodic request. Overview mode requests only the complete compact `/state`; selected-session mode replaces that with one complete Serena or Orchestrator session document; expanded detail stays inside that same selected-session request.

## Validation

Final validation on the modified source tree:

```bash
node --check src/serena/resources/activity/activity-panel.js
node --check src/serena/resources/activity/inline-host.js
node --check src/serena/resources/kendell_dashboard/dashboard.js
node --check src/serena/resources/kendell_dashboard/service-worker.js
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
- test suite: `815 passed, 265 deselected`;
- expanded headless-browser smoke: pass;
- final browser 1,000-session DOM rebuild: 321.5 ms;
- final server 1,000-session benchmark: 435.922 ms median, 484,383 B, all 1,000 summaries returned, one cached Git read, zero Git refreshes, one running-job metadata query and zero terminal telemetry operations.

U00-U03 are complete. The remaining operational action after the checkpoint commit is to restart the deployed Serena service onto the completed cutover.
