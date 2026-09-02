"""Durable delegate model and state transitions for Orchestrator."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from orchestrator.config import OrchestratorConfig

_MAX_PARENT_NOTES_CHARS = 2_000
_MAX_TASK_PACKET_CHARS = 16_000
_DEFAULT_RESULT_BUDGET_CHARS = 6_000
_MAX_RESULT_BUDGET_CHARS = 12_000
_MAX_CANCEL_REASON_CHARS = 500
_MAX_AUDIT_EVENTS = 50

ShortText = Annotated[str, Field(min_length=1, max_length=500)]
MediumText = Annotated[str, Field(min_length=1, max_length=2_000)]
LongText = Annotated[str, Field(min_length=1, max_length=4_000)]
TextList = Annotated[list[ShortText], Field(max_length=20)]


class DelegateError(ValueError):
    """Reports an invalid delegate operation without exposing storage internals."""


class DelegateKind(StrEnum):
    """Classifies the expected worker task and hand-back contract."""

    EXPLORE = "explore"
    CODE = "code"
    REVIEW = "review"
    RESEARCH = "research"


class DelegateState(StrEnum):
    """Represents the durable lifecycle of a delegate."""

    WAITING_FOR_CHAT = "WAITING_FOR_CHAT"
    QUEUED = "QUEUED"
    RUNNING_CHAT = "RUNNING_CHAT"
    RUNNING_CODEX = "RUNNING_CODEX"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


_ACTIVE_STATES = {
    DelegateState.WAITING_FOR_CHAT,
    DelegateState.QUEUED,
    DelegateState.RUNNING_CHAT,
    DelegateState.RUNNING_CODEX,
}
_TERMINAL_STATES = {
    DelegateState.COMPLETED,
    DelegateState.FAILED,
    DelegateState.CANCELLED,
    DelegateState.TIMED_OUT,
}


class ProviderPolicy(StrEnum):
    """Identifies the requested delegate provider policy."""

    CHAT = "chat"
    CODEX = "codex"
    AUTO = "auto"


class ActiveProvider(StrEnum):
    """Identifies the provider currently executing a delegate."""

    CHAT = "chat"
    CODEX = "codex"


class ResultStatus(StrEnum):
    """Summarizes whether a worker fully completed its assigned goal."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class CreateDelegateRequest(BaseModel):
    """Defines the bounded task information supplied by a parent session."""

    model_config = ConfigDict(extra="forbid")

    project_name: ShortText
    project_root: str | None = Field(default=None, max_length=2_000)
    kind: DelegateKind
    provider_policy: ProviderPolicy = ProviderPolicy.CHAT
    goal: LongText
    known_context: TextList = Field(default_factory=list)
    scope: TextList = Field(default_factory=list)
    out_of_scope: TextList = Field(default_factory=list)
    acceptance_criteria: Annotated[list[ShortText], Field(min_length=1, max_length=20)]
    verification: TextList = Field(default_factory=list)
    parent_notes: str = Field(default="", max_length=_MAX_PARENT_NOTES_CHARS)
    base_revision: str | None = Field(default=None, max_length=200)
    result_budget_chars: int = Field(default=_DEFAULT_RESULT_BUDGET_CHARS, ge=1_000, le=_MAX_RESULT_BUDGET_CHARS)

    @model_validator(mode="after")
    def validate_packet_budget(self) -> CreateDelegateRequest:
        """Rejects unsupported provider state and task packets that defeat isolation."""
        if self.provider_policy in {ProviderPolicy.CODEX, ProviderPolicy.AUTO} and self.project_root is None:
            raise ValueError("Codex-capable delegates require an explicit project_root; Orchestrator does not resolve Serena projects.")

        serialized = self.model_dump_json(exclude_none=True)
        if len(serialized) > _MAX_TASK_PACKET_CHARS:
            raise ValueError(f"Delegate task packet exceeds the {_MAX_TASK_PACKET_CHARS}-character budget.")
        return self


class ResultContract(BaseModel):
    """Describes the compact result object expected from a worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    fields: list[str]


class DelegateSpec(BaseModel):
    """Defines the bounded task packet returned only to the claiming worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegate_id: str
    project_name: str
    project_root: str | None
    kind: DelegateKind
    goal: str
    known_context: list[str]
    scope: list[str]
    out_of_scope: list[str]
    acceptance_criteria: list[str]
    verification: list[str]
    result_schema: ResultContract
    result_budget_chars: int
    parent_notes: str
    base_revision: str | None
    instructions: str


class ExploreResult(BaseModel):
    """Defines the compact hand-back contract for exploratory work."""

    model_config = ConfigDict(extra="forbid")

    status: ResultStatus
    conclusion: MediumText
    findings: Annotated[list[MediumText], Field(max_length=5)] = Field(default_factory=list)
    evidence: TextList = Field(default_factory=list)
    recommendation: MediumText
    uncertainties: TextList = Field(default_factory=list)


