"""Tools for durable long-running shell jobs."""

from __future__ import annotations

import json
from typing import Literal

from serena.jobs import (
    DEFAULT_MAX_CONCURRENT_JOBS,
    JobManager,
    JobPersistenceInfo,
    JobRecord,
    JobRuntimeInfo,
    JobSnapshot,
    JobStatus,
)
from serena.tools.tools_base import Tool, ToolMarkerCanEdit, ToolMarkerDoesNotRequireActiveProject, ToolMarkerOptional


class _JobTool(Tool, ToolMarkerOptional):
    """Shared access to the persistent Serena job manager."""

    def __init__(self, agent):
        super().__init__(agent)
        self._job_manager = JobManager(max_concurrent_jobs=DEFAULT_MAX_CONCURRENT_JOBS)

    @staticmethod
    def _record_payload(record: JobRecord) -> dict[str, object]:
        return {
            "job_id": record.job_id,
            "label": record.label,
            "project": record.project_name,
            "project_root": record.project_root,
            "cwd": record.cwd,
            "status": record.status.value,
            "created_at": record.created_at,
            "finished_at": record.finished_at,
            "return_code": record.return_code,
            "timeout_seconds": record.timeout_seconds,
            "status_message": record.status_message,
        }

    @staticmethod
    def _runtime_payload(runtime: JobRuntimeInfo) -> dict[str, object]:
        return {
            "elapsed_seconds": round(runtime.elapsed_seconds, 3),
            "seconds_since_last_output": (
                round(runtime.seconds_since_last_output, 3) if runtime.seconds_since_last_output is not None else None
            ),
            "memory_bytes": runtime.memory_bytes,
            "cpu_seconds": round(runtime.cpu_seconds, 3) if runtime.cpu_seconds is not None else None,
            "process_count": runtime.process_count,
        }

    @staticmethod
    def _persistence_payload(persistence: JobPersistenceInfo) -> dict[str, object]:
        if persistence.survives_logout:
            summary = "Survives Serena restarts and user logout while the host remains up; does not survive a host reboot."
        else:
            summary = "Survives Serena restarts; user logout may stop the job because systemd user lingering is disabled; does not survive a host reboot."
        return {
            "survives_serena_restart": persistence.survives_serena_restart,
            "survives_logout": persistence.survives_logout,
            "survives_reboot": persistence.survives_reboot,
            "linger_enabled": persistence.linger_enabled,
            "summary": summary,
        }

    @staticmethod
    def _json(payload: dict[str, object]) -> str:
        return json.dumps(payload, ensure_ascii=False)


