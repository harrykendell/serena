"""Unattended delegate-provider execution for Orchestrator."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from mcp_runtime.shell_environment import user_shell_environment
from orchestrator.config import OrchestratorConfig
from orchestrator.delegates import DelegateError, DelegateKind, DelegateProviderTask, DelegateState, DelegateStore, ProviderPolicy
from orchestrator.worktrees import WorktreeAllocation, WorktreeError, WorktreeManager

_MAX_ERROR_CHARS = 500
_MAX_LOG_TAIL_CHARS = 2_000


class DelegateProvider(Protocol):
    """Defines the lifecycle surface required from an unattended provider."""

    @property
    def policy(self) -> ProviderPolicy:
        """Returns the provider policy implemented by this provider."""
        ...

    def start(self, delegate_id: str) -> None:
        """Queues one persisted delegate for unattended execution."""
        ...

    def cancel(self, delegate_id: str) -> bool:
        """Cancels queued/running provider execution when it is still owned locally."""
        ...

    def availability_for_auto(self) -> ProviderAvailability:
        """Returns whether policy allows this provider to receive an automatic fallback."""
        ...


@dataclass(frozen=True)
class ProviderAvailability:
    """Describes whether an unattended provider may receive automatic fallback work."""

    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class ProviderUsage:
    """Stores bounded token accounting parsed from provider event output."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None


@dataclass(frozen=True)
class CodexEventSummary:
    """Stores bounded metadata parsed from a Codex JSONL event stream."""

    event_count: int
    invalid_event_count: int
    usage: ProviderUsage
    model: str | None
    reasoning_effort: str | None


class CodexJsonlParser:
    """Parses Codex JSONL without exposing raw provider events to model-visible status."""

    _TOKEN_KEYS = {
        "input_tokens": "input_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "cache_write_input_tokens": "cache_write_input_tokens",
        "output_tokens": "output_tokens",
        "reasoning_output_tokens": "reasoning_output_tokens",
    }

    def parse(self, path: Path) -> CodexEventSummary:
        """Returns usage/model metadata from one private Codex event log."""
        latest_usage: dict[str, int] = {}
        model: str | None = None
        reasoning_effort: str | None = None
        event_count = 0
        invalid_event_count = 0

        try:
            stream = path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return CodexEventSummary(0, 0, ProviderUsage(), None, None)

        with stream:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    invalid_event_count += 1
                    continue
                if not isinstance(event, dict):
                    invalid_event_count += 1
                    continue
                event_count += 1
                for mapping in self._mappings(event):
                    for source_key, target_key in self._TOKEN_KEYS.items():
                        value = mapping.get(source_key)
                        if isinstance(value, int) and value >= 0:
                            latest_usage[target_key] = max(latest_usage.get(target_key, 0), value)
                    if model is None and isinstance(mapping.get("model"), str):
                        model = mapping["model"]
                    for key in ("reasoning_effort", "model_reasoning_effort"):
                        value = mapping.get(key)
                        if reasoning_effort is None and isinstance(value, str):
                            reasoning_effort = value

        return CodexEventSummary(
            event_count=event_count,
            invalid_event_count=invalid_event_count,
            usage=ProviderUsage(**latest_usage),
            model=model,
            reasoning_effort=reasoning_effort,
        )

    def _mappings(self, value: Any) -> list[dict[str, Any]]:
        """Returns nested mappings that may contain provider metadata or usage."""
        mappings: list[dict[str, Any]] = []
        if isinstance(value, dict):
            mappings.append(value)
            for nested in value.values():
                mappings.extend(self._mappings(nested))
        elif isinstance(value, list):
            for nested in value:
                mappings.extend(self._mappings(nested))
        return mappings


