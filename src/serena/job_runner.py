"""Minimal subprocess runner for transient Serena systemd jobs."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import psutil

from serena.jobs import JobStatus, JobStore

_TERMINATION_GRACE_SECONDS = 2.0


def _owned_job_processes(job_id: str, process: subprocess.Popen[str]) -> list[psutil.Process]:
    """Return all discoverable child/job-owned processes except the runner itself."""
    processes: dict[int, psutil.Process] = {}
    try:
        root = psutil.Process(process.pid)
        processes[root.pid] = root
        for child in root.children(recursive=True):
            processes[child.pid] = child
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # The inherited marker survives cgroup/session changes and typical daemonisation/reparenting.
    for candidate in psutil.process_iter():
        if candidate.pid == os.getpid():
            continue
        try:
            if candidate.environ().get("SERENA_JOB_ID") == job_id:
                processes[candidate.pid] = candidate
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return list(processes.values())


def _terminate_job_processes(process: subprocess.Popen[str], job_id: str) -> None:
    """Terminate every discoverable process owned by the job, then hard-kill survivors."""
    targets = _owned_job_processes(job_id, process)
    for target in reversed(targets):
        try:
            if target.is_running():
                target.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    _, alive = psutil.wait_procs(targets, timeout=_TERMINATION_GRACE_SECONDS)
    for target in alive:
        try:
            target.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if alive:
        psutil.wait_procs(alive, timeout=_TERMINATION_GRACE_SECONDS)

    # Catch an environment-clearing process that nevertheless remained in the command's primary process group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_job(state_file: Path, command_file: Path) -> int:
    """Run a stored command and persist its natural terminal status before exiting."""
    store = JobStore(state_file.parent)
    record = store.read(state_file.stem)
    process: subprocess.Popen[str] | None = None

    def handle_termination(signum: int, frame: FrameType | None) -> None:
        del frame
        if process is not None:
            _terminate_job_processes(process, record.job_id)
        # Leave the persisted job state as RUNNING. The manager that requested cancellation, or a
        # later reconciliation after a systemd timeout/external stop, owns the terminal status.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_termination)
    signal.signal(signal.SIGINT, handle_termination)

    try:
        # consume the private command file once so command text is not retained by Serena
        command = command_file.read_text(encoding="utf-8")
        command_file.unlink(missing_ok=True)

        # Give the command a dedicated process group/session. Snap applications can move themselves to
        # another cgroup, but the process group remains a useful fallback ownership boundary.
        process = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            text=True,
            cwd=record.cwd,
            start_new_session=True,
        )
        store.update(record.job_id, process_group_id=process.pid)
        return_code = process.wait()
        status = JobStatus.COMPLETED if return_code == 0 else JobStatus.FAILED
        status_message = "Job completed successfully." if return_code == 0 else f"Job exited with status {return_code}."
    except Exception as e:
        command_file.unlink(missing_ok=True)
        if process is not None:
            _terminate_job_processes(process, record.job_id)
        return_code = 127
        status = JobStatus.FAILED
        status_message = f"Job runner failed: {e.__class__.__name__}: {e}"

    store.update(
        record.job_id,
        status=status,
        finished_at=datetime.now(UTC).isoformat(),
        return_code=return_code,
        status_message=status_message,
    )
    return return_code


def main() -> None:
    """Run the job runner CLI."""
    parser = argparse.ArgumentParser(description="Run one persisted Serena background job.")
    parser.add_argument("state_file", type=Path)
    parser.add_argument("command_file", type=Path)
    args = parser.parse_args()
    raise SystemExit(run_job(args.state_file, args.command_file))


if __name__ == "__main__":
    main()
