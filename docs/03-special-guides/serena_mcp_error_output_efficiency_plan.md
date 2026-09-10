# Serena MCP Error and Output Efficiency Plan

Status: COMPLETE (2026-09-10)

Baseline: Serena 2.0.0, `c6bbb8e0` (`Release Serena 2.0 as standalone runtime`)

Scope: all 46 ChatGPT-visible Serena tools, plus the shared MCP, execution/activity, retained-output, logging, validation, timeout, diagnostics, and native-media paths that determine what those tools return.

This plan follows the Serena 2.0 standalone architecture. It does not reintroduce the removed task executor, legacy dashboard log scraping, modes/contexts, JetBrains integration, project-query service, or upstream compatibility machinery.

## 1. Goal

Make Serena a lower-noise, more agent-efficient MCP without losing useful diagnostic information or operational safety.

The target contract is:

```text
expected tool/domain failure
    -> concise Serena user-facing exception
    -> one native FastMCP ToolError
    -> the same concise text in ExecutionStore.error

unexpected internal failure
    -> one private traceback in Serena logs
    -> bounded native FastMCP ToolError
    -> bounded error text in ExecutionStore.error

successful tool call
    -> only information that changes the caller's next action
    -> retained-output paging when the useful result is genuinely large
```

For example, a failed `replace_content` search should return:

```text
No matches of search expression found.
```

not a `ValueError -> ToolCallError -> ToolError -> ToolError` chain or a traceback.

## 2. Architectural invariants

1. **The MCP boundary owns external error presentation.** `SerenaFastMCPTool.run()` is the single place that converts Serena failures to native FastMCP `ToolError` responses.
2. **Expected failures are explicit.** Do not infer user-facing safety from broad Python classes such as `ValueError` or `RuntimeError`; those classes are also used for genuine invariants and defects.
3. **Unexpected failures keep one traceback.** Internal bugs remain debuggable, but a traceback is never copied into the model-visible response, activity state, or dashboard error field.
4. **One canonical persisted error.** `ExecutionStore.error` contains the same concise error text presented through MCP. Activity/dashboard layers are views over that field rather than alternative error formatters.
5. **No universal response envelope.** Do not add `{success, error, data, ...}` around every tool. Native MCP errors already distinguish failure; successful calls should remain natural strings, JSON, or native media as appropriate.
6. **Do not compact away decision-relevant information.** Diffs, diagnostics, symbol locations, Git output, job IDs/state, dry-run occurrence IDs, and file/media resources remain available when they affect the next action.
7. **Do remove repeated invariants and instructions.** Tool descriptions already teach the model how a tool behaves. Routine results should not repeatedly explain paging, persistence, safety guarantees, or arguments the model just supplied.
8. **Retained output remains the single large-result mechanism.** Large textual results are retained once and paged by stable `output_id`; callers must not need to rerun expensive searches or commands to recover omitted content.
9. **LSP recovery semantics are preserved.** Read operations may be replayed once after an affected language server is restarted. Mutating operations are never automatically replayed after uncertain partial execution.
10. **Timeout/cancellation does not release mutation safety early.** Existing detached-worker and project-coordinator semantics remain intact while the request receives a concise timeout/cancellation response.
11. **Native media stays native.** Images, rendered PDF pages, files, and resource links are not flattened into verbose JSON merely for consistency.
12. **Tests assert observable MCP behaviour.** They should survive behaviour-preserving refactors and should not freeze internal exception class structure.

## 3. Current Serena 2.0 baseline

The major prerequisites are already complete:

- tool execution is direct through the per-project execution coordinator; the old `TaskExecutor` is gone;
- `ExecutionStore` is authoritative for executions and activity state;
- dashboard/activity views no longer scrape INFO/ERROR logs to reconstruct results;
- execution IDs propagate through MCP, worker execution, retained output, and activity state;
- project access (`READ`, `WRITE`, `SESSION_CONTROL`) and side-effect-aware LSP recovery are explicit;
- large result paging is centralised in `ToolOutputStore` / `read_tool_output`;
- the tool catalogue and runtime policy are fixed for ChatGPT.

