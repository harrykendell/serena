# Serena + Orchestrator MCP Plan

Status: O09 implemented; stopped before optional O10 providers

Baseline checkpoint: `d865d17f` (`Improve retained Serena job activity`)

This document is the implementation plan for extending the Kendell Serena repository with two deliberately independent MCP servers:

- **Serena MCP** is the local coding/jobs MCP. It owns project routing, semantic coding, file/media operations, Git, shell execution, durable jobs, memories, language servers, and its coding/job UI.
- **Orchestrator MCP** is the agent-delegation MCP. It owns delegate lifecycle, ChatGPT worker handoff, Codex provider policy, timeout/fallback, fan-out/fan-in, provider usage, compact results, and its delegate UI.

The two MCPs are peers used by ChatGPT. They should have **no shared runtime state, no cross-MCP locking protocol, no shared job accounting, and no requirement to call one another**. Their only practical relationship is that a ChatGPT worker may receive a task from Orchestrator and then use Serena itself to work on the named project.

## 1. Architectural invariants

The implementation should preserve these rules.

1. **Serena and Orchestrator are independent MCPs.** Neither proxies the other's model-visible tools.
2. **Serena owns all Serena-side concurrency.** Multiple ChatGPT sessions may use Serena simultaneously. Sessions working on different projects should be able to read and write concurrently.
3. **Orchestrator does not participate in Serena project locking.** It does not inspect Serena's active sessions, job ledger, language servers, or project leases.
4. **Serena jobs remain entirely Serena-owned.** Only Serena can submit them, so `start_job`, `job_status`, `cancel_job`, the 12-job cap, process telemetry, and `~/.serena/jobs` do not need to change for Orchestrator.
5. **ChatGPT is the bridge between the MCPs.** A delegated ChatGPT worker claims work from Orchestrator, activates the specified project in Serena, performs the work, then hands the result back to Orchestrator.
6. **Orchestrator-managed Codex work should avoid live checkouts when modifying code.** Modifying Codex delegates should normally receive isolated Orchestrator-owned Git worktrees. This prevents clashes with Serena and also prevents Orchestrator's own concurrent Codex delegates from editing the same checkout.
7. **No global Serena writer lock.** `writer(serena)` and `writer(qengine)` should be able to run simultaneously. Same-project safety is a Serena-internal concern.
8. **Keep the primary ChatGPT context small.** Delegates return bounded typed results rather than transcripts, full logs, or all tool activity.
9. **Keep Serena's upstream-facing changes narrow.** Multi-session project safety and coding/jobs improvements belong in Serena; provider/delegation machinery belongs in Orchestrator.

## 2. Non-goals

The first implementation should not attempt to:

- automate creation of ordinary ChatGPT conversations through browser automation or undocumented ChatGPT APIs;
- rotate or share credentials to evade subscription or Codex usage limits;
- make Orchestrator aware of Serena's job slots or process accounting;
- build a shared `ProjectLeaseStore`, `ResourceRegistry`, or other coordination service between the MCPs;
- make Orchestrator proxy Serena coding tools;
- make Serena expose delegate/provider tools;
- let modifying Codex delegates edit the user's live checkout by default;
- automatically merge Codex worktree results;
- recursively expose Serena to Codex unless a concrete future need justifies it;
- make either MCP a second general-purpose LLM agent.

## 3. Target operating model

```text
                              primary ChatGPT
                     architect / planner / reviewer
                              /             \
                             /               \
                            v                 v
                 +------------------+   +------------------+
                 | Orchestrator MCP |   |    Serena MCP    |
                 |------------------|   |------------------|
                 | delegate state   |   | session projects |
                 | provider policy  |   | semantic coding  |
                 | claim/fallback   |   | files / media    |
                 | Codex workers    |   | Git / shell      |
                 | fan-out/fan-in   |   | durable jobs     |
                 | delegate UI      |   | memories / UI    |
                 +--------+---------+   +---------+--------+
                          |                       |
              +-----------+-----------+           |
              |                       |           |
       fresh ChatGPT worker        Codex CLI      |
       manually claimed            worktree       |
              |                                   |
              +-----------------------------------+
                           worker chat uses Serena
```

