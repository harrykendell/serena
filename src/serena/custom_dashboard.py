"""State and routes for the Kendell Serena/Orchestrator dashboard."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, abort, redirect, request
from mcp.types import ResourceLink
from pydantic import AnyUrl

from orchestrator.config import OrchestratorConfig
from orchestrator.dashboard_sessions import OrchestratorDashboardSessionArchive
from orchestrator.delegates import DelegateError, DelegateStore
from serena.activity_view import ActivityCallDetail, ActivityJobDetail, ActivityOverview, ActivitySnapshot, ActivityView
from serena.dashboard_activity import DashboardActivityArchive
from serena.dashboard_widgets import orchestrator_dashboard_widget_html, serena_dashboard_widget_html
from serena.push_notifications import WebPushNotifier
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
        """Returns compact runtime metadata for the custom dashboard."""
        project = self._agent.get_default_project()
        if project is None:
            languages: list[str] = []
            memories: list[str] = []
        else:
            languages = [language.value for language in project.get_language_server_candidates()]
            memories = project.memory_manager.list_memories().get_full_list()

        return {
            "status": "success",
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


class DashboardSerenaActivityOverview:
    """Adapts typed ``ActivityView`` snapshots to the legacy dashboard HTTP contract."""

    def __init__(self, activity_view: ActivityView) -> None:
        self._activity_view = activity_view

    def dashboard_state(self, include_state: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """Returns Serena panels and global running jobs from one canonical overview query."""
        overview = self._activity_view.dashboard_overview()
        return self._panels_payload(overview, include_state=include_state), self._running_jobs_payload(overview)

    def get_panels(self, include_state: bool = False) -> dict[str, Any]:
        """Returns every retained Serena session from one compact overview query."""
        return self._panels_payload(self._activity_view.dashboard_overview(), include_state=include_state)

    def get_running_jobs(self) -> dict[str, Any]:
        """Returns current global running-job metadata without runtime telemetry."""
        return self._running_jobs_payload(self._activity_view.dashboard_overview())

    def panel_id_for_job(self, job_id: str) -> str | None:
        """Resolves a dashboard panel directly from canonical durable-job ownership."""
        return self._activity_view.panel_id_for_job(job_id)

    def get_panel(self, panel_id: str, changed_since: float | None = None) -> dict[str, Any]:
        """Returns one retained Serena session through the legacy panel transport shape."""
        snapshot = self._activity_view.for_session(panel_id)
        payload = self._snapshot_payload(snapshot)
        if changed_since is not None:
            payload["calls"] = [
                call
                for call in payload["calls"]
                if max(float(call.get("started_at") or 0.0), float(call.get("finished_at") or 0.0)) > changed_since
            ]
            payload["jobs"] = [
                job
                for job in payload["jobs"]
                if job.get("status") == "running"
                or max(float(job.get("started_at") or 0.0), float(job.get("finished_at") or 0.0)) > changed_since
            ]
            payload["partial"] = True
        return payload

    def get_session_document(self, panel_id: str, expanded_entry_id: str | None = None) -> dict[str, Any]:
        """Returns the new complete selected-session document with optional expanded detail."""
        snapshot = self._activity_view.for_session(panel_id, expanded_entry_id=expanded_entry_id)
        payload = self._snapshot_payload(snapshot)
        payload["panel_id"] = snapshot.panel_id
        payload["session_id"] = snapshot.session_id
        payload["expanded_call"] = self._call_detail_payload(snapshot.expanded_call) if snapshot.expanded_call is not None else None
        payload["expanded_job"] = self._job_detail_payload(snapshot.expanded_job) if snapshot.expanded_job is not None else None
        return payload

    def get_call_detail(self, panel_id: str, call_id: str) -> dict[str, Any]:
        """Returns call detail through the canonical selected-session view."""
        snapshot = self._activity_view.for_session(panel_id, expanded_entry_id=call_id)
        detail = snapshot.expanded_call
        if detail is None:
            raise KeyError(call_id)
        return self._call_detail_payload(detail)

    def get_call_media(self, panel_id: str, call_id: str) -> DashboardMediaContent:
        """Returns retained media bytes for one dashboard activity call."""
        snapshot = self._activity_view.for_session(panel_id, expanded_entry_id=call_id)
        detail = snapshot.expanded_call
        media = detail.media if detail is not None else None
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
        """Returns one job detail through canonical job-to-session ownership."""
        panel_id = self._activity_view.panel_id_for_job(job_id)
        if panel_id is None:
            raise KeyError(job_id)
        snapshot = self._activity_view.for_session(panel_id, expanded_entry_id=job_id)
        detail = snapshot.expanded_job
        if detail is None:
            raise KeyError(job_id)
        return self._job_detail_payload(detail)

    def _panels_payload(self, overview: ActivityOverview, *, include_state: bool) -> dict[str, Any]:
        """Converts the typed overview to the current outer-dashboard discovery contract."""
        running_by_session: dict[str, list[dict[str, Any]]] = {}
        for job in overview.running_jobs:
            if job.session_id is not None:
                running_by_session.setdefault(job.session_id, []).append(self._job_payload(job))

        panels: list[dict[str, Any]] = []
        for summary in overview.sessions:
            panel: dict[str, Any] = {
                "panel_id": summary.panel_id,
                "project_name": summary.project_name,
                "display_name": summary.display_name,
                "started_at": summary.started_at,
                "updated_at": summary.updated_at,
                "revision": self._summary_revision(summary, running_by_session.get(summary.session_id, [])),
                "active": summary.active,
            }
            if include_state:
                latest_calls = [self._entry_payload(summary.latest_call)] if summary.latest_call is not None else []
                jobs = running_by_session.get(summary.session_id, [])
                panel["initial_state"] = {
                    "run_id": summary.panel_id,
                    "project_name": summary.project_name,
                    "session_title": summary.display_name,
                    "started_at": summary.started_at,
                    "updated_at": summary.updated_at,
                    "revision": panel["revision"],
                    "superseded": False,
                    "summary_only": True,
                    "partial": False,
                    "initial_expanded": summary.active,
                    "tool_count": summary.tool_count,
                    "job_count": summary.job_count,
                    "submission_span_seconds": summary.submission_span_seconds,
                    "git_additions": summary.git_metrics.additions,
                    "git_deletions": summary.git_metrics.deletions,
                    "git_ahead_commits": summary.git_metrics.ahead_commits,
                    "calls": latest_calls,
                    "jobs": jobs,
                }
            panels.append(panel)
        return {"status": "success", "panels": panels}

    @staticmethod
    def _running_jobs_payload(overview: ActivityOverview) -> dict[str, Any]:
        """Converts lightweight current running jobs to dashboard metadata."""
        jobs = [DashboardSerenaActivityOverview._job_payload(job) for job in overview.running_jobs]
        return {
            "status": "success",
            "jobs": jobs,
            "running_jobs": len(jobs),
            "max_concurrent_jobs": overview.max_concurrent_jobs,
        }

    @staticmethod
    def _snapshot_payload(snapshot: ActivitySnapshot) -> dict[str, Any]:
        """Converts one complete typed session snapshot to the legacy renderer shape."""
        calls = [DashboardSerenaActivityOverview._entry_payload(call) for call in snapshot.calls]
        jobs = [DashboardSerenaActivityOverview._job_payload(job) for job in snapshot.jobs]
        submission_times = [call.started_at for call in snapshot.calls]
        submission_span = max(submission_times) - min(submission_times) if submission_times else None
        revision = DashboardSerenaActivityOverview._revision(
            snapshot.updated_at,
            [(call.call_id, call.status, call.finished_at, call.job_id) for call in snapshot.calls],
            [(job.job_id, job.status, job.finished_at) for job in snapshot.jobs],
            snapshot.git_metrics.additions,
            snapshot.git_metrics.deletions,
            snapshot.git_metrics.ahead_commits,
        )
        return {
            "run_id": snapshot.run_id or snapshot.panel_id,
            "project_name": snapshot.project_name,
            "session_title": snapshot.session_title,
            "started_at": snapshot.started_at,
            "updated_at": snapshot.updated_at,
            "revision": revision,
            "superseded": snapshot.superseded,
            "summary_only": False,
            "partial": False,
            "tool_count": len(calls),
            "job_count": len(jobs),
            "submission_span_seconds": submission_span,
            "git_additions": snapshot.git_metrics.additions,
            "git_deletions": snapshot.git_metrics.deletions,
            "git_ahead_commits": snapshot.git_metrics.ahead_commits,
            "calls": calls,
            "jobs": jobs,
        }

    @staticmethod
    def _entry_payload(call: Any) -> dict[str, Any]:
        """Converts one typed execution summary without including historical bodies."""
        payload: dict[str, Any] = {
            "call_id": call.call_id,
            "tool_name": call.tool_name,
            "detail": call.detail,
            "scope": call.scope,
            "project_name": call.project_name,
            "submitted_at": call.started_at,
            "started_at": call.started_at,
            "finished_at": call.finished_at,
            "status": call.status,
        }
        if call.job_id is not None:
            payload["job_id"] = call.job_id
        if call.job_label is not None:
            payload["job_label"] = call.job_label
        return payload

    @staticmethod
    def _job_payload(job: Any) -> dict[str, Any]:
        """Converts one lightweight typed durable-job summary."""
        return {
            "job_id": job.job_id,
            "label": job.label,
            "project": job.project,
            "status": job.status,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "current_turn": job.current_turn,
        }

    @staticmethod
    def _call_detail_payload(detail: ActivityCallDetail) -> dict[str, Any]:
        """Converts one typed call detail at the HTTP boundary."""
        return {
            "call_id": detail.call_id,
            "tool_name": detail.tool_name,
            "status": detail.status,
            "arguments": detail.arguments,
            "structured_arguments": detail.arguments,
            "result": detail.result,
            "structured_result": detail.structured_result,
            "error": detail.error,
            "media": detail.media.public_dict() if detail.media is not None else None,
        }

    @staticmethod
    def _job_detail_payload(detail: ActivityJobDetail) -> dict[str, Any]:
        """Converts one typed job detail at the HTTP boundary."""
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

    @staticmethod
    def _summary_revision(summary: Any, running_jobs: list[dict[str, Any]]) -> str:
        """Returns a temporary legacy browser revision outside the canonical view model."""
        latest = summary.latest_call
        return DashboardSerenaActivityOverview._revision(
            summary.updated_at,
            summary.active,
            summary.tool_count,
            summary.job_count,
            (
                latest.call_id,
                latest.status,
                latest.finished_at,
                latest.job_id,
            )
            if latest is not None
            else None,
            [(job["job_id"], job["status"], job["finished_at"]) for job in running_jobs],
            summary.git_metrics.additions,
            summary.git_metrics.deletions,
            summary.git_metrics.ahead_commits,
        )

    @staticmethod
    def _revision(*parts: Any) -> str:
        """Returns a deterministic short revision for the legacy iframe dirty protocol."""
        serialized = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


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
    """Serena-specific dashboard integration kept outside the bundled frontend implementation."""

    def __init__(self, app: Flask, agent: SerenaAgent):
        self._session_overview = DashboardSessionOverview(agent)
        self._memory_overview = DashboardMemoryOverview(agent)
        self._activity_archive = DashboardActivityArchive(agent.execution_store)
        self._activity_view = ActivityView(
            execution_store=agent.execution_store,
            job_source=agent.job_manager,
            git_metrics_source=agent,
        )
        self._serena_activity_overview = DashboardSerenaActivityOverview(self._activity_view)
        self._orchestrator_overview = DashboardOrchestratorOverview()
        self._push_notifier = WebPushNotifier()
        self._register_routes(app)

    def set_serena_session_name(self, session_id: str, display_name: str) -> str:
        """Sets the retained dashboard name for one ChatGPT conversation."""
        return self._activity_archive.set_display_name(session_id, display_name)

    def dashboard_state(self, *, include_state: bool = False) -> dict[str, Any]:
        """Returns the complete dashboard state payload for API and first-paint bootstrap use."""
        serena, jobs = self._serena_activity_overview.dashboard_state(include_state=include_state)
        return {
            "status": "success",
            "session": self._session_overview.get_session(),
            "jobs": jobs,
            "serena": serena,
            "orchestrator": self._orchestrator_overview.get_panels(),
        }

    def render_index_html(self) -> str:
        """Returns the dashboard shell with compact first-paint state and versioned static assets."""
        index_path = self.static_dir / "index.html"
        html = index_path.read_text(encoding="utf-8")

        assets = (
            "dashboard.js",
            "styles.css",
            "service-worker.js",
            "manifest.webmanifest",
            "serena-logo.svg",
            "orchestrator-logo.svg",
            "serena-icon-128.png",
        )
        asset_paths = [self.static_dir / name for name in assets]
        revision_input = "\x1f".join(f"{path.name}:{path.stat().st_mtime_ns}:{path.stat().st_size}" for path in asset_paths)
        asset_version = hashlib.blake2s(revision_input.encode("utf-8"), digest_size=6).hexdigest()
        for asset in assets:
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

        @app.route("/dashboard/job/<job_id>", methods=["GET"])
        def open_serena_job(job_id: str) -> Response:
            panel_id = self._serena_activity_overview.panel_id_for_job(job_id)
            if panel_id is None:
                return redirect("/dashboard/")  # type: ignore[return-value]
            return redirect(f"/dashboard/?panel={panel_id}&job={job_id}")  # type: ignore[return-value]

        @app.route("/dashboard/api/state", methods=["GET"])
        def get_dashboard_state() -> Response:
            include_state = request.args.get("include_state") == "1"
            return self._conditional_json_response(app, self.dashboard_state(include_state=include_state))

        @app.route("/dashboard/api/session", methods=["GET"])
        def get_session() -> Response:
            return self._conditional_json_response(app, self._session_overview.get_session())

        @app.route("/dashboard/api/push/config", methods=["GET"])
        def get_push_config() -> dict[str, str]:
            return {"public_key": self._push_notifier.public_key}

        @app.route("/dashboard/api/push/subscribe", methods=["POST"])
        def subscribe_to_push() -> dict[str, str]:
            payload = request.get_json(silent=True)
            if payload is None:
                abort(400)
            try:
                self._push_notifier.save_subscription(payload)
            except (TypeError, ValueError):
                abort(400)
            return {"status": "success"}

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

        @app.route("/dashboard/api/serena/sessions/<panel_id>", methods=["GET"])
        def get_serena_session_document(panel_id: str) -> Response:
            expanded = request.args.get("expanded") or None
            try:
                payload = self._serena_activity_overview.get_session_document(panel_id, expanded_entry_id=expanded)
            except ValueError:
                abort(400)
            except KeyError:
                abort(404)
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
