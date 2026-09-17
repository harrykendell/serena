import json
import time
from typing import cast
from unittest.mock import MagicMock

import requests
import websocket

from serena.chatgpt_approval_watcher import ChatGPTApprovalWatcher
from serena.chatgpt_auth import ChatGPTWebAuthSnapshot, ChatGPTWebSession, ChatGPTWebSessionProvider
from serena.push_notifications import WebPushNotifier


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Socket:
    def __init__(self, conversation_id: str) -> None:
        self.sent: list[str] = []
        self._frames: list[str] = [
            json.dumps(
                [
                    {
                        "type": "message",
                        "topic_id": "conversations",
                        "payload": {
                            "type": "conversation-turn-complete",
                            "payload": {"conversation_id": conversation_id},
                            "metadata": None,
                        },
                    }
                ]
            )
        ]

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        raise websocket.WebSocketTimeoutException()

    def close(self) -> None:
        pass


def _watcher_for(conversation_id: str, conversation: dict[str, object], monkeypatch):
    requests_seen: list[str] = []
    http = MagicMock(spec=requests.Session)

    def get(url: str, **_kwargs: object) -> _Response:
        requests_seen.append(url)
        if url.endswith("/celsius/ws/user"):
            return _Response({"websocket_url": "wss://ws.example.invalid/signed"})
        return _Response(conversation)

    http.get.side_effect = get
    auth = ChatGPTWebAuthSnapshot("access-token", "account-a", None)
    web_session = ChatGPTWebSession(http=cast(requests.Session, http), auth=auth)
    provider = MagicMock(spec=ChatGPTWebSessionProvider)
    provider.session.return_value = web_session
    notifier = MagicMock(spec=WebPushNotifier)
    notifier.send_chatgpt_approval.return_value = True
    socket = _Socket(conversation_id)
    monkeypatch.setattr("serena.chatgpt_approval_watcher.websocket.create_connection", lambda *_args, **_kwargs: socket)
    watcher = ChatGPTApprovalWatcher(
        session_provider=cast(ChatGPTWebSessionProvider, provider),
        notifier=cast(WebPushNotifier, notifier),
    )
    return watcher, notifier, provider, socket, requests_seen


def test_watcher_forwards_pending_confirmation_from_completed_conversation(monkeypatch) -> None:
    conversation_id = "conversation-a"
    message_id = "approval-message"
    conversation = {
        "current_node": message_id,
        "messages": [
            {
                "id": message_id,
                "metadata": {
                    "jit_plugin_data": {
                        "from_server": {
                            "type": "confirm_action",
                            "body": {
                                "connector_id": "serena",
                                "connector_name": "Serena",
                                "tool_call_safety_summary": {
                                    "title": "Allow file materialization?",
                                    "description": "ChatGPT needs your approval.",
                                },
                            },
                        }
                    }
                },
            }
        ],
    }
    watcher, notifier, _provider, socket, requests_seen = _watcher_for(conversation_id, conversation, monkeypatch)

    watcher.start()
    deadline = time.monotonic() + 2
    while notifier.send_chatgpt_approval.call_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    watcher.stop()

    assert notifier.send_chatgpt_approval.call_count == 1
    notification = notifier.send_chatgpt_approval.call_args.args[0]
    assert notification.conversation_id == conversation_id
    assert notification.message_id == message_id
    assert notification.title == "Allow file materialization?"
    assert requests_seen == [
        "https://chatgpt.com/backend-api/celsius/ws/user",
        f"https://chatgpt.com/backend-api/conversations/{conversation_id}",
    ]
    subscription = json.loads(socket.sent[0])
    assert subscription[1]["command"] == {"type": "subscribe", "topic_id": "conversations"}
    status = watcher.status()
    assert status.events_seen == 1
    assert status.conversations_checked == 1
    assert status.approvals_forwarded == 1


def test_watcher_does_not_forward_non_confirmation_current_node(monkeypatch) -> None:
    conversation_id = "conversation-b"
    conversation = {
        "current_node": "answer",
        "mapping": {"answer": {"message": {"id": "answer", "metadata": {}}}},
    }
    watcher, notifier, _provider, _socket, _requests_seen = _watcher_for(conversation_id, conversation, monkeypatch)

    watcher.start()
    deadline = time.monotonic() + 2
    while watcher.status().conversations_checked == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    watcher.stop()

    assert notifier.send_chatgpt_approval.call_count == 0
    assert watcher.status().conversations_checked == 1


def test_watcher_invalidates_linked_session_after_auth_rejection(monkeypatch) -> None:
    http = MagicMock(spec=requests.Session)
    http.get.return_value = _Response({}, status_code=403)
    auth = ChatGPTWebAuthSnapshot("access-token", "account-a", None)
    web_session = ChatGPTWebSession(http=cast(requests.Session, http), auth=auth)
    provider = MagicMock(spec=ChatGPTWebSessionProvider)
    provider.session.return_value = web_session
    notifier = MagicMock(spec=WebPushNotifier)
    watcher = ChatGPTApprovalWatcher(
        session_provider=cast(ChatGPTWebSessionProvider, provider),
        notifier=cast(WebPushNotifier, notifier),
    )

    watcher.start()
    deadline = time.monotonic() + 2
    while provider.invalidate.call_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    watcher.stop()

    assert provider.invalidate.call_count >= 1