class CodeResult(BaseModel):
    """Defines the compact hand-back contract for implementation work."""

    model_config = ConfigDict(extra="forbid")

    status: ResultStatus
    summary: MediumText
    changed_files: TextList = Field(default_factory=list)
    tests: TextList = Field(default_factory=list)
    commit: str | None = Field(default=None, max_length=200)
    worktree: str | None = Field(default=None, max_length=2_000)
    diff_summary: str = Field(default="", max_length=2_000)
    remaining_issues: TextList = Field(default_factory=list)
    artifacts: TextList = Field(default_factory=list)


class ReviewResult(BaseModel):
    """Defines the compact hand-back contract for review work."""

    model_config = ConfigDict(extra="forbid")

    status: ResultStatus
    verdict: MediumText
    findings: Annotated[list[MediumText], Field(max_length=10)] = Field(default_factory=list)
    severity: ShortText
    evidence: TextList = Field(default_factory=list)


class ResearchResult(BaseModel):
    """Defines the compact hand-back contract for research work."""

    model_config = ConfigDict(extra="forbid")

    status: ResultStatus
    answer: LongText
    sources: TextList = Field(default_factory=list)
    caveats: TextList = Field(default_factory=list)


DelegateResult = ExploreResult | CodeResult | ReviewResult | ResearchResult
_RESULT_MODELS: dict[DelegateKind, type[BaseModel]] = {
    DelegateKind.EXPLORE: ExploreResult,
    DelegateKind.CODE: CodeResult,
    DelegateKind.REVIEW: ReviewResult,
    DelegateKind.RESEARCH: ResearchResult,
}
_RESULT_CONTRACTS: dict[DelegateKind, ResultContract] = {
    DelegateKind.EXPLORE: ResultContract(
        name="ExploreResult",
        fields=["status", "conclusion", "findings", "evidence", "recommendation", "uncertainties"],
    ),
    DelegateKind.CODE: ResultContract(
        name="CodeResult",
        fields=["status", "summary", "changed_files", "tests", "commit", "worktree", "diff_summary", "remaining_issues", "artifacts"],
    ),
    DelegateKind.REVIEW: ResultContract(
        name="ReviewResult",
        fields=["status", "verdict", "findings", "severity", "evidence"],
    ),
    DelegateKind.RESEARCH: ResultContract(
        name="ResearchResult",
        fields=["status", "answer", "sources", "caveats"],
    ),
}


class DelegateRecord(BaseModel):
    """Represents the durable internal record for one delegate."""

    model_config = ConfigDict(extra="forbid")

    delegate_id: str
    parent_session_id: str
    worker_session_id: str | None = None
    project_name: str
    project_root: str | None = None
    kind: DelegateKind
    provider_policy: ProviderPolicy = ProviderPolicy.CHAT
    active_provider: ActiveProvider | None = None
    state: DelegateState
    created_at: datetime
    claim_deadline: datetime | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    goal: str
    known_context: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str]
    verification: list[str] = Field(default_factory=list)
    result_schema: ResultContract
    result_budget_chars: int
    parent_notes: str = ""
    base_revision: str | None = None
    worktree: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    result_path: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_manual_chat_state(self) -> DelegateRecord:
        """Enforces durable lifecycle invariants for ChatGPT and Codex delegates."""
        if self.state == DelegateState.WAITING_FOR_CHAT:
            waiting_policy = self.provider_policy in {ProviderPolicy.CHAT, ProviderPolicy.AUTO}
            invalid_deadline = (self.provider_policy == ProviderPolicy.AUTO) != (self.claim_deadline is not None)
            if (
                not waiting_policy
                or invalid_deadline
                or any(
                    value is not None
                    for value in (
                        self.worker_session_id,
                        self.active_provider,
                        self.claimed_at,
                        self.started_at,
                        self.finished_at,
                        self.result_path,
                    )
                )
            ):
                raise ValueError("WAITING_FOR_CHAT delegates must be unclaimed ChatGPT or auto work.")
        elif self.state == DelegateState.QUEUED:
            if (
                self.provider_policy not in {ProviderPolicy.CODEX, ProviderPolicy.AUTO}
                or self.active_provider != ActiveProvider.CODEX
                or self.worker_session_id is not None
                or self.claimed_at is not None
                or self.started_at is not None
                or self.finished_at is not None
                or self.result_path is not None
            ):
                raise ValueError("QUEUED delegates must be unstarted Codex-capable work.")
        elif self.state == DelegateState.RUNNING_CHAT:
            if (
                self.provider_policy not in {ProviderPolicy.CHAT, ProviderPolicy.AUTO}
                or self.worker_session_id is None
                or self.active_provider != ActiveProvider.CHAT
                or self.claimed_at is None
                or self.started_at is None
                or self.finished_at is not None
                or self.result_path is not None
            ):
                raise ValueError("RUNNING_CHAT delegates require one active ChatGPT worker and no terminal result.")
        elif self.state == DelegateState.RUNNING_CODEX:
            if (
                self.provider_policy not in {ProviderPolicy.CODEX, ProviderPolicy.AUTO}
                or self.worker_session_id is not None
                or self.active_provider != ActiveProvider.CODEX
                or self.started_at is None
                or self.finished_at is not None
                or self.result_path is not None
            ):
                raise ValueError("RUNNING_CODEX delegates require one active Codex provider and no terminal result.")
        elif self.state == DelegateState.COMPLETED:
            if self.finished_at is None or self.result_path is None or self.started_at is None:
                raise ValueError("COMPLETED delegates require timestamps and a persisted result.")
            if self.active_provider == ActiveProvider.CHAT and (self.worker_session_id is None or self.claimed_at is None):
                raise ValueError("COMPLETED ChatGPT delegates require worker ownership.")
            if self.active_provider == ActiveProvider.CODEX and self.worker_session_id is not None:
                raise ValueError("COMPLETED Codex delegates cannot contain ChatGPT worker ownership.")
        elif self.state == DelegateState.FAILED:
            if self.finished_at is None:
                raise ValueError("FAILED delegates require a terminal timestamp.")
            if self.active_provider == ActiveProvider.CHAT and self.worker_session_id is None:
                raise ValueError("FAILED ChatGPT delegates require worker ownership.")
        elif self.state in {DelegateState.CANCELLED, DelegateState.TIMED_OUT}:
            if self.finished_at is None or self.result_path is not None:
                raise ValueError(f"{self.state} delegates require a terminal timestamp and no result.")
        return self


