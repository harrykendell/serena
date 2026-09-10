# Serena MCP P06 response-efficiency audit

P06 audits successful model-visible responses after P01-P05. The criterion is not minimum byte count in isolation: output is removed only when it cannot change the model's next action. The review covers all 46 public tools through the section 6 coverage families, their FastMCP transport boundary, retained output, activity persistence, and routine logging.

## Changes made

### Foreground shell output

`execute_shell_command` previously returned the resolved working directory and an empty `stderr` field on every ordinary successful call. Both restated known state in the common case.

The compact contract is now:

- always return `return_code`;
- include `stdout` only when non-empty;
- include `stderr` only when non-empty;
- do not echo `cwd` in the ordinary MCP result;
- for retained oversized transcripts, keep only `return_code` as header detail because the full transcript is already retained under its stable output ID.

A command with no output and exit status 7 therefore returns `{"return_code": 7}` rather than a four-field object containing empty streams and the project path.

### Durable jobs

The job family contained the largest remaining repetition. `start_job` and initial `job_status` responses repeated immutable job arguments, project roots, null timestamps/result fields, persistence flags plus a prose summary of those flags, and prose `next_step` instructions already present in tool descriptions. Status responses also emitted every paging condition as `false` on the normal path.

P06 keeps the fields that change continuation behaviour:

- `start_job`: `job_id`, current `status`, `running_jobs`, and `max_concurrent_jobs`;
- one-shot `job_status`: recoverable job identity/state, relevant timestamps/result state, non-null runtime telemetry, bounded output, and `next_cursor` when available;
- cursor-based `job_status`: job ID/state plus new output, telemetry, terminal result state when reached, and navigation/exception conditions only when they are active;
- job listing: recoverable identity/state for recent jobs, runtime telemetry only for still-running jobs, and concurrency usage;
- `cancel_job`: resulting job identity/state, with return/status detail only when it carries additional terminal information.

Persistence behaviour and normal polling guidance remain in the public tool descriptions instead of being serialized into every result. `status_message` is retained only for cases where status/return code is insufficient, such as a timeout or a failed job with no process exit code. Non-default job working directories remain visible in recoverable status/listing data because they can materially identify the execution context.

### Duplicate response wrapper

The unused `Tool._wrapped_tool_response()` helper, which could construct a redundant `{message, response}` success envelope, had no references and was removed. Public successes continue to use their natural string, JSON, or native MCP media/resource shape.

## Reviewed families with no further compaction

### Configuration and workflow

`activate_project`, `initial_instructions`, and `onboarding` are intentionally control-plane-heavy: their content changes later tool selection and project behaviour. `get_current_config` contains runtime/project/tool facts requested by the caller. `open_dashboard` retains the URL plus whether it opened automatically or must be opened manually; that status changes the user's next action.

### File and text

Reads return file content directly. Trivial mutations return `OK` (plus overwrite or newly introduced diagnostics when relevant). Directory listings distinguish directories from files. `find_file` returns only matched paths. `replace_in_files` retains occurrence IDs/minimal diffs for dry runs and per-file counts for applied multi-file edits. Pattern search retains match locations/context and delegates genuinely large results to retained output.

### Semantic and symbolic

Symbol queries return only requested identity/location/body/info data. Reference/declaration/implementation evidence is needed to choose the next source operation. Editing tools use `OK` plus actionable new diagnostics. Rename retains the applied-change count; safe delete retains references only when they block deletion.

### Git

Git tools return native concise Git output or `OK`; diffs/logs are paged rather than summarized away. Commit identities, branch state, and rejection/output details remain decision-relevant.

### Memories

Memory mutations use `OK` except when rename reference updates add meaningful state. Reads return content directly. Memory listing keeps writable versus read-only grouping because that affects whether a subsequent mutation is valid.

### Retained output and native media

P04 already reduced `read_tool_output` to stable ID, total/range/cursor/completeness/content fields and compacted truncation headers. File downloads use native resource links; fetched media and rendered PDF pages remain native MCP media. Upload keeps the destination identity plus immutable source snapshot reference required for later provenance/recovery.

### Execution/activity and logging

`SerenaFastMCPTool.run()` creates one authoritative execution record. `ActivityTracker` appends/views that same record rather than creating a second execution or separately reserializing an error presentation. Successful results are serialized once for execution/activity persistence and returned naturally through MCP; there is no additional success envelope.

Routine complete tool arguments/results are no longer INFO-logged after P01. Remaining tool logging is lifecycle/recovery information rather than a second copy of large source, shell, search, or diff payloads.

## Validation focus

Behaviour tests cover the compact shell payload, compact job start/status/delta/list/cancel payloads, retained-output continuation, and the existing native media/resource contracts. P07 remains responsible for the complete final suite, plan bookkeeping, final diff audit, and final implementation checkpoint/cutover decision.
