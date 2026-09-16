"""Web Push notification support for the Serena dashboard."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from filelock import FileLock
from pywebpush import WebPushException, webpush

from serena.config.serena_config import SerenaConfig, SerenaPaths
from serena.jobs import JobRecord, JobStatus


@dataclass(frozen=True)
class WebPushSubscription:
    """Validated browser Web Push subscription details."""

    endpoint: str
    p256dh: str
    auth: str

    @classmethod
    def from_payload(cls, payload: object) -> WebPushSubscription:
        """Construct a validated subscription from a browser ``PushSubscription`` payload."""
        if not isinstance(payload, dict):
            raise ValueError("Push subscription must be a JSON object")
        data = cast(dict[str, object], payload)
        endpoint = data.get("endpoint")
        keys = data.get("keys")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("Push subscription endpoint must be an HTTPS URL")
        if not isinstance(keys, dict):
            raise ValueError("Push subscription keys are required")
        key_data = cast(dict[str, object], keys)
        p256dh = key_data.get("p256dh")
        auth = key_data.get("auth")
        if not isinstance(p256dh, str) or not p256dh or not isinstance(auth, str) or not auth:
            raise ValueError("Push subscription p256dh and auth keys are required")
        return cls(endpoint=endpoint, p256dh=p256dh, auth=auth)

    def to_dict(self) -> dict[str, object]:
        """:return: JSON/Web Push representation of this subscription."""
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


@dataclass(frozen=True)
class ChatGPTApprovalNotification:
    """Validated metadata for one cloud ChatGPT approval request."""

    conversation_id: str
    title: str
    message_id: str
    connector_id: str
    connector_name: str | None = None
    tool_name: str | None = None
    tool_title: str | None = None

    @classmethod
    def from_payload(cls, payload: object) -> ChatGPTApprovalNotification:
        """Construct a validated approval notification from a loopback watcher payload."""
        if not isinstance(payload, dict):
            raise ValueError("ChatGPT approval notification must be a JSON object")
        data = cast(dict[str, object], payload)

        def required_string(name: str) -> str:
            value = data.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ChatGPT approval notification {name} is required")
            return value.strip()

        def optional_string(name: str) -> str | None:
            value = data.get(name)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError(f"ChatGPT approval notification {name} must be a string")
            return value.strip() or None

        return cls(
            conversation_id=required_string("conversation_id"),
            title=optional_string("title") or "ChatGPT",
            message_id=required_string("message_id"),
            connector_id=required_string("connector_id"),
            connector_name=optional_string("connector_name"),
            tool_name=optional_string("tool_name"),
            tool_title=optional_string("tool_title"),
        )


class WebPushNotifier:
    """Owns Serena's durable Web Push subscription set and delivery."""

    _VAPID_SUBJECT = "https://mcp.kendell.uk"
    _SEND_TIMEOUT_SECONDS = 5
    _TTL_SECONDS = 86_400
    _STALE_STATUS_CODES = frozenset({404, 410})

    def __init__(
        self,
        root: Path | None = None,
        sender: Callable[..., Any] = webpush,
        minimum_job_duration_seconds: float | None = None,
    ) -> None:
        if root is None:
            root = Path(SerenaPaths().serena_user_home_dir) / "push"
        if minimum_job_duration_seconds is None:
            minimum_job_duration_seconds = SerenaConfig.load_tool_timeout_from_config_file()

        self._root = root
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        self._private_key_path = self._root / "vapid_private.pem"
        self._subscriptions_path = self._root / "subscriptions.json"
        self._legacy_subscription_path = self._root / "subscription.json"
        self._key_lock = FileLock(str(self._root / ".vapid.lock"))
        self._subscription_lock = FileLock(str(self._root / ".subscriptions.lock"))
        self._sender = sender
        self._minimum_job_duration_seconds = minimum_job_duration_seconds

    @property
    def public_key(self) -> str:
        """:return: URL-safe uncompressed P-256 VAPID public key for browser subscription."""
        private_key = self._load_or_create_private_key()
        encoded = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")

    def save_subscription(self, payload: object) -> WebPushSubscription:
        """Validate and upsert one browser subscription by its push endpoint."""
        subscription = WebPushSubscription.from_payload(payload)
        with self._subscription_lock:
            subscriptions = {item.endpoint: item for item in self._read_subscriptions_unlocked()}
            subscriptions[subscription.endpoint] = subscription
            self._write_subscriptions_unlocked(list(subscriptions.values()))
        return subscription

    def send_chatgpt_approval(self, notification: ChatGPTApprovalNotification) -> bool:
        """Send one cloud ChatGPT approval request to every registered browser."""
        detail = ["Approval required"]
        if notification.connector_name:
            detail.append(notification.connector_name)
        elif notification.connector_id:
            detail.append(notification.connector_id)
        if notification.tool_title:
            detail.append(notification.tool_title)
        elif notification.tool_name:
            detail.append(notification.tool_name)

        payload = json.dumps(
            {
                "title": notification.title,
                "body": " · ".join(detail),
                "tag": f"chatgpt-approval-{notification.conversation_id}-{notification.message_id}",
                "url": "/dashboard/",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._send_payload(payload)

    def send_job_finished(self, record: JobRecord) -> bool:
        """Send a completion notification to every registered browser for a qualifying job."""
        duration_seconds = self._duration_seconds(record)
        if (
            record.status not in {JobStatus.COMPLETED, JobStatus.FAILED}
            or duration_seconds is None
            or duration_seconds <= self._minimum_job_duration_seconds
        ):
            return False

        status = "Completed" if record.status is JobStatus.COMPLETED else "Failed"
        title = record.label or record.job_id
        detail = [status]
        if record.project_name:
            detail.append(record.project_name)
        detail.append(self._format_duration(duration_seconds))
        payload = json.dumps(
            {
                "title": title,
                "body": " · ".join(detail),
                "tag": f"serena-job-{record.job_id}",
                "url": f"/dashboard/job/{record.job_id}" if record.session_id else "/dashboard/",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._send_payload(payload)

    def _send_payload(self, payload: str) -> bool:
        """Send one prepared Web Push payload to every registered browser."""
        subscriptions = self._load_subscriptions()
        if not subscriptions:
            return False

        self._load_or_create_private_key()
        stale_endpoints: set[str] = set()
        first_error: Exception | None = None
        delivered = False
        for subscription in subscriptions:
            try:
                self._sender(
                    subscription_info=subscription.to_dict(),
                    data=payload,
                    vapid_private_key=str(self._private_key_path),
                    vapid_claims={"sub": self._VAPID_SUBJECT},
                    timeout=self._SEND_TIMEOUT_SECONDS,
                    ttl=self._TTL_SECONDS,
                )
            except WebPushException as error:
                if error.status_code in self._STALE_STATUS_CODES:
                    stale_endpoints.add(subscription.endpoint)
                elif first_error is None:
                    first_error = error
            except Exception as error:
                if first_error is None:
                    first_error = error
            else:
                delivered = True

        if stale_endpoints:
            self._remove_subscriptions(stale_endpoints)
        if first_error is not None:
            raise first_error
        return delivered

    def _load_subscriptions(self) -> list[WebPushSubscription]:
        """:return: snapshot of all currently registered browser subscriptions."""
        with self._subscription_lock:
            return self._read_subscriptions_unlocked()

    def _read_subscriptions_unlocked(self) -> list[WebPushSubscription]:
        """:return: persisted subscriptions while the subscription lock is held."""
        if self._subscriptions_path.exists():
            payload = json.loads(self._subscriptions_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("Persisted push subscriptions must be a JSON array")
            subscriptions = [WebPushSubscription.from_payload(item) for item in payload]
        elif self._legacy_subscription_path.exists():
            payload = json.loads(self._legacy_subscription_path.read_text(encoding="utf-8"))
            subscriptions = [WebPushSubscription.from_payload(payload)]
        else:
            return []

        unique = {subscription.endpoint: subscription for subscription in subscriptions}
        return list(unique.values())

    def _write_subscriptions_unlocked(self, subscriptions: list[WebPushSubscription]) -> None:
        """Persist the complete subscription set while the subscription lock is held."""
        payload = [subscription.to_dict() for subscription in subscriptions]
        self._write_private_file(
            self._subscriptions_path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        self._legacy_subscription_path.unlink(missing_ok=True)

    def _remove_subscriptions(self, endpoints: set[str]) -> None:
        """Remove subscriptions whose provider has declared their endpoints stale."""
        with self._subscription_lock:
            remaining = [subscription for subscription in self._read_subscriptions_unlocked() if subscription.endpoint not in endpoints]
            self._write_subscriptions_unlocked(remaining)

    @staticmethod
    def _duration_seconds(record: JobRecord) -> float | None:
        """:return: elapsed wall-clock seconds for a finished job when timestamps are valid."""
        if not record.finished_at:
            return None
        try:
            return max(0.0, (datetime.fromisoformat(record.finished_at) - datetime.fromisoformat(record.created_at)).total_seconds())
        except ValueError:
            return None

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """:return: compact wall-clock duration for a finished job."""
        rounded = round(seconds)
        hours, remainder = divmod(rounded, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"

    def _load_or_create_private_key(self) -> ec.EllipticCurvePrivateKey:
        """Load the persisted VAPID private key, creating it atomically on first use."""
        with self._key_lock:
            if not self._private_key_path.exists():
                private_key = ec.generate_private_key(ec.SECP256R1())
                encoded = private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
                self._write_private_file(self._private_key_path, encoded)
                return private_key

            loaded = serialization.load_pem_private_key(self._private_key_path.read_bytes(), password=None)
            if not isinstance(loaded, ec.EllipticCurvePrivateKey):
                raise ValueError("Persisted VAPID key is not an EC private key")
            return loaded

    @staticmethod
    def _write_private_file(path: Path, data: bytes) -> None:
        """Atomically replace one Serena-owned private state file with owner-only permissions."""
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            temporary.write_bytes(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
