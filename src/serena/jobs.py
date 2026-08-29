"""Persistent long-running job execution for Serena."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import psutil
from filelock import FileLock

from serena.config.serena_config import SerenaPaths

DEFAULT_MAX_CONCURRENT_JOBS = 6
DEFAULT_OUTPUT_CHAR_LIMIT = 12_000
DEFAULT_JOB_RETENTION = timedelta(days=7)
_MAX_CURSOR_LENGTH = 4096
_JOB_UNIT_PREFIX = "serena-job-"
_ANSI_ESCAPE_RE = re.compile(r"(?:\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\\\))")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INHERITED_ENVIRONMENT_VARIABLES = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "SERENA_HOME",
    "NIX_PATH",
    "NIX_PROFILES",
    "LD_LIBRARY_PATH",
    "CUDA_VISIBLE_DEVICES",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


class JobStatus(str, Enum):
    """Lifecycle state of a Serena background job."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        """:return: whether no further execution is expected for this state."""
        return self is not JobStatus.RUNNING


@dataclass(frozen=True)
class JobRecord:
    """Persisted metadata for one background job."""

    job_id: str
    unit_name: str
    project_root: str
    cwd: str
    status: JobStatus
    created_at: str
    project_name: str | None = None
    label: str | None = None
    timeout_seconds: int | None = None
    process_group_id: int | None = None
    finished_at: str | None = None
    return_code: int | None = None
    status_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """:return: JSON-serialisable representation of the record."""
        result = asdict(self)
        result["status"] = self.status.value
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        """Construct a record from persisted JSON data."""
        return cls(
            job_id=str(data["job_id"]),
            unit_name=str(data["unit_name"]),
            project_root=str(data["project_root"]),
            cwd=str(data["cwd"]),
            status=JobStatus(str(data["status"])),
            created_at=str(data["created_at"]),
            project_name=str(data["project_name"]) if data.get("project_name") is not None else None,
            label=str(data["label"]) if data.get("label") is not None else None,
            timeout_seconds=int(data["timeout_seconds"]) if data.get("timeout_seconds") is not None else None,
            process_group_id=int(data["process_group_id"]) if data.get("process_group_id") is not None else None,
            finished_at=str(data["finished_at"]) if data.get("finished_at") is not None else None,
            return_code=int(data["return_code"]) if data.get("return_code") is not None else None,
            status_message=str(data["status_message"]) if data.get("status_message") is not None else None,
        )


@dataclass(frozen=True)
class JobOutputChunk:
    """Bounded journal output together with its navigation cursors."""

    output: str
    next_cursor: str | None
    has_more_output: bool
    oldest_cursor: str | None = None
    has_earlier_output: bool = False
    output_truncated: bool = False
    earlier_output_omitted: bool = False
    cursor_reset: bool = False


@dataclass(frozen=True)
class JobRuntimeInfo:
    """Lightweight runtime telemetry for one job."""

    elapsed_seconds: float
    seconds_since_last_output: float | None
    memory_bytes: int | None
    cpu_seconds: float | None
    process_count: int | None


@dataclass(frozen=True)
class JobPersistenceInfo:
    """Persistence guarantees provided by the current user-systemd session."""

    survives_serena_restart: bool
    survives_logout: bool
    survives_reboot: bool
    linger_enabled: bool


@dataclass(frozen=True)
class JobSnapshot:
    """Current observable state of a job and, optionally, new output."""

    record: JobRecord
    runtime: JobRuntimeInfo
    output: JobOutputChunk | None = None


class JobLimitError(RuntimeError):
    """Raised when starting a job would exceed the concurrency limit."""


class JobBackend(ABC):
    """Execution backend for persistent Serena jobs."""

    @abstractmethod
    def start(self, record: JobRecord, command_file: Path, state_file: Path) -> None:
        """Start ``record`` without waiting for completion."""

    @abstractmethod
    def is_running(self, record: JobRecord) -> bool:
        """Return whether the job's process tree is still running."""

    @abstractmethod
    def cancel(self, record: JobRecord) -> None:
        """Stop the job's complete process tree."""

    @abstractmethod
    def read_output(
        self,
        record: JobRecord,
        cursor: str | None,
        max_chars: int,
        output_mode: Literal["latest", "start"] = "latest",
    ) -> JobOutputChunk:
        """Read bounded job output using the requested initial-output mode."""

    @abstractmethod
    def read_output_before(self, record: JobRecord, cursor: str, max_chars: int) -> JobOutputChunk:
        """Read bounded job output immediately preceding ``cursor``."""

    @abstractmethod
    def runtime_info(self, record: JobRecord) -> JobRuntimeInfo:
        """Return lightweight runtime telemetry for ``record``."""

    @abstractmethod
    def persistence_info(self) -> JobPersistenceInfo:
        """Return persistence guarantees for jobs owned by this backend."""


