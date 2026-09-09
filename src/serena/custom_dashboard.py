"""State and routes for the Kendell Serena/Orchestrator dashboard."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, abort, request
from mcp.types import ResourceLink
from pydantic import AnyUrl

from orchestrator.config import OrchestratorConfig
from orchestrator.dashboard_sessions import OrchestratorDashboardSessionArchive
from orchestrator.delegates import DelegateError, DelegateStore
from serena.activity import ActivityDetailFormatter, ActivityMedia
from serena.dashboard_activity import DashboardActivityArchive, DashboardActivitySessionSummary
from serena.dashboard_widgets import orchestrator_dashboard_widget_html, serena_dashboard_widget_html
from serena.jobs import JobManager, JobStatus
from serena.tools.media_tools import read_result_file_link

if TYPE_CHECKING:
    from serena.agent import SerenaAgent

CUSTOM_DASHBOARD_DIR = Path(__file__).parent / "resources" / "kendell_dashboard"
_CUSTOM_DASHBOARD_JOB_LIMIT = 1000


@dataclass(frozen=True)
class DashboardMediaContent:
    """Binary media or file content returned by one completed Serena tool execution."""

    data: bytes
    mime_type: str
    media_type: str
    file_name: str | None = None


class DashboardSessionOverview:
    """Read-only summary of the Serena state needed by the custom dashboard."""

    def __init__(self, agent: SerenaAgent):
        self._agent = agent

    def get_session(self) -> dict[str, Any]:
        """Returns compact session metadata for the custom dashboard."""
        project = self._agent.get_default_project()
        if project is None:
            project_info = {"name": None, "path": None}
            languages: list[str] = []
            memories: list[str] = []
        else:
            project_info = {"name": project.project_name, "path": str(project.project_root)}
            languages = [language.value for language in project.get_language_server_candidates()]
            memories = project.memory_manager.list_memories().get_full_list()

        return {
            "status": "success",
            "active_project": project_info,
            "languages": languages,
            "runtime_policy": "ChatGPT",
            "serena_version": self._agent.version,
            "active_tools": self._agent.get_active_tool_names(),
            "total_tools": len(self._agent.get_exposed_tool_instances()),
            "available_memories": memories,
        }


class DashboardMemoryOverview:
    """Read-only access to memories for the active project."""

    def __init__(self, agent: SerenaAgent):
        self._agent = agent

    def get_memory(self, memory_name: str) -> dict[str, Any]:
        """Returns one memory from the currently active project."""
        project = self._agent.get_default_project()
        if project is None:
            raise ValueError("No active project")

        content = project.memory_manager.load_memory(memory_name)
        return {
            "status": "success",
            "memory_name": memory_name,
            "content": content,
        }


class DashboardJobOverview:
    """Read-only overview of retained durable jobs for the custom dashboard."""

    def __init__(self, job_manager: JobManager):
        self._job_manager = job_manager

    def get_jobs(self) -> dict[str, Any]:
        """Returns running jobs followed by retained terminal jobs with lightweight telemetry."""
        snapshots = self._job_manager.list_job_snapshots(limit=_CUSTOM_DASHBOARD_JOB_LIMIT, running_only=False)
        persistence = self._job_manager.persistence_info()

        jobs: list[dict[str, Any]] = []
        running_jobs = 0
        terminal_jobs = 0
        for snapshot in snapshots:
            record = snapshot.record
            runtime = snapshot.runtime
            if record.status is JobStatus.RUNNING:
                running_jobs += 1
            elif record.status.is_terminal:
                terminal_jobs += 1
            jobs.append(
                {
                    "job_id": record.job_id,
                    "label": record.label,
                    "project": record.project_name,
                    "cwd": record.cwd,
                    "status": record.status.value,
                    "created_at": record.created_at,
                    "finished_at": record.finished_at,
                    "return_code": record.return_code,
                    "status_message": record.status_message,
                    "timeout_seconds": record.timeout_seconds,
                    "elapsed_seconds": runtime.elapsed_seconds,
                    "seconds_since_last_output": runtime.seconds_since_last_output,
                    "memory_bytes": runtime.memory_bytes,
                    "cpu_seconds": runtime.cpu_seconds,
                    "process_count": runtime.process_count,
                }
            )

        return {
            "status": "success",
            "jobs": jobs,
            "running_jobs": running_jobs,
            "terminal_jobs": terminal_jobs,
            "max_concurrent_jobs": self._job_manager.max_concurrent_jobs,
            "persistence": {
                "survives_serena_restart": persistence.survives_serena_restart,
                "survives_logout": persistence.survives_logout,
                "survives_reboot": persistence.survives_reboot,
                "linger_enabled": persistence.linger_enabled,
            },
        }

    def get_output(self, job_id: str, mode: str, cursor: str | None) -> dict[str, Any]:
        """Returns one bounded output page for a retained durable job."""
        if mode == "latest":
            if cursor is not None:
                raise ValueError("latest output does not accept a cursor")
            snapshot = self._job_manager.get_job(job_id)
        elif mode == "after":
            if cursor is None:
                raise ValueError("after output requires a cursor")
            snapshot = self._job_manager.get_job(job_id, cursor=cursor)
        elif mode == "before":
            if cursor is None:
                raise ValueError("before output requires a cursor")
            snapshot = self._job_manager.get_job_output_before(job_id, cursor)
        else:
            raise ValueError(f"Unsupported output mode {mode!r}")

        chunk = snapshot.output
        if chunk is None:
            raise RuntimeError(f"No output payload available for job {job_id!r}")
        return {
            "status": "success",
            "job_id": snapshot.record.job_id,
            "job_status": snapshot.record.status.value,
            "output": chunk.output,
            "newest_cursor": chunk.next_cursor,
            "oldest_cursor": chunk.oldest_cursor,
            "has_more_output": chunk.has_more_output,
            "has_earlier_output": chunk.has_earlier_output,
            "output_truncated": chunk.output_truncated,
            "earlier_output_omitted": chunk.earlier_output_omitted,
            "cursor_reset": chunk.cursor_reset,
        }


class DashboardSerenaActivityOverview:
    """Provides retained ChatGPT-session Serena activity for the operator dashboard."""

    def __init__(
        self,
        archive: DashboardActivityArchive,
        job_overview: DashboardJobOverview,
    ) -> None:
        self._archive = archive
        self._job_overview = job_overview
        self._activity_formatter = ActivityDetailFormatter()
        self._jobs_cache_lock = threading.Lock()
        self._jobs_cache_at = 0.0
        self._jobs_cache: dict[str, dict[str, Any]] = {}

    def get_panels(self, include_state: bool = False) -> dict[str, Any]:
        """Returns retained Serena session panels newest first by creation time."""
        jobs = self._jobs_by_id()
        panels: list[dict[str, Any]] = []
        for summary in self._archive.list_session_summaries():
            job_ids = list(summary.job_ids)
            active = summary.has_active_calls or any(jobs.get(job_id, {}).get("status") == "running" for job_id in job_ids)
            panel = {
                "panel_id": summary.panel_id,
                "project_name": summary.project_name,
                "display_name": summary.display_name,
                "started_at": summary.started_at,
                "updated_at": summary.updated_at,
                "revision": self._summary_revision(summary, jobs),
                "active": active,
            }
            if include_state:
                panel["initial_state"] = self._summary_panel_state(summary, jobs)
            panels.append(panel)
        panels.sort(key=lambda item: (float(item.get("started_at") or 0.0), str(item["panel_id"])), reverse=True)
        return {"status": "success", "panels": panels}

    def get_panel(self, panel_id: str, changed_since: float | None = None) -> dict[str, Any]:
        """Returns one retained Serena session, optionally restricted to changes after ``changed_since``."""
        session = self._archive.get_session(panel_id)
        return self._panel_state(session, self._jobs_by_id(), changed_since=changed_since)

    def _summary_panel_state(
        self,
        summary: DashboardActivitySessionSummary,
        jobs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Returns bounded first-paint state for one retained session."""
        visible_jobs = [self._job_payload(jobs[job_id]) for job_id in summary.job_ids if job_id in jobs]
        running_jobs = [job for job in visible_jobs if job.get("status") == "running"]
        terminal_jobs = [job for job in visible_jobs if job.get("status") != "running"]

        latest_timestamps: list[float] = []
        if summary.latest_call is not None:
            latest_call_at = (
                summary.latest_call.get("finished_at") or summary.latest_call.get("started_at") or summary.latest_call.get("submitted_at")
            )
            if isinstance(latest_call_at, int | float):
                latest_timestamps.append(float(latest_call_at))
        for job in visible_jobs:
            job_at = job.get("finished_at") or job.get("started_at")
            if isinstance(job_at, int | float):
                latest_timestamps.append(float(job_at))
        recently_active = (
            summary.has_active_calls or bool(running_jobs) or (latest_timestamps and time.time() - max(latest_timestamps) <= 5 * 60)
        )

        selected_calls = summary.recent_calls if recently_active else ((summary.latest_call,) if summary.latest_call is not None else ())
        payload_jobs = [*running_jobs, *terminal_jobs[-4:]] if recently_active else terminal_jobs[-1:]
        calls = [self._call_payload(call) for call in selected_calls]
        return {
            "run_id": summary.panel_id,
            "project_name": summary.project_name,
            "started_at": summary.started_at,
            "updated_at": summary.updated_at,
            "revision": self._summary_revision(summary, jobs),
            "superseded": False,
            "summary_only": True,
            "partial": False,
            "initial_expanded": bool(recently_active),
            "tool_count": summary.tool_count,
            "job_count": len(visible_jobs),
            "calls": calls,
            "jobs": payload_jobs,
        }

    def _panel_state(
        self,
        session: dict[str, Any],
        jobs: dict[str, dict[str, Any]],
        *,
        summary: bool = False,
        changed_since: float | None = None,
    ) -> dict[str, Any]:
        """Returns one retained session snapshot, optionally compact or incremental."""
        calls = list(session.get("calls", []))
        job_ids = self._session_job_ids(session)
        visible_jobs = [self._job_payload(jobs[job_id]) for job_id in job_ids if job_id in jobs]

        # keep inactive bootstrap state tiny; the full history remains available on expansion
        if summary:
            payload_calls = calls[-1:]
            payload_jobs = visible_jobs[-1:]
        elif changed_since is not None:
            payload_calls = [call for call in calls if self._call_changed_after(call, changed_since)]
            running_jobs = [job for job in visible_jobs if job.get("status") == "running"]
            terminal_jobs = [job for job in visible_jobs if job.get("status") != "running"]
            payload_jobs = [*running_jobs, *terminal_jobs[-4:]]
        else:
            payload_calls = calls
            payload_jobs = visible_jobs

        return {
            "run_id": session["panel_id"],
            "project_name": session.get("project_name") or "",
            "started_at": session.get("started_at"),
            "updated_at": float(session.get("updated_at") or 0.0),
            "revision": self._panel_revision(session, jobs, job_ids),
            "superseded": False,
            "summary_only": summary,
            "partial": changed_since is not None and not summary,
            "tool_count": len(calls),
            "job_count": len(visible_jobs),
            "calls": [self._call_payload(call) for call in payload_calls],
            "jobs": payload_jobs,
        }

    @staticmethod
    def _call_changed_after(call: dict[str, Any], timestamp: float) -> bool:
        """Returns whether one call may have changed after ``timestamp``."""
        if call.get("status") in {"running", "queued"}:
            return True
        for key in ("submitted_at", "started_at", "finished_at"):
            value = call.get(key)
            if isinstance(value, int | float) and float(value) > timestamp:
                return True
        return False

    @staticmethod
    def _summary_revision(
        summary: DashboardActivitySessionSummary,
        jobs: dict[str, dict[str, Any]],
    ) -> str:
        """Returns a compact revision from retained-session metadata and visible jobs."""
        parts = [str(summary.updated_at)]
        for job_id in summary.job_ids:
            item = jobs.get(job_id)
            if item is None:
                continue
            parts.extend(
                (
                    job_id,
                    str(item.get("status") or ""),
                    str(item.get("finished_at") or ""),
                    str(item.get("return_code") if item.get("return_code") is not None else ""),
                )
            )
        return hashlib.blake2s("\x1f".join(parts).encode("utf-8"), digest_size=8).hexdigest()

    @staticmethod
    def _panel_revision(session: dict[str, Any], jobs: dict[str, dict[str, Any]], job_ids: list[str]) -> str:
        """Returns a compact revision that changes with visible tool or job state."""
        parts = [str(session.get("updated_at") or 0.0)]
        for job_id in job_ids:
            item = jobs.get(job_id)
            if item is None:
                continue
            parts.extend(
                (
                    job_id,
                    str(item.get("status") or ""),
                    str(item.get("finished_at") or ""),
                    str(item.get("return_code") if item.get("return_code") is not None else ""),
                )
            )
        return hashlib.blake2s("\x1f".join(parts).encode("utf-8"), digest_size=8).hexdigest()

    def get_call_detail(self, panel_id: str, call_id: str) -> dict[str, Any]:
        """Returns one retained tool call's bounded detail."""
        call = self._archive.get_call(panel_id, call_id)
        media = ActivityMedia.from_storage_dict(call.get("media")) or ActivityMedia.from_serialized_result(str(call.get("result") or ""))
        parameters = str(call.get("parameters") or "")
        return {
            "call_id": call_id,
            "tool_name": call.get("tool_name") or "",
            "status": call.get("status") or "completed",
            "arguments": parameters or "{}",
            "structured_arguments": self._activity_formatter.parse_parameters(parameters),
            "result": None if media is not None else call.get("error") or call.get("result"),
            "media": media.public_dict() if media is not None else None,
        }

    def get_call_media(self, panel_id: str, call_id: str) -> DashboardMediaContent:
        """Returns retained media bytes for one dashboard activity call."""
        call = self._archive.get_call(panel_id, call_id)
        media = ActivityMedia.from_storage_dict(call.get("media")) or ActivityMedia.from_serialized_result(str(call.get("result") or ""))
        if media is None:
            raise ValueError("Activity call has no retained media")
        link = ResourceLink(
            type="resource_link",
            name=media.name,
            uri=AnyUrl(media.uri),
            mimeType=media.mime_type,
        )
        return DashboardMediaContent(
            data=read_result_file_link(link),
            mime_type=media.mime_type,
            media_type=media.media_type,
            file_name=media.name,
        )

    def get_job_detail(self, job_id: str) -> dict[str, Any]:
        """Returns one retained durable job in the inline-widget detail shape."""
        jobs = self._jobs_by_id()
        item = jobs.get(job_id)
        if item is None:
            raise KeyError(job_id)
        try:
            output = self._job_overview.get_output(job_id, "latest", None)
        except (KeyError, RuntimeError, ValueError):
            output = {"output": "", "has_earlier_output": False, "earlier_output_omitted": False}
        return {
            "job_id": job_id,
            "status": item.get("status"),
            "project": item.get("project"),
            "elapsed_seconds": item.get("elapsed_seconds"),
            "seconds_since_last_output": item.get("seconds_since_last_output"),
            "memory_bytes": item.get("memory_bytes"),
            "cpu_seconds": item.get("cpu_seconds"),
            "process_count": item.get("process_count"),
            "timeout_seconds": item.get("timeout_seconds"),
            "return_code": item.get("return_code"),
            "output": output.get("output") or "",
            "has_earlier_output": bool(output.get("has_earlier_output")),
            "earlier_output_omitted": bool(output.get("earlier_output_omitted")),
        }

    def _jobs_by_id(self) -> dict[str, dict[str, Any]]:
        """Returns briefly cached durable jobs indexed by job identifier."""
        now = time.monotonic()
        with self._jobs_cache_lock:
            if self._jobs_cache and now - self._jobs_cache_at < 0.5:
                return self._jobs_cache

            payload = self._job_overview.get_jobs()
            self._jobs_cache = {str(item["job_id"]): item for item in payload.get("jobs", [])}
            self._jobs_cache_at = time.monotonic()
            return self._jobs_cache

    @staticmethod
    def _session_job_ids(session: dict[str, Any]) -> list[str]:
        """Returns unique durable jobs launched from one retained ChatGPT session."""
        result: list[str] = []
        for call in session.get("calls", []):
            job_id = call.get("job_id")
            if job_id and job_id not in result:
                result.append(str(job_id))
        return result

    def _call_payload(self, call: dict[str, Any]) -> dict[str, Any]:
        """Returns one persistent call in the inline-widget list shape."""
        tool_name = call.get("tool_name") or ""
        parameters = call.get("parameters")
        summary = self._activity_formatter.format_parameters(tool_name, parameters if isinstance(parameters, str) else None)

        # re-derive historical entries from retained parameters; fall back to stored fields when parameters are unavailable
        detail = summary.detail if summary.detail or summary.scope else call.get("detail") or ""
        scope = summary.scope if summary.detail or summary.scope else call.get("scope") or ""
        return {
            "call_id": call.get("call_id"),
            "tool_name": tool_name,
            "detail": detail,
            "scope": scope,
            "status": call.get("status") or "completed",
            "submitted_at": call.get("submitted_at") or call.get("started_at"),
            "started_at": call.get("started_at") or call.get("submitted_at"),
            "finished_at": call.get("finished_at"),
            "job_id": call.get("job_id"),
        }

    @staticmethod
    def _job_payload(item: dict[str, Any]) -> dict[str, Any]:
        """Returns one dashboard job in the inline-widget list shape."""
        created_at = item.get("created_at")
        finished_at = item.get("finished_at")
        return {
            "job_id": item.get("job_id"),
            "label": item.get("label") or "background job",
            "project": item.get("project") or "",
            "status": item.get("status") or "completed",
            "started_at": datetime.fromisoformat(created_at).timestamp() if isinstance(created_at, str) else time.time(),
            "finished_at": datetime.fromisoformat(finished_at).timestamp() if isinstance(finished_at, str) else None,
            "current_turn": True,
        }


