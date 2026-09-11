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
from serena.activity import ACTIVITY_RESOURCE_DIR
from serena.activity_view import ActivityCallDetail, ActivityJobDetail, ActivityOverview, ActivitySnapshot, ActivityView
from serena.push_notifications import WebPushNotifier
from serena.tools.media_tools import read_result_file_link

if TYPE_CHECKING:
    from serena.agent import SerenaAgent

CUSTOM_DASHBOARD_DIR = Path(__file__).parent / "resources" / "kendell_dashboard"


@dataclass(frozen=True)
class DashboardMediaContent:
    """Binary media returned by one completed Serena tool execution."""

    data: bytes
    mime_type: str
    file_name: str | None = None


def _activity_entry_payload(call: Any) -> dict[str, Any]:
    """Converts one renderer-facing execution summary at the HTTP boundary."""
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


def _activity_job_payload(job: Any) -> dict[str, Any]:
    """Converts one renderer-facing durable-job summary at the HTTP boundary."""
    return {
        "job_id": job.job_id,
        "label": job.label,
        "project": job.project,
        "status": job.status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "current_turn": job.current_turn,
    }


def _activity_call_detail_payload(detail: ActivityCallDetail) -> dict[str, Any]:
    """Converts one bounded call detail at the HTTP boundary."""
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


def _activity_job_detail_payload(detail: ActivityJobDetail) -> dict[str, Any]:
    """Converts one bounded durable-job detail at the HTTP boundary."""
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


def _activity_snapshot_payload(snapshot: ActivitySnapshot) -> dict[str, Any]:
    """Converts one complete typed activity snapshot to the shared renderer contract."""
    calls = [_activity_entry_payload(call) for call in snapshot.calls]
    jobs = [_activity_job_payload(job) for job in snapshot.jobs]
    starts = [call.started_at for call in snapshot.calls]
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
        "job_count": len(jobs),
        "submission_span_seconds": max(starts) - min(starts) if starts else None,
        "git_additions": snapshot.git_metrics.additions,
        "git_deletions": snapshot.git_metrics.deletions,
        "git_ahead_commits": snapshot.git_metrics.ahead_commits,
        "calls": calls,
        "jobs": jobs,
        "expanded_call": _activity_call_detail_payload(snapshot.expanded_call) if snapshot.expanded_call is not None else None,
        "expanded_job": _activity_job_detail_payload(snapshot.expanded_job) if snapshot.expanded_job is not None else None,
    }