There is no Orchestrator -> Serena runtime dependency.

A normal ChatGPT delegate flow is:

```text
parent ChatGPT
    -> @Orchestrator create delegate

fresh worker ChatGPT
    -> @Orchestrator claim delegate
    -> @Serena activate project
    -> @Serena inspect/edit/test/git
    -> @Orchestrator complete delegate

parent ChatGPT
    -> @Orchestrator collect result
```

A Codex flow is separate:

```text
parent ChatGPT
    -> @Orchestrator create delegate provider=codex

Orchestrator
    -> create isolated worktree
    -> run Codex there
    -> retain logs/usage
    -> return commit/diff/test summary

parent ChatGPT
    -> review/integrate as desired, normally through Serena/Git
```

## 4. Serena MCP changes

### 4.1 Session-scoped project binding

Current Serena uses `SerenaAgent._active_project` as process-wide mutable state. ChatGPT MCP requests already expose an `openai/session` value through `get_mcp_session_id()`. The current session should therefore own its selected project.

Conceptually:

```text
openai/session A -> serena
openai/session B -> qengine
openai/session C -> thesis
```

`activate_project()` in ChatGPT context should bind the calling session rather than redirect every connected chat.

The requirement is stronger than correct naming: project execution must actually be independent. A write in session A against `serena` should be able to overlap a write in session B against `qengine`.

Do **not** achieve safety by wrapping all tool calls in one process-global `active_project_context` lock.

### 4.2 Independent project runtimes

Prefer one Serena MCP process that can cache several independent project runtimes.

Conceptually:

```text
SessionProjectRegistry
    session A -> ProjectRuntime(serena)
    session B -> ProjectRuntime(qengine)
    session C -> ProjectRuntime(thesis)

ProjectRuntime
    project instance
    language-server manager
    project memories/config
    project-local lifecycle/state
```

Several sessions may intentionally share the same `ProjectRuntime`. Distinct project runtimes should not block one another merely because they live in one Serena process.

Audit project-dependent state including:

- `SerenaAgent._active_project`;
- exposed project-dependent tool instances;
- modes/context assumptions affected by project activation;
- language-server manager ownership;
- memory manager/project configuration;
- project activation/shutdown side effects;
- dashboard project attribution;
- activity-panel project attribution;
- any cached project-dependent fields on tools or the agent.

The existing `ProjectServer` is useful precedent for caching project instances, but its process-wide `_active_project_lock` is not the intended final execution model for ordinary multi-session Serena use.

If upstream Serena invariants make independent in-process project runtimes excessively invasive, stop at the O01 gate and compare that design with a thin Serena supervisor routing to project-fixed headless Serena workers. This is a fallback, not the starting choice.

### 4.3 Serena owns same-project safety

Orchestrator does not solve same-project collisions for Serena.

Serena should define its own policy for sessions intentionally using the same live project. The conservative initial guarantee is correctness rather than maximal parallelism.

The important invariant is:

```text
writer(serena) + writer(qengine)  -> allowed concurrently
writer(serena) + writer(thesis)   -> allowed concurrently
same live project contention       -> handled internally by Serena
```

This may initially mean project-local serialization for editing operations if the underlying Serena/language-server state requires it. That serialization must be **per project runtime**, never process-global.

### 4.4 Serena jobs remain unchanged

No Orchestrator integration is required for durable jobs.

Serena remains the sole owner of:

```text
start_job
job_status
cancel_job
~/.serena/jobs
12-job concurrency ceiling
job output retention
job CPU/memory/process telemetry
```

Jobs can only be submitted through Serena, so there is no cross-MCP job race to solve. Orchestrator must not subtract Codex workers from Serena's job allowance or read Serena job state as part of normal scheduling.

If real workstation overload later becomes a problem, solve it as a separate host-level resource-management feature based on evidence rather than coupling the two MCPs pre-emptively.

### 4.5 Serena activity UI

Keep the existing Serena inline component focused on local execution:

```text
Serena
find_symbol     SessionProjectRegistry
start_job       B25 reoptimisation
3 jobs running
```

It should show:

- Serena tool lifecycle;
- project attribution for the current session;
- durable jobs;
- live job output/details;
- retained old-panel activity as already designed.

A Serena panel in one ChatGPT session must not absorb another session's tool activity merely because both sessions connect to the same MCP process.

## 5. Orchestrator MCP

### 5.1 Separate package/server in the Serena repository

Add Orchestrator as a second MCP entry point in this repository, but keep its runtime independent from Serena.

Orchestrator owns:

- delegate persistence and state transitions;
- parent/worker session relationships;
- ChatGPT worker claim handoff;
- provider selection (`chat`, `codex`, later others);
- Codex execution and fallback policy;
- Orchestrator-owned Codex worktrees;
- fan-out/fan-in;
- provider usage/budget accounting;
- bounded task packets and typed results;
- delegation-specific activity UI and audit logs.

Orchestrator does **not** own or inspect:

- Serena active projects/sessions;
- Serena language servers;
- Serena jobs;
- Serena tool activity;
- Serena project locks;
- Serena memory state.

Persist Orchestrator state separately, for example:

```text
~/.orchestrator/
    delegates/
    logs/
    worktrees/
    provider-state/
```

The exact location may be configurable, but it should not masquerade as Serena runtime state.

### 5.2 ChatGPT workers are claimable delegates

An ordinary ChatGPT conversation cannot be programmatically started by Orchestrator through a supported subscription interface. Model it as persisted work claimed by a fresh chat.

Desired workflow:

1. Parent calls `create_delegate(...)`.
2. Orchestrator persists a bounded `DelegateSpec`.
3. Parent receives a delegate ID and tiny launch prompt.
4. User opens a fresh ChatGPT chat and sends, for example, `@Orchestrator claim delegate d_8f31 and complete it independently.`
5. Worker calls `claim_delegate("d_8f31")`.
6. Orchestrator binds that worker session and returns the full task packet.
7. Task packet names the project but does not activate Serena itself.
8. Worker independently calls Serena and performs the work.
9. Worker calls `complete_delegate(...)`.
10. Parent calls `collect_delegate(...)` and receives only the bounded typed result.

A fresh ordinary chat is preferable to branching the parent because it avoids inheriting the parent's large context.

### 5.3 Delegate providers

Keep provider choice behind a small interface:

```text
DelegateProvider
    |- InteractiveChatProvider
    |- CodexCliProvider
    `- future LocalModelProvider / API provider