class DashboardOrchestratorOverview:
    """Provides global read-only Orchestrator activity for the operator dashboard."""

    def __init__(
        self,
        delegate_store: DelegateStore | None = None,
        session_archive: OrchestratorDashboardSessionArchive | None = None,
    ) -> None:
        self._delegate_store = delegate_store
        self._session_archive = session_archive

    def _store(self) -> DelegateStore:
        """Returns the shared durable Orchestrator store, creating it only when queried."""
        if self._delegate_store is None:
            self._delegate_store = DelegateStore(OrchestratorConfig.from_environment())
        return self._delegate_store

    def _sessions(self) -> OrchestratorDashboardSessionArchive:
        """Returns retained Orchestrator conversation metadata."""
        if self._session_archive is None:
            self._session_archive = OrchestratorDashboardSessionArchive(OrchestratorConfig.from_environment())
        return self._session_archive

    def _panels(self) -> list[dict[str, Any]]:
        """Merges durable delegate activity with retained conversation metadata."""
        by_id = {panel["panel_id"]: dict(panel) for panel in self._store().list_dashboard_activity()}
        for session in self._sessions().list_sessions():
            panel_id = str(session["panel_id"])
            panel = by_id.setdefault(
                panel_id,
                {
                    "panel_id": panel_id,
                    "started_at": session.get("started_at"),
                    "updated_at": session.get("updated_at"),
                    "active": False,
                    "delegates": [],
                },
            )
            panel["display_name"] = session.get("display_name") or ""
            panel["started_at"] = min(float(panel.get("started_at") or session["started_at"]), float(session["started_at"]))
            panel["updated_at"] = max(float(panel.get("updated_at") or 0.0), float(session.get("updated_at") or 0.0))

        panels = list(by_id.values())
        for panel in panels:
            panel["revision"] = str(panel.get("updated_at") or panel.get("started_at") or 0.0)
        panels.sort(key=lambda panel: (float(panel.get("started_at") or 0.0), str(panel["panel_id"])), reverse=True)
        return panels

    def get_panels(self) -> dict[str, Any]:
        """Returns retained orchestration panels across ChatGPT sessions."""
        return {"status": "success", "panels": self._panels()}

    def get_panel(self, panel_id: str) -> dict[str, Any]:
        """Returns one retained orchestration panel by its opaque dashboard identifier."""
        for panel in self._panels():
            if panel["panel_id"] == panel_id:
                return {
                    "run_id": panel["panel_id"],
                    "started_at": panel["started_at"],
                    "superseded": False,
                    "delegates": panel["delegates"],
                }
        raise KeyError(panel_id)

    def get_delegate_detail(self, delegate_id: str) -> dict[str, Any]:
        """Returns operator-visible detail for one durable delegate."""
        try:
            return self._store().dashboard_detail(delegate_id).model_dump(mode="json")
        except DelegateError as exc:
            raise KeyError(delegate_id) from exc