The main remaining legacy residue is concentrated in the tool/MCP boundary:

- `ToolCallError` is still an intermediate wrapper;
- production always invokes `Tool.apply_ex(log_call=True, catch_exceptions=False)`, leaving obsolete compatibility switches;
- `Tool.apply_ex()` still logs complete tool arguments and complete successful results at INFO even though `ExecutionStore` already owns them;
- generic tool exceptions are prefixed with their Python class name and logged with `exc_info` before being wrapped;
- `execute_fn()` converts `ToolCallError` to FastMCP `ToolError`, after which `SerenaFastMCPTool.run()` can wrap that again as `Error executing tool <name>: ...`;
- FastMCP/Pydantic argument-validation failures use the same generic error path;
- `ActivityTracker.finish_tool()` treats failed exceptions as arbitrary serialised results instead of accepting canonical error text;
- retained-output responses still include repeated explanatory prose that is already present in tool descriptions.

## 4. Error taxonomy

### 4.1 Expected request/domain failure

Introduce one small Serena exception, provisionally `UserFacingError`, whose message is complete, bounded, and safe to return directly to the model.

Use it for failures such as:

- requested path/file/memory/job does not exist;
- a replacement has zero, multiple, ambiguous, or stale matches;
- an argument combination is invalid after schema validation;
- a path attempts to escape the project;
- Git rejects a requested operation;
- a requested media/PDF operation is unsupported or exceeds configured limits;
- a required external command for a requested feature is unavailable;
- a mutating LSP operation terminates and Serena deliberately refuses to replay it.

At the MCP boundary:

```text
UserFacingError(message)
    -> ToolError(message) from None
```

No class prefix, tool-name prefix, traceback, or chained exception is model-visible.

### 4.2 MCP/schema validation failure

FastMCP/Pydantic validation is expected agent-recovery behaviour, not an internal fault.

Format validation errors compactly from structured validation entries:

```text
Invalid arguments: max_chars must be between 1 and 20000.
```

For multiple failures, join concise field messages with semicolons. Exclude input dumps, documentation URLs, Python repr noise, and repeated schema context.

### 4.3 Normal negative result

Do not turn legitimate empty/blocked query results into exceptions merely to make error handling uniform.

Examples:

- no references found;
- no files matched a discovery query where empty results are an ordinary answer;
- `safe_delete_symbol` reports references that prevent deletion;
- a shell command exits non-zero when the tool's contract is explicitly to return `return_code`, stdout, and stderr.

These remain successful results because the caller needs the returned state to decide what to do next.

### 4.4 Operational external failure

Failures from Git, systemd/job management, PDF rendering, downloads, filesystem access, or LSP startup can be expected operational failures when Serena already knows what went wrong and can provide a corrective message. Convert those deliberately at their owning abstraction to `UserFacingError`.

Do not blanket-convert every `RuntimeError`: storage corruption, impossible state, programmer errors, and broken invariants remain internal failures.

### 4.5 Unexpected internal failure

An ordinary unclassified exception reaching the MCP boundary indicates a Serena defect or invariant failure.

Required behaviour:

1. log the traceback once at the MCP boundary (or the single owning internal boundary when already handled there);
2. persist a bounded concise error in `ExecutionStore.error`;
3. return a bounded native `ToolError` without traceback text;
4. do not retry a mutating operation unless its execution contract explicitly proves replay safety.

The external message may retain a compact exception class/message when useful for diagnosis, but it must not include stack frames or nested wrapper text.

### 4.6 Timeout, cancellation, and elicitation

Keep these control-flow cases distinct from ordinary failures:

- `UrlElicitationRequiredError`: propagate using FastMCP's native elicitation flow;
- request timeout: return one concise timeout error while the detached worker retains its coordinator permit until it actually stops;
- MCP cancellation: preserve the existing detached-worker accounting and do not convert cancellation into an ordinary domain error;
- LSP termination: preserve the current access-aware read retry / write non-replay policy.

