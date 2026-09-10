# Serena Central Result Presentation Plan

Status: C00-C05 COMPLETE (2026-09-10); C06 NEXT

Scope: successful Serena tool results, retained-output paging, MCP transport, `ExecutionStore`, activity/dashboard inspection, and the removal of tool-local presentation truncation.

Current working tree note: there is uncommitted experimental structured-compaction work in `src/serena/structured_output.py`, `execution_store.py`, `tools_base.py`, `symbol_tools.py`, and associated tests. It is useful evidence for the problem but is not the target architecture. Do not build further special cases on top of it; replace or salvage it deliberately during C01-C04.

## 1. Goal

Make every Serena tool produce its complete logical result and move all response-size handling to one central result-presentation layer.

The model and dashboard must see the same canonical presented result for a tool execution. The dashboard may render that result more richly, but it must not independently truncate, compact, expand, summarize, or otherwise change its content.

The target flow is:

```text
tool.apply()
    -> complete logical result
    -> central ResultPresenter
         -> result fits presentation budget
              -> canonical presented result = complete result
         -> result exceeds presentation budget
              -> retain exact complete result
              -> construct one bounded structured preview
              -> attach stable output_id / truncation metadata
    -> persist that exact canonical presented result in ExecutionStore
    -> return that exact canonical presented result through MCP
    -> dashboard renders the persisted canonical result without changing content
```

The principal invariant is:

> A Serena tool result is always complete at the tool boundary. Any omission visible to the caller is introduced exactly once by the central presentation layer, the dashboard displays exactly that same presented content, and the omitted exact result is recoverable through retained output.

## 2. Architectural invariants

1. **Tools return complete logical results.** Tool implementations do not truncate, summarize, tail, compact, or otherwise reduce a result because of transport/display size.
2. **Presentation truncation happens once.** One central component at the MCP result boundary owns response-size enforcement.
3. **Dashboard and model share one presentation.** The canonical presented result is produced once, persisted once, returned to the model, and later rendered by the dashboard. There is no independent dashboard result budget.
4. **Exact output is retained separately.** When presentation truncation occurs, retained output stores an exact deterministic serialization of the complete logical result before any omission.
5. **Structured data stays structured.** Dict/list/dataclass-like results are compacted before serialization and remain valid structured output. Serena does not recover structure by parsing prose-wrapped JSON after the fact.
6. **Natural text stays text.** Full textual results remain strings. If oversized, the central presenter uses an adaptive head/tail preview that consumes the available budget efficiently.
7. **Native media remains native.** MCP images, rendered PDF pages, file/resource links, elicitation, and other native non-text result types bypass ordinary text/JSON compaction while still using the same execution-recording path.
8. **Semantic query limits are not presentation truncation.** Parameters such as file line ranges, `max_matches`, `job_status` cursor/delta semantics, and `read_tool_output` paging may continue to define what the operation asks for. Generic `max_answer_chars`-style display controls do not belong in ordinary tools.
9. **Tool schemas may describe meaning, not perform truncation.** Typed result fields may carry central presentation metadata such as “bulky previewable text” or “identity field”, but the tool only constructs the complete result.
10. **No prose-plus-JSON truncation protocol.** A truncated structured result is one valid structured value, never a header/prose block concatenated with JSON.
11. **One configured presentation budget.** Model and dashboard result content use the same canonical budget. The dashboard solves layout problems with wrapping/virtualisation, not a second content budget.
12. **Tests assert observable equivalence.** The key tests compare the MCP-visible canonical result with the dashboard/activity result for the same execution.

## 3. Canonical result representations

Separate three concepts explicitly.

### 3.1 Logical result

The complete value returned by a tool. Examples:

- `find_symbol`: all requested symbol records and complete requested bodies/info;
- `search_for_pattern`: all requested matches/context;
- `execute_shell_command`: complete `return_code`, stdout and stderr for that foreground command;
- `git_diff`: complete diff produced by Git;
- `read_memory`: complete memory text;
- `job_status`: the complete semantic snapshot/delta requested by that paging operation.

