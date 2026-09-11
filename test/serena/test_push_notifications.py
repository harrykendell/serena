import json
from pathlib import Path

from serena.jobs import JobRecord, JobStatus
from serena.push_notifications import WebPushNotifier


def test_web_push_notifier_persists_subscription_and_sends_job_completion(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []
    notifier = WebPushNotifier(tmp_path / "push", sender=lambda **kwargs: sent.append(kwargs))
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
        finished_at="2026-09-11T10:01:00+00:00",
        return_code=0,
        session_id="session-a",
        project_name="thesis",
        label="T07 validation",
    )

    assert notifier.public_key.startswith("B")
    assert notifier.send_job_finished(record) is True
    payload = json.loads(str(sent[0]["data"]))

    assert payload["title"] == "T07 validation"
    assert payload["body"] == "Completed · thesis · 1m 00s"
    assert payload["url"] == "/dashboard/job/0123456789abcdef0123456789abcdef"
    assert sent[0]["subscription_info"] == {
        "endpoint": "https://push.example.invalid/subscription",
        "keys": {"p256dh": "public-key", "auth": "auth-secret"},
    }
    assert (tmp_path / "push" / "vapid_private.pem").stat().st_mode & 0o777 == 0o600
