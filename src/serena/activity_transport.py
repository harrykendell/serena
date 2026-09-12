"""Transport serialization for the shared Serena activity renderer."""

from __future__ import annotations

from typing import Any

from serena.activity_view import (
    ActivityCallDetail,
    ActivityEntrySummary,
    ActivityJobDetail,
    ActivityJobSummary,
    ActivityLatestSummary,
    ActivityOverview,
    ActivityRunningJobs,
    ActivitySnapshot,
)


def activity_entry_payload(call: ActivityEntrySummary) -> dict[str, Any]:
    """Returns one execution summary in the shared renderer contract."""
    payload: dict[str, Any] = {
        "call_id": call.call_id,
        "tool_name": call.tool_name,
        "detail": call.detail,
        "project_name": call.project_name,
        "started_at": call.started_at,
        "finished_at": call.finished_at,
        "status": call.status,
    }
    if call.scope:
        payload["scope"] = call.scope
    if call.job_id is not None:
        payload["job_id"] = call.job_id
    if call.job_label is not None:
        payload["job_label"] = call.job_label
    return payload


def activity_job_payload(job: ActivityJobSummary) -> dict[str, Any]:
    """Returns one durable-job summary in the shared renderer contract."""
    return {
        "job_id": job.job_id,
        "label": job.label,
        "project": job.project,
        "status": job.status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "current_turn": job.current_turn,
        "panel_id": job.panel_id,
    }


def activity_latest_payload(latest: ActivityLatestSummary | None) -> dict[str, Any] | None:
    """Returns the canonical latest-activity summary used by every renderer host."""
    if latest is None:
        return None
    return {
        "label": latest.label,
        "detail": latest.detail,
        "scope": latest.scope,
        "status": latest.status,
        "started_at": latest.started_at,
        "finished_at": latest.finished_at,
    }


def activity_call_detail_payload(detail: ActivityCallDetail) -> dict[str, Any]:
    """Returns one bounded call detail in the shared renderer contract."""
    return {
        "call_id": detail.call_id,
        "tool_name": detail.tool_name,
        "status": detail.status,
        "arguments": detail.arguments,
        "result": detail.result,
        "structured_result": detail.structured_result,
        "error": detail.error,
        "media": detail.media.public_dict() if detail.media is not None else None,
    }


def activity_job_detail_payload(detail: ActivityJobDetail) -> dict[str, Any]:
    """Returns one bounded durable-job detail in the shared renderer contract."""
    return {
        "job_id": detail.job_id,
        "label": detail.label,
        "project": detail.project,
        "cwd": detail.cwd,
        "status": detail.status,
        "status_message": detail.status_message,
        "return_code": detail.return_code,
        "timeout_seconds": detail.timeout_seconds,
        "elapsed_seconds": detail.elapsed_seconds,
        "seconds_since_last_output": detail.seconds_since_last_output,
        "memory_bytes": detail.memory_bytes,
        "cpu_seconds": detail.cpu_seconds,
        "process_count": detail.process_count,
        "output": detail.output,
        "output_truncated": detail.output_truncated,
        "earlier_output_omitted": detail.earlier_output_omitted,
        "has_earlier_output": detail.has_earlier_output,
        "cursor_reset": detail.cursor_reset,
    }


def activity_snapshot_payload(snapshot: ActivitySnapshot) -> dict[str, Any]:
    """Returns one complete activity snapshot in the shared renderer contract."""
    calls = [activity_entry_payload(call) for call in snapshot.calls]
    jobs = [activity_job_payload(job) for job in snapshot.jobs]
    job_count = len(jobs) if snapshot.run_id is None else sum(1 for job in snapshot.jobs if job.current_turn)
    return {
        "panel_id": snapshot.panel_id,
        "session_id": snapshot.session_id,
        "run_id": snapshot.run_id,
        "project_name": snapshot.project_name,
        "session_title": snapshot.session_title,
        "started_at": snapshot.started_at,
        "updated_at": snapshot.updated_at,
        "superseded": snapshot.superseded,
        "tool_count": len(calls),
        "job_count": job_count,
        "submission_span_seconds": snapshot.submission_span_seconds,
        "latest_activity": activity_latest_payload(snapshot.latest_activity),
        "git_additions": snapshot.git_metrics.additions,
        "git_deletions": snapshot.git_metrics.deletions,
        "git_ahead_commits": snapshot.git_metrics.ahead_commits,
        "calls": calls,
        "jobs": jobs,
        "expanded_call": activity_call_detail_payload(snapshot.expanded_call) if snapshot.expanded_call is not None else None,
        "expanded_job": activity_job_detail_payload(snapshot.expanded_job) if snapshot.expanded_job is not None else None,
    }


def activity_running_jobs_payload(running: ActivityRunningJobs) -> dict[str, Any]:
    """Returns compact global durable-job metadata for dashboard chrome."""
    jobs = [activity_job_payload(job) for job in running.running_jobs]
    return {
        "status": "success",
        "jobs": jobs,
        "running_jobs": len(jobs),
        "max_concurrent_jobs": running.max_concurrent_jobs,
    }


def activity_overview_payload(overview: ActivityOverview) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns compact Serena session discovery and running-job documents."""
    panels = [
        {
            "panel_id": summary.panel_id,
            "display_name": summary.display_name,
            "started_at": summary.started_at,
            "active": summary.active,
            "tool_count": summary.tool_count,
            "job_count": summary.job_count,
            "submission_span_seconds": summary.submission_span_seconds,
            "git_additions": summary.git_metrics.additions,
            "git_deletions": summary.git_metrics.deletions,
            "git_ahead_commits": summary.git_metrics.ahead_commits,
            "latest_activity": activity_latest_payload(summary.latest_activity),
        }
        for summary in overview.sessions
    ]
    running = ActivityRunningJobs(
        running_jobs=overview.running_jobs,
        max_concurrent_jobs=overview.max_concurrent_jobs,
    )
    return {"status": "success", "panels": panels}, activity_running_jobs_payload(running)
