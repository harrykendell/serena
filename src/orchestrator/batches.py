"""Durable bounded fan-out/fan-in batches for Orchestrator delegates."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.config import OrchestratorConfig
from orchestrator.delegates import (
    CreateDelegateRequest,
    CreateDelegateResponse,
    DelegateError,
    DelegateKind,
    DelegateState,
    DelegateStore,
    ProviderPolicy,
)

_MAX_BATCH_TASKS = 16
_MAX_BATCH_CONCURRENCY = 4
_DEFAULT_BATCH_CONCURRENCY = 2
_DEFAULT_BATCH_RESULT_BUDGET_CHARS = 6_000
_MAX_BATCH_RESULT_BUDGET_CHARS = 12_000
_TERMINAL_STATES = {
    DelegateState.COMPLETED,
    DelegateState.FAILED,
    DelegateState.CANCELLED,
    DelegateState.TIMED_OUT,
}

ShortText = Annotated[str, Field(min_length=1, max_length=500)]
LongText = Annotated[str, Field(min_length=1, max_length=4_000)]
TextList = Annotated[list[ShortText], Field(max_length=20)]


class DelegateBatchError(ValueError):
    """Reports an invalid delegate-batch operation without exposing storage internals."""


class DelegateBatchState(StrEnum):
    """Represents the parent-visible lifecycle of one fan-out/fan-in batch."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class BatchTaskRequest(BaseModel):
    """Defines one read-only task within a delegate batch."""

    model_config = ConfigDict(extra="forbid")

    kind: DelegateKind
    goal: LongText
    acceptance_criteria: Annotated[list[ShortText], Field(min_length=1, max_length=20)]
    known_context: TextList = Field(default_factory=list)
    scope: TextList = Field(default_factory=list)
    out_of_scope: Annotated[list[ShortText], Field(max_length=19)] = Field(default_factory=list)
    verification: TextList = Field(default_factory=list)
    parent_notes: str = Field(default="", max_length=2_000)
    base_revision: str | None = Field(default=None, max_length=200)
    result_budget_chars: int = Field(default=4_000, ge=1_000, le=12_000)

    @model_validator(mode="after")
    def validate_read_only_kind(self) -> BatchTaskRequest:
        """Restricts the first batch release to read-only analysis tasks."""
        if self.kind == DelegateKind.CODE:
            raise ValueError("delegate_batch O07 supports read-only analysis tasks only; code delegates must be created individually.")
        return self

    def delegate_request(
        self,
        *,
        project_name: str,
        project_root: str | None,
        provider_policy: ProviderPolicy,
    ) -> CreateDelegateRequest:
        """Builds the validated single-delegate request owned by this batch task."""
        return CreateDelegateRequest(
            project_name=project_name,
            project_root=project_root,
            kind=self.kind,
            provider_policy=provider_policy,
            goal=self.goal,
            known_context=self.known_context,
            scope=self.scope,
            out_of_scope=[*self.out_of_scope, "Do not modify repository or project state; this is a read-only batch task."],
            acceptance_criteria=self.acceptance_criteria,
            verification=self.verification,
            parent_notes=self.parent_notes,
            base_revision=self.base_revision,
            result_budget_chars=self.result_budget_chars,
        )


class CreateDelegateBatchRequest(BaseModel):
    """Defines one bounded ChatGPT-first fan-out request."""

    model_config = ConfigDict(extra="forbid")

    project_name: ShortText
    project_root: str | None = Field(default=None, max_length=2_000)
    tasks: Annotated[list[BatchTaskRequest], Field(min_length=1, max_length=_MAX_BATCH_TASKS)]
    provider_policy: ProviderPolicy = ProviderPolicy.CHAT
    concurrency: int = Field(default=_DEFAULT_BATCH_CONCURRENCY, ge=1, le=_MAX_BATCH_CONCURRENCY)
    result_budget_chars: int = Field(
        default=_DEFAULT_BATCH_RESULT_BUDGET_CHARS,
        ge=1_000,
        le=_MAX_BATCH_RESULT_BUDGET_CHARS,
    )

    @model_validator(mode="after")
    def validate_chatgpt_first_policy(self) -> CreateDelegateBatchRequest:
        """Rejects direct Codex fan-out in the read-only first release."""
        if self.provider_policy == ProviderPolicy.CODEX:
            raise ValueError("delegate_batch is ChatGPT-first; use provider_policy=chat or auto.")
        if self.provider_policy == ProviderPolicy.AUTO and self.project_root is None:
            raise ValueError("provider_policy=auto requires project_root so unclaimed analysis may fall back to Codex safely.")
        return self


