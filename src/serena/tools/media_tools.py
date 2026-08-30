"""Tools for returning project files and media through MCP-native content blocks."""

import hashlib
import ipaddress
import mimetypes
import os
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin, urlparse

import requests
from mcp.server.fastmcp import Audio, FastMCP, Image
from mcp.types import CallToolResult, ResourceLink
from pydantic import AnyUrl, BaseModel, ConfigDict

from serena.tools.tools_base import Tool, ToolMarkerCanEdit, ToolMarkerOptional

_FILE_RESOURCE_URI_TEMPLATE = "serena-file://export/{token}"
_FILE_EXPORT_MAX_SIZE = 100 * 1024 * 1024


class OpenAIFile(BaseModel):
    """Represents one ChatGPT file passed through ``openai/fileParams``."""

    model_config = ConfigDict(extra="forbid")

    download_url: str
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


@dataclass(frozen=True, kw_only=True)
class _FileSnapshot:
    """Represents one immutable file snapshot retained in temporary storage."""

    path: Path
    link: ResourceLink


class _TemporaryFileStore:
    """Owns short-lived immutable snapshots used by ChatGPT file resources."""

    _MAX_AGE_SECONDS = 24 * 60 * 60
    _TOKEN_BYTES = 24
    _LOCK: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _root(cls) -> Path:
        """Returns the private system-temporary directory used for snapshots."""
        root = Path(tempfile.gettempdir(), f"serena-chat-files-{os.getuid()}")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Serena temporary file store is not a private directory")
        if stat.S_IMODE(root.stat().st_mode) != 0o700:
            root.chmod(0o700)
        return root

    @classmethod
    def _prune(cls, root: Path) -> None:
        """Removes snapshots that have not been used within the retention window."""
        cutoff = time.time() - cls._MAX_AGE_SECONDS
        for path in root.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue

    @classmethod
    def _validate_token(cls, token: str) -> None:
        """Rejects tokens that cannot name one snapshot in the temporary store."""
        if len(token) != cls._TOKEN_BYTES * 2:
            raise ValueError("Invalid Serena file resource")
        try:
            bytes.fromhex(token)
        except ValueError as exc:
            raise ValueError("Invalid Serena file resource") from exc

    @classmethod
    def snapshot(
        cls,
        source_path: Path,
        *,
        display_name: str | None = None,
        description: str = "Temporary file snapshot exported by Serena",
        max_size: int = _FILE_EXPORT_MAX_SIZE,
    ) -> _FileSnapshot:
        """Copies one file into temporary storage and returns its immutable resource link."""
        if not source_path.is_file():
            raise FileNotFoundError(f"File does not exist: {source_path}")

        size = source_path.stat().st_size
        if size > max_size:
            raise ValueError(f"File exceeds the {max_size // (1024 * 1024)} MiB export limit")

        name = display_name or source_path.name
        mime_type, _ = mimetypes.guess_type(name)
        token = secrets.token_hex(cls._TOKEN_BYTES)

        with cls._LOCK:
            root = cls._root()
            cls._prune(root)
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
                            output.write(chunk)
                os.replace(temporary_path, snapshot_path)
            finally:
                temporary_path.unlink(missing_ok=True)

        return _FileSnapshot(
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
    def snapshot_project_file(cls, project, relative_path: str) -> _FileSnapshot:
        """Snapshots one confined project file into temporary storage."""
        project.validate_relative_path(relative_path)
        path = Path(project.project_root, relative_path)
        return cls.snapshot(
            path,
            display_name=path.name,
            description=f"File exported from Serena project {project.project_name}",
        )

    @classmethod
    def read(cls, token: str) -> bytes:
        """Reads one temporary snapshot identified by an opaque resource token."""
        cls._validate_token(token)
        with cls._LOCK:
            root = cls._root()
            cls._prune(root)
            path = root / token
            if not path.is_file():
                raise FileNotFoundError("Serena file snapshot has expired or no longer exists")
            if path.stat().st_size > _FILE_EXPORT_MAX_SIZE:
                raise ValueError(f"File exceeds the {_FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB export limit")
            data = path.read_bytes()
            os.utime(path, None)
            return data


def register_file_export_resource(mcp: FastMCP) -> None:
    """Registers the binary resource used to read temporary file snapshots."""

    @mcp.resource(
        _FILE_RESOURCE_URI_TEMPLATE,
        name="Serena exported file snapshot",
        description="Reads a short-lived immutable file snapshot exported by Serena.",
        mime_type="application/octet-stream",
    )
    def read_exported_file(token: str) -> bytes:
        return _TemporaryFileStore.read(token)


@dataclass(frozen=True, kw_only=True)
class _NativeMediaResult:
    """Pairs native MCP media with the same file as a transferable resource."""

    media: Image | Audio
    file_link: ResourceLink


def get_result_media(result: object) -> Image | Audio | None:
    """Returns native media carried by one media-tool result, if present."""
    if isinstance(result, Image | Audio):
        return result
    if isinstance(result, _NativeMediaResult):
        return result.media
    return None


def get_result_file_link(result: object) -> ResourceLink | None:
    """Returns the transferable project-file link carried by one media-tool result, if present."""
    if isinstance(result, ResourceLink):
        return result
    if isinstance(result, _NativeMediaResult):
        return result.file_link
    return None


def read_result_file_link(link: ResourceLink) -> bytes:
    """Reads bytes from a Serena project-file resource link."""
    uri = str(link.uri)
    prefix = _FILE_RESOURCE_URI_TEMPLATE.split("{token}", 1)[0]
    if not uri.startswith(prefix):
        raise ValueError("Resource link is not a Serena exported project file")
    return _TemporaryFileStore.read(uri.removeprefix(prefix))


class _McpMediaTool(Tool, ToolMarkerOptional):
    """Base for tools returning native media with a transferable snapshot link."""

    @classmethod
    def get_apply_fn_metadata_from_cls(cls, structured_output: bool | None = None):
        # MCP media helpers are content blocks, not JSON-structured outputs
        return super().get_apply_fn_metadata_from_cls(structured_output=False)

    def prepare_mcp_result(self, result: object) -> CallToolResult:
        """Returns native media plus the same snapshot as a transferable resource link."""
        if isinstance(result, ResourceLink):
            return CallToolResult(content=[result])
        if not isinstance(result, _NativeMediaResult):
            raise TypeError(f"Unexpected MCP media result: {type(result).__name__}")

        media = result.media
        if isinstance(media, Image):
            media_content = media.to_image_content()
        elif isinstance(media, Audio):
            media_content = media.to_audio_content()
        else:
            raise TypeError(f"Unexpected MCP media result: {type(media).__name__}")

        return CallToolResult(content=[media_content, result.file_link])


class DownloadFileTool(Tool, ToolMarkerOptional):
    """Transfers a project file into ChatGPT's native file store."""

    @classmethod
    def get_apply_fn_metadata_from_cls(cls, structured_output: bool | None = None):
        return super().get_apply_fn_metadata_from_cls(structured_output=False)

    @classmethod
    def get_mcp_tool_meta(cls) -> dict[str, object] | None:
        """Uses host-side file promotion without requiring a ChatGPT App view."""
        return None

    def apply(self, relative_path: str) -> ResourceLink:
        """Prepares one project file as a standard MCP resource link.

        :param relative_path: project-relative path to the file
        :return: standard MCP resource link for the exported file
        """
        return _TemporaryFileStore.snapshot_project_file(self.project, relative_path).link

    def prepare_mcp_result(self, result: object) -> CallToolResult:
        """Returns the exported project file as a standard MCP resource link."""
        if not isinstance(result, ResourceLink):
            raise TypeError(f"Unexpected exported file result: {type(result).__name__}")

        return CallToolResult(content=[result])


class UploadFileTool(Tool, ToolMarkerCanEdit, ToolMarkerOptional):
    """Uploads one ChatGPT file into the active Serena project."""

    _MAX_REDIRECTS = 4
    _CONNECT_TIMEOUT_SECONDS = 10.0
    _READ_TIMEOUT_SECONDS = 30.0
    _TOTAL_TIMEOUT_SECONDS = 120.0

    @classmethod
    def get_mcp_tool_meta(cls) -> dict[str, object]:
        """Marks the top-level ``file`` argument as a ChatGPT file parameter."""
        return {"openai/fileParams": ["file"]}

    @staticmethod
    def _validate_download_url(download_url: str) -> None:
        """Rejects non-HTTPS and private-network download destinations."""
        parsed = urlparse(download_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("ChatGPT file download URL must be an HTTPS URL without embedded credentials")

        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise ValueError("ChatGPT file download host could not be resolved") from exc
        if not addresses:
            raise ValueError("ChatGPT file download host did not resolve to an address")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ValueError("ChatGPT file download host resolved to an invalid address") from exc
            if not ip.is_global:
                raise ValueError("ChatGPT file download URL must resolve only to public addresses")

    @classmethod
    def _remaining_download_time(cls, deadline: float) -> float:
        """Returns remaining wall-clock transfer time or raises on expiry."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"ChatGPT file download exceeded {cls._TOTAL_TIMEOUT_SECONDS:g} seconds")
        return remaining

    @classmethod
    def _download_to(cls, source: OpenAIFile, destination: Path) -> tuple[int, str]:
        """Downloads one temporary ChatGPT file URL with bounded redirects, size, and wall time."""
        current_url = source.download_url
        bytes_written = 0
        digest = hashlib.sha256()
        deadline = time.monotonic() + cls._TOTAL_TIMEOUT_SECONDS

        with requests.Session() as session:
            for redirect_index in range(cls._MAX_REDIRECTS + 1):
                cls._validate_download_url(current_url)
                remaining = cls._remaining_download_time(deadline)
                response = session.get(
                    current_url,
                    stream=True,
                    allow_redirects=False,
                    timeout=(
                        min(cls._CONNECT_TIMEOUT_SECONDS, remaining),
                        min(cls._READ_TIMEOUT_SECONDS, remaining),
                    ),
                )
                try:
                    if response.is_redirect or response.is_permanent_redirect:
                        if redirect_index >= cls._MAX_REDIRECTS:
                            raise ValueError("ChatGPT file download exceeded the redirect limit")
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("ChatGPT file download redirect did not include a destination")
                        current_url = urljoin(current_url, location)
                        continue

                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None and int(content_length) > _FILE_EXPORT_MAX_SIZE:
                        raise ValueError(f"ChatGPT file exceeds the {_FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB import limit")

                    with destination.open("wb") as output:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            cls._remaining_download_time(deadline)
                            if not chunk:
                                continue
                            bytes_written += len(chunk)
                            if bytes_written > _FILE_EXPORT_MAX_SIZE:
                                raise ValueError(f"ChatGPT file exceeds the {_FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB import limit")
                            output.write(chunk)
                            digest.update(chunk)
                    return bytes_written, digest.hexdigest()
                finally:
                    response.close()

        raise RuntimeError("ChatGPT file download did not produce a response")

    @staticmethod
    def _create_temporary_upload_path(destination: Path) -> Path:
        """Creates one sibling temporary file using normal project-file permissions."""
        for _ in range(100):
            candidate = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.serena-import"
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError:
                continue
            os.close(fd)
            return candidate
        raise RuntimeError("Could not allocate a temporary upload file")

    def apply(self, file: OpenAIFile, relative_path: str, overwrite: bool = False) -> str:
        """Uploads one ChatGPT file into the active project.

        :param file: ChatGPT file reference supplied by ``openai/fileParams``
        :param relative_path: destination path relative to the active project root
        :param overwrite: whether an existing destination file may be replaced
        :return: uploaded filename, byte count, and SHA-256 digest
        """
        source = OpenAIFile.model_validate(file)
        self.project.validate_relative_path(relative_path)

        root = Path(self.get_project_root()).resolve()
        destination = (root / relative_path).resolve(strict=False)
        if not destination.is_relative_to(root):
            raise ValueError("Destination must remain inside the active project")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Destination already exists: {relative_path}")
        if destination.exists() and not destination.is_file():
            raise ValueError(f"Destination is not a regular file: {relative_path}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        original_mode = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else None
        temporary_path = self._create_temporary_upload_path(destination)
        try:
            byte_count, sha256 = self._download_to(source, temporary_path)
            if destination.exists() and not overwrite:
                raise FileExistsError(f"Destination already exists: {relative_path}")
            if destination.exists() and original_mode is None:
                original_mode = stat.S_IMODE(destination.stat().st_mode)
            if original_mode is not None:
                temporary_path.chmod(original_mode)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)

        source_name = source.file_name or source.file_id
        return f"Uploaded {source_name} to {relative_path} ({byte_count} bytes, sha256={sha256})"


class FetchMediaFileTool(_McpMediaTool):
    """Returns native project image/audio media. In ChatGPT, show returned images inline when relevant to the user's request."""

    _MAX_FILE_SIZE = 25 * 1024 * 1024

    def apply(self, relative_path: str) -> _NativeMediaResult:
        """Returns one project media file as native content backed by a temporary snapshot.

        :param relative_path: project-relative path to the media file
        :return: native media with a transferable snapshot link
        """
        self.project.validate_relative_path(relative_path)
        path = Path(self.get_project_root(), relative_path)
        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {relative_path}")
        if path.stat().st_size > self._MAX_FILE_SIZE:
            raise ValueError(f"Media file exceeds the {self._MAX_FILE_SIZE // (1024 * 1024)} MiB size limit")

        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type is not None:
            media_type, _, media_format = mime_type.partition("/")
            if media_type in {"image", "audio"}:
                snapshot = _TemporaryFileStore.snapshot(
                    path,
                    display_name=path.name,
                    description=f"Media exported from Serena project {self.project.project_name}",
                    max_size=self._MAX_FILE_SIZE,
                )
                if media_type == "image":
                    return _NativeMediaResult(media=Image(path=snapshot.path, format=media_format), file_link=snapshot.link)
                return _NativeMediaResult(media=Audio(path=snapshot.path, format=media_format), file_link=snapshot.link)
        raise ValueError("fetch_media_file only accepts image or audio files; use download_file for other files")


class RenderPdfPageTool(_McpMediaTool):
    """Renders one PDF page as native image media. In ChatGPT, show the rendered page inline when relevant to the user's request."""

    _MIN_DPI = 72
    _MAX_DPI = 300
    _RENDER_TIMEOUT_SECONDS = 30.0
    _MAX_RENDERED_FILE_SIZE = 25 * 1024 * 1024

    def apply(self, relative_path: str, page: int, dpi: int = 150) -> _NativeMediaResult:
        """Renders one PDF page into a temporary transferable snapshot.

        :param relative_path: project-relative path to the PDF file
        :param page: 1-based page number to render
        :param dpi: rendering resolution in dots per inch, from 72 through 300
        :return: rendered PNG as native media with a transferable snapshot link
        """
        self.project.validate_relative_path(relative_path)
        path = Path(self.get_project_root(), relative_path)
        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {relative_path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError("render_pdf_page only accepts PDF files")
        if page < 1:
            raise ValueError("page must be a 1-based positive integer")
        if not self._MIN_DPI <= dpi <= self._MAX_DPI:
            raise ValueError(f"dpi must be between {self._MIN_DPI} and {self._MAX_DPI}")

        renderer = shutil.which("pdftoppm")
        if renderer is None:
            raise RuntimeError("PDF rendering requires 'pdftoppm' (Poppler) to be installed")

        with tempfile.TemporaryDirectory(prefix="serena-pdf-") as tmp_dir:
            output_prefix = Path(tmp_dir, "page")
            try:
                result = subprocess.run(
                    [
                        renderer,
                        "-f",
                        str(page),
                        "-l",
                        str(page),
                        "-singlefile",
                        "-png",
                        "-r",
                        str(dpi),
                        str(path),
                        str(output_prefix),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self._RENDER_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"PDF page rendering exceeded {self._RENDER_TIMEOUT_SECONDS:g} seconds") from exc

            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "unknown renderer error"
                raise RuntimeError(f"Failed to render PDF page: {detail}")

            temporary_output = output_prefix.with_suffix(".png")
            if not temporary_output.is_file():
                raise RuntimeError(f"PDF page {page} does not exist or could not be rendered")

            display_name = f"{path.stem}-p{page}-{dpi}dpi.png"
            snapshot = _TemporaryFileStore.snapshot(
                temporary_output,
                display_name=display_name,
                description=f"PDF page rendered from Serena project {self.project.project_name}",
                max_size=self._MAX_RENDERED_FILE_SIZE,
            )

        return _NativeMediaResult(media=Image(path=snapshot.path, format="png"), file_link=snapshot.link)
