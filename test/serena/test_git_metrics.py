import subprocess
from pathlib import Path

from serena.git_metrics import GitLineMetrics, GitProjectMetrics


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True).stdout


def test_git_project_metrics_include_untracked_text_and_skip_binary_lines(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "serena@example.invalid")
    _git(tmp_path, "config", "user.name", "Serena Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\ntwo\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")

    metrics = GitProjectMetrics(tmp_path)
    assert metrics.current is None
    assert metrics.refresh() == GitLineMetrics()

    tracked.write_text("one\nchanged\nthree\n")
    untracked = tmp_path / "untracked.txt"
    untracked.write_text("alpha\nbeta\n")
    (tmp_path / "binary.bin").write_bytes(b"text\x00binary\n")

    assert metrics.refresh() == GitLineMetrics(additions=4, deletions=1)
    assert metrics.current == GitLineMetrics(additions=4, deletions=1)

    untracked.unlink()
    assert metrics.current == GitLineMetrics(additions=4, deletions=1)
    assert metrics.refresh() == GitLineMetrics(additions=2, deletions=1)


def test_git_project_metrics_do_not_double_count_staged_new_files(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "serena@example.invalid")
    _git(tmp_path, "config", "user.name", "Serena Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")

    new_file = tmp_path / "new.txt"
    new_file.write_text("one\ntwo\n")
    metrics = GitProjectMetrics(tmp_path)
    assert metrics.refresh() == GitLineMetrics(additions=2, deletions=0)

    _git(tmp_path, "add", "new.txt")
    assert metrics.refresh() == GitLineMetrics(additions=2, deletions=0)


def test_git_project_metrics_report_commits_ahead_of_configured_upstream(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "serena@example.invalid")
    _git(tmp_path, "config", "user.name", "Serena Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "branch", "upstream")
    _git(tmp_path, "branch", "--set-upstream-to=upstream", "main")

    metrics = GitProjectMetrics(tmp_path)
    assert metrics.refresh() == GitLineMetrics(ahead_commits=0)

    tracked.write_text("next\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "ahead")

    assert metrics.refresh() == GitLineMetrics(ahead_commits=1)


def test_git_project_metrics_omit_ahead_count_without_upstream(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "serena@example.invalid")
    _git(tmp_path, "config", "user.name", "Serena Test")
    (tmp_path / "tracked.txt").write_text("base\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")

    assert GitProjectMetrics(tmp_path).refresh() == GitLineMetrics(ahead_commits=None)
