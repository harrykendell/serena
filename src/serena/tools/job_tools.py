"""Tools for durable long-running shell jobs."""

from __future__ import annotations

import json
import time
from typing import Literal

from serena.errors import UserFacingError
from serena.jobs import JobRecord, JobRuntimeInfo, JobSnapshot, JobStatus
from serena.tools.tools_base import Tool, ToolMarkerCanEdit, ToolMarkerDoesNotRequireActiveProject


class _JobTool(Tool):
    """Shared access to the persistent Serena job manager."""

    def __init__(self, agent):
        super().__init__(agent)
        self._job_manager = agent.job_manager

    @staticmethod
    def _record_payload(record: JobRecord) -> dict[str, object]:
        """Returns compact recoverable identity and state for one job."""
        payload: dict[str, object] = {
            "job_id": record.job_id,
            "label": record.label,
            "project": record.project_name,
            "status": record.status.value,
            "created_at": record.created_at,
        }
        if record.finished_at is not None:
            payload["finished_at"] = record.finished_at
        if record.return_code is not None:
            payload["return_code"] = record.return_code
        if record.timeout_seconds is not None and not record.status.is_terminal:
            payload["timeout_seconds"] = record.timeout_seconds
        if record.cwd != record.project_root:
            payload["cwd"] = record.cwd

        status_detail = _JobTool._status_detail(record)
        if status_detail is not None:
            payload["status_message"] = status_detail
        return payload

    @staticmethod
    def _status_detail(record: JobRecord) -> str | None:
        """Returns non-redundant terminal detail when state alone is insufficient."""
        if record.status is JobStatus.TIMED_OUT:
            return record.status_message
        if record.status is JobStatus.FAILED and record.return_code is None:
            return record.status_message
        return None

    @staticmethod
    def _runtime_payload(runtime: JobRuntimeInfo) -> dict[str, object]:
        """Returns only available runtime telemetry."""
        payload: dict[str, object] = {"elapsed_seconds": round(runtime.elapsed_seconds, 3)}
        if runtime.seconds_since_last_output is not None:
            payload["seconds_since_last_output"] = round(runtime.seconds_since_last_output, 3)
        if runtime.memory_bytes is not None:
            payload["memory_bytes"] = runtime.memory_bytes
        if runtime.cpu_seconds is not None:
            payload["cpu_seconds"] = round(runtime.cpu_seconds, 3)
        if runtime.process_count is not None:
            payload["process_count"] = runtime.process_count
        return payload

    @staticmethod
    def _json(payload: dict[str, object]) -> str:
        return json.dumps(payload, ensure_ascii=False)


class StartJobTool(_JobTool, ToolMarkerCanEdit):
    """Starts one of up to six durable non-interactive commands and returns immediately with a job ID."""

    def apply(
        self,
        command: str,
        label: str,
        cwd: str | None = None,
        timeout_seconds: int | None = None,
        session_id: str = "global",
    ) -> str:
        """Start a long-running command without blocking later Serena calls.

        Use this instead of ``execute_shell_command`` for tests, builds, simulations, optimisations, or other commands that may
        exceed the normal tool timeout. Always give the job a concise, distinctive label so it can be recovered in another chat.
        The job survives Serena MCP restarts. Continue useful work after starting it, then call ``job_status`` when progress or
        the final result is needed. Do not use for interactive commands.

        :param command: shell command to run non-interactively
        :param label: concise human-readable purpose, required for cross-chat recovery
        :param cwd: project-relative working directory; defaults to the active project root and may not escape it
        :param timeout_seconds: optional positive wall-clock runtime limit; omit for no runtime limit
        :param session_id: client session that owns the dashboard panel for this job
        :return: compact JSON containing the job ID, state, and concurrency usage
        """
        record, running_jobs = self._job_manager.start_job(
            command=command,
            project_root=self.get_project_root(),
            label=label,
            project_name=self.project.project_name,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            session_id=session_id,
        )
        return self._json(
            {
                "job_id": record.job_id,
                "status": record.status.value,
                "running_jobs": running_jobs,
                "max_concurrent_jobs": self._job_manager.max_concurrent_jobs,
            }
        )