Tools should preferably return native Python structures rather than pre-serialized JSON strings.

### 3.2 Retained result

An exact deterministic serialization of the logical result, written only when the logical result cannot be presented in full within the canonical budget (or when a streaming producer already owns retained output).

Properties:

- stable `output_id`;
- exact character-addressable content for `read_tool_output`;
- no semantic omission;
- survives restart according to the existing session retention policy;
- compression remains an on-disk concern and must not alter paging semantics.

### 3.3 Presented result

The one value visible to both ChatGPT and the dashboard.

If the logical result fits, the presented result is the logical result unchanged.

If it does not fit, return a valid structured envelope such as:

```json
{
  "truncated": true,
  "total_chars": 65902,
  "output_id": "...",
  "result": {
    "...": "bounded structural preview"
  }
}
```

For oversized plain text, `result` is an adaptive text preview. For structured values, `result` remains structured.

The exact field names can be adjusted during implementation, but there must be only one canonical truncated-result shape and it must be shared by every ordinary tool.

## 4. Central presentation policy

The central presenter operates on the logical Python value before presentation serialization.

### 4.1 Fast path

- Normalize only as required for deterministic JSON/MCP compatibility.
- Measure the actual canonical serialized representation.
- If it fits, return it unchanged and do not create retained output merely for convenience.

### 4.2 Oversized plain text

- Retain the complete text.
- Reserve space for truncation metadata.
- Use essentially all remaining budget for the preview.
- Prefer complete head/tail lines for multiline code/logs when possible.
- Fall back to exact character budgeting for single-line or very tight cases.
- Never use fixed 4096/2048/etc. tiers that can leave a large fraction of the available budget unused.

### 4.3 Oversized structured values

Use a deterministic, schema-aware fitting algorithm rather than serializing first and trying to reconstruct meaning.

Default degradation order:

1. preserve the full object/list topology when it fits after shortening only bulky leaf values;
2. preserve identity/location/status/count fields and collection breadth before allocating large previews to verbose fields;
3. distribute available preview space across repeated records rather than spending most of it on the first/last record;
4. reduce bulky text adaptively to the largest representation that fits;
5. if the structural skeleton itself is too large, compact collections with explicit omitted-item counts while keeping representative head/tail entries;
6. only then omit low-priority fields, with explicit omitted-field counts;
7. retain a minimal structured fallback containing truncation metadata if no useful structure can fit.

Use an exact/adaptive fit search (for example binary search over preview budgets) so the returned representation approaches the configured budget without exceeding it.

### 4.4 Presentation metadata

Avoid a global heuristic list such as `_PRIORITY_KEYS` as the main semantic mechanism.

Where generic structure is insufficient, use a small typed metadata vocabulary interpreted centrally, for example:

- identity/essential scalar;
- location;
- bulky previewable text;
- repeated collection item;
- terminal/status field.

This metadata belongs to result types/schema definitions. It does not authorize the tool to truncate its own result.

Use generic heuristics only as a defensive fallback for legacy/untyped values.

## 5. Dashboard/model identity contract

This is stricter than the current implementation.

`ExecutionStore` must not take a returned result and independently run an 8k serialization/compaction pass. Instead:

1. the central presenter creates the canonical presented result once;
2. the exact serialized form of that presentation is persisted on the execution record;
3. the same presentation value is returned through MCP;
4. activity/dashboard code reads the persisted presentation;
5. dashboard formatting may parse JSON and render fields, code blocks, paths, booleans, integers, etc., but must not alter content.

Therefore remove the concept of a separate dashboard result-size budget.

If the dashboard later offers “view full retained result”, that must be an explicit secondary view obtained through the retained `output_id`, clearly distinguished from the default “what the model saw” view.