## 5. Shared implementation work

### E01 — Add the user-facing Serena error type

- Add a low-level `UserFacingError` (final name may be adjusted) in a small shared Serena module rather than in an individual tool module.
- Keep it deliberately minimal: one message, no error-code hierarchy, severity enum, HTTP status, or universal response payload.
- Its presence is the explicit marker that a failure is intended for direct model presentation.

Completion condition: expected Serena failures can cross internal layers without acquiring Python exception-class prefixes or tracebacks.

### E02 — Make `SerenaFastMCPTool.run()` the single external error boundary

- Catch `UserFacingError` explicitly and raise native `ToolError(str(error)) from None`.
- Compact FastMCP/Pydantic validation failures separately.
- Keep native elicitation/cancellation control flow intact.
- Handle timeout once without `Error executing tool <name>:` duplication.
- Catch genuinely unexpected exceptions once, log one traceback, persist the bounded error, and return one bounded `ToolError`.
- Do not wrap an already-native `ToolError` in another `ToolError`.

Completion condition: every model-visible failure has one framing layer.

### E03 — Simplify `Tool.apply_ex()`

Production has one call path, so remove legacy compatibility that no longer serves Serena 2.0:

- remove `catch_exceptions`;
- remove `log_call`;
- remove `_log_tool_application()`;
- remove complete `Result: ...` INFO logging;
- remove generic exception-to-`ToolCallError` conversion;
- remove `ToolCallError` once its remaining deliberate uses are migrated;
- preserve session/project binding, coordinator access, execution ID propagation, readiness, and LSP recovery.

Update tests to call the simplified contract rather than preserving obsolete flags.

Completion condition: a tool exception reaches the MCP boundary without intermediate presentation wrappers.

### E04 — Canonicalise execution error persistence

- Change execution completion APIs so failure accepts explicit error text rather than serialising an exception object as if it were a result.
- Persist exactly the externally meaningful bounded message in `ExecutionStore.error`.
- Keep detailed traceback data in logs, not execution/activity JSON.
- Remove duplicate generic value-serialisation logic from `ActivityTracker` where `ExecutionStore.serialize_value()` can own it.
- Ensure dashboard and inline activity continue to render the authoritative execution record without alternative formatting.

Completion condition: MCP, inline activity, and dashboard agree on the concise failure text.

### E05 — Compact retained-output protocol text

Keep stable IDs and exact paging, but reduce repeated prose.

Target truncated textual result shape:

```text
truncated=true; total_chars=17694; output_id=e547...
shown_range=12000:17694
<tail content>
```

The `read_tool_output` tool description already explains how to continue.

For `read_tool_output`, retain only fields that influence paging/interpretation:

- `output_id`;
- `total_chars`;
- `offset` / `end_offset` (or one equivalent range representation);
- `next_offset`;
- `complete`;
- `content`.

Audit whether `tool_name`, `previous_offset`, `truncated`, and `is_open` materially change caller behaviour. Remove fields that merely restate known state.

Completion condition: paging remains exact and stable while repeated protocol prose/metadata is reduced.

### E06 — Normalise trivial success responses

Use the shortest response that changes the next action.

Preferred patterns:

- mutation with no extra state: `OK`;
- mutation with one important qualifier: `OK; overwrote existing file`;
- operation returning an identity required later: return that identity and essential state;
- query: return the useful query data without a redundant success wrapper;
- diagnostics-bearing edit: preserve diagnostics because they may require immediate repair.

Do not repeat paths, arguments, safety guarantees, or persistence guarantees merely because the tool already knows them.

Completion condition: routine successful calls do not spend tokens restating the request.

### E07 — Convert expected failures at their owning abstractions

