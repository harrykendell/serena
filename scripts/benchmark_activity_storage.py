#!/usr/bin/env python3
"""Measure the indexed execution and durable-job storage scaling gates.

Run from the project root with ``uv run python scripts/benchmark_activity_storage.py --json``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from serena.execution_store import ExecutionStore
from serena.jobs import JobRecord, JobStatus, JobStore


def _sqlite_bytes(path: Path) -> int:
    """Returns one SQLite database plus its live WAL/SHM footprint."""
    return sum(candidate.stat().st_size for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if candidate.exists())


def _execution_case(root: Path, retained: int, probes: int) -> dict[str, Any]:
    """Measures store startup and start/finish bookkeeping with retained execution history."""
    store = ExecutionStore(root)
    with store.batch_updates():
        for index in range(retained):
            execution_id = f"retained-{index:08d}"
            store.start_execution(
                execution_id=execution_id,
                session_id="retained-session",
                project_name="serena",
                tool_name="read_file",
                arguments={"relative_path": f"file-{index}.txt"},
            )
            store.finish_execution(execution_id, succeeded=True, result="ok")

    started = time.perf_counter()
    measured = ExecutionStore(root)
    startup_ms = (time.perf_counter() - started) * 1000.0

    elapsed_ms: list[float] = []
    for index in range(probes):
        execution_id = f"probe-{index:08d}"
        started = time.perf_counter()
        measured.start_execution(
            execution_id=execution_id,
            session_id="retained-session",
            project_name="serena",
            tool_name="get_current_config",
            arguments={},
        )
        measured.finish_execution(execution_id, succeeded=True, result="ok")
        elapsed_ms.append((time.perf_counter() - started) * 1000.0)

    return {
        "retained_executions": retained,
        "startup_ms": round(startup_ms, 3),
        "start_finish_mean_ms": round(statistics.mean(elapsed_ms), 3),
        "start_finish_median_ms": round(statistics.median(elapsed_ms), 3),
        "sqlite_bytes": _sqlite_bytes(root / "state.sqlite3"),
    }


def _job_case(root: Path, terminal: int, probes: int) -> dict[str, Any]:
    """Measures indexed zero-running-job discovery with terminal history present."""
    store = JobStore(root)
    now = datetime.now(UTC).isoformat()
    for index in range(terminal):
        job_id = f"{index:032x}"
        store.create(
            JobRecord(
                job_id=job_id,
                unit_name=f"serena-job-{job_id}.service",
                project_root="/tmp/serena",
                cwd="/tmp/serena",
                status=JobStatus.COMPLETED,
                created_at=now,
                session_id="retained-session",
                project_name="serena",
                label=f"Terminal job {index}",
                finished_at=now,
                return_code=0,
            )
        )

    elapsed_ms: list[float] = []
    for _ in range(probes):
        started = time.perf_counter()
        records = store.list_running_records()
        elapsed_ms.append((time.perf_counter() - started) * 1000.0)
        if records:
            raise RuntimeError("Expected zero running jobs")

    return {
        "terminal_jobs": terminal,
        "running_query_mean_ms": round(statistics.mean(elapsed_ms), 4),
        "running_query_median_ms": round(statistics.median(elapsed_ms), 4),
        "sqlite_bytes": _sqlite_bytes(root / "state.sqlite3"),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executions", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--jobs", type=int, nargs="+", default=[100, 1000, 5000])
    parser.add_argument("--probes", type=int, default=100)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result: dict[str, Any] = {"executions": [], "jobs": []}
    with tempfile.TemporaryDirectory(prefix="serena-storage-benchmark-") as temporary_root:
        base = Path(temporary_root)
        for retained in args.executions:
            result["executions"].append(_execution_case(base / f"executions-{retained}", retained, args.probes))
        for terminal in args.jobs:
            result["jobs"].append(_job_case(base / f"jobs-{terminal}", terminal, args.probes))

    if args.json_output:
        print(json.dumps(result, indent=2))
        return
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