```

Orchestrator's abstraction is the delegate, not Codex.

For Codex, prefer `codex exec --ephemeral --json` initially. Parse structured events and usage rather than scraping terminal prose.

### 5.4 Codex collision policy

Orchestrator must avoid creating conflicts between its own Codex workers and should also avoid modifying a live checkout that Serena or the user may be using.

The general policy is deliberately simple:

- **Read-only Codex tasks:** may inspect the requested project directly when appropriate.
- **Modifying Codex tasks:** normally run in a unique Orchestrator-owned Git worktree created from a known base commit.
- **Two modifying Codex delegates for the same repository:** each receives a distinct worktree/branch; worktree creation/removal is serialized as necessary inside Orchestrator.
- **Worktree unavailable or unsafe:** do not silently edit the live checkout. Return/emit a clear warning and either queue, fail safely, or require an explicit policy override.
- **Dirty live checkout:** do not attempt to copy uncommitted state implicitly. The task packet/base selection should make the limitation clear.
- **Integration:** return commit/worktree/diff metadata for review; do not auto-merge initially.

This is an Orchestrator-internal safety policy. It does not require a Serena lock or Serena awareness.

The same worktree policy is useful even if Serena did not exist, because concurrent Codex delegates can otherwise clash with each other.

### 5.5 Auto provider with claim timeout

A delegate accepts a provider policy:

- `chat`: wait for an ordinary ChatGPT worker until cancelled/rerouted;
- `codex`: run through Codex immediately;
- `auto`: prefer ChatGPT, then fall back to Codex after a claim window.

Initial `auto` claim window: approximately 90 seconds, configurable.

Do not attempt to infer whether the user is present from unsupported ChatGPT state. A successful claim is the presence signal.

Orchestrator's durable scheduler must make the fallback transition exactly once even across restart/race boundaries.

## 6. Persisted delegate model

Implement a durable `DelegateStore` with atomic writes and process-safe locking.

A delegate record should contain at least:

```text
delegate_id
parent_session_id
worker_session_id | null
project_name
project_root | null
kind                 explore | code | review | research
provider_policy      chat | codex | auto
active_provider      chat | codex | null
state
created_at
claim_deadline | null
claimed_at | null
started_at | null
finished_at | null
goal
scope
out_of_scope
acceptance_criteria
verification
result_schema
result_budget
parent_notes
base_revision | null
worktree | null
provider_metadata
result_path | null
error | null
```

Suggested states:

```text
WAITING_FOR_CHAT
QUEUED
RUNNING_CHAT
RUNNING_CODEX
COMPLETED
FAILED
CANCELLED
TIMED_OUT
```

State transitions belong in one component. Callers should not be able to create invalid combinations directly.

## 7. Delegate task packet

The worker must not receive the parent conversation. Orchestrator constructs a bounded `DelegateSpec` containing only what is required.

Recommended fields:

- one clear goal;
- target project name/path;
- task kind (`explore`, `code`, `review`, `research`);
- known relevant files/symbols if already identified;
- constraints and out-of-scope items;
- acceptance criteria;
- verification commands;
- desired result schema;
- parent notes capped to a small budget, initially around 500 tokens;
- explicit instruction that the worker independently gathers missing repository context.

Avoid copying large diffs, logs, files, or parent-chat transcripts into the packet. Large evidence remains in its source system and should be referenced when possible.

## 8. Context and output budgeting

There is no need for one shared Serena/Orchestrator response-budget implementation.

Each MCP should solve its own context problem:

### Serena

Continue improving bounded/retained output for high-volume coding and job tools:

- shell-command results;
- job output;
- Git diffs;
- search results;
- file reads;
- test output.

Serena's existing stable retained-output IDs/cursors are the right direction.

### Orchestrator

Bound:

- delegate task packets;
- status responses;
- provider event/log output;
- collected worker results;
- fan-in summaries.

Provider logs and oversized events should remain in Orchestrator storage behind stable IDs/cursors rather than being returned to the parent by default.

The two MCPs may independently converge on similar response metadata, but this is not a reason to introduce shared runtime code or state.

## 9. Typed compact hand-back

The context benefit depends on strict result contracts.

### ExploreResult

```text
status
conclusion
findings[]           <= 5 normally
evidence[]
recommendation
uncertainties[]
```

### CodeResult

```text
status
summary
changed_files[]
tests[]
commit | null
worktree | null
remaining_issues[]
artifacts[]
```

### ReviewResult

```text
status
verdict
findings[]
severity
evidence[]
```

### ResearchResult

```text
status
answer
sources[]
caveats[]
```

Default serialized hand-back target: under roughly 1,500 tokens per worker, with a hard initial maximum around 2,500-3,000 tokens.

Never return full worker transcripts, raw tool traces, complete test logs, or whole source files by default.

## 10. Provider policy and limits

Codex is a scarcer execution resource than ordinary ChatGPT usage. Orchestrator should track provider usage and impose its own provider limits without involving Serena.

For `codex exec --json`, retain:

- input tokens;
- cached input tokens;
- cache-write input tokens;
- output tokens;
- reasoning output tokens;
- model/reasoning configuration;
- elapsed time;
- success/failure.

Initial routing guidance:

| Task | Default route |
| --- | --- |
| Tiny lookup/edit | parent ChatGPT + Serena |
| Broad read-only exploration | ChatGPT delegate |
| Architecture comparison | ChatGPT delegate |
| Test/log diagnosis | ChatGPT delegate |
| Diff review | ChatGPT delegate |
| Documentation/research | ChatGPT delegate |
| Substantial implementation | Codex preferred |
| Iterative edit/test/fix unattended | Codex |
| User explicitly requests Codex | Codex |
| `auto` unclaimed after timeout | Codex if available |
| Codex soft budget exhausted | remain/manual ChatGPT queue |

Initial Orchestrator-only limits may be:

```text
Codex delegates globally       2
ChatGPT delegates globally     4
fan-out batch default          2
```

These do not modify Serena's 12-job cap.

Support named credential/provider profiles only for legitimate separately authenticated users or API/local backends. Do not silently rotate Business seats or share another user's credentials.

## 11. Orchestrator MCP surface

Keep the model-visible API small:

```text
create_delegate
claim_delegate
complete_delegate
collect_delegate
delegate_status
delegate_reroute
delegate_cancel
delegate_batch
```

Provider selection is a field/policy, not a collection of separate tools.

### `create_delegate`

Returns:

- delegate ID;
- state;
- provider policy;
- compact manual launch prompt when relevant;
- claim deadline/fallback information;
- no full worker specification in the parent result.

### `claim_delegate`

Uses the current `openai/session`, atomically claims eligible work, binds the worker session, and returns the bounded task packet.

It does not acquire or manipulate Serena state.

### `complete_delegate`

Validates worker ownership and typed result, persists it, and marks the delegate terminal.

### `collect_delegate`

Returns only the typed bounded result to the parent session.

### `delegate_status`

Returns compact state/telemetry. Do not inline private provider logs.

### `delegate_reroute`

Lets the parent change a still-unclaimed delegate between `chat`, `auto`, and `codex`. It must not steal or interrupt work after a ChatGPT or Codex worker has started.

### `delegate_cancel`

Cancels claimable/queued work and terminates Orchestrator-owned Codex processes safely. Terminal cancellation is idempotent.

### `delegate_batch`

Add after single-delegate behaviour is stable. Initial fan-out should be read-only/analysis-oriented.

## 12. Activity UIs

Keep the inline components completely separate.

### Serena panel

Shows Serena activity only:

```text
Serena
find_symbol   ProjectRuntime
start_job     B25 reoptimisation
3 jobs running
```

### Orchestrator panel

Shows delegated agents only:

```text
Orchestrator                 2 agents
ChatGPT   inspect routing       running
Codex     implement fix         queued
```

Expanded Orchestrator rows may show:

- provider;
- task kind;
- target project;
- concise label;
- state;
- elapsed time;
- Codex model/reasoning/usage when available;
- compact result summary;
- launch/copy affordance where supported.

Full provider logs remain private/explicitly retrievable.

A combined operator dashboard could be built later for convenience, but neither MCP should depend on it and it should not imply shared runtime ownership.

## 13. ChatGPT operating instructions

### Serena guidance

Serena's ChatGPT instructions should focus on:

- binding the intended project for the current session;
- semantic inspection/editing;
- safe concurrent multi-project operation;
- Git/files/media;
- durable jobs;
- batching independent calls;
- bounded output.

Do not include delegation/provider policy in Serena's core instructions.

### Orchestrator guidance

Orchestrator's instructions should explain when to:

- stay in the parent and use Serena directly;
- create an ordinary ChatGPT delegate;
- select Codex;
- use `auto` fallback;
- fan out independent analysis;
- collect compact results.

The parent always owns decomposition, architecture decisions, provider overrides, conflict resolution, consequential review, and user communication.

## 14. Implementation phases

Each stage ends in a clean checkpoint and should not silently roll into the next phase.

### O00 - Baseline

Complete before this roadmap work:

- Serena activity/job UI checkpoint `d865d17f`;
- format and type-check were clean;
- baseline full tests had 997 passed, 9 skipped, and the known `upload_file` schema-test incompatibility.

Keep the roadmap itself as one amended commit rather than retaining superseded design commits.

### O01 - Serena multi-session, multi-project execution

Repository area: Serena MCP/core.

Goal: several ChatGPT sessions can bind and operate on independent projects concurrently, including simultaneous writes to different projects.

Work:

- session/project registry;
- replace process-global project selection at the MCP execution boundary;
- audit project-dependent mutable state;
- independent `ProjectRuntime` instances or equivalent;
- project-specific language-server/memory/config lifecycle;
- session-correct dashboard/activity attribution;
- no process-global execution mutex.

Acceptance tests:

- session A on `serena` and session B on `qengine` can interleave reads;
- session A can write `serena` while session B simultaneously writes `qengine`;
- activating `thesis` in session C redirects neither A nor B;
- activation/teardown of one project does not shut down another runtime;
- same-project concurrent sessions remain correct according to Serena's internal policy;
- concurrent calls remain correctly attributed to their session/project.

Gate: if independent in-process runtimes require pervasive upstream surgery, compare with a thin supervisor + project-fixed Serena workers before proceeding.

### O02 - Orchestrator MCP skeleton

Repository area: new independent package/server in this repository.

Goal: establish the second MCP without any runtime dependency on Serena.

Work:

- package/module layout;
- CLI/server entry point;
- MCP factory;
- session identity extraction;
- separate Orchestrator config/user-home state;
- minimal health/info surface;
- side-by-side Serena/Orchestrator test fixture.

Acceptance tests:

- both MCPs start independently;
- restarting Orchestrator does not affect Serena;
- restarting Serena does not affect Orchestrator delegate persistence;
- Orchestrator exposes no Serena coding tools;
- Serena exposes no delegate/provider tools.

### O03 - Manual ChatGPT delegate flow

Repository area: Orchestrator.

Goal: offload context-heavy work to fresh ordinary ChatGPT chats.

Work:

- typed DelegateStore/state machine;
- atomic persistence/locking;
- `create_delegate`;
- `claim_delegate`;
- `complete_delegate`;
- `collect_delegate`;
- parent/worker ownership validation;
- bounded task/result schemas;
- one-line `@Orchestrator` launch prompt.

Acceptance demo:

1. Parent creates an explore delegate.
2. Fresh ChatGPT chat claims it with one line.
3. Worker receives only the task packet.
4. Worker uses Serena independently on the named project.
5. Worker completes through Orchestrator.
6. Parent receives only the bounded result.
7. Unrelated chats cannot claim/collect it incorrectly.

### O04 - Orchestrator status, cancellation, and UI

Repository area: Orchestrator.

Work:

- `delegate_status`;
- `delegate_cancel`;
- Orchestrator activity tracker/resource;
- delegate panel;
- private detail/audit storage;
- correct superseded-panel ownership.

Acceptance demo: create, claim, run, complete, fail, and cancel delegates while Serena's tool/job UI remains independent.

### O05 - Codex provider with worktree safety

Repository area: Orchestrator.

Goal: unattended Codex implementation without modifying live project checkouts by default.

Work:

- `DelegateProvider` abstraction;
- `CodexCliProvider`;
- `codex exec --ephemeral --json`;
- structured result schema;
- process cancellation/timeouts;
- JSONL event/usage parser;
- provider audit logs;
- Orchestrator-only Codex concurrency cap;
- worktree lifecycle manager;
- unique worktree/branch per modifying Codex delegate;
- warning/queue/fail-safe behaviour if isolation cannot be established.

Acceptance tests:

- successful Codex task;
- failed task;
- cancellation;
- timeout;
- usage parsed;
- modifying task does not alter live checkout;
- two modifying Codex delegates for the same repository do not share a checkout;
- unavailable worktree path produces an explicit safe warning/failure rather than live editing;
- result returns commit/worktree/diff/test summary;
- no automatic merge.

### O06 - ChatGPT-first `provider=auto`

Repository area: Orchestrator.

Work:

- configurable claim deadline;
- durable scheduler surviving Orchestrator restart;
- exactly-once fallback from `WAITING_FOR_CHAT` to Codex;
- provider availability/budget policy;
- explicit reroute controls;
- race-safe simultaneous claim/fallback handling.

Acceptance test: an unclaimed task starts Codex exactly once; a manual claim at the boundary cannot create duplicate workers.

### O07 - Fan-out/fan-in

Repository area: Orchestrator.

Goal: parallelise independent investigation without multiplying parent context.

Work:

- `delegate_batch`;
- batch-level concurrency/result budget;
- default fan-out of two;
- ChatGPT-first analysis workers;
- compact aggregation rather than transcript concatenation.

First release should focus on read-only/analysis fan-out. Modifying Codex concurrency remains governed by the O05 worktree policy.

Implemented checkpoint: batches persist their pending task packets separately from delegate records, default to two active ChatGPT-first workers, and expose additional queued tasks only when a slot becomes free. The first release accepts `explore`, `review`, and `research` tasks, rejects `code` fan-out, supports `chat` or `auto` provider policy, and returns a deterministic bounded aggregate of terminal typed results rather than concatenating worker transcripts. Batch concurrency is capped at four and fan-in has its own configurable result budget.

Acceptance coverage verifies that a three-task batch initially exposes exactly two workers, promotes the pending task only after one child becomes terminal, enforces parent ownership, keeps fan-in inside the batch result budget, and rejects modifying code fan-out.

### O08 - Serena response budgeting

Repository area: Serena MCP.

Goal: reduce ordinary Serena context pollution independently of Orchestrator.

Work:

- token-aware or approximate response budgeting where useful;
- migrate high-volume tools incrementally;
- stable retained output IDs/cursors;
- explicit completeness/truncation metadata.

Implemented checkpoint: when `read_tool_output` is available, implicit Serena tool responses now use a configurable approximate token budget (8,000 tokens by default, using four characters per token) instead of the legacy 150,000-character ceiling. Explicit `max_answer_chars` values remain exact character overrides, and contexts without retained-output paging retain the legacy default. The shared bounded-response path covers high-volume file/search/Git-style tools, while shell execution reuses the same resolved budget with its existing retained live transcript path; Serena jobs remain independently bounded by their existing cursor-based output mechanism.

Any oversized bounded result in the retained-output context now keeps the exact full result under a stable output ID even when a compact shortening fits the response budget. Tail responses and `read_tool_output` pages expose explicit `complete`, `truncated`, range/cursor, and live/open state metadata so callers do not need to infer whether they have the whole result. Existing character offsets remain stable across later tool calls.

Acceptance coverage verifies context-sensitive implicit budgeting without changing explicit character limits, exact recovery after compact search shortening, stable output IDs across later results, complete versus partial retained pages, and live-output completeness semantics.

### O09 - Routing guidance and polish

Repository area: both MCP contexts/resources, without runtime coupling.

Work:

- concise Serena concurrent-project instructions;
- discoverable Orchestrator delegation guide;
- route-selection examples;
- parent -> ChatGPT worker -> Serena -> Orchestrator hand-back example;
- parent -> Codex worktree -> review example;
- avoid putting orchestration instructions into Serena tool descriptions.

Implemented checkpoint: the ChatGPT Serena context now states that project selection is conversation-scoped, the intended project should always be activated before repository work, multiple sessions may read the same project, same-project writes are serialized, and writes to different projects may proceed concurrently. This remains local Serena guidance; no Orchestrator provider or delegate workflow was added to Serena tool descriptions.

Orchestrator now exposes a discoverable `orchestrator://delegation-guide` Markdown resource and references it from the MCP server instructions. The guide distinguishes direct parent+Serena work, ChatGPT delegates, bounded read-only fan-out, and Codex delegates; documents `chat`, `codex`, and `auto` provider-policy selection; and preserves the architectural boundary that Orchestrator never resolves Serena projects.

