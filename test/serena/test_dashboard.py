from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock

from serena.dashboard import SerenaDashboardAPI
from serena.jobs import JobPersistenceInfo, JobRecord, JobRuntimeInfo, JobSnapshot, JobStatus
from solidlsp.ls_config import LanguageServerId


class _DummyMemoryLogHandler:
    def get_log_messages(self, from_idx: int = 0):  # pragma: no cover - simple stub
        return SimpleNamespace(messages=[], max_idx=-1)

    def clear_log_messages(self) -> None:  # pragma: no cover - simple stub
        pass


class _DummyAgent:
    def __init__(self, project: SimpleNamespace | None) -> None:
        self._project = project
        self.version = "0.0.0"

    def register_config_changed_callback(self, callback: Callable[[], None]) -> None:
        pass

    def execute_task(self, func, *, logged: bool | None = None, name: str | None = None):
        del logged, name
        return func()

    def get_active_project(self):
        return self._project


def _make_dashboard(project_languages: list[LanguageServerId] | None) -> SerenaDashboardAPI:
    project = None
    if project_languages is not None:
        project = SimpleNamespace(project_config=SimpleNamespace(language_servers=project_languages))
    agent = _DummyAgent(project)
    return SerenaDashboardAPI(memory_log_handler=_DummyMemoryLogHandler(), tool_names=[], agent=agent, tool_usage_stats=None)


def test_available_languages_include_experimental_when_no_active_project():
    dashboard = _make_dashboard(project_languages=None)
    response = dashboard._get_available_languages()
    expected = sorted(lang.value for lang in LanguageServerId.iter_all(include_experimental=True))
    assert response.languages == expected


def test_available_languages_exclude_project_languages():
    dashboard = _make_dashboard(project_languages=[LanguageServerId.PYTHON, LanguageServerId.MARKDOWN])
    response = dashboard._get_available_languages()
    available = set(response.languages)
    assert LanguageServerId.PYTHON.value not in available
    assert LanguageServerId.MARKDOWN.value not in available
    # ensure experimental languages remain available for selection
    assert LanguageServerId.ANSIBLE.value in available


def test_background_jobs_route_exposes_running_job_telemetry() -> None:
    dashboard = _make_dashboard(project_languages=None)
    job_manager = MagicMock()
    job_manager.max_concurrent_jobs = 6
    job_manager.persistence_info.return_value = JobPersistenceInfo(
        survives_serena_restart=True,
        survives_logout=False,
        survives_reboot=False,
        linger_enabled=False,
    )
    job_manager.list_job_snapshots.return_value = [
        JobSnapshot(
            record=JobRecord(
                job_id="0123456789abcdef0123456789abcdef",
                unit_name="serena-job-0123456789abcdef0123456789abcdef.service",
                project_root="/tmp/project",
                cwd="/tmp/project",
                status=JobStatus.RUNNING,
                created_at="2026-08-28T18:00:00+00:00",
                project_name="demo",
                label="Long optimisation",
                timeout_seconds=3600,
            ),
            runtime=JobRuntimeInfo(
                elapsed_seconds=125.0,
                seconds_since_last_output=4.0,
                memory_bytes=256 * 1024 * 1024,
                cpu_seconds=87.5,
                process_count=5,
            ),
        )
    ]
    dashboard._job_manager = job_manager

    client = dashboard._app.test_client()
    response = client.get("/background_jobs").get_json()
    namespaced_response = client.get("/dashboard/api/background_jobs").get_json()

    assert namespaced_response == response
    assert response["status"] == "success"
    assert response["running_jobs"] == 1
    assert response["max_concurrent_jobs"] == 6
    assert response["jobs"][0]["label"] == "Long optimisation"
    assert response["jobs"][0]["memory_bytes"] == 256 * 1024 * 1024
    assert response["jobs"][0]["process_count"] == 5
    assert response["persistence"]["survives_logout"] is False
