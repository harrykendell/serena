"""Unified retention policy for session-owned Serena work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class SessionRetentionPolicy:
    """Defines lifetime and emergency disk budget for retained ChatGPT-session work."""

    max_age: timedelta = timedelta(days=7)
    max_artifact_bytes: int = 10 * 1024 * 1024 * 1024

    def is_expired(self, updated_at: float, now: float) -> bool:
        """:return: whether a session last updated at ``updated_at`` has expired by ``now``."""
        return now - updated_at >= self.max_age.total_seconds()


@dataclass(frozen=True)
class JobRetentionState:
    """Session-retention facts for one persisted durable job."""

    job_id: str
    session_id: str | None
    is_running: bool
    finished_at: float | None


DEFAULT_SESSION_RETENTION = SessionRetentionPolicy()
