"""Durable scheduling for ChatGPT-first automatic provider fallback."""

from __future__ import annotations

import logging
import threading

from orchestrator.config import OrchestratorConfig
from orchestrator.delegates import DelegateStore
from orchestrator.providers import ProviderRouter

log = logging.getLogger(__name__)


class AutoFallbackScheduler:
    """Routes expired ``provider=auto`` delegates to Codex from persisted state."""

    def __init__(self, config: OrchestratorConfig, store: DelegateStore, providers: ProviderRouter) -> None:
        self._store = store
        self._providers = providers
        self._stop = threading.Event()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._poll_interval_seconds = min(1.0, max(0.01, config.auto_claim_timeout_seconds / 5.0))

        # recover transitions that were persisted before a previous server stopped
        self.run_once()
        if self._store.has_waiting_auto():
            self.notify()

    def notify(self) -> None:
        """Ensures deadline polling is active after auto work is created or rerouted."""
        with self._thread_lock:
            if self._stop.is_set():
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="orchestrator-auto-fallback", daemon=True)
            self._thread.start()

    def run_once(self) -> None:
        """Processes persisted recovery work and any newly expired claim windows once."""
        for delegate_id in self._store.queued_auto_delegate_ids():
            self._providers.start_codex(delegate_id)

        due = self._store.due_auto_delegate_ids()
        if not due:
            return

        availability = self._providers.availability_for_auto()
        if not availability.available:
            reason = availability.reason or "Codex automatic fallback is currently unavailable."
            for delegate_id in due:
                self._store.note_auto_fallback_blocked(delegate_id, reason)
            return

        for delegate_id in due:
            if self._store.route_auto_to_codex(delegate_id):
                self._providers.start_codex(delegate_id)

    def close(self) -> None:
        """Stops the local scheduler thread without changing persisted delegate state."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self._poll_interval_seconds * 2.0))

    def _run(self) -> None:
        """Polls persisted deadlines so restart recovery does not depend on in-memory timers."""
        while not self._stop.wait(self._poll_interval_seconds):
            try:
                self.run_once()
            except Exception:
                log.exception("Auto fallback scheduler iteration failed")