class CodexCliProvider:
    """Runs bounded Codex CLI delegates with Orchestrator-owned concurrency and Git isolation."""

    def __init__(self, config: OrchestratorConfig, store: DelegateStore) -> None:
        self._config = config
        self._store = store
        self._worktrees = WorktreeManager(config)
        self._events = CodexJsonlParser()
        self._executor = ThreadPoolExecutor(max_workers=config.codex_concurrency, thread_name_prefix="orchestrator-codex")
        self._lock = threading.RLock()
        self._futures: dict[str, Future[None]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()

    @property
    def policy(self) -> ProviderPolicy:
        """Returns the provider policy implemented by this runner."""
        return ProviderPolicy.CODEX

    def availability_for_auto(self) -> ProviderAvailability:
        """Returns whether Codex is currently eligible for automatic fallback work."""
        environment = user_shell_environment()
        executable = shutil.which(self._config.codex_executable, path=environment.get("PATH"))
        if executable is None:
            return ProviderAvailability(False, f"Codex executable is unavailable: {self._config.codex_executable}")

        budget = self._config.codex_auto_token_budget
        if budget is not None:
            used = self._store.total_codex_tokens()
            if used >= budget:
                return ProviderAvailability(False, f"Codex automatic token budget exhausted ({used}/{budget}).")
        return ProviderAvailability(True)

    def start(self, delegate_id: str) -> None:
        """Queues one Codex delegate behind the Orchestrator-only concurrency cap."""
        with self._lock:
            existing = self._futures.get(delegate_id)
            if existing is not None and not existing.done():
                return
            self._cancelled.discard(delegate_id)
            try:
                future = self._executor.submit(self._run, delegate_id)
            except RuntimeError as exc:
                self._store.fail(delegate_id, f"Codex provider could not queue work: {exc}")
                return
            self._futures[delegate_id] = future
            future.add_done_callback(lambda completed, target=delegate_id: self._forget_future(target, completed))

    def cancel(self, delegate_id: str) -> bool:
        """Cancels owned execution and waits briefly for provider cleanup to finish."""
        with self._lock:
            future = self._futures.get(delegate_id)
            process = self._processes.get(delegate_id)
            owns_queued = future is not None and not future.done()
            owns_process = process is not None and process.poll() is None
            if not owns_queued and not owns_process:
                return False
            self._cancelled.add(delegate_id)
            cancelled_while_queued = future.cancel() if owns_queued and future is not None else False

        terminated_process = False
        if owns_process and process is not None:
            self._terminate_process(process)
            terminated_process = True

        if future is not None:
            try:
                future.result(timeout=3.0)
            except (CancelledError, FutureTimeoutError):
                pass
            except Exception:
                # provider failures are persisted by the worker; cancellation itself stays idempotent
                pass
        return terminated_process or cancelled_while_queued

    def _run(self, delegate_id: str) -> None:
        """Executes one queued Codex delegate and persists only its bounded structured hand-back."""
        allocation: WorktreeAllocation | None = None
        metadata: dict[str, Any] = {"provider": "codex"}
        try:
            provider_task = self._store.provider_task(delegate_id)
            task = provider_task.task

            # establish a safe execution directory before moving the delegate to RUNNING_CODEX
            if task.project_root is None:
                raise WorktreeError("Codex requires an explicit project_root.")
            if task.kind == DelegateKind.CODE:
                allocation = self._worktrees.allocate(delegate_id, task.project_root, task.base_revision)
                cwd = allocation.working_directory
                metadata.update(
                    {
                        "worktree": str(allocation.worktree),
                        "branch": allocation.branch,
                        "base_revision": allocation.base_revision,
                        "live_checkout_dirty": allocation.live_checkout_dirty,
                    }
                )
                if allocation.live_checkout_dirty:
                    metadata["warning"] = "The live checkout had uncommitted changes; Codex ran from the committed base revision only."
            else:
                cwd = Path(task.project_root).expanduser().resolve()
                if not cwd.is_dir():
                    raise WorktreeError(f"Codex project_root does not exist or is not a directory: {cwd}")

            if self._is_cancelled(delegate_id):
                if allocation is not None:
                    self._worktrees.release(allocation)
                return

            log_dir = self._config.codex_logs_dir / delegate_id
            log_dir.mkdir(parents=True, exist_ok=True)
            schema_path = log_dir / "result.schema.json"
            result_path = log_dir / "result.json"
            events_path = log_dir / "events.jsonl"
            stderr_path = log_dir / "stderr.log"
            schema_path.write_text(json.dumps(provider_task.result_json_schema, ensure_ascii=False), encoding="utf-8")
            metadata.update(
                {
                    "event_log": self._relative_state_path(events_path),
                    "stderr_log": self._relative_state_path(stderr_path),
                    "model": self._config.codex_model,
                    "reasoning_effort": self._config.codex_reasoning_effort,
                    "sandbox": "workspace-write" if task.kind == DelegateKind.CODE else "read-only",
                }
            )

            started = self._store.start_codex(
                delegate_id,
                worktree=str(allocation.worktree) if allocation is not None else None,
                provider_metadata=metadata,
            )
            if started.state == DelegateState.CANCELLED:
                if allocation is not None:
                    self._worktrees.release(allocation)
                return
            if self._is_cancelled(delegate_id):
                return

            # run Codex non-interactively with JSONL/private logs and schema-constrained final output
            command = self._command(task, cwd, schema_path, result_path)
            prompt = self._prompt(provider_task, allocation)
            started_at = time.monotonic()
            with events_path.open("w", encoding="utf-8") as events_stream, stderr_path.open("w", encoding="utf-8") as stderr_stream:
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=cwd,
                        stdin=subprocess.PIPE,
                        stdout=events_stream,
                        stderr=stderr_stream,
                        text=True,
                        env=user_shell_environment(),
                        start_new_session=True,
                    )
                except OSError as exc:
                    metadata["elapsed_seconds"] = time.monotonic() - started_at
                    self._store.update_provider_metadata(delegate_id, metadata)
                    self._store.fail(delegate_id, f"Codex executable could not be started: {exc}")
                    return

                cancelled_after_launch = self._register_process(delegate_id, process)
                if cancelled_after_launch:
                    self._terminate_process(process)
                try:
                    process.communicate(input=prompt, timeout=self._config.codex_timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._terminate_process(process)
                    metadata = self._final_metadata(metadata, events_path, time.monotonic() - started_at, process.returncode)
                    self._store.timeout_codex(
                        delegate_id,
                        f"Codex exceeded the {self._config.codex_timeout_seconds:g}-second execution timeout.",
                        metadata,
                    )
                    return
                finally:
                    self._unregister_process(delegate_id, process)

            metadata = self._final_metadata(metadata, events_path, time.monotonic() - started_at, process.returncode)
            if self._is_cancelled(delegate_id):
                self._store.update_provider_metadata(delegate_id, metadata)
                return
            if process.returncode != 0:
                metadata["stderr_tail"] = self._tail(stderr_path)
                self._store.update_provider_metadata(delegate_id, metadata)
                self._store.fail(delegate_id, self._failure_message(process.returncode, metadata["stderr_tail"]))
                return

            result = self._read_result(result_path)
            if allocation is not None:
                result = self._augment_code_result(result, allocation)
            self._store.complete_codex(
                delegate_id,
                result,
                provider_metadata=metadata,
                worktree=str(allocation.worktree) if allocation is not None else None,
            )
        except WorktreeError as exc:
            metadata["safe_isolation_failure"] = str(exc)[:_MAX_LOG_TAIL_CHARS]
            try:
                self._store.update_provider_metadata(delegate_id, metadata)
                self._store.fail(
                    delegate_id,
                    f"Codex isolation could not be established; the live checkout was not used: {exc}",
                )
            except DelegateError:
                pass
        except (DelegateError, OSError, ValueError, json.JSONDecodeError) as exc:
            try:
                self._store.update_provider_metadata(delegate_id, metadata)
                self._store.fail(delegate_id, f"Codex provider failed safely: {exc}")
            except DelegateError:
                pass
        finally:
            with self._lock:
                self._processes.pop(delegate_id, None)

    def _command(self, task: Any, cwd: Path, schema_path: Path, result_path: Path) -> list[str]:
        """Builds the supported non-interactive Codex CLI invocation."""
        command = [
            self._config.codex_executable,
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--sandbox",
            "workspace-write" if task.kind == DelegateKind.CODE else "read-only",
            "-C",
            str(cwd),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
        ]
        if task.kind != DelegateKind.CODE:
            command.append("--skip-git-repo-check")
        if self._config.codex_model is not None:
            command.extend(["--model", self._config.codex_model])
        if self._config.codex_reasoning_effort is not None:
            command.extend(["-c", f'model_reasoning_effort="{self._config.codex_reasoning_effort}"'])
        command.append("-")
        return command

    @staticmethod
    def _prompt(provider_task: DelegateProviderTask, allocation: WorktreeAllocation | None) -> str:
        """Builds a bounded self-contained Codex task packet without parent conversation context."""
        task = provider_task.task
        lines = [
            "You are an Orchestrator Codex delegate. Complete only the supplied task.",
            "Return ONLY valid JSON matching the supplied output schema.",
            f"Goal: {task.goal}",
            f"Project: {task.project_name}",
            f"Kind: {task.kind.value}",
        ]
        for title, values in (
            ("Known context", task.known_context),
            ("Scope", task.scope),
            ("Out of scope", task.out_of_scope),
            ("Acceptance criteria", task.acceptance_criteria),
            ("Verification", task.verification),
        ):
            if values:
                lines.append(f"{title}:")
                lines.extend(f"- {value}" for value in values)
        if task.parent_notes:
            lines.append(f"Parent notes: {task.parent_notes}")
        if allocation is not None:
            lines.extend(
                [
                    "You are already in an isolated Orchestrator-owned Git worktree.",
                    "Do not modify or switch to another checkout and do not merge into another branch.",
                    "Run the requested verification and commit the completed changes to the current branch when appropriate.",
                ]
            )
        else:
            lines.append("This is a read-only delegate. Do not modify files.")
        return "\n".join(lines)

    def _final_metadata(self, metadata: dict[str, Any], events_path: Path, elapsed: float, return_code: int | None) -> dict[str, Any]:
        """Adds bounded execution and usage telemetry parsed from private JSONL events."""
        summary = self._events.parse(events_path)
        result = dict(metadata)
        result.update(
            {
                "elapsed_seconds": elapsed,
                "return_code": return_code,
                "event_count": summary.event_count,
                "invalid_event_count": summary.invalid_event_count,
                "usage": asdict(summary.usage),
            }
        )
        if result.get("model") is None and summary.model is not None:
            result["model"] = summary.model
        if result.get("reasoning_effort") is None and summary.reasoning_effort is not None:
            result["reasoning_effort"] = summary.reasoning_effort
        return result

    def _augment_code_result(self, result: dict[str, Any], allocation: WorktreeAllocation) -> dict[str, Any]:
        """Replaces review metadata with Git-observed worktree state rather than trusting provider prose."""
        summary = self._worktrees.summarize(allocation)
        augmented = dict(result)
        augmented["worktree"] = str(allocation.worktree)
        augmented["changed_files"] = summary.changed_files
        augmented["commit"] = summary.commit
        augmented["diff_summary"] = summary.diff_summary
        return augmented

    @staticmethod
    def _read_result(path: Path) -> dict[str, Any]:
        """Reads the schema-constrained final message persisted by Codex."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Codex final output is not a JSON object.")
        return raw

    def _relative_state_path(self, path: Path) -> str:
        """Returns a private state-relative path suitable for persisted metadata."""
        return str(path.relative_to(self._config.state_root))

    @staticmethod
    def _tail(path: Path) -> str:
        """Returns a bounded tail of one private provider error log."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-_MAX_LOG_TAIL_CHARS:]

    @staticmethod
    def _failure_message(return_code: int | None, stderr_tail: str) -> str:
        """Builds one bounded status-safe provider failure summary."""
        detail = " ".join(stderr_tail.split())
        prefix = f"Codex exited with status {return_code}."
        if detail:
            return f"{prefix} {detail}"[:_MAX_ERROR_CHARS]
        return prefix

    def _register_process(self, delegate_id: str, process: subprocess.Popen[str]) -> bool:
        """Registers a process and reports whether cancellation already won the launch race."""
        with self._lock:
            self._processes[delegate_id] = process
            return delegate_id in self._cancelled

    def _unregister_process(self, delegate_id: str, process: subprocess.Popen[str]) -> None:
        """Drops process ownership only when the same process is still registered."""
        with self._lock:
            if self._processes.get(delegate_id) is process:
                self._processes.pop(delegate_id, None)

    def _is_cancelled(self, delegate_id: str) -> bool:
        """Returns whether cancellation has won the local provider race for one delegate."""
        with self._lock:
            return delegate_id in self._cancelled

    def _forget_future(self, delegate_id: str, completed: Future[None]) -> None:
        """Drops completed provider bookkeeping without disturbing a newer retry."""
        with self._lock:
            if self._futures.get(delegate_id) is completed:
                self._futures.pop(delegate_id, None)
            self._cancelled.discard(delegate_id)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        """Terminates the Codex process group, escalating only if it does not exit promptly."""
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=2.0)


