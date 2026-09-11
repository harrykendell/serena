#!/usr/bin/env python3
"""Measure retained-dashboard state scaling against a synthetic execution history.

This is a regression benchmark rather than a pytest test. It deliberately builds retained
state directly so fixture setup does not dominate the measurement. Run it from the project
root with ``uv run python scripts/benchmark_dashboard_state.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import Flask

from serena.custom_dashboard import CustomDashboard
from serena.execution_store import _STATE_VERSION, ActivityPanelRun, ExecutionRecord, ExecutionStore, SessionRecord
from serena.git_metrics import GitLineMetrics
from serena.jobs import (
    JobBackend,
    JobManager,
    JobOutputChunk,
    JobPersistenceInfo,
    JobRecord,
    JobRuntimeInfo,
    JobSnapshot,
    JobStatus,
    JobStore,
)


class _BenchmarkJobBackend(JobBackend):
    """Keeps synthetic running records live without external process probes."""

    def start(self, record: JobRecord, command_file: Path, state_file: Path) -> None:
        raise AssertionError("benchmark does not start jobs")

    def is_running(self, record: JobRecord) -> bool:
        return record.status is JobStatus.RUNNING

    def cancel(self, record: JobRecord) -> None:
        raise AssertionError("benchmark does not cancel jobs")

    def read_output(
        self,
        record: JobRecord,
        cursor: str | None,
        max_chars: int,
        output_mode: str = "latest",
    ) -> JobOutputChunk:
        raise AssertionError("overview benchmark must not read job output")

    def read_output_before(self, record: JobRecord, cursor: str, max_chars: int) -> JobOutputChunk:
        raise AssertionError("overview benchmark must not read job output")

    def runtime_info(self, record: JobRecord) -> JobRuntimeInfo:
        raise AssertionError("overview benchmark must not collect job telemetry")

    def persistence_info(self) -> JobPersistenceInfo:
        return JobPersistenceInfo(
            survives_serena_restart=True,
            survives_logout=True,
            survives_reboot=True,
            linger_enabled=True,
        )


class _CountingJobManager:
    """Instrumented facade over the real durable-job manager."""

    def __init__(self, root: Path, records: list[JobRecord]) -> None:
        store = JobStore(root / "jobs")
        for record in records:
            store.create(record)
        self._manager = JobManager(store=store, backend=_BenchmarkJobBackend())
        self.snapshot_queries = 0
        self.telemetry_operations = 0

    @property
    def max_concurrent_jobs(self) -> int:
        """Returns the real manager's configured concurrency limit."""
        return self._manager.max_concurrent_jobs

    def reset_counts(self) -> None:
        """Resets per-request instrumentation."""
        self.snapshot_queries = 0
        self.telemetry_operations = 0

    def list_job_snapshots(self, limit: int = 20, running_only: bool = False) -> list[JobSnapshot]:
        """Returns real snapshots while counting telemetry work."""
        self.snapshot_queries += 1
        snapshots = self._manager.list_job_snapshots(limit=limit, running_only=running_only)
        self.telemetry_operations += len(snapshots)
        return snapshots

    def list_running_jobs(self) -> list[JobRecord]:
        """Returns real running metadata while counting one overview query."""
        self.snapshot_queries += 1
        return self._manager.list_running_jobs()

    def get_job_record(self, job_id: str) -> JobRecord:
        """Returns one real lightweight retained job record."""
        return self._manager.get_job_record(job_id)

    def get_job_records(self, job_ids: set[str]) -> list[JobRecord]:
        """Returns selected real lightweight retained job records."""
        return self._manager.get_job_records(job_ids)

    def get_job(self, job_id: str) -> JobSnapshot:
        """Returns real job detail while counting its telemetry work."""
        self.telemetry_operations += 1
        return self._manager.get_job(job_id)

    @staticmethod
    def persistence_info() -> JobPersistenceInfo:
        """Returns stable persistence metadata without probing the workstation."""
        return JobPersistenceInfo(
            survives_serena_restart=True,
            survives_logout=True,
            survives_reboot=True,
            linger_enabled=True,
        )


