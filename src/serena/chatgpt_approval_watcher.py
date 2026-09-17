"""Lightweight ChatGPT approval monitoring over the account pub/sub stream."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast

import requests
import websocket

from serena.chatgpt_auth import ChatGPTWebAuthSnapshot, ChatGPTWebSession, ChatGPTWebSessionProvider
from serena.push_notifications import ChatGPTApprovalNotification, WebPushNotifier

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatGPTApprovalWatcherStatus:
    """Immutable diagnostic snapshot for the standalone approval watcher."""

    state: str
    connected: bool
    last_connected_at: str | None
    last_event_at: str | None
    last_conversation_check_at: str | None
    last_approval_at: str | None
    last_error: str | None
    events_seen: int
    conversations_checked: int
    approvals_forwarded: int

    def to_dict(self) -> dict[str, object]:
        """:return: JSON-compatible dashboard representation."""
        return cast(dict[str, object], asdict(self))


class ChatGPTApprovalWatcher:
    """Watches ChatGPT's account conversation topic and forwards pending approvals."""

    _BASE_URL = "https://chatgpt.com/backend-api"
    _RECONNECT_DELAY_SECONDS = 10.0
    _AUTH_RETRY_DELAY_SECONDS = 60.0
    _SOCKET_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        *,
        session_provider: ChatGPTWebSessionProvider | None = None,
        notifier: WebPushNotifier | None = None,
    ) -> None:
        self._notifier = notifier or WebPushNotifier()
        self._session_provider = session_provider or ChatGPTWebSessionProvider(notifier=self._notifier)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seen_approvals: set[str] = set()
        self._state = "stopped"
        self._connected = False
        self._last_connected_at: str | None = None
        self._last_event_at: str | None = None
        self._last_conversation_check_at: str | None = None
        self._last_approval_at: str | None = None
        self._last_error: str | None = None
        self._events_seen = 0
        self._conversations_checked = 0
        self._approvals_forwarded = 0

    def start(self) -> None:
        """Start one daemon watcher thread if it is not already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._state = "starting"
            self._thread = threading.Thread(target=self._run, name="chatgpt-approval-watcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Request watcher shutdown without blocking Serena termination."""
        self._stop.set()
        self._wake.set()

    def status(self) -> ChatGPTApprovalWatcherStatus:
        """:return: current watcher diagnostics without exposing authentication material."""
        with self._lock:
            return ChatGPTApprovalWatcherStatus(
                state=self._state,
                connected=self._connected,
                last_connected_at=self._last_connected_at,
                last_event_at=self._last_event_at,
                last_conversation_check_at=self._last_conversation_check_at,
                last_approval_at=self._last_approval_at,
                last_error=self._last_error,
                events_seen=self._events_seen,
                conversations_checked=self._conversations_checked,
                approvals_forwarded=self._approvals_forwarded,
            )

    def _run(self) -> None:
        """Maintain the account websocket with bounded reconnect and auth recovery."""
        while not self._stop.is_set():
            web_session = self._session_provider.session()
            if web_session is None:
                self._set_disconnected("auth_required", "Codex-managed ChatGPT web session is unavailable")
                self._wait(self._AUTH_RETRY_DELAY_SECONDS)
                continue

            try:
                websocket_url = self._websocket_url(web_session)
                self._consume_websocket(websocket_url, web_session)
            except Exception as error:
                log.warning("ChatGPT approval watcher disconnected: %s", error.__class__.__name__)
                self._set_disconnected("error", self._safe_error(error))

            if not self._stop.is_set():
                self._wait(self._RECONNECT_DELAY_SECONDS)

        self._set_disconnected("stopped", None)

    def _websocket_url(self, web_session: ChatGPTWebSession) -> str:
        """:return: signed ChatGPT account websocket URL."""
        response = self._authenticated_get("/celsius/ws/user", web_session)
        payload = response.json()
        websocket_url = payload.get("websocket_url") if isinstance(payload, dict) else None
        if not isinstance(websocket_url, str) or not websocket_url.startswith("wss://"):
            raise RuntimeError("ChatGPT did not return a websocket URL")
        return websocket_url

    def _consume_websocket(self, websocket_url: str, web_session: ChatGPTWebSession) -> None:
        """Subscribe to account conversation updates until the socket closes."""
        socket = websocket.create_connection(websocket_url, timeout=self._SOCKET_TIMEOUT_SECONDS, origin="https://chatgpt.com")
        try:
            socket.send(
                json.dumps(
                    [
                        {"id": 1, "command": {"type": "connect", "presence": {"type": "presence", "state": "background"}}},
                        {"id": 2, "command": {"type": "subscribe", "topic_id": "conversations"}},
                    ],
                    separators=(",", ":"),
                )
            )
            self._set_connected()

            while not self._stop.is_set():
                try:
                    raw = socket.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if raw is None or raw == "":
                    raise RuntimeError("ChatGPT websocket closed")
                self._handle_frame(raw, web_session)
        finally:
            try:
                socket.close()
            finally:
                self._set_disconnected("reconnecting", None)

    def _handle_frame(self, raw: object, web_session: ChatGPTWebSession) -> None:
        """Inspect one pub/sub frame and process completed conversation turns."""
        if not isinstance(raw, str):
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        frames = payload if isinstance(payload, list) else [payload]
        for frame in frames:
            if not isinstance(frame, dict) or frame.get("type") != "message" or frame.get("topic_id") != "conversations":
                continue
            outer = frame.get("payload")
            if not isinstance(outer, dict) or outer.get("type") != "conversation-turn-complete":
                continue
            inner = outer.get("payload")
            conversation_id = inner.get("conversation_id") if isinstance(inner, dict) else None
            if not isinstance(conversation_id, str) or not conversation_id:
                continue
            self._record_event()
            self._inspect_conversation(conversation_id, web_session)

    def _inspect_conversation(self, conversation_id: str, web_session: ChatGPTWebSession) -> None:
        """Fetch one changed conversation and forward its current pending confirmation, if any."""
        response = self._authenticated_get(
            f"/conversations/{conversation_id}",
            web_session,
            params={"include_has_versions": "true", "num_turns": "10"},
        )
        payload = response.json()
        with self._lock:
            self._conversations_checked += 1
            self._last_conversation_check_at = self._now()

        notification = self._current_confirmation(payload, conversation_id)
        if notification is None:
            return
        key = f"{notification.conversation_id}|{notification.message_id}"
        with self._lock:
            if key in self._seen_approvals:
                return
            self._seen_approvals.add(key)
        try:
            self._notifier.send_chatgpt_approval(notification)
        except Exception:
            with self._lock:
                self._seen_approvals.discard(key)
            raise
        with self._lock:
            self._approvals_forwarded += 1
            self._last_approval_at = self._now()

    def _authenticated_get(self, path: str, web_session: ChatGPTWebSession, **kwargs: Any) -> requests.Response:
        """:return: successful ChatGPT response, invalidating the linked session after auth rejection."""
        response = web_session.http.get(
            f"{self._BASE_URL}{path}",
            headers=self._headers(web_session.auth),
            timeout=15,
            **kwargs,
        )
        if response.status_code in {401, 403}:
            self._session_provider.invalidate()
        response.raise_for_status()
        return response

    def _headers(self, auth: ChatGPTWebAuthSnapshot) -> dict[str, str]:
        """:return: minimal authenticated headers for ChatGPT web backend requests."""
        headers = {"Authorization": f"Bearer {auth.access_token}", "Accept": "application/json"}
        if auth.account_id:
            headers["ChatGPT-Account-ID"] = auth.account_id
        return headers

    @classmethod
    def _current_confirmation(cls, payload: object, conversation_id: str) -> ChatGPTApprovalNotification | None:
        """:return: confirmation represented by the current conversation node, when present."""
        if not isinstance(payload, dict):
            return None
        data = cast(dict[str, Any], payload)
        current_node = data.get("current_node")
        message: object = None

        mapping = data.get("mapping")
        if isinstance(current_node, str) and isinstance(mapping, dict):
            node = mapping.get(current_node)
            if isinstance(node, dict):
                message = node.get("message")

        if message is None and isinstance(current_node, str):
            messages = data.get("messages")
            if isinstance(messages, list):
                message = next((item for item in messages if isinstance(item, dict) and item.get("id") == current_node), None)

        if not isinstance(message, dict):
            return None
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            return None
        jit = metadata.get("jit_plugin_data")
        from_server = jit.get("from_server") if isinstance(jit, dict) else None
        if not isinstance(from_server, dict) or from_server.get("type") != "confirm_action":
            return None
        body = from_server.get("body")
        if not isinstance(body, dict):
            body = {}
        summary = body.get("tool_call_safety_summary")
        if not isinstance(summary, dict):
            summary = {}
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            return None

        notification_payload = {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "title": summary.get("title") if isinstance(summary.get("title"), str) else "Approval required",
            "description": summary.get("description") if isinstance(summary.get("description"), str) else None,
            "connector_id": body.get("connector_id") if isinstance(body.get("connector_id"), str) else "chatgpt",
            "connector_name": body.get("connector_name") if isinstance(body.get("connector_name"), str) else None,
            "tool_name": body.get("tool_name") if isinstance(body.get("tool_name"), str) else None,
            "tool_title": body.get("tool_title") if isinstance(body.get("tool_title"), str) else None,
        }
        return ChatGPTApprovalNotification.from_payload(notification_payload)

    def _set_connected(self) -> None:
        """Record a healthy subscribed websocket connection."""
        with self._lock:
            self._state = "connected"
            self._connected = True
            self._last_connected_at = self._now()
            self._last_error = None

    def _set_disconnected(self, state: str, error: str | None) -> None:
        """Record a disconnected watcher state."""
        with self._lock:
            self._state = state
            self._connected = False
            self._last_error = error

    def _record_event(self) -> None:
        """Record one relevant conversation completion signal."""
        with self._lock:
            self._events_seen += 1
            self._last_event_at = self._now()

    def _wait(self, timeout_seconds: float) -> None:
        """Wait for retry delay, waking immediately when Codex hands off fresh auth."""
        self._wake.wait(timeout_seconds)
        self._wake.clear()

    @staticmethod
    def _now() -> str:
        """:return: compact UTC timestamp for diagnostics."""
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _safe_error(error: Exception) -> str:
        """:return: bounded diagnostic without signed URLs or authentication material."""
        if isinstance(error, requests.HTTPError) and error.response is not None:
            response = error.response
            detail = [f"HTTP {response.status_code}"]
            cf_mitigated = response.headers.get("cf-mitigated")
            if cf_mitigated:
                detail.append(f"cf-mitigated={cf_mitigated}")
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if content_type:
                detail.append(content_type)
            return " · ".join(detail)
        return error.__class__.__name__