Audit deliberately raised `ValueError`, `FileNotFoundError`, `FileExistsError`, `PermissionError`, `TimeoutError`, `RuntimeError`, and relevant LSP exceptions by semantics. Convert only failures that are part of the supported tool contract.

Work from the public tool inward: the owning abstraction should know whether a failure is recoverable/user-facing. Avoid a global exception-class whitelist in `mcp.py`.

Completion condition: all 46 public tools have an explicit expected-failure policy recorded in the coverage ledger below and implemented where applicable.

### E08 — Behavioural MCP tests and final audit

- Exercise representative failures through `SerenaFastMCPTool.run()`, not only direct `.apply()` calls.
- Assert expected failure responses omit `Traceback`, `ToolCallError`, redundant `ToolError`, `Error executing tool`, and Python exception-class prefixes.
- Verify the matching `ExecutionStore.error` is canonical and concise.
- Inject one unexpected internal failure and assert exactly one internal traceback is logged while the model-visible/persisted response is bounded.
- Re-run timeout/cancellation and LSP mutation-safety tests.
- Search all public tool paths for remaining deliberate generic exceptions and classify each as expected, normal result, or internal invariant.
- Run the Serena completion checks from `mem:task_completion`, `git diff --check`, and inspect the final diff.

Completion condition: no exposed tool family is unclassified and the full Serena test suite passes.

## 6. Public tool coverage ledger

This ledger is based on the 46 active ChatGPT tools in Serena 2.0. Each row must be reviewed during E07 even when the final decision is "no change".

### 6.1 Configuration and workflow

| Tool | Expected failure treatment | Output treatment |
|---|---|---|
| `activate_project` | Unknown/unavailable project and invalid project path are user-facing. Runtime initialisation defects remain internal. | Keep the activation message and relevant memories/instructions because they materially alter subsequent tool use; remove only duplicated framing. |
| `get_current_config` | Configuration-read failures are normally internal unless caused by a known unavailable project/runtime state. | Keep compact runtime/project/tool facts; retained paging only if future growth requires it. |
| `initial_instructions` | Treat unavailable required project/runtime context as user-facing where actionable; prompt construction defects are internal. | Keep policy/instructions: this is control-plane context, not routine acknowledgement output. Avoid duplicated text already mounted elsewhere. |
| `onboarding` | Expected missing/invalid onboarding state is user-facing; storage defects are internal. | Keep actionable onboarding instructions only; do not add generic success framing. |
| `open_dashboard` | Known dashboard unavailable/address/port failures are user-facing. Unexpected server defects are internal. | Return only the usable dashboard location/status needed by the caller. |

### 6.2 File and text tools

| Tool | Expected failure treatment | Output treatment |
|---|---|---|
| `create_text_file` | Invalid/out-of-project path, permission/known filesystem rejection are user-facing. | Prefer `OK`; add only an overwrite qualifier when that changes interpretation. Preserve new diagnostics. |
| `read_file` | Missing path, directory-vs-file mismatch, out-of-project access, invalid range are user-facing. | Return content directly; retained paging for large text without repeated continuation prose. |
| `list_dir` | Missing/not-directory/out-of-project paths and invalid limits are user-facing. | Keep compact JSON listing; retain/paginate only when necessary. |
| `find_file` | Invalid search root/glob syntax is user-facing. No matches remain a normal successful result. | Keep only matching paths and essential completeness metadata. |
| `replace_content` | No match, ambiguous/multiple match when disallowed, invalid mode/pattern, stale/missing path are user-facing. | `OK` on clean edit; preserve diagnostics if introduced. |
| `replace_in_files` | Invalid pattern, invalid occurrence IDs, stale IDs, expected-count mismatch, ambiguity and missing roots are user-facing where the operation cannot proceed. A guarded/dry-run listing remains a normal informative result where designed. | Preserve occurrence IDs and minimal diffs during dry run/guard failure; applied edit returns compact count/per-file summary plus diagnostics. |
| `search_for_pattern` | Invalid regex/glob/root/range limits are user-facing. No matches remain a normal result. | Preserve compact matches and retained paging; shorten truncation boilerplate. |

