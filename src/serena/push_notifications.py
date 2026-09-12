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
from pywebpush import webpush

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


class WebPushNotifier:
    """Owns Serena's single-device Web Push proof-of-concept state and delivery."""

    _VAPID_SUBJECT = "https://mcp.kendell.uk"
    _SEND_TIMEOUT_SECONDS = 5
    _TTL_SECONDS = 86_400

    def __init__(
        self,
        root: Path | None = None,
        sender: Callable[..., Any] = webpush,
        minimum_job_duration_seconds: float | None = None,
    ) -> None:
        if root is None:
            root = Path(SerenaPaths().serena_user_home_dir) / "push"
        if minimum_job_duration_seconds is None:
            minimum_job_duration_seconds = SerenaConfig.from_config_file().tool_timeout

        self._root = root
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        self._private_key_path = self._root / "vapid_private.pem"
        self._subscription_path = self._root / "subscription.json"
        self._key_lock = FileLock(str(self._root / ".vapid.lock"))
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
        """Validate and persist the single proof-of-concept browser subscription."""
        subscription = WebPushSubscription.from_payload(payload)
        self._write_private_file(
            self._subscription_path,
            json.dumps(subscription.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        return subscription

    def send_job_finished(self, record: JobRecord) -> bool:
        """Send a completion notification for a sufficiently long naturally completed or failed job."""
        duration_seconds = self._duration_seconds(record)
        if (
            record.status not in {JobStatus.COMPLETED, JobStatus.FAILED}
            or duration_seconds is None
            or duration_seconds <= self._minimum_job_duration_seconds
            or not self._subscription_path.exists()
        ):
            return False

        subscription = WebPushSubscription.from_payload(json.loads(self._subscription_path.read_text(encoding="utf-8")))
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
        self._sender(
            subscription_info=subscription.to_dict(),
            data=payload,
            vapid_private_key=str(self._private_key_path),
            vapid_claims={"sub": self._VAPID_SUBJECT},
            timeout=self._SEND_TIMEOUT_SECONDS,
            ttl=self._TTL_SECONDS,
        )
        return True

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
