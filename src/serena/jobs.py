"""Persistent long-running job execution for Serena."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import psutil
from filelock import FileLock

from mcp_runtime.shell_environment import user_shell_environment
from serena.config.serena_config import SerenaPaths
from serena.errors import UserFacingError
from serena.retention import JobRetentionState

DEFAULT_MAX_CONCURRENT_JOBS = 12
DEFAULT_OUTPUT_CHAR_LIMIT = 12_000
_STARTUP_GRACE_SECONDS = 5.0
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

    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        """:return: whether no further execution is expected for this state."""
        return self not in (JobStatus.STARTING, JobStatus.RUNNING)


class JobRetentionObserver(Protocol):
    """Receives durable-job state used by unified session retention."""

    def sync_job_retention(self, jobs: list[JobRetentionState]) -> set[str]:
        """Synchronize durable jobs and return identifiers still owned by retained sessions."""
        ...


@dataclass(frozen=True)
class JobRecord:
    """Persisted metadata for one background job."""

    job_id: str
    unit_name: str
    project_root: str
    cwd: str
    status: JobStatus
    created_at: str
    session_id: str | None = None
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
            session_id=str(data["session_id"]) if data.get("session_id") is not None else None,
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


class JobLimitError(UserFacingError):
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
    _CPU_WEIGHT = 20
    _NICE = 10

    @staticmethod
    def _run_required_command(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        """Run a required system command or present an actionable operational failure."""
        try:
            return subprocess.run(args, check=False, **kwargs)
        except OSError as error:
            raise UserFacingError(f"Unable to run {args[0]}: {error.strerror or error}") from None

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
            f"--property=CPUWeight={self._CPU_WEIGHT}",
            f"--property=Nice={self._NICE}",
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
        shell_environment = user_shell_environment()
        for name in _INHERITED_ENVIRONMENT_VARIABLES:
            value = shell_environment.get(name)
            if value is not None:
                args.append(f"--setenv={name}={value}")
        args.extend(["--", sys.executable, "-m", "serena.job_runner", str(state_file), str(command_file)])

        result = self._run_required_command(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise UserFacingError(f"systemd-run failed with status {result.returncode}: {detail}")

    def is_running(self, record: JobRecord) -> bool:
        result = self._run_required_command(
            ["systemctl", "--user", "is-active", "--quiet", record.unit_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
        if result.returncode in (3, 4):
            return False
        raise UserFacingError(f"Unable to query systemd unit {record.unit_name!r} (status {result.returncode})")

    def cancel(self, record: JobRecord) -> None:
        # Snapshot job-owned processes before stopping the unit. Descendants may live in Snap scopes
        # or independent sessions/cgroups, but retain the inherited SERENA_JOB_ID environment marker.
        owned_processes = self._owned_job_processes(record.job_id)

        result = self._run_required_command(
            ["systemctl", "--user", "stop", record.unit_name],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "not loaded" not in detail.lower() and "not found" not in detail.lower():
                raise UserFacingError(f"Unable to stop systemd unit {record.unit_name!r}: {detail}")

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
            raise UserFacingError(f"Unsupported output mode {output_mode!r}")
        if cursor is not None and (len(cursor) > _MAX_CURSOR_LENGTH or "\x00" in cursor or "\n" in cursor):
            raise UserFacingError("Invalid journal cursor")
        if cursor is not None:
            return self._read_incremental_output(record, cursor, max_chars)
        if output_mode == "start":
            return self._read_from_start(record, max_chars)
        return self._read_recent_output(record, max_chars)

    def read_output_before(self, record: JobRecord, cursor: str, max_chars: int) -> JobOutputChunk:
        """Read bounded journal output immediately preceding ``cursor``."""
        if len(cursor) > _MAX_CURSOR_LENGTH or "\x00" in cursor or "\n" in cursor:
            raise UserFacingError("Invalid journal cursor")
        try:
            return self._read_previous_output(record, cursor, max_chars)
        except UserFacingError as e:
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
        except UserFacingError as e:
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
        try:
            return subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            raise UserFacingError(f"Unable to run journalctl: {error.strerror or error}") from None

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
        raise UserFacingError(
            f"Unable to read output for job {record.job_id!r}: {detail or f'journalctl exited with {process.returncode}'}"
        )

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
    """Indexed SQLite storage for durable-job metadata."""

    _DATABASE_FILENAME = "state.sqlite3"
    _BUSY_TIMEOUT_MS = 5_000

    def __init__(self, root: Path | None = None):
        if root is None:
            root = Path(SerenaPaths().serena_user_home_dir) / "jobs"
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self._start_lock = FileLock(self.root / ".start.lock")
        self._lock = threading.RLock()
        self._database_path = self.root / self._DATABASE_FILENAME
        self._connection = sqlite3.connect(
            self._database_path,
            timeout=self._BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_database()
        self._create_schema()

    def dashboard_revision(self) -> str:
        """Returns an O(1) process-local revision for dashboard-visible durable-job state."""
        with self._lock:
            data_version = int(self._connection.execute("PRAGMA data_version").fetchone()[0])
            total_changes = self._connection.total_changes
        return f"{total_changes}:{data_version}"

    def start_lock(self) -> FileLock:
        """:return: process-safe lock serialising concurrency-limit checks and job creation."""
        return self._start_lock

    def state_file(self, job_id: str) -> Path:
        """:return: stable runner locator for ``job_id`` within this store root."""
        self.validate_job_id(job_id)
        return self._database_path

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
        running = {record.job_id for record in self.list_running_records()}
        for path in self.root.glob(".*.command"):
            job_id = path.name[1 : -len(".command")]
            try:
                self.validate_job_id(job_id)
            except UserFacingError:
                continue
            if job_id not in running:
                path.unlink(missing_ok=True)

    def create(self, record: JobRecord) -> None:
        """Persist a newly-created job, rejecting duplicate IDs."""
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, unit_name, project_root, cwd, status, created_at, session_id,
                        project_name, label, timeout_seconds, process_group_id, finished_at,
                        return_code, status_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._record_values(record),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(f"Job {record.job_id!r} already exists") from error

    def read(self, job_id: str) -> JobRecord:
        """Read one persisted job by primary key."""
        self.validate_job_id(job_id)
        with self._lock:
            row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise UserFacingError(f"Unknown job ID {job_id!r}")
        return self._record_from_row(row)

    def update(self, job_id: str, **changes: object) -> JobRecord:
        """Atomically update fields on one job record."""
        self.validate_job_id(job_id)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                if row is None:
                    raise UserFacingError(f"Unknown job ID {job_id!r}")
                updated = replace(self._record_from_row(row), **changes)
                self._write_record(updated)
                self._connection.commit()
                return updated
            except Exception:
                self._connection.rollback()
                raise

    def transition_status(
        self,
        job_id: str,
        expected_status: JobStatus,
        status: JobStatus,
        **changes: object,
    ) -> JobRecord:
        """Atomically transition one job from ``expected_status`` and otherwise return its current record."""
        self.validate_job_id(job_id)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                if row is None:
                    raise UserFacingError(f"Unknown job ID {job_id!r}")
                current = self._record_from_row(row)
                if current.status is not expected_status:
                    self._connection.rollback()
                    return current
                updated = replace(current, status=status, **changes)
                self._write_record(updated)
                self._connection.commit()
                return updated
            except Exception:
                self._connection.rollback()
                raise

    def _write_record(self, record: JobRecord) -> None:
        """Persist the complete mutable row for one job inside the caller's transaction."""
        self._connection.execute(
            """
            UPDATE jobs
            SET unit_name = ?, project_root = ?, cwd = ?, status = ?, created_at = ?,
                session_id = ?, project_name = ?, label = ?, timeout_seconds = ?,
                process_group_id = ?, finished_at = ?, return_code = ?, status_message = ?
            WHERE job_id = ?
            """,
            (*self._record_values(record)[1:], record.job_id),
        )

    def list_records(self) -> list[JobRecord]:
        """:return: all persisted job records."""
        with self._lock:
            rows = self._connection.execute("SELECT * FROM jobs").fetchall()
            return [self._record_from_row(row) for row in rows]

    def list_running_records(self) -> list[JobRecord]:
        """:return: jobs whose persisted lifecycle is still non-terminal via the status index."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at DESC",
                (JobStatus.STARTING.value, JobStatus.RUNNING.value),
            ).fetchall()
            return [self._record_from_row(row) for row in rows]

    def prune_unretained_terminal_jobs(self, retained_job_ids: set[str]) -> None:
        """Delete terminal job metadata no longer owned by a retained session."""
        terminal_values = tuple(status.value for status in JobStatus if status.is_terminal)
        placeholders = ",".join("?" for _ in terminal_values)
        with self._lock:
            if retained_job_ids:
                retained_placeholders = ",".join("?" for _ in retained_job_ids)
                rows = self._connection.execute(
                    f"""
                    SELECT job_id FROM jobs
                    WHERE status IN ({placeholders})
                      AND job_id NOT IN ({retained_placeholders})
                    """,
                    (*terminal_values, *sorted(retained_job_ids)),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    f"SELECT job_id FROM jobs WHERE status IN ({placeholders})",
                    terminal_values,
                ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            if job_ids:
                delete_placeholders = ",".join("?" for _ in job_ids)
                self._connection.execute(
                    f"DELETE FROM jobs WHERE job_id IN ({delete_placeholders})",
                    tuple(job_ids),
                )
        for job_id in job_ids:
            (self.root / f".{job_id}.command").unlink(missing_ok=True)

    @staticmethod
    def validate_job_id(job_id: str) -> None:
        """Validate the externally supplied opaque job identifier."""
        try:
            parsed = UUID(job_id)
        except (ValueError, AttributeError):
            raise UserFacingError(f"Invalid job ID {job_id!r}") from None
        if parsed.hex != job_id:
            raise UserFacingError(f"Invalid job ID {job_id!r}")

    def _configure_database(self) -> None:
        """Configures SQLite for concurrent Serena and runner-process access."""
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")

    def _create_schema(self) -> None:
        """Creates the durable-job table and lifecycle access-path indexes."""
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                unit_name TEXT NOT NULL,
                project_root TEXT NOT NULL,
                cwd TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                session_id TEXT,
                project_name TEXT,
                label TEXT,
                timeout_seconds INTEGER,
                process_group_id INTEGER,
                finished_at TEXT,
                return_code INTEGER,
                status_message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                ON jobs(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_session
                ON jobs(session_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_finished
                ON jobs(finished_at DESC, created_at DESC);
            """
        )

    @staticmethod
    def _record_values(record: JobRecord) -> tuple[object, ...]:
        """Returns one job's ordered SQLite column values."""
        return (
            record.job_id,
            record.unit_name,
            record.project_root,
            record.cwd,
            record.status.value,
            record.created_at,
            record.session_id,
            record.project_name,
            record.label,
            record.timeout_seconds,
            record.process_group_id,
            record.finished_at,
            record.return_code,
            record.status_message,
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> JobRecord:
        """Projects one SQLite row to durable-job metadata."""
        return JobRecord(
            job_id=str(row["job_id"]),
            unit_name=str(row["unit_name"]),
            project_root=str(row["project_root"]),
            cwd=str(row["cwd"]),
            status=JobStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            session_id=str(row["session_id"]) if row["session_id"] is not None else None,
            project_name=str(row["project_name"]) if row["project_name"] is not None else None,
            label=str(row["label"]) if row["label"] is not None else None,
            timeout_seconds=int(row["timeout_seconds"]) if row["timeout_seconds"] is not None else None,
            process_group_id=int(row["process_group_id"]) if row["process_group_id"] is not None else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
            return_code=int(row["return_code"]) if row["return_code"] is not None else None,
            status_message=str(row["status_message"]) if row["status_message"] is not None else None,
        )


class JobManager:
    """Coordinates durable jobs while keeping execution independent of Serena's process lifecycle."""

    def __init__(
        self,
        store: JobStore | None = None,
        backend: JobBackend | None = None,
        max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
        output_char_limit: int = DEFAULT_OUTPUT_CHAR_LIMIT,
        retention_observer: JobRetentionObserver | None = None,
    ):
        if max_concurrent_jobs <= 0:
            raise ValueError("max_concurrent_jobs must be positive")
        if output_char_limit <= 0:
            raise ValueError("output_char_limit must be positive")
        self._store = store or JobStore()
        self._backend = backend or SystemdJobBackend()
        self._max_concurrent_jobs = max_concurrent_jobs
        self._output_char_limit = output_char_limit
        self._retention_observer = retention_observer

        self._store.cleanup_orphan_command_files()
        self._sync_retention_observer(self._store.list_records())

    def dashboard_revision(self) -> str:
        """Returns a cheap token for durable-job state visible to dashboard documents."""
        return self._store.dashboard_revision()

    def _sync_retention_observer(self, records: list[JobRecord] | None = None) -> None:
        """Synchronize durable jobs with session retention and prune unowned terminal metadata."""
        if self._retention_observer is None:
            return
        current_records = records if records is not None else self._store.list_records()
        jobs = [
            JobRetentionState(
                job_id=record.job_id,
                session_id=record.session_id,
                is_running=not record.status.is_terminal,
                finished_at=(datetime.fromisoformat(record.finished_at).timestamp() if record.finished_at else None),
            )
            for record in current_records
        ]
        retained_job_ids = self._retention_observer.sync_job_retention(jobs)
        self._store.prune_unretained_terminal_jobs(retained_job_ids)

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
        session_id: str | None = None,
    ) -> tuple[JobRecord, int]:
        """Start a non-interactive command and return immediately with its durable job record."""
        command = command.strip()
        if not command:
            raise UserFacingError("Command must not be empty")
        if "\x00" in command:
            raise UserFacingError("Command must not contain NUL bytes")
        label = label.strip()
        if not label:
            raise UserFacingError("Job label must not be empty")
        if len(label) > 200:
            raise UserFacingError("Job label must be at most 200 characters")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise UserFacingError("timeout_seconds must be positive when provided")
        resolved_cwd = self._resolve_cwd(project_root, cwd)

        # serialise starts across ChatGPT chats and Serena processes so the concurrency limit is strict
        with self._store.start_lock():
            self._store.cleanup_orphan_command_files()
            records = [self._reconcile_record(record) for record in self._store.list_records()]
            live = [record for record in records if not record.status.is_terminal]
            if len(live) >= self._max_concurrent_jobs:
                self._sync_retention_observer(records)
                active_ids = ", ".join(record.job_id for record in live)
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
                status=JobStatus.STARTING,
                created_at=now,
                session_id=session_id,
                project_name=project_name,
                label=label,
                timeout_seconds=timeout_seconds,
                status_message="Job launch is being established.",
            )
            self._store.create(record)

            command_file: Path | None = None
            try:
                command_file = self._store.create_command_file(job_id, command)
                self._backend.start(record, command_file, self._store.state_file(job_id))
                record = self._store.transition_status(
                    job_id,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    status_message="Job started and is running independently of the Serena MCP process.",
                )
            except Exception as error:
                if command_file is not None:
                    command_file.unlink(missing_ok=True)
                status_message = str(error) if isinstance(error, UserFacingError) else "Job could not be started."
                try:
                    failed_record = self._store.transition_status(
                        job_id,
                        JobStatus.STARTING,
                        JobStatus.FAILED,
                        finished_at=datetime.now(UTC).isoformat(),
                        status_message=status_message,
                    )
                    self._sync_retention_observer([*records, failed_record])
                except Exception:
                    self._sync_retention_observer(records)
                raise

            self._sync_retention_observer([*records, record])
            live_count = len(live) + (0 if record.status.is_terminal else 1)
            return record, live_count

    def get_job(
        self,
        job_id: str,
        cursor: str | None = None,
        output_mode: Literal["latest", "start"] = "latest",
    ) -> JobSnapshot:
        """Return current state, telemetry, and bounded output for one job."""
        record = self._reconcile_record(self._store.read(job_id))
        self._sync_retention_observer()
        output = self._backend.read_output(record, cursor, self._output_char_limit, output_mode=output_mode)
        return JobSnapshot(record=record, runtime=self._backend.runtime_info(record), output=output)

    def get_job_record(self, job_id: str) -> JobRecord:
        """Returns lightweight current metadata for one job without runtime telemetry or output."""
        stored = self._store.read(job_id)
        record = self._reconcile_record(stored)
        if not stored.status.is_terminal and record.status.is_terminal:
            self._sync_retention_observer()
        return record

    def get_job_records(self, job_ids: set[str]) -> list[JobRecord]:
        """Returns current lightweight metadata for selected jobs without global work when unchanged."""
        if not job_ids:
            return []
        records: list[JobRecord] = []
        lifecycle_changed = False
        for job_id in job_ids:
            try:
                stored = self._store.read(job_id)
            except KeyError:
                continue
            record = self._reconcile_record(stored)
            lifecycle_changed = lifecycle_changed or (not stored.status.is_terminal and record.status.is_terminal)
            records.append(record)
        if lifecycle_changed:
            self._sync_retention_observer()
        return records

    def list_running_jobs(self) -> list[JobRecord]:
        """Returns current non-terminal job metadata from the indexed live set."""
        stored_records = self._store.list_running_records()
        running: list[JobRecord] = []
        lifecycle_changed = False
        for stored in stored_records:
            current = self._reconcile_record(stored)
            lifecycle_changed = lifecycle_changed or current.status.is_terminal
            if not current.status.is_terminal:
                running.append(current)
        if lifecycle_changed:
            self._sync_retention_observer()
        return sorted(running, key=lambda record: record.created_at, reverse=True)

    def get_job_output_before(self, job_id: str, cursor: str) -> JobSnapshot:
        """Return bounded output immediately preceding ``cursor`` for one job."""
        record = self._reconcile_record(self._store.read(job_id))
        output = self._backend.read_output_before(record, cursor, self._output_char_limit)
        return JobSnapshot(record=record, runtime=self._backend.runtime_info(record), output=output)

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        """List all non-terminal jobs followed by recent terminal jobs."""
        if limit <= 0:
            raise ValueError("limit must be positive")

        self._store.cleanup_orphan_command_files()
        records = [self._reconcile_record(record) for record in self._store.list_records()]
        self._sync_retention_observer(records)
        running = sorted(
            (record for record in records if not record.status.is_terminal),
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
            records = [record for record in records if not record.status.is_terminal]
        return [JobSnapshot(record=record, runtime=self._backend.runtime_info(record)) for record in records]

    def cancel_job(self, job_id: str) -> JobRecord:
        """Cancel a running job and its complete process tree."""
        record = self._reconcile_record(self._store.read(job_id))
        if record.status.is_terminal:
            self._store.delete_command_file(job_id)
            self._sync_retention_observer()
            return record

        self._backend.cancel(record)

        # preserve a natural terminal result if the command completed while cancellation was being requested
        current = self._store.read(job_id)
        if current.status.is_terminal:
            self._store.delete_command_file(job_id)
            self._sync_retention_observer()
            return current
        updated = self._store.update(
            job_id,
            status=JobStatus.CANCELLED,
            finished_at=datetime.now(UTC).isoformat(),
            status_message="Job was cancelled on request.",
        )
        self._store.delete_command_file(job_id)
        self._sync_retention_observer()
        return updated

    def _reconcile_record(self, record: JobRecord) -> JobRecord:
        if record.status.is_terminal:
            self._store.delete_command_file(record.job_id)
            return record

        # accept an active backend as authoritative evidence that launch succeeded
        if self._backend.is_running(record):
            if record.status is JobStatus.STARTING:
                return self._store.transition_status(
                    record.job_id,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    status_message="Job started and is running independently of the Serena MCP process.",
                )
            return record

        # re-read after querying systemd because the runner may have written a new lifecycle state concurrently
        current = self._store.read(record.job_id)
        if current.status.is_terminal:
            self._store.delete_command_file(record.job_id)
            return current

        now = datetime.now(UTC)
        created_at = datetime.fromisoformat(current.created_at)
        age_seconds = (now - created_at).total_seconds()
        if current.status is JobStatus.STARTING:
            if age_seconds < _STARTUP_GRACE_SECONDS:
                return current

            # one final backend check avoids failing a launch that became active at the grace boundary
            if self._backend.is_running(current):
                return self._store.transition_status(
                    current.job_id,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    status_message="Job started and is running independently of the Serena MCP process.",
                )

            self._backend.cancel(current)
            updated = self._store.transition_status(
                current.job_id,
                JobStatus.STARTING,
                JobStatus.FAILED,
                finished_at=now.isoformat(),
                status_message=(f"Job did not finish starting within {_STARTUP_GRACE_SECONDS:g} seconds."),
            )
            if updated.status.is_terminal:
                self._store.delete_command_file(current.job_id)
            return updated

        # clean up any process-group descendants that escaped the systemd cgroup before recording the terminal state
        self._backend.cancel(current)

        timed_out = current.timeout_seconds is not None and age_seconds >= current.timeout_seconds
        if timed_out:
            updated = self._store.transition_status(
                current.job_id,
                JobStatus.RUNNING,
                JobStatus.TIMED_OUT,
                finished_at=now.isoformat(),
                status_message=f"Job exceeded its {current.timeout_seconds}-second runtime limit.",
            )
        else:
            updated = self._store.transition_status(
                current.job_id,
                JobStatus.RUNNING,
                JobStatus.FAILED,
                finished_at=now.isoformat(),
                status_message=(
                    "The job process disappeared before recording a result, for example because of a reboot or external termination."
                ),
            )
        if updated.status.is_terminal:
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
            raise UserFacingError(f"Job working directory is not a directory: {candidate}")
        if candidate != root and root not in candidate.parents:
            raise UserFacingError("Job working directory must stay within the active project.")
        return candidate