The guide includes both requested end-to-end examples: parent -> fresh ChatGPT worker -> Orchestrator claim -> Serena project activation/work -> typed Orchestrator completion -> parent collection, and parent -> Codex modifying delegate -> isolated Orchestrator worktree -> terminal collection -> explicit Git review/integration. It also documents that modifying Codex work is not silently merged into Serena's live checkout and that batch fan-out remains read-only.

Acceptance coverage verifies the Serena context surfaces the concurrent-project contract and the Orchestrator MCP exposes the routing resource with both hand-off workflows.

### O10 - Optional providers

Only after the architecture proves useful:

- API-backed provider with explicit monetary budget;
- local-model provider;
- legitimate alternate authenticated-user profiles where policy permits.

Keep provider-specific behaviour behind Orchestrator's provider interface.

## 15. Testing strategy

Follow Serena's project rule: test externally observable guarantees rather than implementation layout.

Priority Serena tests:

1. **Session isolation:** project activation/tool execution cannot cross sessions.
2. **Cross-project concurrency:** a writer on project A can overlap a writer on project B.
3. **No global project mutex:** hold a write open in fixture A while a write in fixture B completes.
4. **Project lifecycle isolation:** language servers, memories, activation, shutdown, and UI attribution remain project-correct.
5. **Same-project correctness:** concurrent sessions on one project obey Serena's defined local policy.
6. **Jobs unchanged:** existing durable-job behaviour and global 12-job ceiling remain intact.
7. **Backward compatibility:** non-ChatGPT Serena contexts retain existing behaviour unless deliberately migrated.