class StartJobTool(_JobTool, ToolMarkerCanEdit):
    """Starts one of up to six durable non-interactive commands and returns immediately with a job ID."""

    def apply(self, command: str, label: str, cwd: str | None = None, timeout_seconds: int | None = None) -> str:
        """Start a long-running command without blocking later Serena calls.

        Use this instead of ``execute_shell_command`` for tests, builds, simulations, optimisations, or other commands that may
        exceed the normal tool timeout. Always give the job a concise, distinctive label so it can be recovered in another chat.
        The job survives Serena MCP restarts. Continue useful work after starting it, then call ``job_status`` when progress or
        the final result is needed. Do not use for interactive commands.

        :param command: shell command to run non-interactively
        :param label: concise human-readable purpose, required for cross-chat recovery
        :param cwd: project-relative working directory; defaults to the active project root and may not escape it
        :param timeout_seconds: optional positive wall-clock runtime limit; omit for no runtime limit
        :return: JSON containing the job ID, state, concurrency usage, persistence guarantees, and suggested next action
        """
        record, running_jobs = self._job_manager.start_job(
            command=command,
            project_root=self.get_project_root(),
            label=label,
            project_name=self.project.project_name,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        payload = self._record_payload(record)
        payload.update(
            {
                "running_jobs": running_jobs,
                "max_concurrent_jobs": self._job_manager.max_concurrent_jobs,
                "persistence": self._persistence_payload(self._job_manager.persistence_info()),
                "next_step": (
                    "Continue other useful work. Call job_status with this job_id when progress or the final result is needed; "
                    "preserve the returned next_cursor between polls to avoid repeating output."
                ),
            }
        )
        return self._json(payload)


class JobStatusTool(_JobTool, ToolMarkerDoesNotRequireActiveProject):
    """Checks a durable job or lists recent jobs when no job ID is supplied."""

    def apply(
        self,
        job_id: str | None = None,
        cursor: str | None = None,
        output: Literal["latest", "start"] = "latest",
    ) -> str:
        """Return job state, telemetry, and bounded output.

        With a ``job_id``, the first call defaults to the latest bounded output tail. Set ``output="start"`` to read from the
        beginning instead. Pass ``next_cursor`` back on later calls to receive only new output. A stale journal cursor is recovered
        automatically by returning the latest tail with ``cursor_reset=true``. Without a ``job_id``, lists all running jobs first
        followed by recent terminal jobs so work can be recovered after Serena restarts or in another chat.

        :param job_id: opaque job ID returned by ``start_job``; omit to list recent jobs
        :param cursor: opaque cursor returned by the preceding status call for this job
        :param output: initial output position when no cursor is supplied: ``latest`` (default) or ``start``
        :return: JSON describing current state, telemetry, bounded output, persistence guarantees, and the appropriate next action
        """
        if job_id is None:
            if cursor is not None:
                raise ValueError("cursor requires job_id")
            snapshots = self._job_manager.list_job_snapshots()
            running_jobs = sum(snapshot.record.status is JobStatus.RUNNING for snapshot in snapshots)
            jobs: list[dict[str, object]] = []
            for snapshot in snapshots:
                item = self._record_payload(snapshot.record)
                item["runtime"] = self._runtime_payload(snapshot.runtime)
                jobs.append(item)
            return self._json(
                {
                    "jobs": jobs,
                    "running_jobs": running_jobs,
                    "max_concurrent_jobs": self._job_manager.max_concurrent_jobs,
                    "persistence": self._persistence_payload(self._job_manager.persistence_info()),
                    "next_step": "Call job_status with a job_id to retrieve that job's bounded output.",
                }
            )

        snapshot = self._job_manager.get_job(job_id, cursor, output_mode=output)
        return self._json(self._snapshot_payload(snapshot))

    def _snapshot_payload(self, snapshot: JobSnapshot) -> dict[str, object]:
        record = snapshot.record
        output = snapshot.output
        assert output is not None

        payload = self._record_payload(record)
        payload.update(
            {
                "runtime": self._runtime_payload(snapshot.runtime),
                "persistence": self._persistence_payload(self._job_manager.persistence_info()),
                "output": output.output,
                "next_cursor": output.next_cursor,
                "has_more_output": output.has_more_output,
                "output_truncated": output.output_truncated,
                "earlier_output_omitted": output.earlier_output_omitted,
                "cursor_reset": output.cursor_reset,
            }
        )

        if output.cursor_reset:
            payload["next_step"] = (
                "The previous journal cursor was no longer available, so Serena reset to the latest output tail. "
                "Use next_cursor for subsequent incremental polls."
            )
        elif output.has_more_output:
            payload["next_step"] = "Call job_status again immediately with next_cursor to drain already-buffered output."
        elif record.status is JobStatus.RUNNING:
            prefix = "The latest bounded output tail is shown; earlier output was omitted. " if output.earlier_output_omitted else ""
            payload["next_step"] = (
                prefix + "The job is still running. Continue other useful work and poll later with next_cursor when progress is needed."
            )
        elif output.earlier_output_omitted:
            payload["next_step"] = (
                "The job is finished; the latest bounded output tail is shown and earlier output was omitted. "
                'Call job_status with output="start" and no cursor if earlier output needs inspection.'
            )
        else:
            payload["next_step"] = "The job is finished; no further polling is needed."
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
        payload = self._record_payload(record)
        payload["next_step"] = "No further polling is needed." if record.status.is_terminal else "Check job_status for current state."
        return self._json(payload)