Arguments are not part of this result-identity contract: the model already supplied them. Their persistence can remain separately bounded if required, provided the dashboard makes clear that argument detail is an execution log rather than a model response.

## 6. Implementation programme

### C00 — Freeze the target contract and baseline the current WIP

- Record the invariants in this document before further code changes.
- Keep the current uncommitted `StructuredOutputCompactor` work only as reference/test evidence.
- Identify which parts are reusable (normalization, line-preserving text preview, exact retained-output tests) and which should be deleted (tool-local `prefer_structured_preview`, fixed size tiers, semantic priority-key heuristics, dashboard-only compaction).
- Capture the current list of `_limit_length()` callers and `max_answer_chars` parameters as the migration ledger.

Completion condition: every subsequent change can be judged against one explicit result contract rather than the current mixed tool/dashboard behaviour.

#### C00 baseline captured 2026-09-10

The target contract is the set of invariants in Sections 1–5 above. C01 onward must be evaluated against those invariants, not against the behaviour of the current experimental compactor.

The pre-C01 working implementation is deliberately left untouched as reference evidence. Its relevant WIP consists of:

- `src/serena/structured_output.py` (untracked `StructuredOutputCompactor`);
- `src/serena/tools/tools_base.py` (structured preview integration in `_limit_length()`);
- `src/serena/tools/symbol_tools.py` (`prefer_structured_preview` for `find_symbol(include_body=True)`);
- `src/serena/execution_store.py` (independent 8k `serialize_for_storage()` compaction);
- `test/serena/test_tool_output.py` and `test/serena/test_activity.py` (behavioural evidence for structured previews, retained exact output, line-preserving text, and dashboard rendering).

Reusable concepts/evidence to salvage deliberately in C01–C04/C08:

- deterministic normalization of arbitrary Python values into JSON/MCP-safe values;
- multiline head/tail text previewing that prefers complete lines and falls back to exact character budgeting;
- exact retained-output recovery and stable `output_id` tests;
- tests demonstrating that structured output should remain machine-readable and that dashboard rich rendering should parse the same presented value.

Parts explicitly superseded by the target architecture and therefore candidates for deletion/replacement rather than extension:

- fixed `_STRING_LIMITS`, `_LIST_LIMITS`, and `_DICT_LIMITS` degradation tiers;
- `_PRIORITY_KEYS` as the primary semantic preservation mechanism;
- decoding JSON-looking strings in order to reconstruct structure after a tool has serialized its result;
- `prefer_structured_preview` and other tool-local presentation selection;
- `shortened_result_factories` and size-driven tool-specific summary closures;
- `ExecutionStore.serialize_value()` independently compacting successful results to 8k;
- any dashboard-only result compaction distinct from the model-facing presentation.

The exact migration ledger for `_limit_length()` callers and `max_answer_chars` parameters is frozen in Section 7.

### C01 — Introduce one central result-presentation abstraction

Create one component owned by the MCP/execution boundary, provisionally `ResultPresenter` / `ToolResultPresenter`.

Responsibilities:

- accept the complete logical tool result plus execution/tool identity;
- recognize native media/resource results that bypass ordinary compaction;
- deterministically normalize/serialize ordinary results;
- decide whether retention is required;
- retain the exact complete result when required;
- construct the one canonical bounded presentation;
- return a small presentation object containing the transport value, persisted serialization, and retained-output metadata required by execution bookkeeping.

Do not put this behaviour back into `Tool` or individual tools.

Completion condition: a synthetic full text result and a synthetic structured result can both pass through one central presenter and yield exact retained output plus one bounded presentation.

#### C01 completion captured 2026-09-10