class SystemdJobBackend(JobBackend):
    """Runs jobs as transient user systemd services with journald output."""

    _STOP_TIMEOUT_SECONDS = 5

    def start(self, record: JobRecord, command_file: Path, state_file: Path) -> None:
        # construct a self-contained transient service outside Serena's own cgroup
        args = [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            f"--unit={record.unit_name}",
            f"--description=Serena background job {record.job_id}",
            f"--working-directory={record.cwd}",
            "--property=Type=exec",
            "--property=KillMode=mixed",
            f"--property=TimeoutStopSec={self._STOP_TIMEOUT_SECONDS}s",
            "--property=StandardOutput=journal",
            "--property=StandardError=journal",
            f"--property=SyslogIdentifier={record.unit_name}",
            f"--setenv=SERENA_JOB_ID={record.job_id}",
            "--setenv=PYTHONUNBUFFERED=1",
            "--setenv=PYTHONIOENCODING=utf-8",
        ]
        if record.timeout_seconds is not None:
            args.append(f"--property=RuntimeMaxSec={record.timeout_seconds}s")
        for name in _INHERITED_ENVIRONMENT_VARIABLES:
            value = os.environ.get(name)
            if value is not None:
                args.append(f"--setenv={name}={value}")
        args.extend(["--", sys.executable, "-m", "serena.job_runner", str(state_file), str(command_file)])

        result = subprocess.run(
            args,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"systemd-run failed with status {result.returncode}: {detail}")

    def is_running(self, record: JobRecord) -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", record.unit_name],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
        if result.returncode in (3, 4):
            return False
        raise RuntimeError(f"Unable to query systemd unit {record.unit_name!r} (status {result.returncode})")

    def cancel(self, record: JobRecord) -> None:
        # Snapshot job-owned processes before stopping the unit. Descendants may live in Snap scopes
        # or independent sessions/cgroups, but retain the inherited SERENA_JOB_ID environment marker.
        owned_processes = self._owned_job_processes(record.job_id)

        result = subprocess.run(
            ["systemctl", "--user", "stop", record.unit_name],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "not loaded" not in detail.lower() and "not found" not in detail.lower():
                raise RuntimeError(f"Unable to stop systemd unit {record.unit_name!r}: {detail}")

        # The runner should have shut its tree down cleanly. These hard-kill fallbacks catch any
        # descendants that escaped before the runner handled termination or survived a runner crash.
        self._kill_processes(owned_processes)
        self._kill_processes(self._owned_job_processes(record.job_id))
        self._kill_persisted_process_group(record.process_group_id)

    def read_output(
        self,
        record: JobRecord,
        cursor: str | None,
        max_chars: int,
        output_mode: Literal["latest", "start"] = "latest",
    ) -> JobOutputChunk:
        if output_mode not in ("latest", "start"):
            raise ValueError(f"Unsupported output mode {output_mode!r}")
        if cursor is not None and (len(cursor) > _MAX_CURSOR_LENGTH or "\x00" in cursor or "\n" in cursor):
            raise ValueError("Invalid journal cursor")
        if cursor is not None:
            return self._read_incremental_output(record, cursor, max_chars)
        if output_mode == "start":
            return self._read_from_start(record, max_chars)
        return self._read_recent_output(record, max_chars)

    def read_output_before(self, record: JobRecord, cursor: str, max_chars: int) -> JobOutputChunk:
        """Read bounded journal output immediately preceding ``cursor``."""
        if len(cursor) > _MAX_CURSOR_LENGTH or "\x00" in cursor or "\n" in cursor:
            raise ValueError("Invalid journal cursor")
        try:
            return self._read_previous_output(record, cursor, max_chars)
        except RuntimeError as e:
            if not self._is_stale_cursor_error(str(e)):
                raise
            return JobOutputChunk(
                output="",
                next_cursor=None,
                has_more_output=False,
                cursor_reset=True,
            )

    def runtime_info(self, record: JobRecord) -> JobRuntimeInfo:
        now = datetime.now(UTC)
        created_at = datetime.fromisoformat(record.created_at)
        terminal_time = datetime.fromisoformat(record.finished_at) if record.finished_at is not None else now
        elapsed_seconds = max(0.0, (terminal_time - created_at).total_seconds())

        memory_bytes: int | None = None
        cpu_seconds: float | None = None
        process_count: int | None = None
        seconds_since_last_output: float | None = None
        if record.status is JobStatus.RUNNING:
            main_pid = self._main_pid(record)
            processes = self._process_tree(main_pid) if main_pid is not None else self._owned_job_processes(record.job_id)
            if processes:
                process_count = len(processes)
                memory_total = 0
                cpu_total = 0.0
                observed_memory = False
                observed_cpu = False
                for process in processes:
                    try:
                        memory_total += process.memory_info().rss
                        observed_memory = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    try:
                        times = process.cpu_times()
                        cpu_total += times.user + times.system
                        observed_cpu = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                memory_bytes = memory_total if observed_memory else None
                cpu_seconds = cpu_total if observed_cpu else None

            last_output_at = self._last_output_at(record)
            seconds_since_last_output = None if last_output_at is None else max(0.0, (now - last_output_at).total_seconds())

        return JobRuntimeInfo(
            elapsed_seconds=elapsed_seconds,
            seconds_since_last_output=seconds_since_last_output,
            memory_bytes=memory_bytes,
            cpu_seconds=cpu_seconds,
            process_count=process_count,
        )

    def persistence_info(self) -> JobPersistenceInfo:
        result = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        linger_enabled = result.returncode == 0 and result.stdout.strip().lower() == "yes"
        return JobPersistenceInfo(
            survives_serena_restart=True,
            survives_logout=linger_enabled,
            survives_reboot=False,
            linger_enabled=linger_enabled,
        )

    def _read_recent_output(self, record: JobRecord, max_chars: int) -> JobOutputChunk:
        # read newest journal entries first so the first chat poll reaches current progress immediately
        process = self._start_journal_reader(record, reverse=True)
        assert process.stdout is not None
        assert process.stderr is not None

        messages: list[str] = []
        output_chars = 0
        newest_cursor: str | None = None
        oldest_cursor: str | None = None
        earlier_output_omitted = False
        output_truncated = False

        try:
            for raw_line in process.stdout:
                entry = json.loads(raw_line)
                message = self._normalise_journal_message(entry.get("MESSAGE", ""))
                entry_cursor = entry.get("__CURSOR")
                if not isinstance(entry_cursor, str):
                    continue
                if newest_cursor is None:
                    newest_cursor = entry_cursor

                separator_chars = 1 if messages else 0
                prospective_chars = output_chars + separator_chars + len(message)
                if messages and prospective_chars > max_chars:
                    earlier_output_omitted = True
                    break

                if not messages and len(message) > max_chars:
                    marker = "[earlier part of output line omitted]\n"
                    if len(marker) < max_chars:
                        message = marker + message[-(max_chars - len(marker)) :]
                    else:
                        message = message[-max_chars:]
                    output_truncated = True
                    earlier_output_omitted = True

                oldest_cursor = entry_cursor
                messages.append(message)
                output_chars += separator_chars + len(message)
                if output_truncated:
                    break
        finally:
            self._finish_journal_reader(process, interrupted=earlier_output_omitted)

        self._raise_for_journal_error(process, record, ignore_error=earlier_output_omitted)
        messages.reverse()
        return JobOutputChunk(
            output="\n".join(messages),
            next_cursor=newest_cursor,
            has_more_output=False,
            oldest_cursor=oldest_cursor,
            has_earlier_output=earlier_output_omitted,
            output_truncated=output_truncated,
            earlier_output_omitted=earlier_output_omitted,
        )

    def _read_from_start(self, record: JobRecord, max_chars: int) -> JobOutputChunk:
        return self._read_forward_output(record, max_chars=max_chars, after_cursor=None)

    def _read_incremental_output(self, record: JobRecord, cursor: str, max_chars: int) -> JobOutputChunk:
        try:
            return self._read_forward_output(record, max_chars=max_chars, after_cursor=cursor)
        except RuntimeError as e:
            if not self._is_stale_cursor_error(str(e)):
                raise
            recovered = self._read_recent_output(record, max_chars)
            return replace(recovered, cursor_reset=True)

    def _read_previous_output(self, record: JobRecord, cursor: str, max_chars: int) -> JobOutputChunk:
        # read backward from the oldest displayed entry while excluding that boundary entry itself
        process = self._start_journal_reader(record, cursor=cursor, reverse=True)
        assert process.stdout is not None
        assert process.stderr is not None

        messages: list[str] = []
        output_chars = 0
        oldest_cursor: str | None = None
        newest_cursor: str | None = None
        has_earlier_output = False
        output_truncated = False
        skipped_boundary = False

        try:
            for raw_line in process.stdout:
                entry = json.loads(raw_line)
                entry_cursor = entry.get("__CURSOR")
                if not isinstance(entry_cursor, str):
                    continue
                if not skipped_boundary and entry_cursor == cursor:
                    skipped_boundary = True
                    continue
                skipped_boundary = True

                message = self._normalise_journal_message(entry.get("MESSAGE", ""))
                separator_chars = 1 if messages else 0
                prospective_chars = output_chars + separator_chars + len(message)
                if messages and prospective_chars > max_chars:
                    has_earlier_output = True
                    break

                if not messages and len(message) > max_chars:
                    marker = "[earlier part of output line omitted]\n"
                    if len(marker) < max_chars:
                        message = marker + message[-(max_chars - len(marker)) :]
                    else:
                        message = message[-max_chars:]
                    output_truncated = True
                    has_earlier_output = True

                if newest_cursor is None:
                    newest_cursor = entry_cursor
                oldest_cursor = entry_cursor
                messages.append(message)
                output_chars += separator_chars + len(message)
                if output_truncated:
                    break
        finally:
            self._finish_journal_reader(process, interrupted=has_earlier_output)

        self._raise_for_journal_error(process, record, ignore_error=has_earlier_output)
        messages.reverse()
        return JobOutputChunk(
            output="\n".join(messages),
            next_cursor=newest_cursor,
            has_more_output=False,
            oldest_cursor=oldest_cursor,
            has_earlier_output=has_earlier_output,
            output_truncated=output_truncated,
            earlier_output_omitted=has_earlier_output,
        )

    def _read_forward_output(self, record: JobRecord, max_chars: int, after_cursor: str | None) -> JobOutputChunk:
        process = self._start_journal_reader(record, after_cursor=after_cursor)
        assert process.stdout is not None
        assert process.stderr is not None

        messages: list[str] = []
        output_chars = 0
        next_cursor = after_cursor
        oldest_cursor: str | None = None
        has_more_output = False
        output_truncated = False

        try:
            for raw_line in process.stdout:
                entry = json.loads(raw_line)
                message = self._normalise_journal_message(entry.get("MESSAGE", ""))
                entry_cursor = entry.get("__CURSOR")
                if not isinstance(entry_cursor, str):
                    continue

                separator_chars = 1 if messages else 0
                prospective_chars = output_chars + separator_chars + len(message)
                if messages and prospective_chars > max_chars:
                    has_more_output = True
                    break

                if not messages and len(message) > max_chars:
                    marker = "\n[remaining part of output line omitted]"
                    keep = max(0, max_chars - len(marker))
                    message = message[:keep] + marker if keep else message[:max_chars]
                    output_truncated = True

                if oldest_cursor is None:
                    oldest_cursor = entry_cursor
                messages.append(message)
                output_chars += separator_chars + len(message)
                next_cursor = entry_cursor
        finally:
            self._finish_journal_reader(process, interrupted=has_more_output)

        self._raise_for_journal_error(process, record, ignore_error=has_more_output)
        return JobOutputChunk(
            output="\n".join(messages),
            next_cursor=next_cursor,
            has_more_output=has_more_output,
            oldest_cursor=oldest_cursor,
            output_truncated=output_truncated,
        )

    def _start_journal_reader(
        self,
        record: JobRecord,
        after_cursor: str | None = None,
        cursor: str | None = None,
        reverse: bool = False,
    ) -> subprocess.Popen[str]:
        if after_cursor is not None and cursor is not None:
            raise ValueError("after_cursor and cursor are mutually exclusive")

        args = [
            "journalctl",
            "--user",
            f"--identifier={record.unit_name}",
            "--no-pager",
            "--quiet",
            "--output=json",
        ]
        if after_cursor is not None:
            args.append(f"--after-cursor={after_cursor}")
        if cursor is not None:
            args.append(f"--cursor={cursor}")
        if reverse:
            args.append("--reverse")
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    @staticmethod
    def _finish_journal_reader(process: subprocess.Popen[str], interrupted: bool) -> None:
        if interrupted and process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    @staticmethod
    def _raise_for_journal_error(process: subprocess.Popen[str], record: JobRecord, ignore_error: bool) -> None:
        if ignore_error or process.returncode == 0:
            return
        assert process.stderr is not None
        detail = process.stderr.read().strip()
        raise RuntimeError(f"Unable to read output for job {record.job_id!r}: {detail or f'journalctl exited with {process.returncode}'}")

    @staticmethod
    def _is_stale_cursor_error(message: str) -> bool:
        lowered = message.lower()
        return "cursor" in lowered and any(term in lowered for term in ("seek", "failed", "invalid", "not found"))

    @staticmethod
    def _normalise_journal_message(message: object) -> str:
        if isinstance(message, list) and all(isinstance(value, int) for value in message):
            byte_values = [value for value in message if isinstance(value, int)]
            text = bytes(byte_values).decode("utf-8", errors="replace")
        else:
            text = message if isinstance(message, str) else str(message)
        text = _ANSI_ESCAPE_RE.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return _CONTROL_CHAR_RE.sub("", text).rstrip("\n")

    def _main_pid(self, record: JobRecord) -> int | None:
        """Return the transient unit's current main PID, if available."""
        result = subprocess.run(
            ["systemctl", "--user", "show", record.unit_name, "--property=MainPID", "--value"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return int(value) if value.isdigit() and int(value) > 0 else None

    @staticmethod
    def _process_tree(main_pid: int) -> list[psutil.Process]:
        """Return the runner and all descendants, even if descendants moved to another cgroup."""
        try:
            root = psutil.Process(main_pid)
            return [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

    @staticmethod
    def _owned_job_processes(job_id: str) -> list[psutil.Process]:
        """Return processes that inherited this job's opaque ownership marker."""
        processes: list[psutil.Process] = []
        for process in psutil.process_iter():
            try:
                if process.environ().get("SERENA_JOB_ID") == job_id:
                    processes.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return processes

    @staticmethod
    def _kill_processes(processes: list[psutil.Process]) -> None:
        """Hard-kill an already-identified set of job-owned processes without following arbitrary PIDs."""
        unique = {process.pid: process for process in processes}
        alive: list[psutil.Process] = []
        for process in unique.values():
            try:
                if process.is_running():
                    process.kill()
                    alive.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=1)

    @staticmethod
    def _kill_persisted_process_group(process_group_id: int | None) -> None:
        """Hard-kill a job-owned process group after its systemd unit has been stopped."""
        if process_group_id is None:
            return
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _last_output_at(self, record: JobRecord) -> datetime | None:
        result = subprocess.run(
            [
                "journalctl",
                "--user",
                f"--identifier={record.unit_name}",
                "--no-pager",
                "--quiet",
                "--output=json",
                "--lines=1",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            entry = json.loads(result.stdout.splitlines()[-1])
            timestamp = int(entry["__REALTIME_TIMESTAMP"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return datetime.fromtimestamp(timestamp / 1_000_000, tz=UTC)


class JobStore:
    """Atomic persistent storage for job metadata."""

    def __init__(self, root: Path | None = None):
        if root is None:
            root = Path(SerenaPaths().serena_user_home_dir) / "jobs"
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self._start_lock = FileLock(self.root / ".start.lock")

    def start_lock(self) -> FileLock:
        """:return: process-safe lock serialising concurrency-limit checks and job creation."""
        return self._start_lock

    def state_file(self, job_id: str) -> Path:
        """:return: validated metadata path for ``job_id``."""
        self.validate_job_id(job_id)
        return self.root / f"{job_id}.json"

    def create_command_file(self, job_id: str, command: str) -> Path:
        """Persist ``command`` in a private one-shot file consumed by the runner."""
        self.validate_job_id(job_id)
        path = self.root / f".{job_id}.command"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(command)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def delete_command_file(self, job_id: str) -> None:
        """Delete any private one-shot command file for ``job_id``."""
        self.validate_job_id(job_id)
        (self.root / f".{job_id}.command").unlink(missing_ok=True)

    def cleanup_orphan_command_files(self) -> None:
        """Remove command files that no longer belong to a running persisted job."""
        records = {record.job_id: record for record in self.list_records()}
        for path in self.root.glob(".*.command"):
            job_id = path.name[1 : -len(".command")]
            try:
                self.validate_job_id(job_id)
            except ValueError:
                continue
            record = records.get(job_id)
            if record is None or record.status.is_terminal:
                path.unlink(missing_ok=True)

    def create(self, record: JobRecord) -> None:
        """Persist a newly-created job, rejecting duplicate IDs."""
        path = self.state_file(record.job_id)
        if path.exists():
            raise ValueError(f"Job {record.job_id!r} already exists")
        self._write_atomic(path, record)

    def read(self, job_id: str) -> JobRecord:
        """Read one persisted job."""
        path = self.state_file(job_id)
        if not path.exists():
            raise ValueError(f"Unknown job ID {job_id!r}")
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid state for job {job_id!r}")
        return JobRecord.from_dict(data)

    def update(self, job_id: str, **changes: object) -> JobRecord:
        """Atomically update fields on one job record."""
        path = self.state_file(job_id)
        with FileLock(str(path) + ".lock"):
            record = self.read(job_id)
            updated = replace(record, **changes)
            self._write_atomic(path, updated)
            return updated

    def list_records(self) -> list[JobRecord]:
        """:return: all valid persisted job records."""
        records: list[JobRecord] = []
        for path in self.root.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    records.append(JobRecord.from_dict(data))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return records

    def prune_terminal_jobs(self, retention: timedelta) -> None:
        """Delete metadata for terminal jobs older than ``retention``."""
        cutoff = datetime.now(UTC) - retention
        for record in self.list_records():
            if not record.status.is_terminal or record.finished_at is None:
                continue
            try:
                finished_at = datetime.fromisoformat(record.finished_at)
            except ValueError:
                continue
            if finished_at >= cutoff:
                continue
            path = self.state_file(record.job_id)
            path.unlink(missing_ok=True)
            Path(str(path) + ".lock").unlink(missing_ok=True)
            (self.root / f".{record.job_id}.command").unlink(missing_ok=True)

    @staticmethod
    def validate_job_id(job_id: str) -> None:
        """Validate the externally supplied opaque job identifier."""
        try:
            parsed = UUID(job_id)
        except (ValueError, AttributeError) as e:
            raise ValueError(f"Invalid job ID {job_id!r}") from e
        if parsed.hex != job_id:
            raise ValueError(f"Invalid job ID {job_id!r}")

    @staticmethod
    def _write_atomic(path: Path, record: JobRecord) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record.to_dict(), f, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temp_name).unlink(missing_ok=True)
            raise


class JobManager:
    """Coordinates durable jobs while keeping execution independent of Serena's process lifecycle."""

    def __init__(
        self,
        store: JobStore | None = None,
        backend: JobBackend | None = None,
        max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
        output_char_limit: int = DEFAULT_OUTPUT_CHAR_LIMIT,
        retention: timedelta = DEFAULT_JOB_RETENTION,
    ):
        if max_concurrent_jobs <= 0:
            raise ValueError("max_concurrent_jobs must be positive")
        if output_char_limit <= 0:
            raise ValueError("output_char_limit must be positive")
        self._store = store or JobStore()
        self._backend = backend or SystemdJobBackend()
        self._max_concurrent_jobs = max_concurrent_jobs
        self._output_char_limit = output_char_limit
        self._retention = retention
        self._store.cleanup_orphan_command_files()

    @property
    def max_concurrent_jobs(self) -> int:
        """:return: hard global concurrency limit for this Serena installation."""
        return self._max_concurrent_jobs

    def persistence_info(self) -> JobPersistenceInfo:
        """:return: persistence guarantees of the current execution backend."""
        return self._backend.persistence_info()

    def start_job(
        self,
        command: str,
        project_root: str,
        label: str,
        project_name: str | None = None,
        cwd: str | None = None,
        timeout_seconds: int | None = None,
    ) -> tuple[JobRecord, int]:
        """Start a non-interactive command and return immediately with its durable job record."""
        command = command.strip()
        if not command:
            raise ValueError("Command must not be empty")
        if "\x00" in command:
            raise ValueError("Command must not contain NUL bytes")
        label = label.strip()
        if not label:
            raise ValueError("Job label must not be empty")
        if len(label) > 200:
            raise ValueError("Job label must be at most 200 characters")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")
        resolved_cwd = self._resolve_cwd(project_root, cwd)

        # serialise starts across ChatGPT chats and Serena processes so the six-job limit is strict
        with self._store.start_lock():
            self._store.prune_terminal_jobs(self._retention)
            self._store.cleanup_orphan_command_files()
            records = [self._reconcile_record(record) for record in self._store.list_records()]
            running = [record for record in records if record.status is JobStatus.RUNNING]
            if len(running) >= self._max_concurrent_jobs:
                active_ids = ", ".join(record.job_id for record in running)
                raise JobLimitError(
                    f"Cannot start another job: the limit of {self._max_concurrent_jobs} concurrent jobs is already in use. "
                    f"Running job IDs: {active_ids}. Check them with job_status or cancel one explicitly."
                )

            job_id = uuid4().hex
            now = datetime.now(UTC).isoformat()
            record = JobRecord(
                job_id=job_id,
                unit_name=f"{_JOB_UNIT_PREFIX}{job_id}.service",
                project_root=str(Path(project_root).resolve()),
                cwd=str(resolved_cwd),
                status=JobStatus.RUNNING,
                created_at=now,
                project_name=project_name,
                label=label,
                timeout_seconds=timeout_seconds,
                status_message="Job started and is running independently of the Serena MCP process.",
            )
            self._store.create(record)

            command_file: Path | None = None
            try:
                command_file = self._store.create_command_file(job_id, command)
                self._backend.start(record, command_file, self._store.state_file(job_id))
            except Exception as e:
                if command_file is not None:
                    command_file.unlink(missing_ok=True)
                try:
                    self._store.update(
                        job_id,
                        status=JobStatus.FAILED,
                        finished_at=datetime.now(UTC).isoformat(),
                        status_message=f"Job could not be started: {e}",
                    )
                except Exception:
                    pass
                raise RuntimeError(f"Failed to start job {job_id}: {e}") from e

            return record, len(running) + 1

    def get_job(
        self,
        job_id: str,
        cursor: str | None = None,
        output_mode: Literal["latest", "start"] = "latest",
    ) -> JobSnapshot:
        """Return current state, telemetry, and bounded output for one job."""
        record = self._reconcile_record(self._store.read(job_id))
        output = self._backend.read_output(record, cursor, self._output_char_limit, output_mode=output_mode)
        return JobSnapshot(record=record, runtime=self._backend.runtime_info(record), output=output)

    def get_job_output_before(self, job_id: str, cursor: str) -> JobSnapshot:
        """Return bounded output immediately preceding ``cursor`` for one job."""
        record = self._reconcile_record(self._store.read(job_id))
        output = self._backend.read_output_before(record, cursor, self._output_char_limit)
        return JobSnapshot(record=record, runtime=self._backend.runtime_info(record), output=output)

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        """List all running jobs followed by recent terminal jobs."""
        if limit <= 0:
            raise ValueError("limit must be positive")

        self._store.prune_terminal_jobs(self._retention)
        self._store.cleanup_orphan_command_files()
        records = [self._reconcile_record(record) for record in self._store.list_records()]
        running = sorted(
            (record for record in records if record.status is JobStatus.RUNNING),
            key=lambda record: record.created_at,
            reverse=True,
        )
        terminal = sorted(
            (record for record in records if record.status.is_terminal),
            key=lambda record: record.finished_at or record.created_at,
            reverse=True,
        )
        terminal_limit = max(0, limit - len(running))
        return running + terminal[:terminal_limit]

    def list_job_snapshots(self, limit: int = 20, running_only: bool = False) -> list[JobSnapshot]:
        """List jobs with lightweight telemetry but without retrieving their output."""
        records = self.list_jobs(limit=limit)
        if running_only:
            records = [record for record in records if record.status is JobStatus.RUNNING]
        return [JobSnapshot(record=record, runtime=self._backend.runtime_info(record)) for record in records]

    def cancel_job(self, job_id: str) -> JobRecord:
        """Cancel a running job and its complete process tree."""
        record = self._reconcile_record(self._store.read(job_id))
        if record.status.is_terminal:
            self._store.delete_command_file(job_id)
            return record

        self._backend.cancel(record)

        # preserve a natural terminal result if the command completed while cancellation was being requested
        current = self._store.read(job_id)
        if current.status.is_terminal:
            self._store.delete_command_file(job_id)
            return current
        updated = self._store.update(
            job_id,
            status=JobStatus.CANCELLED,
            finished_at=datetime.now(UTC).isoformat(),
            status_message="Job was cancelled on request.",
        )
        self._store.delete_command_file(job_id)
        return updated

    def _reconcile_record(self, record: JobRecord) -> JobRecord:
        if record.status is not JobStatus.RUNNING:
            self._store.delete_command_file(record.job_id)
            return record
        if self._backend.is_running(record):
            return record

        # re-read after querying systemd because the runner may have written its terminal state concurrently
        current = self._store.read(record.job_id)
        if current.status is not JobStatus.RUNNING:
            self._store.delete_command_file(record.job_id)
            return current

        # Clean up any process-group descendants that escaped the systemd cgroup before recording the terminal state.
        self._backend.cancel(current)

        now = datetime.now(UTC)
        created_at = datetime.fromisoformat(current.created_at)
        timed_out = current.timeout_seconds is not None and (now - created_at).total_seconds() >= current.timeout_seconds
        if timed_out:
            updated = self._store.update(
                current.job_id,
                status=JobStatus.TIMED_OUT,
                finished_at=now.isoformat(),
                status_message=f"Job exceeded its {current.timeout_seconds}-second runtime limit.",
            )
        else:
            updated = self._store.update(
                current.job_id,
                status=JobStatus.FAILED,
                finished_at=now.isoformat(),
                status_message=(
                    "The job process disappeared before recording a result, for example because of a reboot or external termination."
                ),
            )
        self._store.delete_command_file(current.job_id)
        return updated

    @staticmethod
    def _resolve_cwd(project_root: str, cwd: str | None) -> Path:
        root = Path(project_root).resolve()
        candidate = root if cwd is None else Path(cwd)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not candidate.is_dir():
            raise FileNotFoundError(f"Job working directory is not a directory: {candidate}")
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Job working directory must stay within the active project: {root}")
        return candidate
