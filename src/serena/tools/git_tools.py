"""Project-scoped Git tools with deliberately constrained write operations."""

import re
import subprocess

from serena.tools.tools_base import Tool, ToolMarkerCanEdit, ToolMarkerOptional


class _GitTool(Tool, ToolMarkerOptional):
    _SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

    def _run_git(self, args: list[str], max_answer_chars: int = -1) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.get_project_root(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"git exited with status {result.returncode}"
            raise RuntimeError(detail)
        output = result.stdout.strip()
        if result.stderr.strip():
            output = f"{output}\n{result.stderr.strip()}".strip()
        return self._limit_length(output or "OK", max_answer_chars)

    @classmethod
    def _validate_git_name(cls, value: str, kind: str) -> str:
        if not cls._SAFE_NAME.fullmatch(value) or value.startswith("-") or ".." in value:
            raise ValueError(f"Invalid {kind}: {value!r}")
        return value

    def _validate_paths(self, paths: list[str]) -> list[str]:
        if not paths:
            raise ValueError("At least one project-relative path is required")
        for path in paths:
            self.project.validate_relative_path(path)
        return paths


class GitStatusTool(_GitTool):
    """Shows concise Git status for the active project."""

    def apply(self, max_answer_chars: int = -1) -> str:
        """Returns branch and working-tree status.

        :param max_answer_chars: maximum output length, or -1 for the configured default
        :return: concise porcelain Git status
        """
        return self._run_git(["status", "--short", "--branch"], max_answer_chars)


class GitFetchTool(_GitTool, ToolMarkerCanEdit):
    """Fetches updates from a named Git remote without changing the working tree."""

    def apply(self, remote: str = "origin", max_answer_chars: int = -1) -> str:
        """Fetches one configured remote without pruning or force-updating the working tree.

        :param remote: configured Git remote name
        :param max_answer_chars: maximum output length, or -1 for the configured default
        :return: Git fetch output
        """
        remote = self._validate_git_name(remote, "remote")
        return self._run_git(["fetch", remote], max_answer_chars)


class GitLogTool(_GitTool):
    """Shows recent Git history for the active project."""

    def apply(self, limit: int = 20, ref: str | None = None, max_answer_chars: int = -1) -> str:
        """Returns a compact recent commit log.

        :param limit: number of commits to return, from 1 through 200
        :param ref: optional branch, tag, or commit-ish to inspect
        :param max_answer_chars: maximum output length, or -1 for the configured default
        :return: compact Git log output
        """
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        args = ["log", f"-{limit}", "--date=short", "--pretty=format:%h %ad %d %s"]
        if ref is not None:
            args.append(self._validate_git_name(ref, "ref"))
        return self._run_git(args, max_answer_chars)


class GitDiffTool(_GitTool):
    """Shows a Git diff, optionally restricted to project-relative paths."""

    def apply(self, staged: bool = False, paths: list[str] | None = None, max_answer_chars: int = -1) -> str:
        """Returns unstaged or staged changes.

        :param staged: whether to show staged rather than unstaged changes
        :param paths: optional project-relative paths to restrict the diff
        :param max_answer_chars: maximum output length, or -1 for the configured default
        :return: unified Git diff
        """
        args = ["diff"]
        if staged:
            args.append("--staged")
        if paths:
            for path in paths:
                self.project.validate_relative_path(path)
            args.extend(["--", *paths])
        return self._run_git(args, max_answer_chars)


class GitBranchTool(_GitTool, ToolMarkerCanEdit):
    """Lists, creates, switches, or safely deletes local Git branches."""

    def apply(self, action: str, name: str | None = None, start_point: str | None = None, max_answer_chars: int = -1) -> str:
        """Performs a constrained local branch operation.

        ``delete`` uses Git's safe ``-d`` mode and never force-deletes. ``switch`` never discards changes.

        :param action: one of ``list``, ``create``, ``switch``, or ``delete``
        :param name: branch name for create, switch, or delete
        :param start_point: optional validated start point for create
        :param max_answer_chars: maximum output length, or -1 for the configured default
        :return: Git branch operation output
        """
        if action == "list":
            return self._run_git(["branch", "--verbose", "--no-abbrev"], max_answer_chars)
        if action not in {"create", "switch", "delete"}:
            raise ValueError("action must be one of: list, create, switch, delete")
        if name is None:
            raise ValueError(f"name is required for branch action {action!r}")
        name = self._validate_git_name(name, "branch name")

        if action == "create":
            args = ["branch", name]
            if start_point is not None:
                args.append(self._validate_git_name(start_point, "start point"))
            return self._run_git(args, max_answer_chars)
        if action == "switch":
            return self._run_git(["switch", name], max_answer_chars)
        return self._run_git(["branch", "-d", name], max_answer_chars)


class GitCommitTool(_GitTool, ToolMarkerCanEdit):
    """Creates a commit containing only explicitly selected project paths."""

    def apply(self, message: str, paths: list[str], max_answer_chars: int = -1) -> str:
        """Commits only the explicitly listed paths.

        Existing unrelated staged changes are excluded from the commit. Empty messages are rejected.

        :param message: commit message
        :param paths: project-relative paths to include in the commit
        :param max_answer_chars: maximum output length, or -1 for the configured default
        :return: Git commit output
        """
        if not message.strip():
            raise ValueError("Commit message must not be empty")
        paths = self._validate_paths(paths)

        # stage the requested paths, then commit only those paths so unrelated staged work is preserved
        self._run_git(["add", "--", *paths])
        return self._run_git(["commit", "--only", "-m", message, "--", *paths], max_answer_chars)


class GitPullTool(_GitTool, ToolMarkerCanEdit):
    """Pulls from a remote using fast-forward-only semantics."""

    def apply(self, remote: str = "origin", branch: str | None = None, max_answer_chars: int = -1) -> str:
        """Pulls without creating merge commits or rebasing local work.

        :param remote: configured Git remote name
        :param branch: optional remote branch name; omit to use Git's configured upstream
        :param max_answer_chars: maximum output length, or -1 for the configured default
        :return: Git pull output
        """
        remote = self._validate_git_name(remote, "remote")
        args = ["pull", "--ff-only", remote]
        if branch is not None:
            args.append(self._validate_git_name(branch, "branch"))
        return self._run_git(args, max_answer_chars)


class GitPushTool(_GitTool, ToolMarkerCanEdit):
    """Pushes the current branch without force or deletion semantics."""

    def apply(self, remote: str = "origin", set_upstream: bool = False, max_answer_chars: int = -1) -> str:
        """Pushes ``HEAD`` to the same branch name on a configured remote.

        Force pushes, remote branch deletion, arbitrary refspecs, and tag pushes are intentionally unsupported.

        :param remote: configured Git remote name
        :param set_upstream: whether to set the remote tracking branch
        :param max_answer_chars: maximum output length, or -1 for the configured default
        :return: Git push output
        """
        remote = self._validate_git_name(remote, "remote")
        branch = self._run_git(["branch", "--show-current"]).strip()
        if not branch or branch == "OK":
            raise RuntimeError("Cannot push while HEAD is detached")
        self._validate_git_name(branch, "branch")
        args = ["push"]
        if set_upstream:
            args.append("--set-upstream")
        args.extend([remote, f"HEAD:refs/heads/{branch}"])
        return self._run_git(args, max_answer_chars)