def _serena_overview_payload(overview: ActivityOverview) -> tuple[dict[str, Any], dict[str, Any]]:
    """Converts one canonical activity overview to compact dashboard discovery documents."""
    running_by_session: dict[str, list[dict[str, Any]]] = {}
    panel_by_session = {summary.session_id: summary.panel_id for summary in overview.sessions}
    for job in overview.running_jobs:
        if job.session_id is not None:
            running_by_session.setdefault(job.session_id, []).append(_activity_job_payload(job))

    panels: list[dict[str, Any]] = []
    for summary in overview.sessions:
        latest_candidates = list(running_by_session.get(summary.session_id, []))
        if summary.latest_call is not None:
            latest_candidates.append(_activity_entry_payload(summary.latest_call))
        latest = max(latest_candidates, key=lambda item: float(item.get("started_at") or 0.0), default=None)
        latest_activity = None
        if latest is not None:
            latest_activity = {
                "label": latest.get("tool_name") or latest.get("label") or "Activity",
                "detail": latest.get("detail") or ("durable job" if latest.get("job_id") else ""),
                "scope": latest.get("scope") or latest.get("project") or "",
                "status": latest.get("status"),
                "started_at": latest.get("started_at"),
                "finished_at": latest.get("finished_at"),
            }
        panels.append(
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
                "latest_activity": latest_activity,
            }
        )

    jobs = [
        {
            **_activity_job_payload(job),
            "panel_id": panel_by_session.get(job.session_id) if job.session_id is not None else None,
        }
        for job in overview.running_jobs
    ]
    return (
        {"status": "success", "panels": panels},
        {
            "status": "success",
            "jobs": jobs,
            "running_jobs": len(jobs),
            "max_concurrent_jobs": overview.max_concurrent_jobs,
        },
    )


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

    def get_panel(self, panel_id: str, expanded_entry_id: str | None = None) -> dict[str, Any]:
        """Returns one complete retained Orchestrator session document."""
        for panel in self._panels():
            if panel["panel_id"] != panel_id:
                continue
            expanded_delegate = None
            if expanded_entry_id is not None:
                delegate_ids = {str(delegate.get("delegate_id") or "") for delegate in panel["delegates"]}
                if expanded_entry_id not in delegate_ids:
                    raise ValueError("Orchestrator entry is not available in this session")
                expanded_delegate = self.get_delegate_detail(expanded_entry_id)
            return {
                "panel_id": panel["panel_id"],
                "display_name": panel.get("display_name") or "Orchestrator",
                "started_at": panel["started_at"],
                "updated_at": panel.get("updated_at"),
                "active": panel.get("active", False),
                "delegates": panel["delegates"],
                "expanded_delegate": expanded_delegate,
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
        self._execution_store = agent.execution_store
        self._activity_view = ActivityView(
            execution_store=agent.execution_store,
            job_source=agent.job_manager,
            git_metrics_source=agent,
        )
        self._orchestrator_overview = DashboardOrchestratorOverview()
        self._push_notifier = WebPushNotifier()
        self._register_routes(app)

    def set_serena_session_name(self, session_id: str, display_name: str) -> str:
        """Sets the retained dashboard name for one ChatGPT conversation."""
        return self._execution_store.set_session_display_name(session_id, display_name)

    def dashboard_state(self) -> dict[str, Any]:
        """Returns the complete compact dashboard overview document."""
        serena, jobs = _serena_overview_payload(self._activity_view.dashboard_overview())
        return {
            "status": "success",
            "session": self._session_overview.get_session(),
            "jobs": jobs,
            "serena": serena,
            "orchestrator": self._orchestrator_overview.get_panels(),
        }

    def _activity_media(self, panel_id: str, call_id: str) -> DashboardMediaContent:
        """Returns retained media bytes for one selected-session call."""
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
            file_name=media.name,
        )

    def render_index_html(self) -> str:
        """Returns the dashboard shell with compact bootstrap state and shared activity assets."""
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
        shared_assets = (
            ACTIVITY_RESOURCE_DIR / "activity-panel.css",
            ACTIVITY_RESOURCE_DIR / "activity-panel.js",
        )
        asset_paths = [*(self.static_dir / name for name in assets), *shared_assets]
        revision_input = "\x1f".join(f"{path.name}:{path.stat().st_mtime_ns}:{path.stat().st_size}" for path in asset_paths)
        asset_version = hashlib.blake2s(revision_input.encode("utf-8"), digest_size=6).hexdigest()
        for asset in assets:
            html = html.replace(f'"{asset}"', f'"{asset}?v={asset_version}"')
        html = html.replace('<html lang="en">', f'<html lang="en" data-asset-version="{asset_version}">', 1)

        activity_styles = shared_assets[0].read_text(encoding="utf-8")
        activity_renderer = shared_assets[1].read_text(encoding="utf-8")
        bootstrap = json.dumps(self.dashboard_state(), ensure_ascii=False, separators=(",", ":"))
        bootstrap = bootstrap.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        injected = (
            f"<style>{activity_styles}</style>\n"
            f"<script>{activity_renderer}</script>\n"
            f'<script id="dashboard-bootstrap" type="application/json">{bootstrap}</script>'
        )
        return html.replace("</head>", f"  {injected}\n</head>", 1)

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
        """Registers the compact route-oriented dashboard API."""

        @app.route("/dashboard/job/<job_id>", methods=["GET"])
        def open_serena_job(job_id: str) -> Response:
            panel_id = self._activity_view.panel_id_for_job(job_id)
            if panel_id is None:
                return redirect("/dashboard/")  # type: ignore[return-value]
            return redirect(f"/dashboard/?panel={panel_id}&job={job_id}")  # type: ignore[return-value]

        @app.route("/dashboard/api/state", methods=["GET"])
        def get_dashboard_state() -> Response:
            return self._conditional_json_response(app, self.dashboard_state())

        @app.route("/dashboard/api/serena/sessions/<panel_id>", methods=["GET"])
        def get_serena_session_document(panel_id: str) -> Response:
            expanded = request.args.get("expanded") or None
            payload: dict[str, Any] = {}
            try:
                payload = _activity_snapshot_payload(self._activity_view.for_session(panel_id, expanded_entry_id=expanded))
            except ValueError:
                abort(400)
            except KeyError:
                abort(404)
            return self._conditional_json_response(app, payload)

        @app.route("/dashboard/api/serena/sessions/<panel_id>/media/<call_id>", methods=["GET"])
        def get_serena_call_media(panel_id: str, call_id: str) -> Response:
            media: DashboardMediaContent | None = None
            try:
                media = self._activity_media(panel_id, call_id)
            except (KeyError, ValueError, FileNotFoundError):
                abort(404)
            if media is None:
                raise RuntimeError("Media route continued after abort")
            response = Response(media.data, mimetype=media.mime_type)
            response.headers["Cache-Control"] = "private, max-age=3600"
            if media.file_name:
                response.headers["Content-Disposition"] = f'inline; filename="{media.file_name}"'
            return response

        @app.route("/dashboard/api/orchestrator/sessions/<panel_id>", methods=["GET"])
        def get_orchestrator_session_document(panel_id: str) -> Response:
            expanded = request.args.get("expanded") or None
            payload: dict[str, Any] = {}
            try:
                payload = self._orchestrator_overview.get_panel(panel_id, expanded_entry_id=expanded)
            except ValueError:
                abort(400)
            except KeyError:
                abort(404)
            return self._conditional_json_response(app, payload)

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
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