### 6.3 Semantic and symbolic tools

| Tool | Expected failure treatment | Output treatment |
|---|---|---|
| `get_symbols_overview` | Missing/non-file path, unsupported file/language, unavailable known LSP are user-facing. | Keep compact symbol hierarchy; retain paging if needed. |
| `find_symbol` | Invalid path/pattern and unsupported semantic analysis are user-facing. Ordinary no-match behaviour should remain whichever successful contract the tool currently documents rather than being globally converted to failure. | Keep symbol identity/location/body only as requested; no success envelope. |
| `find_declaration` | Invalid location/path, no resolvable declaration where the tool treats this as failure, and unsupported analysis are user-facing. | Keep declaration identity/location and requested info only. |
| `find_implementations` | Invalid symbol/path and unsupported analysis are user-facing; zero implementations can be a normal result. | Keep implementation identities/locations, optional info only when requested. |
| `find_referencing_symbols` | Invalid symbol/path/context range and unsupported analysis are user-facing; zero references are normal. | Keep compact references and requested context; retained paging if large. |
| `get_diagnostics_for_file` | Missing/unsupported file and invalid ranges/severity are user-facing. | Keep grouped diagnostics; omit empty/redundant metadata where it does not affect interpretation. |
| `replace_symbol_body` | Missing/ambiguous symbol, invalid body/location, unsupported analysis and write-side LSP termination are user-facing. | `OK` plus newly introduced diagnostics when present. |
| `insert_after_symbol` | Missing/ambiguous target, invalid insertion location, unsupported analysis and write-side LSP termination are user-facing. | `OK` plus diagnostics when present. |
| `insert_before_symbol` | Same classification as `insert_after_symbol`. | `OK` plus diagnostics when present. |
| `rename_symbol` | Missing/ambiguous symbol, invalid rename target, unsupported analysis and unsafe LSP failure are user-facing. | Return concise successful rename acknowledgement; do not restate the full request. |
| `safe_delete_symbol` | Invalid target/analysis failures are user-facing. Existing references that intentionally block deletion are a normal successful result, not an exception. | On blocked deletion return the compact reference evidence needed to decide; on deletion return `OK`. |

### 6.4 Shell and durable jobs

| Tool | Expected failure treatment | Output treatment |
|---|---|---|
| `execute_shell_command` | Invalid working directory and shell invocation/timeout infrastructure failures are user-facing. A command's ordinary non-zero exit remains structured result data. | Keep `return_code`, stdout, stderr, cwd only where useful; use retained output for large transcripts. |
| `start_job` | Empty/invalid command or label, invalid cwd, concurrency/config limits, and known systemd start failures are user-facing. | Return job ID, state and only essential recovery/concurrency information; remove prose restating persistence guarantees already in the description. |
| `job_status` | Unknown/invalid job ID, invalid cursor/mode/wait combination and known journal-read failures are user-facing. | Preserve the existing delta/cursor design. Audit immutable metadata on repeated calls and keep only fields affecting the next action. |
| `cancel_job` | Invalid/unknown job ID and known stop failures are user-facing. Already-terminal jobs remain a normal idempotent result. | Return resulting state and identity only. |

### 6.5 Git tools

