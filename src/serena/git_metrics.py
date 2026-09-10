from __future__ import annotations

import os
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class GitLineMetrics:
    """Current textual changes and local upstream divergence for one project scope."""

    additions: int = 0
    deletions: int = 0
    ahead_commits: int | None = None


class GitMetricsSource(Protocol):
    """Provides cached and explicitly refreshed Git line metrics for named Serena projects."""

    def get_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        """Returns cached line metrics for ``project_name`` without performing Git work."""
        ...

    def refresh_project_git_metrics(self, project_name: str) -> GitLineMetrics | None:
        """Recomputes and returns line metrics for ``project_name``."""
        ...


@dataclass(frozen=True)
class _FileFingerprint:
    size: int
    mtime_ns: int
    inode: int


@dataclass(frozen=True)
class _CachedUntrackedFile:
    fingerprint: _FileFingerprint
    lines: int


class GitProjectMetrics:
    """Owns cached terminal-style Git line metrics for one Serena project."""

    _BINARY_PROBE_BYTES = 8_000
    _READ_CHUNK_BYTES = 256 * 1024

    def __init__(self, project_root: str | Path) -> None:
        self._project_path = Path(project_root).resolve()
        self._lock = threading.Lock()
        self._metrics: GitLineMetrics | None = None
        self._untracked_files: dict[str, _CachedUntrackedFile] = {}

    @property
    def current(self) -> GitLineMetrics | None:
        """Returns the most recently measured project metrics without performing Git work."""
        with self._lock:
            return self._metrics

    def refresh(self) -> GitLineMetrics | None:
        """Recomputes current project metrics and updates the untracked-file line cache."""
        with self._lock:
            previous_untracked = self._untracked_files

        metrics, untracked_files = self._measure(previous_untracked)

        with self._lock:
            self._metrics = metrics
            self._untracked_files = untracked_files
        return metrics

    def _measure(
        self,
        previous_untracked: dict[str, _CachedUntrackedFile],
    ) -> tuple[GitLineMetrics | None, dict[str, _CachedUntrackedFile]]:
        repository_result = self._git(self._project_path, "rev-parse", "--show-toplevel", check=False)
        if repository_result.returncode != 0:
            return None, {}

        repository_root = Path(repository_result.stdout.strip()).resolve()
        try:
            relative_project = self._project_path.relative_to(repository_root)
        except ValueError:
            return None, {}
        project_pathspec = "." if relative_project == Path(".") else relative_project.as_posix()

        baseline = self._baseline_tree(repository_root)
        tracked = self._git(
            repository_root,
            "diff",
            "--numstat",
            "--no-ext-diff",
            "--no-textconv",
            baseline,
            "--",
            project_pathspec,
        )
        additions, deletions = self._parse_numstat(tracked.stdout)

        untracked = self._git(
            repository_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            project_pathspec,
        )
        current_untracked: dict[str, _CachedUntrackedFile] = {}
        for relative_path in filter(None, untracked.stdout.split("\0")):
            absolute_path = repository_root / relative_path
            cached = self._count_untracked_file(absolute_path, previous_untracked.get(relative_path))
            if cached is None:
                continue
            current_untracked[relative_path] = cached
            additions += cached.lines

        ahead = self._git(repository_root, "rev-list", "--count", "@{upstream}..HEAD", check=False)
        ahead_commits = int(ahead.stdout.strip()) if ahead.returncode == 0 and ahead.stdout.strip().isdigit() else None

        return GitLineMetrics(additions=additions, deletions=deletions, ahead_commits=ahead_commits), current_untracked

    def _baseline_tree(self, repository_root: Path) -> str:
        head = self._git(repository_root, "rev-parse", "--verify", "HEAD", check=False)
        if head.returncode == 0:
            return "HEAD"
        return self._git(repository_root, "hash-object", "-t", "tree", "--stdin", input_text="").stdout.strip()

    def _count_untracked_file(
        self,
        path: Path,
        previous: _CachedUntrackedFile | None,
    ) -> _CachedUntrackedFile | None:
        try:
            file_stat = path.lstat()
        except OSError:
            return None

        fingerprint = _FileFingerprint(size=file_stat.st_size, mtime_ns=file_stat.st_mtime_ns, inode=file_stat.st_ino)
        if previous is not None and previous.fingerprint == fingerprint:
            return previous

        if stat.S_ISLNK(file_stat.st_mode):
            try:
                content = os.fsencode(os.readlink(path))
            except OSError:
                return None
            lines = self._count_text_lines(content)
        elif stat.S_ISREG(file_stat.st_mode):
            lines = self._count_regular_file_lines(path)
        else:
            return None
        return _CachedUntrackedFile(fingerprint=fingerprint, lines=lines)

    def _count_regular_file_lines(self, path: Path) -> int:
        try:
            with path.open("rb") as stream:
                first = stream.read(self._BINARY_PROBE_BYTES)
                if b"\0" in first:
                    return 0

                lines = first.count(b"\n")
                has_content = bool(first)
                ends_with_newline = first.endswith(b"\n")
                while chunk := stream.read(self._READ_CHUNK_BYTES):
                    lines += chunk.count(b"\n")
                    has_content = True
                    ends_with_newline = chunk.endswith(b"\n")
        except OSError:
            return 0

        return lines + int(has_content and not ends_with_newline)

    @staticmethod
    def _count_text_lines(content: bytes) -> int:
        if not content:
            return 0
        return content.count(b"\n") + int(not content.endswith(b"\n"))

    @staticmethod
    def _parse_numstat(output: str) -> tuple[int, int]:
        additions = 0
        deletions = 0
        for line in output.splitlines():
            fields = line.split("\t", 2)
            if len(fields) < 2 or not fields[0].isdigit() or not fields[1].isdigit():
                continue
            additions += int(fields[0])
            deletions += int(fields[1])
        return additions, deletions

    @staticmethod
    def _git(
        cwd: Path,
        *arguments: str,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Runs one Git command with deterministic text decoding."""
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env=environment,
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            input=input_text,
        )