- Added `ToolResultPresenter` and immutable `ToolResultPresentation` in `src/serena/result_presentation.py` as the single boundary-owned abstraction for successful-result presentation.
- The presenter accepts logical result plus tool/execution identity, bypasses native MCP media/resource results, normalizes ordinary values deterministically, retains exact oversized logical serializations through `ToolOutputStore`, and returns one bounded canonical transport value together with its persisted serialization and retained-output metadata.
- C01 deliberately reuses `StructuredOutputCompactor` only as a transitional structured fitter; replacing its fixed-tier/priority-key algorithm remains C04.
- Added focused behavioural tests for oversized text, oversized structured data, small unretained results, and native resource bypass. The retained text/JSON is recovered exactly and bounded structured output remains machine-readable.
- No MCP execution path was rewired and no old tool-local truncation path was removed; those changes remain C02 onward as planned.

### C02 — Move presentation to the MCP boundary

Refactor `SerenaFastMCPTool.run()` so the successful worker result remains the complete logical result until the worker has finished.

After the tool returns:

1. perform any internal metadata extraction that must inspect the logical result (for example durable job identity);
2. pass the logical result through the central presenter exactly once;
3. persist the resulting canonical presentation;
4. return that same presentation through MCP.

Preserve timeout, cancellation, `UserFacingError`, native `ToolError`, elicitation, execution coordinator, and LSP recovery semantics unchanged.

Review `Tool.prepare_mcp_result()` and FastMCP conversion ordering so the value persisted for dashboard inspection is semantically identical to what FastMCP exposes to the model. Add an integration test at the actual MCP boundary rather than assuming conversion is transparent.

Completion condition: ordinary tools no longer need `_limit_length()` for correctness; central presentation can be exercised through `SerenaFastMCPTool.run()`.

#### C02 completion captured 2026-09-10

- `SerenaFastMCPTool` now leaves `tool.apply_ex()` results logical through worker completion and invokes `ToolResultPresenter` once at the MCP execution boundary before transport conversion.
- `Tool.prepare_mcp_result()` now runs after central presentation, preserving native media/resource conversion while preventing ordinary results from being serialized before the presenter sees them.
- FastMCP output schemas are extended with the canonical truncation-envelope alternative, and truncated ordinary results are converted directly to matching text plus structured content rather than being revalidated against their original return type.
- Durable-job/media bookkeeping metadata is extracted from the complete logical result before presentation and passed separately through execution finalization, so omitted preview content cannot break job association or native-media persistence.
- Existing retained output for the same execution is reused when present, avoiding a second retained blob while the legacy tool-local path still coexists during migration.
- Added an integration test through FastMCP's low-level `tools/call` handler. It verifies output-schema validation, equality between text/structured MCP views, exact retained recovery, retained metadata on the execution, and semantic equality with the persisted result.
- Timeout, cancellation, user-facing/native MCP errors, durable-job association, activity tracking, and native media behaviour remain covered by the existing focused MCP/session/activity suite.
- C03 still owns removal of the execution-store reserialization/8k compaction pass and byte-for-byte canonical result persistence; it should preserve the explicit metadata handoff established here.

### C03 — Make `ExecutionStore.result` the canonical presented result

- Remove result compaction from `ExecutionStore.serialize_value()` or split argument/error serialization from result persistence so successful results are not reprocessed.
- Update `ActivityTracker.finish_tool()` to accept the already-presented result/serialization rather than serializing the logical result again.
- Preserve the explicit durable-job/media metadata handoff introduced in C02 so activity bookkeeping never reparses a truncated presentation.
- Ensure retained-output ID/character count recorded on the execution corresponds to the exact logical result retained by the presenter.
- Keep error persistence on the existing canonical error path; this plan is about successful result presentation.

Completion condition: the execution record contains exactly the canonical result presentation produced by C01/C02, byte-for-byte for textual persistence.

#### C03 completion captured 2026-09-10