| Tool | Expected failure treatment | Output treatment |
|---|---|---|
| `git_status` | Known Git repository/command failure is user-facing. | Keep concise porcelain status; no success wrapper. |
| `git_fetch` | Invalid remote/known Git rejection/network-auth failure text is user-facing. | Keep concise Git output that affects follow-up; `OK` when there is genuinely no useful output. |
| `git_log` | Invalid ref/limit and known Git failure are user-facing. | Keep compact log output and retained paging. |
| `git_diff` | Invalid path/ref-like input where applicable and known Git failure are user-facing. | Preserve diff text; retained paging is essential. |
| `git_branch` | Invalid action/name/start point, unsafe delete/switch rejection and known Git failure are user-facing. | Return resulting branch information only when useful; otherwise concise acknowledgement. |
| `git_commit` | Empty message/path validation, nothing-to-commit/known Git rejection and hook failure are user-facing. | Preserve commit identity and concise Git summary; do not add generic framing. |
| `git_pull` | Invalid remote/branch and merge-safety/network/auth Git rejection are user-facing. | Preserve concise Git output needed to understand resulting state. |
| `git_push` | Detached HEAD, invalid remote/branch, non-fast-forward/network/auth rejection are user-facing. | Preserve concise remote result; no `RuntimeError:` prefix. |

### 6.6 Memory tools

| Tool | Expected failure treatment | Output treatment |
|---|---|---|
| `write_memory` | Invalid name/path, read-only target and content-size limit are user-facing. | `OK` unless memory-specific follow-up information is required. |
| `read_memory` | Missing/invalid memory and invalid limits are user-facing. | Return memory content directly; retain/paginate large content. |
| `list_memories` | Invalid topic/limit is user-facing. Empty list is normal. | Return names/topics only, compactly. |
| `delete_memory` | Missing/invalid/read-only memory is user-facing. | `OK`. |
| `rename_memory` | Missing source, existing destination, invalid/read-only names are user-facing. | `OK`; do not restate both names unless needed to disambiguate a transformed path. |
| `edit_memory` | Missing/read-only memory, invalid mode/pattern, zero/multiple/ambiguous matches are user-facing. | `OK` on edit; no repeated memory content. |

### 6.7 Retained output and media/file transfer

| Tool | Expected failure treatment | Output treatment |
|---|---|---|
| `read_tool_output` | Unknown/expired output ID, invalid offset/max size are user-facing. | Compact paging schema to essential range/cursor/completeness/content fields. |
| `download_file` | Missing file, invalid/out-of-project path, snapshot/size-limit failure are user-facing. | Preserve native MCP resource link; avoid duplicate textual file metadata unless required. |
| `upload_file` | Invalid destination, existing destination without overwrite, URL/source validation, size/timeout/download failure are user-facing. | Keep destination identity and immutable snapshot reference; audit whether byte count/SHA are useful by default or should be omitted unless needed. |
| `fetch_media_file` | Missing/wrong-type/oversized media and invalid path are user-facing. | Preserve native image/audio media result; no JSON wrapper. |
| `render_pdf_page` | Missing/wrong-type PDF, invalid page/DPI, missing renderer, rendering timeout/failure are user-facing. | Preserve native image result; no duplicate text payload. |

## 7. Other output surfaces to audit

These are not public tools themselves but materially affect token use or error quality.

### 7.1 Tool descriptions and schemas

- Keep descriptions concise but sufficient to steer correct tool choice.
- Do not move important usage constraints out of descriptions merely to save tokens if doing so causes more failed calls.
- Remove response prose only when the schema/description already makes the next action unambiguous.
- Ensure validation constraints are represented in schemas where possible so malformed calls are prevented before execution.

### 7.2 Editing diagnostics

- Preserve newly introduced diagnostics after editing operations; they are high-value immediate feedback.
- Keep empty-diagnostic edits at the trivial success form.
- Audit diagnostics JSON for fields that never affect model action, but do not remove source position, severity, message, diagnostic code/source, or symbol grouping where useful.

### 7.3 Execution/activity state

- Store one bounded result or error representation in `ExecutionStore`.
- Activity and dashboard views should reference that state rather than duplicate serialisation or error transformation.
- Retain execution ID internally for correlation; do not print it into every MCP response unless a concrete recovery/debug workflow requires it.

### 7.4 Server logging

- Remove full routine tool argument/result INFO logging now superseded by execution state.
- Keep concise lifecycle/service logs needed for operations.
- Log one traceback for unexpected failures, LSP/service defects, and background callback failures where the log is the diagnostic source of truth.
- Do not log expected user/domain failures with `exc_info`.
- Avoid logging large source/replacement contents, file payloads, or shell transcripts when the retained-output/execution systems already own them.

