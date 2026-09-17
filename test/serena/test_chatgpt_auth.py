import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from serena.chatgpt_auth import (
    ChatGPTWebSessionProvider,
    CodexAuthError,
    CodexAuthRefresher,
    CodexAuthSnapshot,
    CodexAuthStore,
)


def _jwt_with_exp(expiry: int, *, account_id: str = "account-a") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode("ascii")
    claims = {
        "exp": expiry,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{header}.{payload}.signature"


def test_codex_auth_store_reads_managed_chatgpt_state_without_exposing_refresh_token(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    token = _jwt_with_exp(2_000_000_000)
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": token,
                    "refresh_token": "refresh-secret",
                    "account_id": "account-a",
                },
                "last_refresh": "2026-09-17T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    snapshot = CodexAuthStore(auth_path).read()

    assert snapshot.access_token == token
    assert snapshot.account_id == "account-a"
    assert snapshot.expires_at_epoch == 2_000_000_000
    assert snapshot.last_refresh == "2026-09-17T12:00:00Z"
    assert "refresh-secret" not in repr(snapshot)


def test_codex_auth_store_rejects_non_chatgpt_auth(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"auth_mode": "api_key", "tokens": {}}), encoding="utf-8")

    with pytest.raises(CodexAuthError):
        CodexAuthStore(auth_path).read()


def test_web_session_provider_links_codex_token_into_chatgpt_session() -> None:
    now = int(time.time())
    codex = CodexAuthSnapshot(
        access_token=_jwt_with_exp(now + 5_000),
        account_id="account-a",
        expires_at_epoch=now + 5_000,
        last_refresh="now",
    )
    web_token = _jwt_with_exp(now + 4_000, account_id="account-a")
    store = SimpleNamespace(read=lambda: codex)
    refresher = SimpleNamespace(refresh=lambda: codex)
    notifier = SimpleNamespace(send_chatgpt_auth_required=lambda: True)
    http = MagicMock(spec=requests.Session)
    http.headers = {}
    initial = MagicMock()
    initial.raise_for_status.return_value = None
    linked = MagicMock()
    linked.raise_for_status.return_value = None
    session_response = MagicMock()
    session_response.raise_for_status.return_value = None
    session_response.json.return_value = {"accessToken": web_token}
    http.get.side_effect = [initial, session_response]
    http.post.return_value = linked
    provider = ChatGPTWebSessionProvider(
        store=store,
        refresher=refresher,
        notifier=notifier,
        session_factory=lambda: http,
    )

    session = provider.session()

    assert session is not None
    assert session.http is http
    assert session.auth.access_token == web_token
    assert session.auth.account_id == "account-a"
    http.get.assert_any_call("https://chatgpt.com/api/auth/session", timeout=15)
    http.post.assert_called_once_with(
        "https://chatgpt.com/api/auth/link-session",
        json={"auth_token": codex.access_token, "expires_in": pytest.approx(5_000, abs=2)},
        headers={"x-i-am-a-browser": "true"},
        timeout=15,
    )
    http.get.assert_any_call("https://chatgpt.com/api/auth/session?refresh_account=true", timeout=15)


def test_web_session_provider_refreshes_codex_token_near_expiry() -> None:
    now = int(time.time())
    stale = CodexAuthSnapshot("stale", "account-a", now + 10, "before")
    fresh = CodexAuthSnapshot(_jwt_with_exp(now + 5_000), "account-a", now + 5_000, "after")
    store = SimpleNamespace(read=lambda: stale)
    refresher = MagicMock(spec=CodexAuthRefresher)
    refresher.refresh.return_value = fresh
    notifier = SimpleNamespace(send_chatgpt_auth_required=lambda: True)
    http = MagicMock(spec=requests.Session)
    http.headers = {}
    response = MagicMock()
    response.raise_for_status.return_value = None
    final = MagicMock()
    final.raise_for_status.return_value = None
    final.json.return_value = {"accessToken": _jwt_with_exp(now + 4_000)}
    http.get.side_effect = [response, final]
    http.post.return_value = response
    provider = ChatGPTWebSessionProvider(
        store=store,
        refresher=refresher,
        notifier=notifier,
        session_factory=lambda: http,
    )

    assert provider.session() is not None
    refresher.refresh.assert_called_once_with()


def test_web_session_provider_notifies_once_when_linking_fails() -> None:
    now = int(time.time())
    codex = CodexAuthSnapshot(_jwt_with_exp(now + 5_000), "account-a", now + 5_000, "now")
    store = SimpleNamespace(read=lambda: codex)
    refresher = SimpleNamespace(refresh=lambda: codex)
    notifications: list[bool] = []
    notifier = SimpleNamespace(send_chatgpt_auth_required=lambda: notifications.append(True) or True)
    http = MagicMock(spec=requests.Session)
    http.headers = {}
    http.get.side_effect = requests.ConnectionError("offline")
    provider = ChatGPTWebSessionProvider(
        store=store,
        refresher=refresher,
        notifier=notifier,
        session_factory=lambda: http,
    )

    assert provider.session() is None
    assert provider.session() is None
    assert notifications == [True]