class CreateDelegateResponse(BaseModel):
    """Returns only launch metadata to the parent session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegate_id: str
    state: DelegateState
    provider_policy: ProviderPolicy
    launch_prompt: str
    claim_deadline: datetime | None
    fallback: str | None


class ClaimDelegateResponse(BaseModel):
    """Returns the full bounded task packet to the owning worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegate_id: str
    state: DelegateState
    task: DelegateSpec


class CompleteDelegateResponse(BaseModel):
    """Confirms terminal persistence after a worker hand-back."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegate_id: str
    state: DelegateState


class CollectDelegateResponse(BaseModel):
    """Returns only the validated bounded result to the parent session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegate_id: str
    state: DelegateState
    result: dict[str, Any]


class DelegateStatusResponse(BaseModel):
    """Returns compact lifecycle state without private task or provider detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegate_id: str
    project_name: str
    kind: DelegateKind
    provider_policy: ProviderPolicy
    active_provider: ActiveProvider | None
    state: DelegateState
    created_at: datetime
    claim_deadline: datetime | None
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    result_available: bool
    message: str | None = None


class CancelDelegateResponse(BaseModel):
    """Confirms cancellation or an idempotent terminal no-op."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegate_id: str
    state: DelegateState
    cancelled: bool


class DelegateAuditEvent(BaseModel):
    """Represents one bounded private lifecycle event for a delegate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: str
    at: datetime
    actor: str
    state: DelegateState
    detail: str = Field(default="", max_length=500)


class DelegatePrivateDetail(BaseModel):
    """Provides authorized app-only delegate detail and private audit history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegate_id: str
    project_name: str
    kind: DelegateKind
    provider_policy: ProviderPolicy
    active_provider: ActiveProvider | None
    state: DelegateState
    goal: str
    error: str | None
    provider_metadata: dict[str, Any]
    audit: list[DelegateAuditEvent]