class JobStatusTool(_JobTool, ToolMarkerDoesNotRequireActiveProject):
    """Checks a durable job or lists recent jobs when no job ID is supplied."""

    def apply(
        self,
        job_id: str | None = None,
        cursor: str | None = None,
        output: Literal["latest", "start"] = "latest",
        wait_for: float | Literal["completed"] | None = None,
    ) -> dict[str, object]:
        """Return job state, telemetry, and the complete journal snapshot or delta selected by this operation.

        With a ``job_id``, the first call defaults to the latest bounded output tail. Set ``output="start"`` to read from the
        beginning instead. Pass ``next_cursor`` back on later calls to receive only new output; those cursor-based calls return a
        compact delta payload rather than repeating immutable job metadata. Paging-condition fields such as ``has_more_output``,
        ``earlier_output_omitted``, and ``cursor_reset`` are emitted only when true. A stale journal cursor is recovered
        automatically by returning the latest tail with ``cursor_reset=true``. ``wait_for`` accepts either a numeric duration from
        0 through 60 seconds or ``"completed"``. A duration waits until that time has elapsed or the job reaches a terminal state;
        ``"completed"`` waits until the job reaches a terminal state. Newly available output does not end either kind of wait.
        Without a ``job_id``, all running jobs are listed first followed by recent terminal jobs so work can be recovered after
        Serena restarts or in another chat.

        :param job_id: opaque job ID returned by ``start_job``; omit to list recent jobs
        :param cursor: opaque cursor returned by the preceding status call for this job
        :param output: initial output position when no cursor is supplied: ``latest`` (default) or ``start``
        :param wait_for: optional wait duration in seconds, or ``"completed"`` to wait until the job finishes
        :return: native current state, telemetry, and selected journal output
        """
        deadline: float | None = None
        if wait_for is not None and wait_for != "completed":
            if wait_for < 0 or wait_for > 60:
                raise UserFacingError("numeric wait_for must be between 0 and 60 seconds")
            deadline = time.monotonic() + wait_for

        if job_id is None:
            if cursor is not None:
                raise UserFacingError("cursor requires job_id")
            if wait_for is not None:
                raise UserFacingError("wait_for requires job_id")
            snapshots = self._job_manager.list_job_snapshots()
            running_jobs = sum(snapshot.record.status is JobStatus.RUNNING for snapshot in snapshots)
            jobs: list[dict[str, object]] = []
            for snapshot in snapshots:
                record = snapshot.record
                item = self._record_payload(record)
                if not record.status.is_terminal:
                    item["runtime"] = self._runtime_payload(snapshot.runtime)
                jobs.append(item)
            return {
                "jobs": jobs,
                "running_jobs": running_jobs,
                "max_concurrent_jobs": self._job_manager.max_concurrent_jobs,
            }

        snapshot = self._job_manager.get_job(job_id, cursor, output_mode=output)
        if wait_for is not None and snapshot.record.status is JobStatus.RUNNING:
            while snapshot.record.status is JobStatus.RUNNING:
                if deadline is None:
                    sleep_seconds = 0.25
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sleep_seconds = min(0.25, remaining)

                time.sleep(sleep_seconds)
                snapshot = self._job_manager.get_job(job_id, cursor, output_mode=output)

        return self._snapshot_payload(snapshot, delta=cursor is not None)

    def _snapshot_payload(self, snapshot: JobSnapshot, *, delta: bool) -> dict[str, object]:
        record = snapshot.record
        output = snapshot.output
        assert output is not None

        if delta:
            payload: dict[str, object] = {
                "job_id": record.job_id,
                "status": record.status.value,
            }
            if record.finished_at is not None:
                payload["finished_at"] = record.finished_at
            if record.return_code is not None:
                payload["return_code"] = record.return_code
            status_detail = self._status_detail(record)
            if status_detail is not None:
                payload["status_message"] = status_detail
        else:
            payload = self._record_payload(record)

        payload["runtime"] = self._runtime_payload(snapshot.runtime)
        payload["output"] = output.output
        if output.next_cursor is not None:
            payload["next_cursor"] = output.next_cursor
        if output.has_more_output:
            payload["has_more_output"] = True
        if output.output_truncated:
            payload["output_truncated"] = True
        if output.earlier_output_omitted:
            payload["earlier_output_omitted"] = True
        if output.cursor_reset:
            payload["cursor_reset"] = True
        return payload


class CancelJobTool(_JobTool, ToolMarkerCanEdit, ToolMarkerDoesNotRequireActiveProject):
    """Cancels one Serena job and its complete process tree."""

    def apply(self, job_id: str) -> str:
        """Cancel a running job by its Serena job ID.

        Cancellation is restricted to jobs created by Serena; arbitrary PIDs or systemd units cannot be targeted. Cancelling a job
        that has already finished is safe and leaves its terminal result unchanged.

        :param job_id: opaque job ID returned by ``start_job``
        :return: JSON containing the resulting terminal or already-terminal state
        """
        record = self._job_manager.cancel_job(job_id)
        payload: dict[str, object] = {"job_id": record.job_id, "status": record.status.value}
        if record.return_code is not None:
            payload["return_code"] = record.return_code
        status_detail = self._status_detail(record)
        if status_detail is not None:
            payload["status_message"] = status_detail
        return self._json(payload)