Priority Orchestrator tests:

1. **Delegate ownership:** only parent/claimed worker can perform relevant transitions.
2. **State machine:** invalid transitions are rejected; terminal operations are idempotent where sensible.
3. **Crash recovery:** restart around create/claim/complete/Codex launch/cancellation boundaries.
4. **No duplicate fallback:** exactly one provider becomes active.
5. **Context bounds:** parent-facing results remain bounded even with huge provider logs.
6. **Codex worktree isolation:** modifying delegates do not touch live checkouts and do not share worktrees with one another.
7. **MCP independence:** Serena can be unavailable while Orchestrator manages its persisted delegates, and vice versa.
8. **UI lifecycle:** each MCP's panel reports only its own activity.

Use multiple fixture repositories for Serena concurrency tests and temporary repositories/worktrees for Orchestrator Codex tests.

## 16. Security and trust boundaries

### Serena

Enforce:

- registered-project confinement;
- session-scoped project routing;
- safe command/job constraints already present;
- project-local concurrency guarantees;
- no arbitrary PID cancellation;
- file/media path confinement.

### Orchestrator

Enforce:

- parent/worker session ownership;
- explicit task/provider policy;
- Orchestrator-owned Codex process cancellation only;
- worktree path confinement;
- no implicit live-checkout modification for Codex code tasks;
- private provider logs by default;
- no credential values in delegate specs/results/UI;
- no account/seat rotation intended to circumvent service limits.