- `ExecutionStore.serialize_value()` was split into the explicitly auxiliary `serialize_auxiliary_value()` path, which remains only for bounded arguments/errors; successful result persistence no longer passes through the execution-store compactor.
- `ActivityTracker.finish_tool()` now accepts the already-presented serialization plus retained-output metadata and explicit pre-presentation durable-job identity/label and media metadata. Activity bookkeeping no longer reparses or reserializes a successful result, and `ExecutionStore.finish_execution()` no longer falls back to regex job-ID extraction from persisted result text.
- `SerenaFastMCPTool.run()` now persists `ToolResultPresentation.persisted_serialization` directly and records the presentation's retained-output ID/character count on the same `finish_execution()` call, both with and without an activity tracker.
- The successful path no longer performs the old post-hoc execution-output lookup. Failure handling retains that lookup only for outputs created before a worker fails, preserving existing error semantics.
- The manually registered `show_activity` tool now also uses `ToolResultPresenter` for successful persistence instead of the auxiliary execution serializer.
- Transitional tool-local retained output is carried through `ToolResultPresentation` even when the legacy bounded result is already below the central budget, preserving exact recovery metadata until C05 removes those local truncation paths.
- Behavioural coverage now asserts byte-for-byte equality between canonical presenter serialization and `ExecutionStore.result`, exact retained-output identity/counts at the MCP boundary, and presentation-preserving dashboard detail decoding.
- Validation completed with `uv run poe format`, `uv run poe type-check`, and `uv run poe test` (`793 passed, 265 deselected`).

### C04 — Implement robust central fitting

Replace the experimental fixed-tier compactor with the final central algorithm.

Required behaviours:

- exact budget compliance for every positive budget;
- high budget utilisation rather than 4096-character plateaus;
- valid structured output at every structured truncation stage;
- adaptive multiline head/tail text previews;
- preservation of collection breadth and essential fields before verbose leaves;
- explicit omitted item/field markers;
- deterministic output for the same logical value and budget;
- no mutation of the logical result;
- graceful fallback for unknown objects via JSON-safe normalization.

Test with adversarial values: very small budgets, Unicode, deeply nested mappings/lists, huge single strings, many medium strings, many symbol-like records, enormous stdout/stderr, and already-JSON-looking strings.

Completion condition: the presenter uses the available budget efficiently and never needs to parse prose-wrapped JSON.

#### C04 completion captured 2026-09-10

- Replaced the fixed `_STRING_LIMITS` / `_LIST_LIMITS` / `_DICT_LIMITS` fitter with adaptive binary-search fitting over the actual serialized size.
- Bulky text leaves now share preview capacity fairly across repeated records; multiline previews retain head/tail context and prefer complete lines without leaving fixed-size plateaus.
- Collection topology is preserved while possible, then lists are compacted with representative entries and explicit `_serena_omitted_items` markers; mapping fields are omitted only afterwards with explicit `_serena_omitted_fields` markers.
- Removed the global semantic `_PRIORITY_KEYS` list from central fitting. Generic fallback ranking now preserves compact scalar/small structural fields ahead of bulky leaves without depending on tool-specific field names.
- Structured values remain JSON-safe throughout fitting, JSON-looking logical strings remain text rather than being reparsed by the presenter, unknown values normalize safely, and the logical result is never mutated.
- Pathological positive budgets are bounded rather than raising when the full retained-output metadata envelope cannot physically fit; normal budgets continue to use the canonical structured truncation envelope.
- Added adversarial presenter coverage for tiny budgets, Unicode, deep nesting, symbol-like repeated records, large stdout/stderr-style text, many medium strings, omission markers, deterministic fitting, high budget utilisation, JSON-looking strings, unknown objects, and non-mutation.
- Validation completed with `uv run poe format`, `uv run poe type-check`, and `uv run poe test` (`798 passed, 265 deselected`).

### C05 — Migrate tools to complete native results

Remove presentation-size handling from tools family by family.

#### C05a — Symbolic/semantic queries

- `get_symbols_overview`;
- `find_symbol`;
- references;
- implementations;
- declarations;
- symbol/reference support paths.

Return complete native dict/list structures rather than JSON strings where practical. Remove shortened-result factories and `prefer_structured_preview`. Preserve semantic `max_matches` because it constrains the query rather than the transport presentation.

