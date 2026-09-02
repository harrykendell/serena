"""Configuration and filesystem layout for the Orchestrator MCP."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ORCHESTRATOR_HOME_ENV = "ORCHESTRATOR_HOME"
_CODEX_EXECUTABLE_ENV = "ORCHESTRATOR_CODEX_EXECUTABLE"
_CODEX_CONCURRENCY_ENV = "ORCHESTRATOR_CODEX_CONCURRENCY"
_CODEX_TIMEOUT_ENV = "ORCHESTRATOR_CODEX_TIMEOUT_SECONDS"
_CODEX_MODEL_ENV = "ORCHESTRATOR_CODEX_MODEL"
_CODEX_REASONING_ENV = "ORCHESTRATOR_CODEX_REASONING_EFFORT"
_AUTO_CLAIM_TIMEOUT_ENV = "ORCHESTRATOR_AUTO_CLAIM_TIMEOUT_SECONDS"
_CODEX_AUTO_TOKEN_BUDGET_ENV = "ORCHESTRATOR_CODEX_AUTO_TOKEN_BUDGET"


@dataclass(frozen=True)
class OrchestratorConfig:
    """Defines Orchestrator-owned persistent state and Codex provider configuration."""

    state_root: Path
    codex_executable: str = "codex"
    codex_concurrency: int = 2
    codex_timeout_seconds: float = 1_800.0
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    auto_claim_timeout_seconds: float = 90.0
    codex_auto_token_budget: int | None = None

    @classmethod
    def from_environment(cls, state_root: str | Path | None = None) -> OrchestratorConfig:
        """Creates configuration from explicit paths, environment overrides, or defaults."""
        if state_root is not None:
            root = Path(state_root)
        elif configured_root := os.environ.get(_ORCHESTRATOR_HOME_ENV):
            root = Path(configured_root)
        else:
            root = Path.home() / ".orchestrator"

        concurrency = cls._positive_int(os.environ.get(_CODEX_CONCURRENCY_ENV), default=2)
        timeout_seconds = cls._positive_float(os.environ.get(_CODEX_TIMEOUT_ENV), default=1_800.0)
        auto_claim_timeout_seconds = cls._positive_float(os.environ.get(_AUTO_CLAIM_TIMEOUT_ENV), default=90.0)
        auto_token_budget = cls._optional_positive_int(os.environ.get(_CODEX_AUTO_TOKEN_BUDGET_ENV))
        return cls(
            state_root=root.expanduser().resolve(),
            codex_executable=os.environ.get(_CODEX_EXECUTABLE_ENV, "codex"),
            codex_concurrency=concurrency,
            codex_timeout_seconds=timeout_seconds,
            codex_model=os.environ.get(_CODEX_MODEL_ENV) or None,
            codex_reasoning_effort=os.environ.get(_CODEX_REASONING_ENV) or None,
            auto_claim_timeout_seconds=auto_claim_timeout_seconds,
            codex_auto_token_budget=auto_token_budget,
        )

    @property
    def delegates_dir(self) -> Path:
        """Returns the directory reserved for persisted delegate records."""
        return self.state_root / "delegates"

    @property
    def batches_dir(self) -> Path:
        """Returns the directory reserved for persisted delegate batches."""
        return self.state_root / "batches"

    @property
    def logs_dir(self) -> Path:
        """Returns the directory reserved for Orchestrator audit and provider logs."""
        return self.state_root / "logs"

    @property
    def worktrees_dir(self) -> Path:
        """Returns the directory reserved for Orchestrator-owned Git worktrees."""
        return self.state_root / "worktrees"

    @property
    def provider_state_dir(self) -> Path:
        """Returns the directory reserved for provider-specific state."""
        return self.state_root / "provider-state"

    @property
    def dashboard_sessions_dir(self) -> Path:
        """Returns the directory reserved for retained dashboard session metadata."""
        return self.state_root / "dashboard-sessions"

    @property
    def codex_logs_dir(self) -> Path:
        """Returns the directory reserved for private Codex provider logs."""
        return self.logs_dir / "providers" / "codex"

    def ensure_state_layout(self) -> None:
        """Creates the Orchestrator-owned state directories if necessary."""
        for path in (
            self.delegates_dir,
            self.batches_dir,
            self.logs_dir,
            self.worktrees_dir,
            self.provider_state_dir,
            self.dashboard_sessions_dir,
            self.codex_logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _positive_int(value: str | None, *, default: int) -> int:
        """Parses one strictly positive integer environment value."""
        if value is None:
            return default
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("Codex concurrency must be positive.")
        return parsed

    @staticmethod
    def _optional_positive_int(value: str | None) -> int | None:
        """Parses one optional strictly positive integer environment value."""
        if value is None:
            return None
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("Codex auto token budget must be positive.")
        return parsed

    @staticmethod
    def _positive_float(value: str | None, *, default: float) -> float:
        """Parses one strictly positive floating-point environment value."""
        if value is None:
            return default
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("Codex timeout must be positive.")
        return parsed
