"""Persistent immutable file snapshots referenced by Serena execution history."""

import hashlib
import mimetypes
import os
import secrets
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from mcp.types import ResourceLink
from pydantic import AnyUrl

from serena.execution_store import ExecutionStore

FILE_EXPORT_MAX_SIZE = 100 * 1024 * 1024


@dataclass(frozen=True, kw_only=True)
class FileSnapshot:
    """Represents one immutable file snapshot retained in private Serena storage."""

    path: Path
    link: ResourceLink


class FileSnapshotStore:
    """Owns immutable snapshots backing ChatGPT file resources.

    Snapshots are kept in Serena's persistent user-data directory rather than the
    system temporary directory. This lets a chat reopen an exported file after an
    MCP restart or temporary-directory cleanup. Storage remains bounded by evicting
    least-recently-used snapshots only when new snapshots are created.
    """

    _TOKEN_BYTES = 24
    _MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
    _MAX_SNAPSHOTS = 2048
    _LOCK: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _root(cls) -> Path:
        """Returns the private persistent directory used for file snapshots."""
        configured_home = os.getenv("SERENA_HOME", "").strip()
        serena_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".serena"
        root = serena_home / "chat_file_snapshots"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Serena file snapshot store is not a private directory")
        if stat.S_IMODE(root.stat().st_mode) != 0o700:
            root.chmod(0o700)
        return root

    @classmethod
    def _legacy_root(cls) -> Path:
        """Returns the former temporary snapshot directory for compatibility reads."""
        return Path(tempfile.gettempdir(), f"serena-chat-files-{os.getuid()}")

    @classmethod
    def _prune(cls, root: Path, incoming_size: int) -> None:
        """Evicts unreferenced LRU snapshots until a new snapshot fits within bounds."""
        pinned = ExecutionStore.retained_file_tokens_from_disk()
        snapshots: list[tuple[Path, os.stat_result]] = []
        total_size = 0
        for path in root.iterdir():
            if path.name.startswith("."):
                continue
            try:
                file_stat = path.stat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            total_size += file_stat.st_size
            if path.name not in pinned:
                snapshots.append((path, file_stat))

        snapshots.sort(key=lambda item: item[1].st_mtime)
        total_count = sum(1 for path in root.iterdir() if path.is_file() and not path.name.startswith("."))
        while snapshots and (total_count >= cls._MAX_SNAPSHOTS or total_size + incoming_size > cls._MAX_TOTAL_SIZE):
            path, file_stat = snapshots.pop(0)
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            total_count -= 1
            total_size -= file_stat.st_size

    @classmethod
    def _validate_token(cls, token: str) -> None:
        """Rejects tokens that cannot name one snapshot in the private store."""
        if len(token) != cls._TOKEN_BYTES * 2:
            raise ValueError("Invalid Serena file resource")
        try:
            bytes.fromhex(token)
        except ValueError as exc:
            raise ValueError("Invalid Serena file resource") from exc

    @staticmethod
    def _content_versioned_display_name(name: str, digest: str) -> str:
        """Returns a cache-safe display name versioned by immutable file content."""
        path = Path(name)
        suffix = "".join(path.suffixes)
        stem = path.name[: -len(suffix)] if suffix else path.name
        return f"{stem}-{digest[:16]}{suffix}"

    @classmethod
    def snapshot(
        cls,
        source_path: Path,
        *,
        display_name: str | None = None,
        description: str = "Immutable file snapshot exported by Serena",
        max_size: int = FILE_EXPORT_MAX_SIZE,
        version_display_name_by_content: bool = False,
    ) -> FileSnapshot:
        """Copies one file into persistent private storage and returns its immutable resource link."""
        if not source_path.is_file():
            raise FileNotFoundError(f"File does not exist: {source_path}")

        size = source_path.stat().st_size
        if size > max_size:
            raise ValueError(f"File exceeds the {max_size // (1024 * 1024)} MiB export limit")

        name = display_name or source_path.name
        mime_type, _ = mimetypes.guess_type(name)
        token = secrets.token_hex(cls._TOKEN_BYTES)
        content_digest = hashlib.sha256() if version_display_name_by_content else None

        with cls._LOCK:
            root = cls._root()
            cls._prune(root, size)
            snapshot_path = root / token
            temporary_path = root / f".{token}.tmp"

            fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            bytes_written = 0
            try:
                with os.fdopen(fd, "wb") as output:
                    with source_path.open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            bytes_written += len(chunk)
                            if bytes_written > max_size:
                                raise ValueError(f"File exceeds the {max_size // (1024 * 1024)} MiB export limit")
                            if content_digest is not None:
                                content_digest.update(chunk)
                            output.write(chunk)
                os.replace(temporary_path, snapshot_path)
            finally:
                temporary_path.unlink(missing_ok=True)

        if content_digest is not None:
            name = cls._content_versioned_display_name(name, content_digest.hexdigest())

        return FileSnapshot(
            path=snapshot_path,
            link=ResourceLink(
                type="resource_link",
                name=name,
                uri=AnyUrl(f"serena-file://export/{token}"),
                mimeType=mime_type or "application/octet-stream",
                size=bytes_written,
                description=description,
            ),
        )

    @classmethod
    def snapshot_project_file(cls, project, relative_path: str) -> FileSnapshot:
        """Snapshots one confined project file into persistent private storage."""
        project.validate_relative_path(relative_path)
        path = Path(project.project_root, relative_path)
        return cls.snapshot(
            path,
            display_name=path.name,
            description=f"File exported from Serena project {project.project_name}",
        )

    @classmethod
    def _migrate_legacy_snapshot(cls, token: str, destination: Path) -> bool:
        """Copies one still-present legacy temporary snapshot into persistent storage."""
        legacy_path = cls._legacy_root() / token
        if not legacy_path.is_file():
            return False
        if legacy_path.stat().st_size > FILE_EXPORT_MAX_SIZE:
            raise ValueError(f"File exceeds the {FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB export limit")

        temporary_path = destination.parent / f".{token}.legacy.tmp"
        try:
            shutil.copyfile(legacy_path, temporary_path)
            temporary_path.chmod(0o600)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        legacy_path.unlink(missing_ok=True)
        return True

    @classmethod
    def read(cls, token: str) -> bytes:
        """Reads one persistent snapshot identified by an opaque resource token."""
        cls._validate_token(token)
        with cls._LOCK:
            root = cls._root()
            path = root / token
            if not path.is_file() and not cls._migrate_legacy_snapshot(token, path):
                raise FileNotFoundError("Serena file snapshot no longer exists")
            if path.stat().st_size > FILE_EXPORT_MAX_SIZE:
                raise ValueError(f"File exceeds the {FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB export limit")
            data = path.read_bytes()
            os.utime(path, None)
            return data