Ensure requested `body` and `info` fields are always complete in the logical result.

#### C05b — File/search queries

- `read_file` returns complete requested text;
- `list_dir` / `find_file` return complete native structures;
- `search_for_pattern` returns complete requested match structures/context;
- remove `make_first_lines_*`, line-number/count summaries, and other size-driven degradation factories.

Retain line ranges/globs/etc. because they are query semantics.

#### C05c — Memories/configuration

Return complete logical text/structures and remove ordinary tool-level presentation limits.

#### C05d — Git and foreground shell

- Git tools return complete Git result text/structures;
- foreground shell returns complete structured `return_code` plus non-empty stdout/stderr;
- remove result-size truncation from those tools;
- retain streaming capture internally where necessary to avoid pipe deadlock, but that is execution mechanics, not presentation truncation.

#### C05e — Durable jobs

Preserve the semantic journal/cursor model. A `job_status` call returns the complete snapshot/delta defined by that operation, and the central presenter controls only the size of that tool response.

Remove generic presentation `max_answer_chars` controls from job tool schemas. Keep cursor/output selection because they define which logical page/delta is requested.

#### C05f — Edit/mutation results

Most are already naturally small. Return their complete concise success structures/strings without calling `_limit_length()` as defensive boilerplate.

Completion condition: no ordinary Serena tool performs response-size truncation or creates retained output merely because its result is large.

#### C05 completion captured 2026-09-10

- Migrated symbolic/semantic queries to complete native dict/list results, retaining only semantic `max_matches`; requested bodies, info, references, implementations, declarations, and diagnostics now reach the central presenter without tool-local shortening.
- Migrated file/search queries so `read_file` returns the complete requested text and `list_dir`, `find_file`, and `search_for_pattern` return complete native structures; removed search/listing degradation factories and edit-listing truncation.
- Migrated memories and current configuration to complete logical results, Git to complete successful command output, and foreground shell execution to a complete native `return_code`/`stdout`/`stderr` result without tool-local retained-output creation.
- Migrated `job_status` to return the complete native snapshot/delta selected by its journal/cursor semantics; cursor/output selection remains semantic rather than presentational.
- Removed defensive `_limit_length()` use from edit diagnostics. No ordinary tool now calls `_limit_length()`, `retain_tool_output()`, `open_tool_output()`, or `render_tool_output_tail()`; the legacy helper/schema parameters remain only for C06 removal.
- Updated behavioural tests to assert complete tool-level results and central MCP-boundary retention/paging rather than tool-local truncation.
- Validation completed with `uv run poe format`, `uv run poe type-check`, and `uv run poe test` (`798 passed, 265 deselected`).

### C06 — Remove presentation controls from ordinary tool schemas

After C05, remove generic `max_answer_chars` parameters whose only purpose is model/dashboard result size.

Keep only parameters that are semantically part of the operation, notably:

- `read_tool_output.max_chars` because explicit retained-output paging is the tool's purpose;
- file start/end lines;
- search/query scope controls such as `max_matches` where they alter the requested computation;
- job cursor/delta controls.

Update ChatGPT policy/tool descriptions so callers rely on retained `output_id` when central presentation truncates a result rather than trying to pre-budget ordinary tools.

Completion condition: tool schemas no longer expose the implementation detail of Serena's presentation budget.

### C07 — Make dashboard rendering presentation-preserving

- Delete the dashboard-only 8k result compaction path.
- Make `get_activity_detail` expose the persisted canonical presented result exactly.
- Keep `structured_result` only as a lossless parsed rendering of that exact persisted result; do not derive a different compact value.
- Ensure rich rendering of `stdout`, `stderr`, `body`, code, paths, integers, booleans and nested structures changes only layout/style.
- Ensure code/output blocks wrap or otherwise remain usable without silently clipping or dropping content.
- If rendering performance requires DOM virtualisation/collapsing, preserve all canonical content and make expansion local to the UI.