class BatchTaskRecord(BaseModel):
    """Persists one validated child request and its delegate identity once launched."""

    model_config = ConfigDict(extra="forbid")

    index: int
    request: CreateDelegateRequest
    delegate_id: str | None = None


class DelegateBatchRecord(BaseModel):
    """Persists batch ownership, limits, and pending child task packets."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    parent_session_id: str
    created_at: datetime
    updated_at: datetime
    concurrency: int
    result_budget_chars: int
    tasks: list[BatchTaskRecord]


class BatchLaunch(BaseModel):
    """Returns one newly exposed child delegate to its parent batch session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_index: int
    delegate_id: str
    provider_policy: ProviderPolicy
    launch_prompt: str
    claim_deadline: datetime | None
    fallback: str | None


class BatchTaskStatus(BaseModel):
    """Returns compact state for one task without its private task packet or transcript."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_index: int
    delegate_id: str | None
    kind: DelegateKind
    state: DelegateState | Literal["PENDING"]


class BatchAggregateItem(BaseModel):
    """Defines one compact deterministic digest of a terminal typed hand-back."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_index: int
    delegate_id: str
    kind: DelegateKind
    state: DelegateState
    result_status: str | None = None
    summary: str
    highlights: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class BatchAggregate(BaseModel):
    """Defines the bounded fan-in result returned instead of concatenated worker outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[BatchAggregateItem]
    omitted_results: int
    result_budget_chars: int


class DelegateBatchResponse(BaseModel):
    """Returns compact durable batch progress, new launch prompts, and bounded fan-in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str
    state: DelegateBatchState
    concurrency: int
    pending: int
    active: int
    terminal: int
    launches: list[BatchLaunch]
    tasks: list[BatchTaskStatus]
    aggregate: BatchAggregate
    next_action: str


