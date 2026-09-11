#!/usr/bin/env python3
"""Measure compact Orchestrator dashboard query scaling against retained delegate history.

This is a regression benchmark rather than a pytest test. It builds canonical Orchestrator
records through ``DelegateStore`` and reports the one-time index rebuild plus periodic overview
and selected-session query costs. Run it from the project root with
``uv run python scripts/benchmark_orchestrator_dashboard.py --json``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from orchestrator.config import OrchestratorConfig
from orchestrator.dashboard_sessions import OrchestratorDashboardSessionArchive
from orchestrator.delegates import CreateDelegateRequest, DelegateStore
from serena.custom_dashboard import DashboardOrchestratorOverview


def _delegate_request() -> CreateDelegateRequest:
    """Returns one small canonical delegate request for scale fixtures."""
    return CreateDelegateRequest.model_validate(
        {
            "project_name": "serena",
            "kind": "explore",
            "goal": "Measure compact retained Orchestrator dashboard queries.",
            "acceptance_criteria": ["The retained dashboard query remains compact and complete."],
        }
    )


def _sqlite_bytes(path: Path) -> int:
    """Returns the live SQLite database plus WAL/SHM footprint."""
    return sum(candidate.stat().st_size for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if candidate.exists())


class _Case:
    """Owns one synthetic retained Orchestrator benchmark case."""

    def __init__(self, root: Path, session_count: int, selected_delegate_count: int) -> None:
        self._session_count = session_count
        self._selected_delegate_count = selected_delegate_count
        self._config = OrchestratorConfig.from_environment(root)
        self._archive = OrchestratorDashboardSessionArchive(self._config)
        store = DelegateStore(self._config)
        request = _delegate_request()

        for index in range(session_count):
            session_id = f"benchmark-session-{index:05d}"
            self._archive.set_display_name(session_id, f"Synthetic orchestration {index:05d}")
            store.create(session_id, request)

        self._selected_session_id = f"benchmark-session-{session_count - 1:05d}"
        for _ in range(max(0, selected_delegate_count - 1)):
            store.create(self._selected_session_id, request)

        started = time.perf_counter()
        self._store = DelegateStore(self._config)
        self._startup_ms = (time.perf_counter() - started) * 1000.0
        self._overview = DashboardOrchestratorOverview(self._store, self._archive)
        self._selected_panel_id = self._archive.panel_id_for_session(self._selected_session_id)

    def measure(self, samples: int) -> dict[str, Any]:
        """Measures compact overview and direct selected-session construction."""
        overview_ms: list[float] = []
        overview_bytes: list[int] = []
        selected_ms: list[float] = []
        selected_bytes: list[int] = []

        for _ in range(samples):
            started = time.perf_counter()
            overview = self._overview.get_panels()
            overview_ms.append((time.perf_counter() - started) * 1000.0)
            overview_bytes.append(len(json.dumps(overview, separators=(",", ":")).encode("utf-8")))

            started = time.perf_counter()
            selected = self._overview.get_panel(self._selected_panel_id)
            selected_ms.append((time.perf_counter() - started) * 1000.0)
            selected_bytes.append(len(json.dumps(selected, separators=(",", ":")).encode("utf-8")))

        panels = self._overview.get_panels()["panels"]
        selected = self._overview.get_panel(self._selected_panel_id)
        if len(panels) != self._session_count:
            raise RuntimeError(f"Expected {self._session_count} panels, got {len(panels)}")
        if len(selected["delegates"]) != self._selected_delegate_count:
            raise RuntimeError(f"Expected {self._selected_delegate_count} selected delegates, got {len(selected['delegates'])}")

        index_path = self._config.delegates_dir / ".dashboard-index.sqlite3"
        return {
            "sessions": self._session_count,
            "selected_delegates": self._selected_delegate_count,
            "startup_rebuild_ms": round(self._startup_ms, 3),
            "overview_median_ms": round(statistics.median(overview_ms), 3),
            "overview_min_ms": round(min(overview_ms), 3),
            "overview_max_ms": round(max(overview_ms), 3),
            "overview_bytes": int(statistics.median(overview_bytes)),
            "selected_median_ms": round(statistics.median(selected_ms), 3),
            "selected_bytes": int(statistics.median(selected_bytes)),
            "dashboard_index_bytes": _sqlite_bytes(index_path),
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, nargs="+", default=[100, 1000])
    parser.add_argument("--selected-delegates", type=int, default=64)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="serena-orchestrator-dashboard-benchmark-") as temporary_root:
        root = Path(temporary_root)
        for session_count in args.sessions:
            case = _Case(root / f"case-{session_count}", session_count, args.selected_delegates)
            results.append(case.measure(args.samples))

    if args.json_output:
        print(json.dumps(results, indent=2))
        return
    for result in results:
        print(
            f"sessions={result['sessions']} selected={result['selected_delegates']} "
            f"startup={result['startup_rebuild_ms']:.3f}ms "
            f"overview={result['overview_median_ms']:.3f}ms/{result['overview_bytes']}B "
            f"selected={result['selected_median_ms']:.3f}ms/{result['selected_bytes']}B "
            f"index={result['dashboard_index_bytes']}B"
        )


if __name__ == "__main__":
    main()