Add one visible marker/control for centrally truncated results so the dashboard can clearly expose the `output_id` and, later if desired, an explicit full-retained-output view.

Completion condition: inspecting a tool call on the dashboard shows the same fields, omission markers, and preview text that the model received.

### C08 — Behavioural equivalence and retention tests

Add tests around externally observable guarantees rather than internal compactor structure.

Minimum cases:

1. small structured result: model and dashboard both receive the complete value;
2. large structured result: both receive exactly the same bounded structured envelope;
3. large plain text result: both receive exactly the same adaptive head/tail preview;
4. exact retained result is recoverable byte-for-byte via `read_tool_output`;
5. later tool calls do not change an earlier `output_id`;
6. retained result survives Serena restart according to policy;
7. multi-record symbol output preserves useful identities across the collection before large bodies;
8. `include_body` and `include_info` logical results are complete before presentation;
9. search results remain structured rather than prose-plus-JSON;
10. stdout/stderr remain typed fields and retain `return_code`;
11. media/resource results remain native and dashboard behaviour is unchanged;
12. tiny budgets never overflow or emit invalid JSON;
13. Unicode character paging remains character-correct;
14. dashboard result serialization equals the canonical MCP presentation for the same execution.

Where feasible, capture the MCP result and execution/dashboard detail from one actual execution and assert equality directly rather than reconstructing expected values independently.

Completion condition: tests make it impossible to reintroduce separate model and dashboard truncation paths unnoticed.

### C09 — Cleanup, full audit, and cutover

- Remove `_limit_length()` and `_effective_max_answer_chars()` when no ordinary caller remains.
- Remove `shortened_result_factories` and their tool-local summary closures.
- Remove the experimental `prefer_structured_preview` path.
- Delete obsolete JSON-string decode/recovery code whose only purpose was reconstructing structure after tool serialization.
- Remove any dashboard result-size constants/logic superseded by the canonical presentation.
- Audit all public tools to ensure no hidden result truncation remains.
- Audit `ExecutionStore`, activity, custom dashboard, MCP conversion, tool output retention, jobs, native media, and restart rehydration together.
- Run formatting, type checking, complete relevant test suite, `git diff --check`, and inspect the final Git diff according to `mem:task_completion`.
- Restart Serena only after the final checkpoint is clean and model/dashboard equivalence has been verified against the running dashboard.

Completion condition: one result-presentation path is authoritative in both code and observable behaviour.

## 7. Migration ledger

Baseline captured for C00 on 2026-09-10. There are **19 `_limit_length()` call sites** in `src/serena/tools` that C05/C06 must remove or deliberately reclassify before `_limit_length()` itself is deleted:

- configuration: `GetCurrentConfigTool.apply` (1);
- jobs: `JobStatusTool.apply` list path and single-job snapshot/delta path (2);
- symbolic/semantic: `GetSymbolsOverviewTool.apply`, `FindSymbolTool.apply`, `FindReferencingSymbolsTool.apply`, `FindImplementationsTool.apply`, `FindDeclarationTool.apply`, `GetDiagnosticsForFileTool.apply` (6);
- foreground shell: `ExecuteShellCommandTool.apply` (1);
- memories: `ReadMemoryTool.apply`, `ListMemoriesTool.apply` (2);
- Git: `_GitTool._run_git` (1), shared by all public Git tools;
- file/search/edit listing: `ReadFileTool.apply`, `ListDirTool.apply`, `FindFileTool.apply`, `ReplaceInFilesTool._render_listing`, `SearchForPatternTool.apply` (5);
- edit diagnostics support: `EditingToolWithDiagnostics.DiagnosticsContext.format_result` (1).

There are **24 public tool-schema `max_answer_chars` parameters** whose presentation-only role must be removed in C06 unless the migration establishes a genuine semantic purpose:

