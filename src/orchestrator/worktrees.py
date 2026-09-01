"""Git worktree isolation for modifying Codex delegates."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

from orchestrator.config import OrchestratorConfig

_MAX_COMMAND_DETAIL = 2_000


class WorktreeError(RuntimeError):
    """Reports an inability to establish or inspect safe Codex isolation."""


@dataclass(frozen=True)
class WorktreeAllocation:
    """Describes one Orchestrator-owned isolated Git checkout."""

    repository_root: Path
    worktree: Path
    working_directory: Path
    branch: str
    base_revision: str
    live_checkout_dirty: bool


@dataclass(frozen=True)
class WorktreeSummary:
    """Describes the review metadata retained after a modifying Codex run."""

    changed_files: list[str]
    commit: str | None
    diff_summary: str


class WorktreeManager:
    """Owns safe creation and inspection of Orchestrator Codex worktrees."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._config.ensure_state_layout()

    def allocate(self, delegate_id: str, project_root: str, base_revision: str | None) -> WorktreeAllocation:
        """Creates a unique branch/worktree without modifying the requested live checkout."""
        requested_root = Path(project_root).expanduser().resolve()
        if not requested_root.is_dir():
            raise WorktreeError(f"Codex project_root does not exist or is not a directory: {requested_root}")

        repository_root = Path(self._git(requested_root, "rev-parse", "--show-toplevel").strip()).resolve()
        base = self._git(repository_root, "rev-parse", "--verify", f"{base_revision or 'HEAD'}^{{commit}}").strip()
        live_checkout_dirty = bool(self._git(repository_root, "status", "--porcelain").strip())

        repository_dir = self._repository_state_dir(repository_root)
        worktree = repository_dir / delegate_id
        branch = f"orchestrator/{delegate_id}"
        repository_dir.mkdir(parents=True, exist_ok=True)

        with FileLock(str(repository_dir / ".worktree.lock")):
            if worktree.exists():
                raise WorktreeError(f"Orchestrator worktree path already exists: {worktree}")
            try:
                self._git(repository_root, "worktree", "add", "-b", branch, str(worktree), base)
            except WorktreeError:
                self._remove_partial(repository_root, worktree, branch)
                raise

        relative_project_path = requested_root.relative_to(repository_root)
        return WorktreeAllocation(
            repository_root=repository_root,
            worktree=worktree,
            working_directory=worktree / relative_project_path,
            branch=branch,
            base_revision=base,
            live_checkout_dirty=live_checkout_dirty,
        )

    def summarize(self, allocation: WorktreeAllocation) -> WorktreeSummary:
        """Returns bounded commit/diff metadata for review without integrating the branch."""
        head = self._git(allocation.worktree, "rev-parse", "HEAD").strip()
        changed_files = self._changed_files(allocation)

        committed_stat = self._git(allocation.worktree, "diff", "--stat", f"{allocation.base_revision}..HEAD").strip()
        working_stat = self._git(allocation.worktree, "diff", "--stat", "HEAD").strip()
        cached_stat = self._git(allocation.worktree, "diff", "--cached", "--stat").strip()
        parts = [part for part in (committed_stat, cached_stat, working_stat) if part]
        diff_summary = "\n".join(parts)
        if len(diff_summary) > _MAX_COMMAND_DETAIL:
            diff_summary = diff_summary[: _MAX_COMMAND_DETAIL - 3] + "..."

        return WorktreeSummary(
            changed_files=changed_files,
            commit=head if head != allocation.base_revision else None,
            diff_summary=diff_summary,
        )

    def release(self, allocation: WorktreeAllocation) -> None:
        """Removes an unused Orchestrator-owned worktree and branch."""
        repository_dir = self._repository_state_dir(allocation.repository_root)
        repository_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(repository_dir / ".worktree.lock")):
            self._remove_partial(allocation.repository_root, allocation.worktree, allocation.branch)

    def _changed_files(self, allocation: WorktreeAllocation) -> list[str]:
        """Returns unique paths changed from the base revision or in the worktree."""
        committed = self._git(allocation.worktree, "diff", "--name-only", f"{allocation.base_revision}..HEAD").splitlines()
        status = self._git(allocation.worktree, "status", "--porcelain").splitlines()
        working = [line[3:].strip() for line in status if len(line) > 3]
        return sorted({path for path in (*committed, *working) if path})

    def _repository_state_dir(self, repository_root: Path) -> Path:
        """Returns a collision-resistant Orchestrator state directory for one repository."""
        digest = hashlib.sha256(str(repository_root).encode("utf-8")).hexdigest()[:10]
        return self._config.worktrees_dir / f"{repository_root.name}-{digest}"

    def _remove_partial(self, repository_root: Path, worktree: Path, branch: str) -> None:
        """Removes only Orchestrator-owned partial state after failed worktree creation."""
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repository_root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repository_root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    @staticmethod
    def _git(cwd: Path, *arguments: str) -> str:
        """Runs one bounded Git command and raises a safe worktree error on failure."""
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise WorktreeError(f"Could not execute Git while preparing Codex isolation: {exc}") from exc
        if completed.returncode != 0:
            detail = " ".join((completed.stderr or completed.stdout).split())[:_MAX_COMMAND_DETAIL]
            raise WorktreeError(f"Git worktree operation failed safely: {detail or 'unknown Git error'}")
        return completed.stdout
