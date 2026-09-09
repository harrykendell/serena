import os
from pathlib import Path

from serena.activity_history import ActivityHistoryStore
from serena.config.serena_config import SerenaPaths
from serena.dashboard_activity import DashboardActivityArchive
from serena.jobs import JobStore
from solidlsp.settings import SolidLSPSettings


def test_default_persistence_uses_suite_owned_state() -> None:
    serena_home = Path(os.environ["SERENA_HOME"]).resolve()
    solidlsp_home = Path(os.environ["SOLIDLSP_DIR"]).resolve()
    real_serena_home = (Path.home() / ".serena").resolve()
    real_solidlsp_home = (Path.home() / ".solidlsp").resolve()

    assert serena_home != real_serena_home
    assert solidlsp_home != real_solidlsp_home
    assert Path(SerenaPaths().serena_user_home_dir).resolve() == serena_home

    solidlsp_settings = SolidLSPSettings()
    assert Path(solidlsp_settings.solidlsp_dir).resolve() == solidlsp_home
    assert Path(solidlsp_settings.ls_resources_dir).resolve().is_relative_to(solidlsp_home)

    job_store = JobStore()
    assert job_store.root.resolve() == serena_home / "jobs"

    run_id = f"f01-test-isolation-check-{os.getpid()}"
    ActivityHistoryStore().save({"run_id": run_id})
    assert (serena_home / "activity_runs" / f"{run_id}.json").is_file()
    assert not (real_serena_home / "activity_runs" / f"{run_id}.json").exists()

    session_id = f"f01-test-isolation-session-{os.getpid()}"
    archive = DashboardActivityArchive()
    archive.set_display_name(session_id, "F01 isolation check")
    panel_id = archive.panel_id_for_session(session_id)
    assert (serena_home / "dashboard_activity_sessions" / f"{panel_id}.json").is_file()
    assert not (real_serena_home / "dashboard_activity_sessions" / f"{panel_id}.json").exists()