class DelegateProviderTask(BaseModel):
    """Provides one bounded internal task packet to an unattended provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: DelegateSpec
    result_json_schema: dict[str, Any]


class DelegateStore:
    """Owns durable delegate persistence, ownership checks, and state transitions."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._config.ensure_state_layout()
        self._lock = FileLock(str(self._config.delegates_dir / ".store.lock"))

    def create(self, parent_session_id: str, request: CreateDelegateRequest) -> CreateDelegateResponse:
        """Creates one durable delegate in the initial state for its provider policy."""
        with self._lock:
            # construct one durable record before exposing the delegate identifier
            delegate_id = self._new_delegate_id()
            now = self._now()
            codex = request.provider_policy == ProviderPolicy.CODEX
            automatic = request.provider_policy == ProviderPolicy.AUTO
            claim_deadline = now + timedelta(seconds=self._config.auto_claim_timeout_seconds) if automatic else None
            record = DelegateRecord(
                delegate_id=delegate_id,
                parent_session_id=parent_session_id,
                project_name=request.project_name,
                project_root=request.project_root,
                kind=request.kind,
                provider_policy=request.provider_policy,
                active_provider=ActiveProvider.CODEX if codex else None,
                state=DelegateState.QUEUED if codex else DelegateState.WAITING_FOR_CHAT,
                created_at=now,
                claim_deadline=claim_deadline,
                goal=request.goal,
                known_context=request.known_context,
                scope=request.scope,
                out_of_scope=request.out_of_scope,
                acceptance_criteria=request.acceptance_criteria,
                verification=request.verification,
                result_schema=_RESULT_CONTRACTS[request.kind],
                result_budget_chars=request.result_budget_chars,
                parent_notes=request.parent_notes,
                base_revision=request.base_revision,
            )
            self._write_record(record)
            self._append_audit(record, "queued" if codex else "created", "parent")

        return CreateDelegateResponse(
            delegate_id=delegate_id,
            state=record.state,
            provider_policy=record.provider_policy,
            launch_prompt=("" if codex else f"@Orchestrator claim delegate {delegate_id} and complete it independently."),
            claim_deadline=claim_deadline,
            fallback="codex" if automatic else None,
        )

    def claim(self, delegate_id: str, worker_session_id: str) -> ClaimDelegateResponse:
        """Atomically binds an eligible waiting delegate to one worker session."""
        with self._lock:
            record = self._read_record(delegate_id)
            if worker_session_id == record.parent_session_id:
                raise DelegateError("The parent session cannot claim its own delegate; use a fresh worker chat.")

            # make a repeated claim by the owning worker idempotent
            if record.state == DelegateState.RUNNING_CHAT and record.worker_session_id == worker_session_id:
                return self._claim_response(record)
            if record.state != DelegateState.WAITING_FOR_CHAT:
                raise DelegateError(f"Delegate {delegate_id} is not claimable (state={record.state}).")

            # bind the worker and persist the transition under the process-safe store lock
            now = self._now()
            record.worker_session_id = worker_session_id
            record.active_provider = ActiveProvider.CHAT
            record.state = DelegateState.RUNNING_CHAT
            record.claimed_at = now
            record.started_at = now
            self._write_record(record)
            self._append_audit(record, "claimed", "worker")
            return self._claim_response(record)

    def complete(self, delegate_id: str, worker_session_id: str, result: dict[str, Any]) -> CompleteDelegateResponse:
        """Validates and atomically persists a terminal result from the owning worker."""
        with self._lock:
            record = self._read_record(delegate_id)
            self._require_worker(record, worker_session_id)
            validated_result = self._validate_result(record, result)

            # make an identical terminal retry idempotent without permitting replacement
            if record.state in {DelegateState.COMPLETED, DelegateState.FAILED} and record.result_path is not None:
                existing_result = self._read_result(record)
                if existing_result == validated_result.model_dump(mode="json"):
                    return CompleteDelegateResponse(delegate_id=delegate_id, state=record.state)
                raise DelegateError(f"Delegate {delegate_id} is already terminal with a different result.")
            if record.state != DelegateState.RUNNING_CHAT:
                raise DelegateError(f"Delegate {delegate_id} cannot be completed from state {record.state}.")

            # enforce the result budget before changing durable lifecycle state
            result_data = validated_result.model_dump(mode="json")
            serialized = json.dumps(result_data, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) > record.result_budget_chars:
                raise DelegateError(
                    f"Delegate result is {len(serialized)} characters, exceeding its {record.result_budget_chars}-character budget."
                )

            # persist the compact hand-back and derive success/failure from its typed status
            result_path = self._delegate_dir(delegate_id) / "result.json"
            self._atomic_write_json(result_path, result_data)
            record.result_path = str(result_path.relative_to(self._config.state_root))
            blocked = getattr(validated_result, "status", None) == ResultStatus.BLOCKED
            record.state = DelegateState.FAILED if blocked else DelegateState.COMPLETED
            record.finished_at = self._now()
            self._write_record(record)
            self._append_audit(
                record, "failed" if blocked else "completed", "worker", "Worker reported a blocked result." if blocked else ""
            )
            return CompleteDelegateResponse(delegate_id=delegate_id, state=record.state)

    def collect(self, delegate_id: str, parent_session_id: str) -> CollectDelegateResponse:
        """Returns a persisted typed result only to the delegate's parent session."""
        with self._lock:
            record = self._read_record(delegate_id)
            if parent_session_id != record.parent_session_id:
                raise DelegateError("Only the parent session may collect this delegate.")
            if record.state not in {DelegateState.COMPLETED, DelegateState.FAILED} or record.result_path is None:
                raise DelegateError(f"Delegate {delegate_id} has no collectable result (state={record.state}).")

            # return only the compact validated hand-back, never private audit/provider detail
            result = self._read_result(record)
            return CollectDelegateResponse(delegate_id=delegate_id, state=record.state, result=result)

    def status(self, delegate_id: str, session_id: str) -> DelegateStatusResponse:
        """Returns compact lifecycle state to the creating parent or bound worker."""
        with self._lock:
            record = self._read_record(delegate_id)
            self._require_visible(record, session_id)
            return self._status_response(record)

    def cancel(self, delegate_id: str, session_id: str, reason: str = "") -> CancelDelegateResponse:
        """Cancels active work while preserving terminal states on safe retries."""
        if len(reason) > _MAX_CANCEL_REASON_CHARS:
            raise DelegateError(f"Cancellation reason exceeds {_MAX_CANCEL_REASON_CHARS} characters.")

        with self._lock:
            record = self._read_record(delegate_id)
            self._require_visible(record, session_id)

            # preserve already-terminal outcomes; repeated cancellation remains harmless
            if record.state in _TERMINAL_STATES:
                return CancelDelegateResponse(
                    delegate_id=delegate_id,
                    state=record.state,
                    cancelled=record.state == DelegateState.CANCELLED,
                )
            if record.state not in _ACTIVE_STATES:
                raise DelegateError(f"Delegate {delegate_id} cannot be cancelled from state {record.state}.")

            # record cancellation without inventing a provider-specific process action in O04
            record.state = DelegateState.CANCELLED
            record.finished_at = self._now()
            record.error = reason or None
            self._write_record(record)
            self._append_audit(record, "cancelled", self._actor_role(record, session_id), reason)
            return CancelDelegateResponse(delegate_id=delegate_id, state=record.state, cancelled=True)

    def fail(self, delegate_id: str, error: str, session_id: str | None = None) -> DelegateStatusResponse:
        """Marks active work failed for internal provider/worker lifecycle integration."""
        detail = " ".join(error.split())
        if not detail:
            raise DelegateError("Failure detail must not be empty.")
        detail = detail[:500]

        with self._lock:
            record = self._read_record(delegate_id)
            if session_id is not None:
                self._require_visible(record, session_id)
            if record.state == DelegateState.FAILED:
                return self._status_response(record)
            if record.state in _TERMINAL_STATES:
                return self._status_response(record)
            if record.state not in _ACTIVE_STATES:
                raise DelegateError(f"Delegate {delegate_id} cannot fail from state {record.state}.")

            # retain only a bounded summary here; full provider logs belong in provider-owned storage
            record.state = DelegateState.FAILED
            record.finished_at = self._now()
            record.error = detail
            self._write_record(record)
            actor = self._actor_role(record, session_id) if session_id is not None else "system"
            self._append_audit(record, "failed", actor, detail)
            return self._status_response(record)

    def list_visible(self, session_id: str, *, active_only: bool = True, limit: int = 50) -> list[DelegateStatusResponse]:
        """Lists bounded delegate status visible to one session for the activity panel."""
        with self._lock:
            records: list[DelegateRecord] = []
            for delegate_dir in self._config.delegates_dir.glob("d_*"):
                if not delegate_dir.is_dir():
                    continue
                try:
                    record = self._read_record(delegate_dir.name)
                except DelegateError:
                    continue
                if session_id not in {record.parent_session_id, record.worker_session_id}:
                    continue
                if active_only and record.state not in _ACTIVE_STATES:
                    continue
                records.append(record)

            # show the most recently created work first and bound the app-facing snapshot
            records.sort(key=lambda item: item.created_at, reverse=True)
            return [self._status_response(record) for record in records[: max(0, limit)]]

    def list_dashboard_activity(self, *, limit_per_session: int = 50) -> list[dict[str, Any]]:
        """Lists global active orchestration groups for the operator dashboard."""
        with self._lock:
            grouped: dict[str, list[DelegateRecord]] = {}
            for delegate_dir in self._config.delegates_dir.glob("d_*"):
                if not delegate_dir.is_dir():
                    continue
                try:
                    record = self._read_record(delegate_dir.name)
                except DelegateError:
                    continue
                grouped.setdefault(record.parent_session_id, []).append(record)

            panels: list[dict[str, Any]] = []
            for parent_session_id, records in grouped.items():
                if not any(record.state in _ACTIVE_STATES for record in records):
                    continue
                records.sort(key=lambda item: item.created_at, reverse=True)
                visible = records[: max(0, limit_per_session)]
                panel_id = uuid.uuid5(uuid.NAMESPACE_URL, f"orchestrator:{parent_session_id}").hex[:16]
                panels.append(
                    {
                        "panel_id": panel_id,
                        "started_at": min(record.created_at for record in visible).timestamp() if visible else self._now().timestamp(),
                        "delegates": [self._status_response(record).model_dump(mode="json") for record in visible],
                    }
                )

            panels.sort(key=lambda panel: panel["started_at"], reverse=True)
            return panels

    def dashboard_detail(self, delegate_id: str) -> DelegatePrivateDetail:
        """Returns one delegate's operator-visible detail without session ownership filtering."""
        with self._lock:
            record = self._read_record(delegate_id)
            return DelegatePrivateDetail(
                delegate_id=record.delegate_id,
                project_name=record.project_name,
                kind=record.kind,
                provider_policy=record.provider_policy,
                active_provider=record.active_provider,
                state=record.state,
                goal=record.goal,
                error=record.error,
                provider_metadata=dict(record.provider_metadata),
                audit=self._read_audit(record.delegate_id),
            )

    def due_auto_delegate_ids(self) -> list[str]:
        """Returns auto delegates whose ChatGPT claim window has expired."""
        with self._lock:
            now = self._now()
            due: list[str] = []
            for delegate_dir in self._config.delegates_dir.glob("d_*"):
                if not delegate_dir.is_dir():
                    continue
                try:
                    record = self._read_record(delegate_dir.name)
                except DelegateError:
                    continue
                if (
                    record.provider_policy == ProviderPolicy.AUTO
                    and record.state == DelegateState.WAITING_FOR_CHAT
                    and record.claim_deadline is not None
                    and record.claim_deadline <= now
                ):
                    due.append(record.delegate_id)
            return due

    def queued_auto_delegate_ids(self) -> list[str]:
        """Returns persisted auto fallbacks that still require provider scheduling."""
        with self._lock:
            queued: list[str] = []
            for delegate_dir in self._config.delegates_dir.glob("d_*"):
                if not delegate_dir.is_dir():
                    continue
                try:
                    record = self._read_record(delegate_dir.name)
                except DelegateError:
                    continue
                if record.provider_policy == ProviderPolicy.AUTO and record.state == DelegateState.QUEUED:
                    queued.append(record.delegate_id)
            return queued

    def has_waiting_auto(self) -> bool:
        """Returns whether any persisted auto delegate still has a ChatGPT claim window."""
        with self._lock:
            for delegate_dir in self._config.delegates_dir.glob("d_*"):
                if not delegate_dir.is_dir():
                    continue
                try:
                    record = self._read_record(delegate_dir.name)
                except DelegateError:
                    continue
                if record.provider_policy == ProviderPolicy.AUTO and record.state == DelegateState.WAITING_FOR_CHAT:
                    return True
            return False

    def route_auto_to_codex(self, delegate_id: str) -> bool:
        """Atomically converts one expired unclaimed auto delegate into queued Codex work."""
        with self._lock:
            record = self._read_record(delegate_id)
            if record.provider_policy != ProviderPolicy.AUTO or record.state != DelegateState.WAITING_FOR_CHAT:
                return False
            if record.claim_deadline is None or record.claim_deadline > self._now():
                return False

            record.state = DelegateState.QUEUED
            record.active_provider = ActiveProvider.CODEX
            record.provider_metadata = {"fallback": "chat claim window expired"}
            self._write_record(record)
            self._append_audit(record, "fallback_to_codex", "scheduler")
            return True

    def reroute_waiting(self, delegate_id: str, parent_session_id: str, provider_policy: ProviderPolicy) -> DelegateStatusResponse:
        """Reroutes unclaimed work explicitly without interrupting an active worker."""
        with self._lock:
            record = self._read_record(delegate_id)
            if record.parent_session_id != parent_session_id:
                raise DelegateError(f"Session {parent_session_id} does not own delegate {delegate_id}.")
            if record.state != DelegateState.WAITING_FOR_CHAT:
                raise DelegateError(f"Delegate {delegate_id} can only be rerouted while waiting for ChatGPT.")

            if provider_policy == ProviderPolicy.CODEX:
                if record.project_root is None:
                    raise DelegateError("Codex rerouting requires an explicit project_root.")
                record.provider_policy = ProviderPolicy.CODEX
                record.active_provider = ActiveProvider.CODEX
                record.state = DelegateState.QUEUED
                record.claim_deadline = None
            elif provider_policy == ProviderPolicy.CHAT:
                record.provider_policy = ProviderPolicy.CHAT
                record.claim_deadline = None
            elif provider_policy == ProviderPolicy.AUTO:
                if record.project_root is None:
                    raise DelegateError("Auto routing requires an explicit project_root.")
                record.provider_policy = ProviderPolicy.AUTO
                record.claim_deadline = self._now() + timedelta(seconds=self._config.auto_claim_timeout_seconds)
            else:
                raise DelegateError(f"Unsupported provider reroute: {provider_policy}.")

            record.provider_metadata = {}
            self._write_record(record)
            self._append_audit(record, f"rerouted_to_{provider_policy.value}", "parent")
            return self._status_response(record)

    def note_auto_fallback_blocked(self, delegate_id: str, warning: str) -> None:
        """Persists a bounded reason why an expired auto delegate remains claimable."""
        with self._lock:
            record = self._read_record(delegate_id)
            if record.provider_policy != ProviderPolicy.AUTO or record.state != DelegateState.WAITING_FOR_CHAT:
                return
            bounded_warning = warning[:500]
            if record.provider_metadata.get("warning") == bounded_warning:
                return
            record.provider_metadata = {"warning": bounded_warning}
            self._write_record(record)
            self._append_audit(record, "fallback_blocked", "scheduler", bounded_warning)

    def total_codex_tokens(self) -> int:
        """Returns persisted Codex input-plus-output token usage for budget policy."""
        with self._lock:
            total = 0
            for delegate_dir in self._config.delegates_dir.glob("d_*"):
                if not delegate_dir.is_dir():
                    continue
                try:
                    record = self._read_record(delegate_dir.name)
                except DelegateError:
                    continue
                usage = record.provider_metadata.get("usage")
                if not isinstance(usage, dict):
                    continue
                for key in ("input_tokens", "output_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int) and value > 0:
                        total += value
            return total

    def private_detail(self, delegate_id: str, session_id: str) -> DelegatePrivateDetail:
        """Returns app-only task/provider detail and audit history to an owning session."""
        with self._lock:
            record = self._read_record(delegate_id)
            self._require_visible(record, session_id)
            return DelegatePrivateDetail(
                delegate_id=record.delegate_id,
                project_name=record.project_name,
                kind=record.kind,
                provider_policy=record.provider_policy,
                active_provider=record.active_provider,
                state=record.state,
                goal=record.goal,
                error=record.error,
                provider_metadata=dict(record.provider_metadata),
                audit=self._read_audit(record.delegate_id),
            )

    def provider_task(self, delegate_id: str) -> DelegateProviderTask:
        """Returns the bounded internal task packet for a queued Codex delegate."""
        with self._lock:
            record = self._read_record(delegate_id)
            if record.provider_policy not in {ProviderPolicy.CODEX, ProviderPolicy.AUTO} or record.state != DelegateState.QUEUED:
                raise DelegateError(f"Delegate {delegate_id} is not queued for Codex.")
            return DelegateProviderTask(
                task=self._claim_response(record).task,
                result_json_schema=_RESULT_MODELS[record.kind].model_json_schema(),
            )

    def start_codex(self, delegate_id: str, *, worktree: str | None, provider_metadata: dict[str, Any]) -> DelegateStatusResponse:
        """Marks one queued Codex delegate running after execution isolation is established."""
        with self._lock:
            record = self._read_record(delegate_id)
            if record.state == DelegateState.CANCELLED:
                return self._status_response(record)
            if record.provider_policy not in {ProviderPolicy.CODEX, ProviderPolicy.AUTO} or record.state != DelegateState.QUEUED:
                raise DelegateError(f"Delegate {delegate_id} cannot start Codex from state {record.state}.")

            record.state = DelegateState.RUNNING_CODEX
            record.active_provider = ActiveProvider.CODEX
            record.started_at = self._now()
            record.worktree = worktree
            record.provider_metadata = dict(provider_metadata)
            self._write_record(record)
            self._append_audit(record, "started", "codex")
            return self._status_response(record)

    def update_provider_metadata(self, delegate_id: str, provider_metadata: dict[str, Any]) -> None:
        """Replaces private provider metadata without changing lifecycle state."""
        with self._lock:
            record = self._read_record(delegate_id)
            record.provider_metadata = dict(provider_metadata)
            self._write_record(record)

    def complete_codex(
        self,
        delegate_id: str,
        result: dict[str, Any],
        *,
        provider_metadata: dict[str, Any],
        worktree: str | None,
    ) -> CompleteDelegateResponse:
        """Persists one bounded structured Codex result if the delegate is still running."""
        with self._lock:
            record = self._read_record(delegate_id)
            if record.state in {DelegateState.CANCELLED, DelegateState.TIMED_OUT}:
                return CompleteDelegateResponse(delegate_id=delegate_id, state=record.state)
            if record.provider_policy not in {ProviderPolicy.CODEX, ProviderPolicy.AUTO} or record.state != DelegateState.RUNNING_CODEX:
                raise DelegateError(f"Delegate {delegate_id} cannot complete Codex from state {record.state}.")

            validated_result = self._validate_result(record, result)
            result_data = validated_result.model_dump(mode="json")
            serialized = json.dumps(result_data, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) > record.result_budget_chars:
                raise DelegateError(
                    f"Delegate result is {len(serialized)} characters, exceeding its {record.result_budget_chars}-character budget."
                )

            result_path = self._delegate_dir(delegate_id) / "result.json"
            self._atomic_write_json(result_path, result_data)
            record.result_path = str(result_path.relative_to(self._config.state_root))
            record.worktree = worktree
            record.provider_metadata = dict(provider_metadata)
            blocked = getattr(validated_result, "status", None) == ResultStatus.BLOCKED
            record.state = DelegateState.FAILED if blocked else DelegateState.COMPLETED
            record.finished_at = self._now()
            self._write_record(record)
            self._append_audit(
                record,
                "failed" if blocked else "completed",
                "codex",
                "Codex returned a blocked result." if blocked else "",
            )
            return CompleteDelegateResponse(delegate_id=delegate_id, state=record.state)

    def timeout_codex(self, delegate_id: str, message: str, provider_metadata: dict[str, Any]) -> DelegateStatusResponse:
        """Marks one running Codex delegate timed out without overwriting cancellation."""
        with self._lock:
            record = self._read_record(delegate_id)
            if record.state == DelegateState.CANCELLED:
                return self._status_response(record)
            if record.provider_policy not in {ProviderPolicy.CODEX, ProviderPolicy.AUTO} or record.state != DelegateState.RUNNING_CODEX:
                return self._status_response(record)

            record.state = DelegateState.TIMED_OUT
            record.finished_at = self._now()
            record.error = " ".join(message.split())[:500]
            record.provider_metadata = dict(provider_metadata)
            self._write_record(record)
            self._append_audit(record, "timed_out", "codex", record.error or "")
            return self._status_response(record)

    def _claim_response(self, record: DelegateRecord) -> ClaimDelegateResponse:
        """Builds the worker-visible packet without parent ownership metadata."""
        task = DelegateSpec(
            delegate_id=record.delegate_id,
            project_name=record.project_name,
            project_root=record.project_root,
            kind=record.kind,
            goal=record.goal,
            known_context=record.known_context,
            scope=record.scope,
            out_of_scope=record.out_of_scope,
            acceptance_criteria=record.acceptance_criteria,
            verification=record.verification,
            result_schema=record.result_schema,
            result_budget_chars=record.result_budget_chars,
            parent_notes=record.parent_notes,
            base_revision=record.base_revision,
            instructions=(
                "Gather any missing context independently. For repository work, activate the named project in Serena yourself; "
                "Orchestrator does not activate projects or proxy coding tools. Return only the requested result object."
            ),
        )
        return ClaimDelegateResponse(delegate_id=record.delegate_id, state=record.state, task=task)

    def _require_worker(self, record: DelegateRecord, worker_session_id: str) -> None:
        """Rejects result submission by any session other than the bound worker."""
        if record.worker_session_id is None or worker_session_id != record.worker_session_id:
            raise DelegateError("Only the session that claimed this delegate may complete it.")

    @staticmethod
    def _require_visible(record: DelegateRecord, session_id: str) -> None:
        """Restricts lifecycle visibility to the parent and the bound worker."""
        if session_id not in {record.parent_session_id, record.worker_session_id}:
            raise DelegateError("This delegate is not available to the current session.")

    @staticmethod
    def _actor_role(record: DelegateRecord, session_id: str) -> str:
        """Returns the stable ownership role used in private audit events."""
        if session_id == record.parent_session_id:
            return "parent"
        if session_id == record.worker_session_id:
            return "worker"
        return "system"

    def _status_response(self, record: DelegateRecord) -> DelegateStatusResponse:
        """Builds the compact model/app-facing lifecycle representation."""
        message: str | None = None
        if record.state == DelegateState.FAILED:
            message = record.error or ("Worker returned a blocked result." if record.result_path is not None else "Delegate failed.")
        elif record.state == DelegateState.CANCELLED:
            message = "Cancelled."
        elif record.state == DelegateState.TIMED_OUT:
            message = "Timed out."
        else:
            warning = record.provider_metadata.get("warning")
            if isinstance(warning, str) and warning:
                message = warning[:500]

        return DelegateStatusResponse(
            delegate_id=record.delegate_id,
            project_name=record.project_name,
            kind=record.kind,
            provider_policy=record.provider_policy,
            active_provider=record.active_provider,
            state=record.state,
            created_at=record.created_at,
            claim_deadline=record.claim_deadline,
            claimed_at=record.claimed_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            result_available=record.result_path is not None,
            message=message,
        )

    def _validate_result(self, record: DelegateRecord, result: dict[str, Any]) -> BaseModel:
        """Validates a result against the delegate-kind-specific contract."""
        try:
            return _RESULT_MODELS[record.kind].model_validate(result)
        except ValidationError as exc:
            raise DelegateError(f"Result does not match {record.result_schema.name}: {exc}") from exc

    def _read_result(self, record: DelegateRecord) -> dict[str, Any]:
        """Loads and revalidates a persisted result."""
        if record.result_path is None:
            raise DelegateError(f"Delegate {record.delegate_id} has no persisted result.")
        result_path = (self._config.state_root / record.result_path).resolve()
        if not result_path.is_relative_to(self._config.delegates_dir.resolve()):
            raise DelegateError(f"Delegate {record.delegate_id} contains an invalid result path.")
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DelegateError(f"Delegate {record.delegate_id} result could not be read.") from exc
        validated = self._validate_result(record, raw)
        return validated.model_dump(mode="json")

    def _read_audit(self, delegate_id: str) -> list[DelegateAuditEvent]:
        """Loads the bounded private audit trail for one delegate."""
        path = self._audit_path(delegate_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise DelegateError(f"Delegate {delegate_id} audit history could not be read.") from exc
        if not isinstance(raw, list):
            raise DelegateError(f"Delegate {delegate_id} audit history is invalid.")
        try:
            return [DelegateAuditEvent.model_validate(item) for item in raw]
        except ValidationError as exc:
            raise DelegateError(f"Delegate {delegate_id} audit history is invalid.") from exc

    def _append_audit(self, record: DelegateRecord, event: str, actor: str, detail: str = "") -> None:
        """Appends one bounded private lifecycle event using atomic replacement."""
        events = self._read_audit(record.delegate_id)
        events.append(
            DelegateAuditEvent(
                event=event,
                at=self._now(),
                actor=actor,
                state=record.state,
                detail=" ".join(detail.split())[:500],
            )
        )
        events = events[-_MAX_AUDIT_EVENTS:]
        self._atomic_write_json(self._audit_path(record.delegate_id), [event.model_dump(mode="json") for event in events])

    def _new_delegate_id(self) -> str:
        """Returns a compact delegate identifier not already present in the store."""
        for _ in range(20):
            candidate = f"d_{uuid.uuid4().hex[:12]}"
            if not self._delegate_dir(candidate).exists():
                return candidate
        raise DelegateError("Could not allocate a unique delegate identifier.")

    def _read_record(self, delegate_id: str) -> DelegateRecord:
        """Loads one delegate record from its confined state directory."""
        if not delegate_id.startswith("d_") or not delegate_id[2:].isalnum():
            raise DelegateError("Invalid delegate identifier.")
        path = self._delegate_dir(delegate_id) / "record.json"
        try:
            return DelegateRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DelegateError(f"Unknown delegate {delegate_id}.") from exc
        except (OSError, ValidationError) as exc:
            raise DelegateError(f"Delegate {delegate_id} record could not be read.") from exc

    def _write_record(self, record: DelegateRecord) -> None:
        """Atomically replaces one delegate record."""
        path = self._delegate_dir(record.delegate_id) / "record.json"
        self._atomic_write_json(path, record.model_dump(mode="json"))

    def _delegate_dir(self, delegate_id: str) -> Path:
        """Returns the state directory owned by one delegate."""
        return self._config.delegates_dir / delegate_id

    def _audit_path(self, delegate_id: str) -> Path:
        """Returns the private audit-log path owned by one delegate."""
        return self._config.logs_dir / "delegates" / f"{delegate_id}.json"

    @staticmethod
    def _atomic_write_json(path: Path, value: Any) -> None:
        """Writes JSON through a same-directory temporary file and atomic replace."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _now() -> datetime:
        """Returns a timezone-aware UTC timestamp for durable lifecycle records."""
        return datetime.now(UTC)