class DelegateBatchStore:
    """Owns durable batch scheduling and compact deterministic fan-in."""

    def __init__(self, config: OrchestratorConfig, delegate_store: DelegateStore) -> None:
        self._config = config
        self._config.ensure_state_layout()
        self._delegates = delegate_store
        self._lock = FileLock(str(self._config.batches_dir / ".store.lock"))

    def create(self, parent_session_id: str, request: CreateDelegateBatchRequest) -> DelegateBatchResponse:
        """Creates a durable batch and exposes no more than its concurrency limit."""
        # validate every child packet before persisting or launching any work
        task_records = [
            BatchTaskRecord(
                index=index,
                request=task.delegate_request(
                    project_name=request.project_name,
                    project_root=request.project_root,
                    provider_policy=request.provider_policy,
                ),
            )
            for index, task in enumerate(request.tasks)
        ]

        with self._lock:
            now = self._now()
            record = DelegateBatchRecord(
                batch_id=self._new_batch_id(),
                parent_session_id=parent_session_id,
                created_at=now,
                updated_at=now,
                concurrency=request.concurrency,
                result_budget_chars=request.result_budget_chars,
                tasks=task_records,
            )
            self._write_record(record)
            launches = self._promote_locked(record)
            return self._response_locked(record, launches)

    def refresh(self, batch_id: str, parent_session_id: str) -> DelegateBatchResponse:
        """Refreshes one parent-owned batch, filling newly available fan-out slots."""
        with self._lock:
            record = self._read_record(batch_id)
            self._require_parent(record, parent_session_id)
            launches = self._promote_locked(record)
            return self._response_locked(record, launches)

    def _promote_locked(self, record: DelegateBatchRecord) -> list[BatchLaunch]:
        """Creates pending delegates until the batch reaches its configured active concurrency."""
        active = self._active_count(record)
        launches: list[BatchLaunch] = []

        # expose pending work only when a real batch slot is available
        for task in record.tasks:
            if active >= record.concurrency:
                break
            if task.delegate_id is not None:
                continue

            created = self._delegates.create(record.parent_session_id, task.request)
            task.delegate_id = created.delegate_id
            record.updated_at = self._now()
            self._write_record(record)
            launches.append(self._launch(task.index, created))
            active += 1

        return launches

    def _active_count(self, record: DelegateBatchRecord) -> int:
        """Counts launched child delegates that still occupy a batch concurrency slot."""
        active = 0
        for task in record.tasks:
            if task.delegate_id is None:
                continue
            status = self._delegates.status(task.delegate_id, record.parent_session_id)
            if status.state not in _TERMINAL_STATES:
                active += 1
        return active

    def _response_locked(self, record: DelegateBatchRecord, launches: list[BatchLaunch]) -> DelegateBatchResponse:
        """Builds parent-visible batch state and deterministic bounded aggregation."""
        task_statuses: list[BatchTaskStatus] = []
        terminal_items: list[BatchAggregateItem] = []
        pending = 0
        active = 0
        terminal = 0

        # derive current state only from durable child lifecycle records
        for task in record.tasks:
            if task.delegate_id is None:
                pending += 1
                task_statuses.append(BatchTaskStatus(task_index=task.index, delegate_id=None, kind=task.request.kind, state="PENDING"))
                continue

            status = self._delegates.status(task.delegate_id, record.parent_session_id)
            task_statuses.append(
                BatchTaskStatus(
                    task_index=task.index,
                    delegate_id=task.delegate_id,
                    kind=task.request.kind,
                    state=status.state,
                )
            )
            if status.state in _TERMINAL_STATES:
                terminal += 1
                terminal_items.append(
                    self._aggregate_item(
                        task,
                        record.parent_session_id,
                        status.state,
                        status.result_available,
                        status.message,
                    )
                )
            else:
                active += 1

        state = DelegateBatchState.COMPLETED if pending == 0 and active == 0 else DelegateBatchState.RUNNING
        aggregate = self._bounded_aggregate(terminal_items, record.result_budget_chars)
        if launches:
            next_action = "Launch the returned ChatGPT delegate prompts independently, then refresh this batch as work completes."
        elif state == DelegateBatchState.COMPLETED:
            next_action = "Consume the bounded aggregate; individual typed delegate results remain available by delegate ID if needed."
        else:
            next_action = "Refresh this batch after an active delegate reaches a terminal state."
        return DelegateBatchResponse(
            batch_id=record.batch_id,
            state=state,
            concurrency=record.concurrency,
            pending=pending,
            active=active,
            terminal=terminal,
            launches=launches,
            tasks=task_statuses,
            aggregate=aggregate,
            next_action=next_action,
        )

    def _aggregate_item(
        self,
        task: BatchTaskRecord,
        parent_session_id: str,
        state: DelegateState,
        result_available: bool,
        message: str | None,
    ) -> BatchAggregateItem:
        """Maps one terminal typed result into the compact batch aggregation contract."""
        if task.delegate_id is None:
            raise DelegateBatchError("Cannot aggregate an unlaunched batch task.")
        if not result_available:
            return BatchAggregateItem(
                task_index=task.index,
                delegate_id=task.delegate_id,
                kind=task.request.kind,
                state=state,
                summary=self._truncate(message or f"Delegate ended in state {state.value} without a typed result.", 500),
            )

        try:
            result = self._delegates.collect(task.delegate_id, parent_session_id)
        except DelegateError:
            return BatchAggregateItem(
                task_index=task.index,
                delegate_id=task.delegate_id,
                kind=task.request.kind,
                state=state,
                summary=f"Delegate ended in state {state.value} without a collectable typed result.",
            )

        payload = result.result
        status = str(payload.get("status")) if payload.get("status") is not None else None
        if task.request.kind == DelegateKind.EXPLORE:
            summary = str(payload.get("conclusion", ""))
            highlights = self._strings(payload.get("findings"), 3)
            references = self._strings(payload.get("evidence"), 3)
            caveats = self._strings(payload.get("uncertainties"), 2)
        elif task.request.kind == DelegateKind.REVIEW:
            summary = str(payload.get("verdict", ""))
            highlights = self._strings(payload.get("findings"), 4)
            severity = payload.get("severity")
            if severity:
                highlights.insert(0, f"Severity: {severity}")
            references = self._strings(payload.get("evidence"), 3)
            caveats = []
        else:
            summary = str(payload.get("answer", ""))
            highlights = []
            references = self._strings(payload.get("sources"), 4)
            caveats = self._strings(payload.get("caveats"), 3)

        return BatchAggregateItem(
            task_index=task.index,
            delegate_id=task.delegate_id,
            kind=task.request.kind,
            state=state,
            result_status=status,
            summary=self._truncate(summary, 1_200),
            highlights=[self._truncate(value, 500) for value in highlights],
            references=[self._truncate(value, 500) for value in references],
            caveats=[self._truncate(value, 500) for value in caveats],
        )

    @staticmethod
    def _strings(value: object, limit: int) -> list[str]:
        """Extracts a bounded string list from one already validated typed result field."""
        if not isinstance(value, list):
            return []
        return [str(item) for item in value[:limit]]

    def _bounded_aggregate(self, items: list[BatchAggregateItem], result_budget_chars: int) -> BatchAggregate:
        """Fits deterministic result digests inside the configured batch fan-in budget."""
        kept: list[BatchAggregateItem] = []
        omitted = 0
        for item in items:
            trial = BatchAggregate(items=[*kept, item], omitted_results=omitted, result_budget_chars=result_budget_chars)
            if len(trial.model_dump_json()) <= result_budget_chars:
                kept.append(item)
                continue

            compact = item.model_copy(
                update={
                    "summary": self._truncate(item.summary, 240),
                    "highlights": [],
                    "references": [],
                    "caveats": [],
                }
            )
            trial = BatchAggregate(items=[*kept, compact], omitted_results=omitted, result_budget_chars=result_budget_chars)
            if len(trial.model_dump_json()) <= result_budget_chars:
                kept.append(compact)
            else:
                omitted += 1

        aggregate = BatchAggregate(items=kept, omitted_results=omitted, result_budget_chars=result_budget_chars)
        if len(aggregate.model_dump_json()) > result_budget_chars:
            raise DelegateBatchError("Batch aggregate metadata exceeds the configured result budget.")
        return aggregate

    @staticmethod
    def _launch(task_index: int, created: CreateDelegateResponse) -> BatchLaunch:
        """Maps one newly created child delegate into the batch launch surface."""
        return BatchLaunch(
            task_index=task_index,
            delegate_id=created.delegate_id,
            provider_policy=created.provider_policy,
            launch_prompt=created.launch_prompt,
            claim_deadline=created.claim_deadline,
            fallback=created.fallback,
        )

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        """Truncates one fan-in field without splitting the output contract."""
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 1)] + "…"

    @staticmethod
    def _require_parent(record: DelegateBatchRecord, parent_session_id: str) -> None:
        """Requires the creating parent session for every batch refresh."""
        if record.parent_session_id != parent_session_id:
            raise DelegateBatchError("Only the parent session may refresh this delegate batch.")

    def _read_record(self, batch_id: str) -> DelegateBatchRecord:
        """Reads one persisted batch record after validating its opaque identifier."""
        if len(batch_id) != 32 or any(character not in "0123456789abcdef" for character in batch_id):
            raise DelegateBatchError("Invalid delegate batch identifier.")
        path = self._batch_path(batch_id)
        if not path.is_file():
            raise DelegateBatchError(f"Unknown delegate batch {batch_id}.")
        try:
            return DelegateBatchRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DelegateBatchError(f"Delegate batch {batch_id} is unreadable.") from exc

    def _write_record(self, record: DelegateBatchRecord) -> None:
        """Atomically persists one batch record."""
        self._atomic_write_json(self._batch_path(record.batch_id), record.model_dump(mode="json"))

    def _batch_path(self, batch_id: str) -> Path:
        """Returns the Orchestrator-owned path for one batch record."""
        return self._config.batches_dir / f"{batch_id}.json"

    @staticmethod
    def _atomic_write_json(path: Path, payload: object) -> None:
        """Writes JSON through a sibling temporary file before atomic replacement."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _new_batch_id() -> str:
        """Returns a collision-resistant opaque batch identifier."""
        return uuid.uuid4().hex

    @staticmethod
    def _now() -> datetime:
        """Returns the current UTC time."""
        return datetime.now(UTC)