### 7.5 Long-running jobs and shell output

- Keep the cursor/delta model for `job_status`; it already avoids repeating full output.
- Retain only bounded transcript data in ordinary tool responses.
- Do not repeat stable job metadata on every delta response.
- Preserve job IDs and next cursors because they are required for continuation.

### 7.6 Native file/media results

- Keep FastMCP native resources/media rather than serialising binary/resource state into model-visible JSON.
- Error text for media operations follows the same concise user-facing contract as text tools.

### 7.7 Control-plane responses

`initial_instructions`, activation messages, and onboarding are intentionally larger than ordinary acknowledgements because they steer future behaviour. Optimise duplication, not their necessary policy content.

## 8. Implementation sequence and checkpoints

### P01 — Error boundary and execution-state cleanup

Status: COMPLETE

Implement E01-E04 together:

- add `UserFacingError`;
- remove `ToolCallError` and obsolete `apply_ex` flags/logging;
- make `SerenaFastMCPTool.run()` the single error presentation boundary;
- persist canonical error strings.

Tests/gate:

- direct existing session/project concurrency tests still pass;
- LSP read-retry/write-non-replay tests still pass;
- timeout/cancellation detached-worker tests still pass;
- one representative `replace_content` failure has a one-line MCP error and identical `ExecutionStore.error`.

Checkpoint before broad tool conversion.

### P02 — File, semantic, and editing tools

Status: COMPLETE

Convert expected failures for sections 6.2 and 6.3, including lower-level `Project`, `ContentReplacer`, symbol/code-editor, and LSP-manager paths where those abstractions own the error meaning.

Normalise trivial edit success output while preserving diagnostics and dry-run/reference evidence.

Checkpoint after focused file/symbol/diagnostics tests.

### P03 — Jobs, shell, Git, and memory

Status: COMPLETE

Convert sections 6.4-6.6.

Pay particular attention to the distinction between:

- shell non-zero exit as normal result;
- Git non-zero exit as a user-facing tool failure;
- systemd/journal operational failure as user-facing where actionable;
- job-store/runtime invariant failure as internal.

Checkpoint after jobs/Git/memory tests.

### P04 — Media, retained output, configuration/workflow

Status: COMPLETE

Convert sections 6.1 and 6.7, then implement E05-E06.

Verify native media/resource results still render correctly in ChatGPT and that exact retained paging remains stable after compaction.

Checkpoint after MCP/media/tool-output tests.

### P05 — Full catalogue audit

Status: COMPLETE

Use the 46-row ledger as a completion checklist. Search the complete active Serena call graph for deliberate generic exceptions and classify every remaining reachable site.

For each remaining `raise ValueError`, `FileNotFoundError`, `FileExistsError`, `PermissionError`, `TimeoutError`, or `RuntimeError` reachable from a public tool, record one of:

- converted expected failure;
- intentionally normal result at a higher layer;
- intentionally internal invariant/defect;
- control-flow exception with dedicated handling.

Do not require converting unrelated CLI/dashboard-only exceptions solely because they use the same Python class.

### P06 — Final response-efficiency audit

Status: COMPLETE

Exercise representative successful calls across all families and inspect the actual MCP payloads for:

- arguments repeated in the result;
- explanatory prose already present in the description;
- duplicate success/result wrappers;
- redundant paging metadata;
- repeated immutable job metadata;
- duplicate execution/activity serialisation;
- large logs that are already retained elsewhere.

Only compact output when the removed material cannot change the next model action.

### P07 — Final validation and checkpoint

Status: COMPLETE

- run focused unit/MCP tests throughout implementation;
- run the complete Serena test suite required by `mem:task_completion`;
- run `git diff --check`;
- run memory consistency check if memories changed during implementation;
- inspect final Git diff against this plan;
- update this document's status and per-phase completion markers;
- create a final local checkpoint before any deployment/restart step.