class CustomDashboard:
    """Fork-specific dashboard integration kept outside Serena's upstream frontend implementation."""

    def __init__(self, app: Flask, agent: SerenaAgent):
        self._session_overview = DashboardSessionOverview(agent)
        self._memory_overview = DashboardMemoryOverview(agent)
        self._activity_archive = DashboardActivityArchive(agent.execution_store)
        self._job_overview = DashboardJobOverview(JobManager())
        self._serena_activity_overview = DashboardSerenaActivityOverview(
            self._activity_archive,
            self._job_overview,
        )
        self._orchestrator_overview = DashboardOrchestratorOverview()
        self._register_routes(app)

    def set_serena_session_name(self, session_id: str, display_name: str) -> str:
        """Sets the retained dashboard name for one ChatGPT conversation."""
        return self._activity_archive.set_display_name(session_id, display_name)

    def dashboard_state(self, *, include_state: bool = False) -> dict[str, Any]:
        """Returns the complete dashboard state payload for API and first-paint bootstrap use."""
        return {
            "status": "success",
            "session": self._session_overview.get_session(),
            "serena": self._serena_activity_overview.get_panels(include_state=include_state),
            "orchestrator": self._orchestrator_overview.get_panels(),
        }

    def render_index_html(self) -> str:
        """Returns the dashboard shell with compact first-paint state and versioned static assets."""
        index_path = self.static_dir / "index.html"
        html = index_path.read_text(encoding="utf-8")

        asset_paths = [self.static_dir / name for name in ("dashboard.js", "styles.css", "serena-logo.svg", "orchestrator-logo.svg")]
        revision_input = "\x1f".join(f"{path.name}:{path.stat().st_mtime_ns}:{path.stat().st_size}" for path in asset_paths)
        asset_version = hashlib.blake2s(revision_input.encode("utf-8"), digest_size=6).hexdigest()
        for asset in ("dashboard.js", "styles.css", "serena-logo.svg", "orchestrator-logo.svg"):
            html = html.replace(f'"{asset}"', f'"{asset}?v={asset_version}"')
        html = html.replace('<html lang="en">', f'<html lang="en" data-asset-version="{asset_version}">', 1)

        bootstrap = json.dumps(self.dashboard_state(include_state=True), ensure_ascii=False, separators=(",", ":"))
        bootstrap = bootstrap.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        bootstrap_tag = f'<script id="dashboard-bootstrap" type="application/json">{bootstrap}</script>'
        return html.replace("</head>", f"  {bootstrap_tag}\n</head>", 1)

    @property
    def static_dir(self) -> Path:
        """Returns the directory containing the custom dashboard frontend."""
        return CUSTOM_DASHBOARD_DIR

    @staticmethod
    def _conditional_json_response(app: Flask, payload: dict[str, Any]) -> Response:
        """Returns cache-revalidated JSON so unchanged dashboard polls carry no response body."""
        body = app.json.dumps(payload)
        response = Response(body, mimetype="application/json")
        response.headers["Cache-Control"] = "private, no-cache"
        response.set_etag(hashlib.blake2s(body.encode("utf-8"), digest_size=16).hexdigest())
        response.make_conditional(request)
        return response

    def _register_routes(self, app: Flask) -> None:
        """Registers all dashboard APIs under the dashboard URL namespace."""

        @app.route("/dashboard/api/state", methods=["GET"])
        def get_dashboard_state() -> Response:
            include_state = request.args.get("include_state") == "1"
            return self._conditional_json_response(app, self.dashboard_state(include_state=include_state))

        @app.route("/dashboard/api/session", methods=["GET"])
        def get_session() -> Response:
            return self._conditional_json_response(app, self._session_overview.get_session())

        @app.route("/dashboard/api/memory", methods=["GET"])
        def get_custom_memory() -> dict[str, Any]:
            try:
                memory_name = request.args.get("name")
                if not memory_name:
                    raise ValueError("Memory name is required")
                return self._memory_overview.get_memory(memory_name)
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @app.route("/dashboard/api/serena", methods=["GET"])
        def get_serena_panels() -> Response:
            include_state = request.args.get("include_state") == "1"
            payload = self._serena_activity_overview.get_panels(include_state=include_state)
            return self._conditional_json_response(app, payload)

        @app.route("/dashboard/api/serena/panels/<panel_id>", methods=["GET"])
        def get_serena_panel(panel_id: str) -> Response:
            changed_since_raw = request.args.get("changed_since")
            try:
                changed_since = float(changed_since_raw) if changed_since_raw is not None else None
                payload = self._serena_activity_overview.get_panel(panel_id, changed_since=changed_since)
            except ValueError:
                abort(400)
            except KeyError:
                abort(404)
            return self._conditional_json_response(app, payload)

        @app.route("/dashboard/api/serena/panels/<panel_id>/calls/<call_id>", methods=["GET"])
        def get_serena_call_detail(panel_id: str, call_id: str) -> dict[str, Any]:
            try:
                detail = self._serena_activity_overview.get_call_detail(panel_id, call_id)
            except KeyError:
                abort(404)
            media = detail.get("media")
            if isinstance(media, dict):
                media["url"] = f"/dashboard/api/serena/panels/{panel_id}/calls/{call_id}/media"
            return detail

        @app.route("/dashboard/api/serena/panels/<panel_id>/calls/<call_id>/media", methods=["GET"])
        def get_serena_call_media(panel_id: str, call_id: str) -> Response:
            try:
                media = self._serena_activity_overview.get_call_media(panel_id, call_id)
            except (KeyError, ValueError, FileNotFoundError):
                abort(404)
            response = Response(media.data, mimetype=media.mime_type)
            response.headers["Cache-Control"] = "private, max-age=3600"
            return response

        @app.route("/dashboard/api/serena/jobs/<job_id>", methods=["GET"])
        def get_serena_job_detail(job_id: str) -> dict[str, Any]:
            try:
                return self._serena_activity_overview.get_job_detail(job_id)
            except KeyError:
                abort(404)

        @app.route("/dashboard/api/orchestrator", methods=["GET"])
        def get_orchestrator_panels() -> Response:
            return self._conditional_json_response(app, self._orchestrator_overview.get_panels())

        @app.route("/dashboard/api/orchestrator/panels/<panel_id>", methods=["GET"])
        def get_orchestrator_panel(panel_id: str) -> Response:
            try:
                payload = self._orchestrator_overview.get_panel(panel_id)
            except KeyError:
                abort(404)
            return self._conditional_json_response(app, payload)

        @app.route("/dashboard/api/orchestrator/delegates/<delegate_id>", methods=["GET"])
        def get_orchestrator_delegate_detail(delegate_id: str) -> dict[str, Any]:
            try:
                return self._orchestrator_overview.get_delegate_detail(delegate_id)
            except KeyError:
                abort(404)

        @app.route("/dashboard/widget/serena", defaults={"panel_id": ""}, methods=["GET"])
        @app.route("/dashboard/widget/serena/<panel_id>", methods=["GET"])
        def get_serena_activity_widget(panel_id: str) -> Response:
            if panel_id:
                try:
                    self._activity_archive.get_session(panel_id)
                except KeyError:
                    abort(404)
            response = Response(serena_dashboard_widget_html(panel_id or None), mimetype="text/html")
            response.headers["Cache-Control"] = "private, no-store" if panel_id else "private, max-age=3600"
            return response

        @app.route("/dashboard/widget/orchestrator", defaults={"panel_id": ""}, methods=["GET"])
        @app.route("/dashboard/widget/orchestrator/<panel_id>", methods=["GET"])
        def get_orchestrator_activity_widget(panel_id: str) -> Response:
            initial_state = None
            if panel_id:
                try:
                    initial_state = self._orchestrator_overview.get_panel(panel_id)
                except KeyError:
                    abort(404)
            response = Response(orchestrator_dashboard_widget_html(panel_id or None, initial_state), mimetype="text/html")
            response.headers["Cache-Control"] = "private, no-store" if panel_id else "private, max-age=3600"
            return response
