"""State and routes for the Kendell Serena/Orchestrator dashboard."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, abort, redirect, request, stream_with_context
from mcp.types import ResourceLink
from pydantic import AnyUrl

from orchestrator.config import OrchestratorConfig
from orchestrator.dashboard_sessions import OrchestratorDashboardSessionArchive
from orchestrator.delegates import DelegateError, DelegateStore
from serena.activity import ACTIVITY_RESOURCE_DIR
from serena.activity_transport import activity_overview_payload, activity_running_jobs_payload, activity_snapshot_payload
from serena.activity_view import ActivityView
from serena.push_notifications import WebPushNotifier
from serena.tools.media_tools import read_result_file_link

if TYPE_CHECKING:
    from serena.agent import SerenaAgent

CUSTOM_DASHBOARD_DIR = Path(__file__).parent / "resources" / "kendell_dashboard"


@lru_cache(maxsize=1)
def _dashboard_static_assets() -> tuple[str, str, str]:
    """Returns the immutable dashboard shell and shared activity assets for this process."""
    index_path = CUSTOM_DASHBOARD_DIR / "index.html"
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
    asset_paths = [*(CUSTOM_DASHBOARD_DIR / name for name in assets), *shared_assets]
    revision_input = "\x1f".join(f"{path.name}:{path.stat().st_mtime_ns}:{path.stat().st_size}" for path in asset_paths)
    asset_version = hashlib.blake2s(revision_input.encode("utf-8"), digest_size=6).hexdigest()
    for asset in assets:
        html = html.replace(f'"{asset}"', f'"{asset}?v={asset_version}"')
    html = html.replace('<html lang="en">', f'<html lang="en" data-asset-version="{asset_version}">', 1)
    return (
        html,
        shared_assets[0].read_text(encoding="utf-8"),
        shared_assets[1].read_text(encoding="utf-8"),
    )


@dataclass(frozen=True)
class DashboardMediaContent:
    """Binary media returned by one completed Serena tool execution."""

    data: bytes
    mime_type: str
    file_name: str | None = None


class DashboardSessionOverview:
    """Read-only summary of the Serena state needed by the custom dashboard."""

    def __init__(self, agent: SerenaAgent):
        self._agent = agent

    def get_session(self) -> dict[str, Any]:
        """Returns compact runtime metadata for the custom dashboard."""
        return {
            "status": "success",
            "runtime_policy": "ChatGPT",
            "serena_version": self._agent.version,
            "active_tools": self._agent.get_active_tool_names(),
        }


class DashboardOrchestratorOverview:
    """Provides compact global Orchestrator activity for the operator dashboard."""

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

    def revision_token(self, panel_id: str | None = None) -> str:
        """Returns cheap invalidation state for overview or one selected Orchestrator session."""
        return f"{self._store().dashboard_revision(panel_id)}:{self._sessions().dashboard_revision(panel_id)}"

    def _panels(self) -> list[dict[str, Any]]:
        """Merges compact delegate aggregates with retained conversation metadata."""
        by_id: dict[str, dict[str, Any]] = {
            summary.panel_id: {
                "panel_id": summary.panel_id,
                "started_at": summary.started_at,
                "updated_at": summary.updated_at,
                "active": summary.active,
                "delegate_count": summary.delegate_count,
                "active_count": summary.active_count,
            }
            for summary in self._store().list_dashboard_summaries()
        }
        for session in self._sessions().list_sessions():
            panel_id = str(session["panel_id"])
            panel = by_id.setdefault(
                panel_id,
                {
                    "panel_id": panel_id,
                    "started_at": session.get("started_at"),
                    "updated_at": session.get("updated_at"),
                    "active": False,
                    "delegate_count": 0,
                    "active_count": 0,
                },
            )
            panel["display_name"] = session.get("display_name") or ""
            panel["started_at"] = min(float(panel.get("started_at") or session["started_at"]), float(session["started_at"]))
            panel["updated_at"] = max(float(panel.get("updated_at") or 0.0), float(session.get("updated_at") or 0.0))

        panels = list(by_id.values())
        panels.sort(key=lambda panel: (float(panel.get("started_at") or 0.0), str(panel["panel_id"])), reverse=True)
        return panels

    def get_panels(self) -> dict[str, Any]:
        """Returns every retained orchestration as a compact overview summary."""
        return {"status": "success", "panels": self._panels()}

    def get_panel(self, panel_id: str, expanded_entry_id: str | None = None) -> dict[str, Any]:
        """Returns one complete retained Orchestrator session document by direct lookup."""
        delegate_session = self._store().get_dashboard_session(panel_id)
        session = self._sessions().get_session(panel_id)
        if delegate_session is None and session is None:
            raise KeyError(panel_id)

        delegates = [] if delegate_session is None else [item.model_dump(mode="json") for item in delegate_session.delegates]
        expanded_delegate = None
        if expanded_entry_id is not None:
            delegate_ids = {str(delegate.get("delegate_id") or "") for delegate in delegates}
            if expanded_entry_id not in delegate_ids:
                raise ValueError("Orchestrator entry is not available in this session")
            expanded_delegate = self.get_delegate_detail(expanded_entry_id)

        delegate_summary = None if delegate_session is None else delegate_session.summary
        session_started_at = None if session is None else float(session["started_at"])
        session_updated_at = None if session is None else float(session.get("updated_at") or 0.0)
        delegate_started_at = None if delegate_summary is None else delegate_summary.started_at
        delegate_updated_at = None if delegate_summary is None else delegate_summary.updated_at
        started_at = min(value for value in (session_started_at, delegate_started_at) if value is not None)
        updated_at = max(value for value in (session_updated_at, delegate_updated_at) if value is not None)

        return {
            "panel_id": panel_id,
            "display_name": (session or {}).get("display_name") or "Orchestrator",
            "started_at": started_at,
            "updated_at": updated_at,
            "active": delegate_summary.active if delegate_summary is not None else False,
            "delegates": delegates,
            "expanded_delegate": expanded_delegate,
        }

    def get_delegate_detail(self, delegate_id: str) -> dict[str, Any]:
        """Returns operator-visible detail for one durable delegate."""
        try:
            return self._store().dashboard_detail(delegate_id).model_dump(mode="json")
        except DelegateError as exc:
            raise KeyError(delegate_id) from exc


class DashboardChangeStream:
    """Fans out lightweight invalidations from one process-wide revision watcher."""

    _CHECK_INTERVAL_SECONDS = 0.1
    _HEARTBEAT_INTERVAL_SECONDS = 15.0

    def __init__(self, revision_source: Callable[[], str]) -> None:
        self._revision_source = revision_source
        self._condition = threading.Condition()
        self._generation = 0
        self._subscribers = 0
        self._started = False
        self._stop_requested = False

    def events(self) -> Iterator[str]:
        """Yields SSE invalidations while canonical state remains in the existing HTTP documents."""
        generation = self._subscribe()
        try:
            yield "retry: 1000\n\n"
            while True:
                with self._condition:
                    changed = self._condition.wait_for(
                        lambda generation=generation: self._generation != generation,
                        timeout=self._HEARTBEAT_INTERVAL_SECONDS,
                    )
                    generation = self._generation
                if changed:
                    yield "event: invalidate\ndata: 1\n\n"
                else:
                    yield ": keepalive\n\n"
        finally:
            self._unsubscribe()

    def _subscribe(self) -> int:
        """Registers one stream consumer and starts the shared watcher when required."""
        with self._condition:
            self._subscribers += 1
            self._stop_requested = False
            if self._started:
                return self._generation
            initial_revision = self._revision_source()
            self._started = True
            generation = self._generation
        threading.Thread(
            target=self._watch,
            args=(initial_revision,),
            name="serena-dashboard-events",
            daemon=True,
        ).start()
        return generation

    def _unsubscribe(self) -> None:
        """Stops the shared watcher once the last stream consumer disconnects."""
        with self._condition:
            self._subscribers = max(0, self._subscribers - 1)
            if self._subscribers == 0:
                self._stop_requested = True
                self._condition.notify_all()

    def _watch(self, revision: str) -> None:
        """Publishes one generation change whenever renderer-visible durable state changes."""
        while True:
            time.sleep(self._CHECK_INTERVAL_SECONDS)
            with self._condition:
                if self._stop_requested and self._subscribers == 0:
                    self._started = False
                    self._stop_requested = False
                    return
            current_revision = self._revision_source()
            if current_revision == revision:
                continue
            revision = current_revision
            with self._condition:
                self._generation += 1
                self._condition.notify_all()


class CustomDashboard:
    """Serena-specific dashboard integration kept outside the bundled frontend implementation."""

    def __init__(self, app: Flask, agent: SerenaAgent):
        self._session_overview = DashboardSessionOverview(agent)
        self._execution_store = agent.execution_store
        self._job_manager = agent.job_manager
        self._activity_view = ActivityView(
            execution_store=agent.execution_store,
            job_source=agent.job_manager,
            git_metrics_source=agent,
        )
        self._orchestrator_overview = DashboardOrchestratorOverview()
        self._push_notifier = WebPushNotifier()
        self._revision_nonce = uuid.uuid4().hex
        self._change_stream = DashboardChangeStream(self._stream_revision)
        self._register_routes(app)

    def set_serena_session_name(self, session_id: str, display_name: str) -> str:
        """Sets the retained dashboard name for one ChatGPT conversation."""
        return self._execution_store.set_session_display_name(session_id, display_name)

    def dashboard_state(self) -> dict[str, Any]:
        """Returns the complete compact dashboard overview document."""
        serena, jobs = activity_overview_payload(self._activity_view.dashboard_overview())
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
        """Returns the dashboard shell with cached shared activity assets."""
        html, activity_styles, activity_renderer = _dashboard_static_assets()
        injected = f"<style>{activity_styles}</style>\n<script>{activity_renderer}</script>"
        return html.replace("</head>", f"  {injected}\n</head>", 1)

    @property
    def static_dir(self) -> Path:
        """Returns the directory containing the custom dashboard frontend."""
        return CUSTOM_DASHBOARD_DIR

    @staticmethod
    def _conditional_json_response(
        app: Flask,
        revision: str,
        payload_factory: Callable[[], dict[str, Any]],
    ) -> Response:
        """Returns cache-revalidated JSON without constructing unchanged documents."""
        etag = hashlib.blake2s(revision.encode("utf-8"), digest_size=16).hexdigest()
        if request.if_none_match.contains(etag):
            response = Response(status=304)
        else:
            response = Response(app.json.dumps(payload_factory()), mimetype="application/json")
        response.headers["Cache-Control"] = "private, no-cache"
        response.set_etag(etag)
        return response

    def _overview_revision(self) -> str:
        """Returns a cheap source revision for the complete overview route document."""
        runtime = json.dumps(self._session_overview.get_session(), sort_keys=True, separators=(",", ":"))
        return "|".join(
            (
                self._revision_nonce,
                self._execution_store.dashboard_revision(),
                self._job_manager.dashboard_revision(),
                self._orchestrator_overview.revision_token(),
                hashlib.blake2s(runtime.encode("utf-8"), digest_size=8).hexdigest(),
            )
        )

    def _stream_revision(self) -> str:
        """Returns the cheap durable revision watched by the live invalidation stream."""
        return (
            f"{self._execution_store.dashboard_revision()}|"
            f"{self._job_manager.dashboard_revision()}|"
            f"{self._orchestrator_overview.revision_token()}"
        )

    def _serena_session_revision(self, panel_id: str, expanded_entry_id: str | None) -> str | None:
        """Returns the source revision for one selected Serena route document."""
        execution_revision = self._execution_store.session_revision(panel_id)
        if execution_revision is None:
            return None
        return "|".join(
            (
                self._revision_nonce,
                panel_id,
                execution_revision,
                self._job_manager.dashboard_revision(),
                expanded_entry_id or "",
            )
        )

    def _orchestrator_session_revision(self, panel_id: str, expanded_entry_id: str | None) -> str:
        """Returns the source revision for one selected Orchestrator route document."""
        return "|".join(
            (
                self._revision_nonce,
                panel_id,
                self._orchestrator_overview.revision_token(panel_id),
                expanded_entry_id or "",
            )
        )

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
            return self._conditional_json_response(app, self._overview_revision(), self.dashboard_state)

        @app.route("/dashboard/api/events", methods=["GET"])
        def get_dashboard_events() -> Response:
            response = Response(stream_with_context(self._change_stream.events()), mimetype="text/event-stream")
            response.headers["Cache-Control"] = "private, no-cache"
            response.headers["X-Accel-Buffering"] = "no"
            return response

        @app.route("/dashboard/api/serena/sessions/<panel_id>", methods=["GET"])
        def get_serena_session_document(panel_id: str) -> Response:
            expanded = request.args.get("expanded") or None
            revision = self._serena_session_revision(panel_id, expanded)
            if revision is None:
                abort(404)
            assert revision is not None

            def payload() -> dict[str, Any]:
                try:
                    snapshot = activity_snapshot_payload(self._activity_view.for_session(panel_id, expanded_entry_id=expanded))
                    snapshot["dashboard_jobs"] = activity_running_jobs_payload(self._activity_view.dashboard_jobs())
                    return snapshot
                except ValueError:
                    abort(400)
                except KeyError:
                    abort(404)
                raise RuntimeError("Serena session payload continued after abort")

            return self._conditional_json_response(app, revision, payload)

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
            revision = self._orchestrator_session_revision(panel_id, expanded)

            def payload() -> dict[str, Any]:
                try:
                    return self._orchestrator_overview.get_panel(panel_id, expanded_entry_id=expanded)
                except ValueError:
                    abort(400)
                except KeyError:
                    abort(404)
                raise RuntimeError("Orchestrator session payload continued after abort")

            return self._conditional_json_response(app, revision, payload)

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