- `GetCurrentConfigTool`;
- `JobStatusTool`;
- `GetSymbolsOverviewTool`, `FindSymbolTool`, `FindReferencingSymbolsTool`, `FindImplementationsTool`, `FindDeclarationTool`, `GetDiagnosticsForFileTool`;
- `ExecuteShellCommandTool`;
- `ReadMemoryTool`, `ListMemoriesTool`;
- `GitStatusTool`, `GitFetchTool`, `GitLogTool`, `GitDiffTool`, `GitBranchTool`, `GitCommitTool`, `GitPullTool`, `GitPushTool`;
- `ReadFileTool`, `ListDirTool`, `FindFileTool`, `ReplaceInFilesTool`, `SearchForPatternTool`.

There are **8 additional internal `max_answer_chars` parameters** outside public tool schemas. Four are ordinary-tool presentation helpers that should disappear or change role with the central presenter: `_GitTool._run_git`, `ReplaceInFilesTool._render_listing`, `Tool._effective_max_answer_chars`, and `Tool._limit_length`. Four are retained-output plumbing that C01 should deliberately reuse, rename, or supersede rather than deleting mechanically: `SerenaAgent.retain_tool_output_with_tail`, `SerenaAgent.render_tool_output_tail`, `ToolOutputStore.retain_with_tail`, and `ToolOutputStore.render_tail`. This gives **32 source-level `max_answer_chars` parameters in total** at the C00 baseline: 24 public and 8 internal.

Configuration documentation also still describes explicit `max_answer_chars` overrides in `src/serena/config/serena_config.py` and `src/serena/resources/serena_config.template.yml`; C06 must update that wording when the public parameters are removed.

The current experimental WIP additionally introduces `StructuredOutputCompactor`, `prefer_structured_preview` on `find_symbol(include_body=True)`, and dashboard/execution-store compaction through `serialize_for_storage()`. These are specifically superseded by C01-C04/C07 and must not survive merely because their current tests pass.

`read_tool_output.max_chars` is intentionally **not** in this removal ledger: explicit retained-output paging is that tool's semantic operation. Likewise, `max_matches`, file line ranges, and job cursor/output/wait controls remain semantic query controls rather than presentation budgets.

## 8. Checkpoint order

Recommended implementation checkpoints:

1. **C01-C02:** central presenter exists and MCP uses it for synthetic/current outputs;
2. **C03-C04:** canonical persistence plus robust fitting, with model/dashboard equality tests;
3. **C05a-C05c:** structured query/file/memory/config migration;
4. **C05d-C05f:** shell/Git/jobs/mutations migration;
5. **C06-C07:** remove per-tool presentation parameters and dashboard-only compaction;
6. **C08-C09:** full behavioural audit, cleanup, test suite, restart/cutover.

Do not remove the old tool-level path until the central presenter has end-to-end tests through `SerenaFastMCPTool.run()`. Once the first migrated family is proven, migrate remaining families directly rather than maintaining two long-lived presentation systems.

## 9. Definition of done

The work is complete only when all of the following are true:

- every ordinary tool returns its complete logical result before the MCP boundary;
- no ordinary tool performs response-size truncation or builds size-driven summaries;
- one central presenter owns presentation-size enforcement;
- any truncated result has one stable retained `output_id` for the exact logical result;
- structured results remain structured and valid after central compaction;
- plain text uses the available budget efficiently;
- `ExecutionStore.result` is the canonical model-facing presented result, not a separately compacted dashboard representation;
- the dashboard shows exactly the same result content as the model for the same execution;
- semantic paging/query controls remain only where they define the requested operation;
- generic `max_answer_chars` controls are removed from ordinary tool schemas;
- model/dashboard equivalence, retained-output recovery, restart retention, native media, shell, search, symbols and tight-budget behaviour are covered by tests;
- obsolete experimental compaction and tool-local truncation machinery are deleted;
- format, type-check, tests and final diff audit pass before Serena is restarted.
