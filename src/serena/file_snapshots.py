"""Persistent immutable file snapshots referenced by Serena execution history."""

import hashlib
import mimetypes
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from mcp.types import ResourceLink
from pydantic import AnyUrl

from serena.errors import UserFacingError

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
    MCP restart or temporary-directory cleanup. Their lifetime is owned by the
    retained ChatGPT session that references them.
    """

    _SHA256_HEX_LENGTH = hashlib.sha256().digest_size * 2
    _LEGACY_TOKEN_HEX_LENGTH = 48
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
    def _validate_token(cls, token: str) -> None:
        """Rejects tokens that cannot name one snapshot in the private store."""
        if len(token) not in {cls._SHA256_HEX_LENGTH, cls._LEGACY_TOKEN_HEX_LENGTH}:
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
            raise UserFacingError(f"File does not exist: {source_path}")

        size = source_path.stat().st_size
        if size > max_size:
            raise UserFacingError(f"File exceeds the {max_size // (1024 * 1024)} MiB export limit")

        name = display_name or source_path.name
        mime_type, _ = mimetypes.guess_type(name)
        content_digest = hashlib.sha256()

        with cls._LOCK:
            root = cls._root()
            fd, temporary_name = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=root)
            temporary_path = Path(temporary_name)
            os.fchmod(fd, 0o600)
            bytes_written = 0
            try:
                with os.fdopen(fd, "wb") as output:
                    with source_path.open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            bytes_written += len(chunk)
                            if bytes_written > max_size:
                                raise UserFacingError(f"File exceeds the {max_size // (1024 * 1024)} MiB export limit")
                            content_digest.update(chunk)
                            output.write(chunk)
                token = content_digest.hexdigest()
                snapshot_path = root / token
                os.replace(temporary_path, snapshot_path)
            finally:
                temporary_path.unlink(missing_ok=True)

        if version_display_name_by_content:
            name = cls._content_versioned_display_name(name, token)

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
    def read(cls, token: str) -> bytes:
        """Reads one persistent snapshot identified by an opaque resource token."""
        cls._validate_token(token)
        with cls._LOCK:
            path = cls._root() / token
            if not path.is_file():
                raise FileNotFoundError("Serena file snapshot no longer exists")
            if path.stat().st_size > FILE_EXPORT_MAX_SIZE:
                raise ValueError(f"File exceeds the {FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB export limit")
            data = path.read_bytes()
            os.utime(path, None)
            return data
