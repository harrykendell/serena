"""ChatGPT web-session bootstrap using Codex-managed authentication."""

from __future__ import annotations

import base64
import json
import select
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests as std_requests
from curl_cffi import requests as curl_requests

from serena.push_notifications import WebPushNotifier


class CodexAuthError(RuntimeError):
    """Raised when Codex-managed ChatGPT authentication cannot be used."""


@dataclass(frozen=True)
class CodexAuthSnapshot:
    """Non-refreshing view of the ChatGPT credentials persisted by Codex."""

    access_token: str
    account_id: str | None
    expires_at_epoch: int | None
    last_refresh: str | None

    def seconds_until_expiry(self, now_epoch: float | None = None) -> float | None:
        """:return: seconds until token expiry, when the JWT exposes ``exp``."""
        if self.expires_at_epoch is None:
            return None
        return self.expires_at_epoch - (time.time() if now_epoch is None else now_epoch)


@dataclass(frozen=True)
class ChatGPTWebAuthSnapshot:
    """Authenticated ChatGPT web identity returned by the linked web session."""

    access_token: str
    account_id: str | None
    expires_at_epoch: int | None

    def seconds_until_expiry(self, now_epoch: float | None = None) -> float | None:
        """:return: seconds until token expiry, when the JWT exposes ``exp``."""
        if self.expires_at_epoch is None:
            return None
        return self.expires_at_epoch - (time.time() if now_epoch is None else now_epoch)


@dataclass(frozen=True)
class ChatGPTWebSession:
    """Linked ChatGPT browser session and its bearer identity."""

    http: Any
    auth: ChatGPTWebAuthSnapshot


class CodexAuthStore:
    """Reads Codex-managed ChatGPT auth without exposing refresh credentials."""

    def __init__(self, auth_path: Path | None = None) -> None:
        self.auth_path = auth_path or Path.home() / ".codex" / "auth.json"

    def read(self) -> CodexAuthSnapshot:
        """:return: current Codex-managed ChatGPT access-token metadata."""
        try:
            payload = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CodexAuthError("Codex ChatGPT authentication is unavailable") from error
        if not isinstance(payload, dict) or payload.get("auth_mode") != "chatgpt":
            raise CodexAuthError("Codex is not using managed ChatGPT authentication")
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict):
            raise CodexAuthError("Codex ChatGPT authentication is incomplete")
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id")
        if not isinstance(access_token, str) or not access_token:
            raise CodexAuthError("Codex ChatGPT access token is unavailable")
        if not isinstance(account_id, str) or not account_id:
            account_id = None
        claims = _jwt_claims(access_token)
        expiry = claims.get("exp")
        return CodexAuthSnapshot(
            access_token=access_token,
            account_id=account_id,
            expires_at_epoch=expiry if isinstance(expiry, int) else None,
            last_refresh=payload.get("last_refresh") if isinstance(payload.get("last_refresh"), str) else None,
        )


class CodexAuthRefresher:
    """Asks the native Codex app-server to run its managed refresh-token flow."""

    _REQUEST_TIMEOUT_SECONDS = 20.0

    def __init__(self, store: CodexAuthStore | None = None, codex_binary: Path | None = None) -> None:
        self._store = store or CodexAuthStore()
        self._codex_binary = codex_binary or Path("/opt/codex-desktop/resources/codex")

    def refresh(self) -> CodexAuthSnapshot:
        """:return: refreshed credentials after Codex completes ``account/read`` refresh."""
        if not self._codex_binary.exists():
            raise CodexAuthError("Codex binary is unavailable")
        process = subprocess.Popen(
            [str(self._codex_binary), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        try:
            self._send(process, {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "serena", "version": "1"}}})
            self._receive(process, 1)
            self._send(process, {"method": "initialized"})
            self._send(process, {"id": 2, "method": "account/read", "params": {"refreshToken": True}})
            response = self._receive(process, 2)
            if "error" in response:
                raise CodexAuthError("Codex rejected the managed authentication refresh")
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        return self._store.read()

    @staticmethod
    def _send(process: subprocess.Popen[str], payload: dict[str, object]) -> None:
        """Send one JSON-RPC message to the short-lived Codex app-server."""
        if process.stdin is None:
            raise CodexAuthError("Codex app-server input is unavailable")
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _receive(self, process: subprocess.Popen[str], request_id: int) -> dict[str, Any]:
        """:return: matching JSON-RPC response within the bounded refresh timeout."""
        if process.stdout is None:
            raise CodexAuthError("Codex app-server output is unavailable")
        deadline = time.monotonic() + self._REQUEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(response, dict) and response.get("id") == request_id:
                return response
        raise CodexAuthError("Codex authentication refresh timed out")