class _BenchmarkAgent:
    """Small agent facade exposing only data consumed by ``CustomDashboard``."""

    version = "benchmark"

    def __init__(self, store: ExecutionStore, jobs: _CountingJobManager) -> None:
        self.execution_store = store
        self.job_manager = jobs
        self.git_cache_reads = 0
        self.git_refreshes = 0

    def reset_counts(self) -> None:
        """Resets per-request Git instrumentation."""
        self.git_cache_reads = 0
        self.git_refreshes = 0

    @staticmethod
    def get_default_project() -> None:
        return None

    @staticmethod
    def get_active_tool_names() -> list[str]:
        return []

    @staticmethod
    def get_exposed_tool_instances() -> list[Any]:
        return []

    def get_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        """Counts cached metric lookups; no subprocess or refresh is performed."""
        self.git_cache_reads += 1
        if project_name == "serena":
            return GitLineMetrics(additions=17, deletions=4, ahead_commits=3)
        return None

    def refresh_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        """Counts explicit refreshes if a dashboard path unexpectedly performs one."""
        self.git_refreshes += 1
        return self.get_project_git_metrics(project_name)


class _Case:
    """Owns one synthetic retained-history benchmark case."""

    def __init__(self, root: Path, session_count: int, calls_per_session: int, job_count: int, running_job_count: int) -> None:
        self._root = root
        self._session_count = session_count
        self._calls_per_session = calls_per_session
        self._job_count = job_count
        self._running_job_count = min(running_job_count, job_count)
        self._write_execution_state()
        self.store = ExecutionStore(root)
        self.jobs = _CountingJobManager(root, self._job_records())
        self.agent = _BenchmarkAgent(self.store, self.jobs)

        orchestrator_home = root / "orchestrator-home"
        orchestrator_home.mkdir(parents=True, exist_ok=True)
        os.environ["ORCHESTRATOR_HOME"] = str(orchestrator_home)
        app = Flask(f"dashboard-scale-{session_count}")
        self.dashboard = CustomDashboard(app, self.agent)
        self.client = app.test_client()

    def _write_execution_state(self) -> None:
        """Writes a current-schema state fixture with deliberately non-trivial historical bodies."""
        now = time.time()
        sessions: dict[str, dict[str, Any]] = {}
        executions: dict[str, dict[str, Any]] = {}
        activity_runs: dict[str, dict[str, Any]] = {}
        argument_padding = "argument-value-" * 96
        result_padding = "result-value-" * 512

        for session_index in range(self._session_count):
            session_id = f"benchmark-session-{session_index:04d}"
            created_at = now - float(self._session_count - session_index)
            updated_at = created_at + self._calls_per_session * 0.1
            session = SessionRecord(
                session_id=session_id,
                panel_id=ExecutionStore.panel_id_for_session(session_id),
                created_at=created_at,
                updated_at=updated_at,
                display_name=f"Synthetic retained session {session_index:04d}",
                project_name="serena",
            )
            sessions[session_id] = asdict(session)

            for call_index in range(self._calls_per_session):
                execution_id = f"execution-{session_index:04d}-{call_index:02d}"
                arguments = {
                    "relative_path": f"src/synthetic/{session_index:04d}.py",
                    "needle": "synthetic-target",
                    "payload": argument_padding,
                    "call_index": call_index,
                }
                result = json.dumps(
                    {
                        "status": "success",
                        "summary": f"synthetic result {session_index}:{call_index}",
                        "body": result_padding,
                    },
                    separators=(",", ":"),
                )
                job_id = f"{session_index:032x}" if call_index == 0 and session_index < self._job_count else None
                execution = ExecutionRecord(
                    execution_id=execution_id,
                    session_id=session_id,
                    project_name="serena",
                    tool_name="replace_content",
                    arguments=arguments,
                    started_at=created_at + call_index * 0.1,
                    status="completed",
                    finished_at=created_at + call_index * 0.1 + 0.05,
                    request_finished_at=created_at + call_index * 0.1 + 0.05,
                    result=result,
                    durable_job_id=job_id,
                    durable_job_label=f"Synthetic job {session_index}" if job_id else None,
                )
                executions[execution_id] = asdict(execution)

            run_id = f"run-{session_index:04d}"
            activity_runs[run_id] = asdict(
                ActivityPanelRun(
                    run_id=run_id,
                    session_id=session_id,
                    project_name="serena",
                    started_at=created_at,
                    superseded=True,
                    execution_ids=[f"execution-{session_index:04d}-{call_index:02d}" for call_index in range(self._calls_per_session)],
                )
            )

        payload = {
            "version": _STATE_VERSION,
            "sessions": sessions,
            "executions": executions,
            "activity_runs": activity_runs,
        }
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "state.json").write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")

    def _job_records(self) -> list[JobRecord]:
        """Returns real retained job records with bounded synthetic running history."""
        now = "2026-09-11T12:00:00+00:00"
        records: list[JobRecord] = []
        for index in range(self._job_count):
            running = index < self._running_job_count
            records.append(
                JobRecord(
                    job_id=f"{index:032x}",
                    unit_name=f"serena-job-{index:032x}.service",
                    project_root=str(self._root),
                    cwd=str(self._root),
                    status=JobStatus.RUNNING if running else JobStatus.COMPLETED,
                    created_at=now,
                    session_id=(f"benchmark-session-{index:04d}" if index < self._session_count else None),
                    project_name="serena",
                    label=f"Synthetic job {index}",
                    finished_at=None if running else now,
                    return_code=None if running else 0,
                )
            )
        return records

    def measure(self, samples: int) -> dict[str, Any]:
        """Measures cold-cache ``/dashboard/api/state`` response construction."""
        elapsed_ms: list[float] = []
        payload_bytes: list[int] = []
        git_reads: list[int] = []
        git_refreshes: list[int] = []
        job_queries: list[int] = []
        telemetry_operations: list[int] = []
        returned_panels: list[int] = []

        for _ in range(samples):
            self.agent.reset_counts()
            self.jobs.reset_counts()
            started = time.perf_counter()
            response = self.client.get("/dashboard/api/state")
            elapsed_ms.append((time.perf_counter() - started) * 1000.0)
            if response.status_code != 200:
                raise RuntimeError(f"dashboard state request failed: HTTP {response.status_code}")
            payload_bytes.append(len(response.data))
            data = response.get_json()
            returned_panels.append(len(data["serena"]["panels"]))
            git_reads.append(self.agent.git_cache_reads)
            git_refreshes.append(self.agent.git_refreshes)
            job_queries.append(self.jobs.snapshot_queries)
            telemetry_operations.append(self.jobs.telemetry_operations)

        return {
            "sessions": self._session_count,
            "calls_per_session": self._calls_per_session,
            "retained_jobs": self._job_count,
            "running_jobs": self._running_job_count,
            "terminal_jobs": self._job_count - self._running_job_count,
            "returned_panels": returned_panels[-1],
            "median_ms": round(statistics.median(elapsed_ms), 3),
            "min_ms": round(min(elapsed_ms), 3),
            "max_ms": round(max(elapsed_ms), 3),
            "response_bytes": int(statistics.median(payload_bytes)),
            "git_cache_reads_per_request": int(statistics.median(git_reads)),
            "git_refreshes_per_request": int(statistics.median(git_refreshes)),
            "running_job_metadata_queries_per_request": int(statistics.median(job_queries)),
            "job_telemetry_operations_per_request": int(statistics.median(telemetry_operations)),
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, nargs="+", default=[10, 100, 1000])
    parser.add_argument("--calls-per-session", type=int, default=4)
    parser.add_argument("--terminal-jobs", type=int, default=100)
    parser.add_argument("--running-jobs", type=int, default=3)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="serena-dashboard-scale-") as temporary_root:
        root = Path(temporary_root)
        for session_count in args.sessions:
            case = _Case(
                root / str(session_count),
                session_count,
                args.calls_per_session,
                args.terminal_jobs + args.running_jobs,
                args.running_jobs,
            )
            results.append(case.measure(args.samples))

    if args.json_output:
        print(json.dumps(results, indent=2))
        return

    print("sessions calls jobs panels median_ms min_ms max_ms response_bytes git_reads git_refreshes job_queries telemetry_ops")
    for result in results:
        print(
            result["sessions"],
            result["calls_per_session"],
            result["terminal_jobs"],
            result["returned_panels"],
            result["median_ms"],
            result["min_ms"],
            result["max_ms"],
            result["response_bytes"],
            result["git_cache_reads_per_request"],
            result["git_refreshes_per_request"],
            result["job_snapshot_queries_per_request"],
            result["job_telemetry_operations_per_request"],
        )


if __name__ == "__main__":
    main()
