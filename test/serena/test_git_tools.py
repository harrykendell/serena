import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.tools import GitBranchTool, GitCommitTool, GitDiffTool, GitLogTool, GitStatusTool


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_tool(tool_cls, project: Project):
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    tool = tool_cls(agent)
    tool._limit_length = lambda result, max_answer_chars: result
    return tool


@pytest.fixture
def git_project(tmp_path: Path) -> tuple[Path, Project]:
    _run_git(tmp_path, "init", "-b", "main")
    _run_git(tmp_path, "config", "user.email", "serena@example.invalid")
    _run_git(tmp_path, "config", "user.name", "Serena Test")
    (tmp_path / "a.txt").write_text("a0\n")
    (tmp_path / "b.txt").write_text("b0\n")
    _run_git(tmp_path, "add", "a.txt", "b.txt")
    _run_git(tmp_path, "commit", "-m", "initial")
    project = Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    return tmp_path, project


def test_git_read_tools_report_repository_state(git_project: tuple[Path, Project]) -> None:
    repo, project = git_project
    (repo / "a.txt").write_text("a1\n")

    status = _make_tool(GitStatusTool, project).apply()
    diff = _make_tool(GitDiffTool, project).apply(paths=["a.txt"])
    log = _make_tool(GitLogTool, project).apply(limit=1)

    assert "a.txt" in status
    assert "-a0" in diff and "+a1" in diff
    assert "initial" in log


def test_git_commit_includes_only_explicit_paths(git_project: tuple[Path, Project]) -> None:
    repo, project = git_project
    (repo / "a.txt").write_text("a1\n")
    (repo / "b.txt").write_text("b1\n")
    _run_git(repo, "add", "b.txt")

    _make_tool(GitCommitTool, project).apply("update a", ["a.txt"])

    assert _run_git(repo, "show", "--name-only", "--format=", "HEAD") == "a.txt"
    assert _run_git(repo, "diff", "--cached", "--name-only") == "b.txt"


def test_git_commit_rejects_paths_outside_project(git_project: tuple[Path, Project]) -> None:
    _, project = git_project

    with pytest.raises(ValueError):
        _make_tool(GitCommitTool, project).apply("bad commit", ["../outside.txt"])


def test_git_branch_delete_never_force_deletes_unmerged_work(git_project: tuple[Path, Project]) -> None:
    repo, project = git_project
    branch_tool = _make_tool(GitBranchTool, project)
    branch_tool.apply("create", "work")
    branch_tool.apply("switch", "work")
    (repo / "a.txt").write_text("branch-only\n")
    _run_git(repo, "add", "a.txt")
    _run_git(repo, "commit", "-m", "branch work")
    branch_tool.apply("switch", "main")

    with pytest.raises(RuntimeError):
        branch_tool.apply("delete", "work")

    assert "work" in _run_git(repo, "branch", "--list", "work")


def test_git_fetch_pull_and_push_use_safe_default_flows(git_project: tuple[Path, Project], tmp_path: Path) -> None:
    repo, project = git_project
    remote = tmp_path.parent / f"{tmp_path.name}-remote.git"
    peer = tmp_path.parent / f"{tmp_path.name}-peer"
    _run_git(remote.parent, "init", "--bare", str(remote))
    _run_git(repo, "remote", "add", "origin", str(remote))

    from serena.tools import GitFetchTool, GitPullTool, GitPushTool

    push_tool = _make_tool(GitPushTool, project)
    fetch_tool = _make_tool(GitFetchTool, project)
    pull_tool = _make_tool(GitPullTool, project)
    push_tool.apply(set_upstream=True)

    subprocess.run(["git", "clone", "-b", "main", str(remote), str(peer)], capture_output=True, text=True, check=True)
    _run_git(peer, "config", "user.email", "peer@example.invalid")
    _run_git(peer, "config", "user.name", "Peer Test")
    (peer / "peer.txt").write_text("remote update\n")
    _run_git(peer, "add", "peer.txt")
    _run_git(peer, "commit", "-m", "remote update")
    _run_git(peer, "push", "origin", "main")

    fetch_tool.apply()
    assert _run_git(repo, "rev-parse", "origin/main") == _run_git(peer, "rev-parse", "HEAD")

    pull_tool.apply()
    assert (repo / "peer.txt").read_text() == "remote update\n"