class ChatGPTWebSessionProvider:
    """Links a browser-style ChatGPT session from the Codex-managed OAuth token."""

    _BASE_URL = "https://chatgpt.com"
    _MINIMUM_VALIDITY_SECONDS = 300.0
    _USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/153.0.0.0 Safari/537.36"

    def __init__(
        self,
        *,
        store: CodexAuthStore | None = None,
        refresher: CodexAuthRefresher | None = None,
        notifier: WebPushNotifier | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._store = store or CodexAuthStore()
        self._refresher = refresher or CodexAuthRefresher(self._store)
        self._notifier = notifier or WebPushNotifier()
        self._session_factory = session_factory or (lambda: curl_requests.Session(impersonate="chrome"))
        self._cached: ChatGPTWebSession | None = None
        self._auth_attention_sent = False

    def session(self) -> ChatGPTWebSession | None:
        """:return: linked web session, refreshing Codex-managed auth when necessary."""
        cached = self._cached
        if cached is not None:
            remaining = cached.auth.seconds_until_expiry()
            if remaining is None or remaining > self._MINIMUM_VALIDITY_SECONDS:
                return cached
            self._cached = None

        try:
            codex = self._store.read()
            remaining = codex.seconds_until_expiry()
            if remaining is not None and remaining <= self._MINIMUM_VALIDITY_SECONDS:
                codex = self._refresher.refresh()
            linked = self._link(codex)
        except (CodexAuthError, curl_requests.RequestsError, std_requests.RequestException, ValueError, KeyError, TypeError):
            self._notify_auth_attention_once()
            return None

        self._cached = linked
        self._auth_attention_sent = False
        return linked

    def invalidate(self) -> None:
        """Discard the linked web session after ChatGPT rejects it."""
        self._cached = None

    def _link(self, codex: CodexAuthSnapshot) -> ChatGPTWebSession:
        """:return: browser-style session created by ChatGPT's desktop link-session flow."""
        remaining = codex.seconds_until_expiry()
        if remaining is None or remaining <= 0:
            raise CodexAuthError("Codex ChatGPT token is expired")
        http = self._session_factory()
        http.headers.update({"User-Agent": self._USER_AGENT, "Accept": "application/json"})

        initial = http.get(f"{self._BASE_URL}/api/auth/session", timeout=15)
        initial.raise_for_status()
        linked = http.post(
            f"{self._BASE_URL}/api/auth/link-session",
            json={"auth_token": codex.access_token, "expires_in": max(1, int(remaining))},
            headers={"x-i-am-a-browser": "true"},
            timeout=15,
        )
        linked.raise_for_status()
        session_response = http.get(f"{self._BASE_URL}/api/auth/session?refresh_account=true", timeout=15)
        session_response.raise_for_status()
        payload = session_response.json()
        if not isinstance(payload, dict):
            raise CodexAuthError("ChatGPT linked session response is invalid")
        access_token = payload.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise CodexAuthError("ChatGPT linked session did not return an access token")
        claims = _jwt_claims(access_token)
        expiry = claims.get("exp")
        auth_claims = claims.get("https://api.openai.com/auth")
        account_id = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
        return ChatGPTWebSession(
            http=http,
            auth=ChatGPTWebAuthSnapshot(
                access_token=access_token,
                account_id=account_id if isinstance(account_id, str) and account_id else codex.account_id,
                expires_at_epoch=expiry if isinstance(expiry, int) else None,
            ),
        )

    def _notify_auth_attention_once(self) -> None:
        """Send one push until a linked ChatGPT session succeeds again."""
        if self._auth_attention_sent:
            return
        self._auth_attention_sent = True
        try:
            self._notifier.send_chatgpt_auth_required()
        except Exception:
            pass


def _jwt_claims(token: str) -> dict[str, Any]:
    """:return: unverified JWT claims used only for local expiry and account metadata."""
    parts = token.split(".")
    if len(parts) != 3:
        raise CodexAuthError("Authentication token is not a JWT")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise CodexAuthError("Authentication token claims are invalid") from error
    if not isinstance(claims, dict):
        raise CodexAuthError("Authentication token claims are invalid")
    return claims