Validation completed 2026-09-10:

- focused MCP/session-project validation: 49 passed;
- `uv run poe format`: passed;
- `uv run poe type-check`: passed for source and tests;
- `uv run poe test`: 778 passed, 265 deselected;
- `git diff --check`: passed;
- no Serena memories changed, so the memory-consistency check was not required;
- cumulative P01-P07 changes were audited against this plan and the P05/P06 coverage records;
- no deployment or Serena restart was performed.

## 9. Required behavioural tests

At minimum, cover these through the real MCP surface:

1. `replace_content` no match -> exact concise domain error.
2. `replace_content` ambiguous/multiple match -> concise corrective error.
3. missing `read_file` path -> concise file error.
4. invalid `read_tool_output` ID/range -> concise recovery error.
5. unknown/invalid `job_status` ID/arguments -> concise error.
6. shell command non-zero exit -> successful structured result, not ToolError.
7. Git command rejection -> concise ToolError with native Git detail and no `RuntimeError:`.
8. memory missing/existing/read-only cases -> concise domain errors.
9. PDF/media invalid type/page/limit -> concise domain errors.
10. malformed MCP arguments -> compact validation message without input dump/docs URL.
11. LSP termination during read -> restart/retry once and preserve existing success/failure semantics.
12. LSP termination during write -> no replay and concise re-inspection instruction.
13. tool timeout -> concise request timeout while the worker remains live/exclusive until completion.
14. MCP cancellation -> execution remains correctly tracked until detached worker stops.
15. injected internal exception -> one internal traceback, bounded MCP error, bounded canonical `ExecutionStore.error`.
16. large textual result -> stable retained `output_id`, compact truncation header, exact continuation by `read_tool_output`.
17. native media success -> still returned as native media/resource content rather than verbose JSON.
18. diagnostics-bearing edit -> concise success plus actionable new diagnostics.

For expected failures, assert the MCP-visible text does not contain:

```text
Traceback
ToolCallError
Error executing tool
ValueError:
RuntimeError:
FileNotFoundError:
FileExistsError:
```

unless a literal external command message legitimately contains one of those strings as domain data.

## 10. Non-goals

- Do not redesign the execution coordinator or session/project runtime.
- Do not add HTTP-style error codes or a public error taxonomy unless a real client needs machine-readable codes.
- Do not wrap every success in structured JSON.
- Do not hide unexpected failures from Serena's private diagnostic logs.
- Do not remove useful Git stderr, compiler/shell output, diagnostics, diffs, or references simply because they are long; retain/page them instead.
- Do not make expected-error classification depend only on Python exception class.
- Do not change Orchestrator error/output behaviour as part of this Serena plan unless a shared `mcp_runtime` helper is genuinely the single correct home and the Orchestrator contract is explicitly audited too.
- Do not restart/deploy Serena as part of an implementation checkpoint unless the active cutover is explicitly requested.

## 11. Completion criteria

This plan is complete when all of the following are true:

- all 46 active Serena tools have been reviewed against the coverage ledger;
- every expected public failure has concise native MCP presentation without traceback/wrapper chains;
- unexpected failures produce at most one private traceback and bounded external/persisted text;
- `ToolCallError`, obsolete `apply_ex` compatibility flags, and redundant full argument/result INFO logging are removed if no remaining caller requires them;
- `ExecutionStore.error` is the canonical presentation-safe error field used by MCP activity/dashboard views;
- retained-output continuation remains exact while boilerplate and redundant fields are reduced;
- trivial mutation responses are compact without losing diagnostics or identities required for continuation;
- shell/job/Git/media distinctions between normal negative results, operational errors, and internal faults are tested;
- timeout/cancellation and LSP mutation-safety guarantees are unchanged;
- full Serena tests and completion checks pass;
- the final diff is consistent with Serena 2.0's standalone, slim-runtime direction.