A claim ID must not become a general authorization token across unrelated authenticated users if the MCP endpoint is ever shared more broadly. Derive session/user ownership from MCP metadata where possible.

There is intentionally **no Serena/Orchestrator shared trust boundary** because there is no shared runtime state.

## 17. Repository and upstream strategy

Both MCPs live in the Serena fork/repository for development/deployment convenience, but should remain architecturally independent.

Keep Serena changes concentrated in:

- session-scoped project selection;
- project-runtime lifecycle isolation;
- multi-project concurrency;
- existing coding/jobs/media/Git/UI improvements;
- Serena response budgeting.

Keep Orchestrator additions concentrated in new modules/packages:

- Orchestrator MCP server/CLI;
- DelegateStore/state machine;
- provider implementations;
- worktree manager;
- Orchestrator UI/audit storage;
- fan-out/fan-in and routing policy.

Do not make upstream Serena core classes import Orchestrator code. Do not make Orchestrator import Serena agent/project runtime internals.

Generic dependency/library reuse is fine where it is incidental, but avoid creating shared mutable runtime abstractions merely to reduce code duplication.

The desired long-term property is that Orchestrator can evolve rapidly while Serena's divergence from upstream remains narrow and understandable.

## 18. Immediate next step

Review the **O09 checkpoint** before deciding whether optional O10 providers are useful.

O09 now makes route selection discoverable without coupling the two MCP runtimes. Serena's ChatGPT context explains conversation-scoped activation and the same-project versus cross-project concurrency contract, while Orchestrator owns the delegation guide and provider-routing examples.

The Orchestrator guide covers direct parent+Serena work, ChatGPT delegates, bounded read-only batches, and isolated Codex modifying delegates. It treats checkout ownership as a routing invariant: Serena-backed work mutates the active project's live checkout under Serena's serialization, while every modifying Codex route, including `auto` fallback, uses an Orchestrator-owned Git worktree. It explicitly demonstrates the ChatGPT-worker hand-back path through Serena and the Codex-worktree review path while preserving explicit parent-side integration.

Acceptance coverage verifies both guidance surfaces. No optional provider was implemented as part of O09.

Do **not** start O10 until this checkpoint has been reviewed and an additional provider has a concrete use case.
