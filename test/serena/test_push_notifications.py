import json
from pathlib import Path
from types import SimpleNamespace

from pywebpush import WebPushException

from serena.jobs import JobRecord, JobStatus
from serena.push_notifications import ChatGPTApprovalNotification, WebPushNotifier


def test_web_push_notifier_persists_subscription_and_sends_job_completion(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []
    notifier = WebPushNotifier(tmp_path / "push", sender=lambda **kwargs: sent.append(kwargs), minimum_job_duration_seconds=240)
    notifier.save_subscription(
        {
            "endpoint": "https://push.example.invalid/subscription",
            "keys": {"p256dh": "public-key", "auth": "auth-secret"},
        }
    )
    record = JobRecord(
        job_id="0123456789abcdef0123456789abcdef",
        unit_name="serena-job-test.service",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        status=JobStatus.COMPLETED,
        created_at="2026-09-11T10:00:00+00:00",
        finished_at="2026-09-11T10:05:00+00:00",
        return_code=0,
        session_id="session-a",
        project_name="thesis",
        label="T07 validation",
    )

    assert notifier.public_key.startswith("B")
    assert notifier.send_job_finished(record) is True
    payload = json.loads(str(sent[0]["data"]))

    assert payload["title"] == "T07 validation"
    assert payload["body"] == "Completed · thesis · 5m 00s"
    assert payload["url"] == "/dashboard/job/0123456789abcdef0123456789abcdef"
    assert sent[0]["subscription_info"] == {
        "endpoint": "https://push.example.invalid/subscription",
        "keys": {"p256dh": "public-key", "auth": "auth-secret"},
    }
    assert (tmp_path / "push" / "vapid_private.pem").stat().st_mode & 0o777 == 0o600


def test_web_push_notifier_sends_chatgpt_approval_to_registered_browsers(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []
    notifier = WebPushNotifier(tmp_path / "push", sender=lambda **kwargs: sent.append(kwargs))
    notifier.save_subscription(
        {
            "endpoint": "https://push.example.invalid/subscription",
            "keys": {"p256dh": "public-key", "auth": "auth-secret"},
        }
    )
    notification = ChatGPTApprovalNotification.from_payload(
        {
            "conversation_id": "chat-ios",
            "title": "Allow file materialization?",
            "description": "ChatGPT needs your approval to materialize 1 file attachment returned by Serena.",
            "message_id": "approval-message",
            "connector_id": "serena",
            "connector_name": "Serena",
        }
    )

    assert notifier.send_chatgpt_approval(notification) is True
    payload = json.loads(str(sent[0]["data"]))

    assert payload == {
        "title": "Allow file materialization?",
        "body": "ChatGPT needs your approval to materialize 1 file attachment returned by Serena.",
        "tag": "chatgpt-approval-chat-ios-approval-message",
        "url": "/dashboard/chatgpt/chat-ios",
    }


def test_web_push_notifier_delivers_to_multiple_registered_browsers(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []
    notifier = WebPushNotifier(tmp_path / "push", sender=lambda **kwargs: sent.append(kwargs), minimum_job_duration_seconds=240)
    for suffix in ("desktop", "phone"):
        notifier.save_subscription(
            {
                "endpoint": f"https://push.example.invalid/{suffix}",
                "keys": {"p256dh": f"public-{suffix}", "auth": f"auth-{suffix}"},
            }
        )
    record = JobRecord(
        job_id="1123456789abcdef0123456789abcdef",
        unit_name="serena-job-test.service",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        status=JobStatus.COMPLETED,
        created_at="2026-09-11T10:00:00+00:00",
        finished_at="2026-09-11T10:05:00+00:00",
        return_code=0,
        session_id="session-a",
        project_name="serena",
        label="multi-device validation",
    )

    assert notifier.send_job_finished(record) is True
    assert [call["subscription_info"] for call in sent] == [
        {
            "endpoint": "https://push.example.invalid/desktop",
            "keys": {"p256dh": "public-desktop", "auth": "auth-desktop"},
        },
        {
            "endpoint": "https://push.example.invalid/phone",
            "keys": {"p256dh": "public-phone", "auth": "auth-phone"},
        },
    ]


def test_web_push_notifier_migrates_legacy_subscription_when_another_browser_registers(tmp_path: Path) -> None:
    push_root = tmp_path / "push"
    push_root.mkdir()
    (push_root / "subscription.json").write_text(
        json.dumps(
            {
                "endpoint": "https://push.example.invalid/desktop",
                "keys": {"p256dh": "public-desktop", "auth": "auth-desktop"},
            }
        ),
        encoding="utf-8",
    )
    sent: list[dict[str, object]] = []
    notifier = WebPushNotifier(push_root, sender=lambda **kwargs: sent.append(kwargs), minimum_job_duration_seconds=240)
    notifier.save_subscription(
        {
            "endpoint": "https://push.example.invalid/phone",
            "keys": {"p256dh": "public-phone", "auth": "auth-phone"},
        }
    )
    record = JobRecord(
        job_id="2123456789abcdef0123456789abcdef",
        unit_name="serena-job-test.service",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        status=JobStatus.COMPLETED,
        created_at="2026-09-11T10:00:00+00:00",
        finished_at="2026-09-11T10:05:00+00:00",
        return_code=0,
        session_id="session-a",
        project_name="serena",
        label="migration validation",
    )

    assert notifier.send_job_finished(record) is True
    assert [call["subscription_info"] for call in sent] == [
        {
            "endpoint": "https://push.example.invalid/desktop",
            "keys": {"p256dh": "public-desktop", "auth": "auth-desktop"},
        },
        {
            "endpoint": "https://push.example.invalid/phone",
            "keys": {"p256dh": "public-phone", "auth": "auth-phone"},
        },
    ]
    assert not (push_root / "subscription.json").exists()


def test_web_push_notifier_removes_stale_browser_without_blocking_other_devices(tmp_path: Path) -> None:
    attempts: list[str] = []

    def sender(**kwargs: object) -> None:
        subscription = kwargs["subscription_info"]
        assert isinstance(subscription, dict)
        endpoint = subscription["endpoint"]
        assert isinstance(endpoint, str)
        attempts.append(endpoint)
        if endpoint.endswith("/desktop"):
            raise WebPushException("gone", response=SimpleNamespace(status_code=410, text="gone", headers={}))

    notifier = WebPushNotifier(tmp_path / "push", sender=sender, minimum_job_duration_seconds=240)
    for suffix in ("desktop", "phone"):
        notifier.save_subscription(
            {
                "endpoint": f"https://push.example.invalid/{suffix}",
                "keys": {"p256dh": f"public-{suffix}", "auth": f"auth-{suffix}"},
            }
        )
    record = JobRecord(
        job_id="3123456789abcdef0123456789abcdef",
        unit_name="serena-job-test.service",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        status=JobStatus.COMPLETED,
        created_at="2026-09-11T10:00:00+00:00",
        finished_at="2026-09-11T10:05:00+00:00",
        return_code=0,
        session_id="session-a",
        project_name="serena",
        label="stale-device validation",
    )

    assert notifier.send_job_finished(record) is True
    assert attempts == [
        "https://push.example.invalid/desktop",
        "https://push.example.invalid/phone",
    ]

    attempts.clear()
    assert notifier.send_job_finished(record) is True
    assert attempts == ["https://push.example.invalid/phone"]


def test_web_push_notifier_skips_jobs_that_do_not_exceed_notification_threshold(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []
    notifier = WebPushNotifier(tmp_path / "push", sender=lambda **kwargs: sent.append(kwargs), minimum_job_duration_seconds=240)
    notifier.save_subscription(
        {
            "endpoint": "https://push.example.invalid/subscription",
            "keys": {"p256dh": "public-key", "auth": "auth-secret"},
        }
    )
    record = JobRecord(
        job_id="fedcba9876543210fedcba9876543210",
        unit_name="serena-job-test.service",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        status=JobStatus.COMPLETED,
        created_at="2026-09-11T10:00:00+00:00",
        finished_at="2026-09-11T10:04:00+00:00",
        return_code=0,
        session_id="session-a",
        project_name="serena",
        label="quick validation",
    )

    assert notifier.send_job_finished(record) is False
    assert sent == []