class ProviderRouter:
    """Routes unattended lifecycle operations without exposing provider-specific MCP tools."""

    def __init__(self, providers: list[DelegateProvider]) -> None:
        self._providers = {provider.policy: provider for provider in providers}

    def start(self, policy: ProviderPolicy, delegate_id: str) -> None:
        """Starts unattended work when the selected policy has a local provider."""
        provider = self._providers.get(policy)
        if provider is not None:
            provider.start(delegate_id)

    def start_codex(self, delegate_id: str) -> None:
        """Starts one delegate through Codex after routing is durably decided."""
        provider = self._providers.get(ProviderPolicy.CODEX)
        if provider is not None:
            provider.start(delegate_id)

    def availability_for_auto(self) -> ProviderAvailability:
        """Returns whether Codex may currently receive an automatic fallback."""
        provider = self._providers.get(ProviderPolicy.CODEX)
        if provider is None:
            return ProviderAvailability(False, "Codex provider is not configured.")
        return provider.availability_for_auto()

    def cancel(self, policy: ProviderPolicy, delegate_id: str) -> bool:
        """Cancels unattended work when it is owned by a local provider."""
        effective_policy = ProviderPolicy.CODEX if policy == ProviderPolicy.AUTO else policy
        provider = self._providers.get(effective_policy)
        return provider.cancel(delegate_id) if provider is not None else False
