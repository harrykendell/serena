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
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from mcp.server.fastmcp import Audio, FastMCP, Image
from mcp.types import CallToolResult, ContentBlock, ResourceLink, TextContent
from pydantic import BaseModel, ConfigDict

from serena.errors import UserFacingError
from serena.file_snapshots import FILE_EXPORT_MAX_SIZE, FileSnapshotStore
from serena.tools.tools_base import Tool, ToolMarkerCanEdit

_FILE_RESOURCE_URI_TEMPLATE = "serena-file://export/{token}"


class OpenAIFile(BaseModel):
    """Represents one ChatGPT file passed through ``openai/fileParams``."""

    model_config = ConfigDict(extra="forbid")

    download_url: str
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


def register_file_export_resource(mcp: FastMCP) -> None:
    """Registers the binary resource used to read immutable file snapshots."""

    @mcp.resource(
        _FILE_RESOURCE_URI_TEMPLATE,
        name="Serena exported file snapshot",
        description="Reads an immutable file snapshot exported by Serena.",
        mime_type="application/octet-stream",
    )
    def read_exported_file(token: str) -> bytes:
        return FileSnapshotStore.read(token)


@dataclass(frozen=True, kw_only=True)
class _NativeMediaResult:
    """Pairs native MCP media with the same file as a transferable resource."""

    media: Image | Audio
    file_link: ResourceLink


def get_result_file_link(result: object) -> ResourceLink | None:
    """Returns the transferable project-file link carried by one media-tool result, if present."""
    if isinstance(result, ResourceLink):
        return result
    if isinstance(result, _NativeMediaResult):
        return result.file_link
    if isinstance(result, CallToolResult):
        return next((block for block in result.content if isinstance(block, ResourceLink)), None)
    return None


def read_result_file_link(link: ResourceLink) -> bytes:
    """Reads bytes from a Serena project-file resource link."""
    uri = str(link.uri)
    prefix = _FILE_RESOURCE_URI_TEMPLATE.split("{token}", 1)[0]
    if not uri.startswith(prefix):
        raise ValueError("Resource link is not a Serena exported project file")
    return FileSnapshotStore.read(uri.removeprefix(prefix))


class _McpMediaTool(Tool):
    """Base for tools returning native media for direct user-visible presentation."""

    @classmethod
    def get_apply_fn_metadata_from_cls(cls, structured_output: bool | None = None):
        # MCP media helpers are content blocks, not JSON-structured outputs
        return super().get_apply_fn_metadata_from_cls(structured_output=False)

    def prepare_mcp_result(self, result: object) -> CallToolResult:
        """Returns native media plus the same file as a transferable resource link."""
        if isinstance(result, ResourceLink):
            return CallToolResult(content=[result])
        if not isinstance(result, _NativeMediaResult):
            raise TypeError(f"Unexpected MCP media result: {type(result).__name__}")

        media = result.media
        if isinstance(media, Image):
            media_content = media.to_image_content()
            display_hint = TextContent(
                type="text",
                text=(
                    "Inspect the native image when it is relevant to the ongoing work, especially for scientific figures, "
                    "plots, diagnostics, and other useful visual results. If the user asked to view, render, show, or inspect "
                    "the image inline, you MUST embed the materialized file in the assistant response using normal Markdown "
                    "image syntax; do not rely on the MCP tool-result UI to display it."
                ),
            )
        elif isinstance(media, Audio):
            media_content = media.to_audio_content()
            display_hint = None
        else:
            raise TypeError(f"Unexpected MCP media result: {type(media).__name__}")

        content: list[ContentBlock] = [media_content, result.file_link]
        if display_hint is not None:
            content.append(display_hint)
        return CallToolResult(content=content)


class DownloadFileTool(Tool):
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
        try:
            return FileSnapshotStore.snapshot_project_file(self.project, relative_path).link
        except OSError as error:
            raise UserFacingError(f"Could not export file: {error.strerror or error}") from None

    def prepare_mcp_result(self, result: object) -> CallToolResult:
        """Returns the exported project file as a standard MCP resource link."""
        if not isinstance(result, ResourceLink):
            raise TypeError(f"Unexpected exported file result: {type(result).__name__}")

        return CallToolResult(content=[result])


class UploadFileTool(Tool, ToolMarkerCanEdit):
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
            raise UserFacingError("ChatGPT file download URL must be HTTPS and contain no embedded credentials")

        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
        except socket.gaierror:
            raise UserFacingError("ChatGPT file download host could not be resolved") from None
        if not addresses:
            raise UserFacingError("ChatGPT file download host did not resolve to an address")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise UserFacingError("ChatGPT file download host resolved to an invalid address") from None
            if not ip.is_global:
                raise UserFacingError("ChatGPT file download URL must resolve only to public addresses")

    @classmethod
    def _remaining_download_time(cls, deadline: float) -> float:
        """Returns remaining wall-clock transfer time or raises on expiry."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UserFacingError(f"ChatGPT file download exceeded {cls._TOTAL_TIMEOUT_SECONDS:g} seconds")
        return remaining

    @staticmethod
    def _download_failure_message(error: requests.RequestException) -> str:
        """Returns a concise transfer failure without echoing the source URL."""
        if isinstance(error, requests.Timeout):
            return "ChatGPT file download request timed out"
        if isinstance(error, requests.HTTPError):
            response = error.response
            if response is not None:
                return f"ChatGPT file download failed with HTTP {response.status_code}"
            return "ChatGPT file download failed with an HTTP error"
        if isinstance(error, requests.ConnectionError):
            return "ChatGPT file download connection failed"
        return "ChatGPT file download failed"

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
                try:
                    response = session.get(
                        current_url,
                        stream=True,
                        allow_redirects=False,
                        timeout=(
                            min(cls._CONNECT_TIMEOUT_SECONDS, remaining),
                            min(cls._READ_TIMEOUT_SECONDS, remaining),
                        ),
                    )
                except requests.RequestException as error:
                    raise UserFacingError(cls._download_failure_message(error)) from None
                try:
                    if response.is_redirect or response.is_permanent_redirect:
                        if redirect_index >= cls._MAX_REDIRECTS:
                            raise UserFacingError("ChatGPT file download exceeded the redirect limit")
                        location = response.headers.get("Location")
                        if not location:
                            raise UserFacingError("ChatGPT file download redirect did not include a destination")
                        current_url = urljoin(current_url, location)
                        continue

                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            announced_size = int(content_length)
                        except ValueError:
                            raise UserFacingError("ChatGPT file download returned an invalid Content-Length") from None
                        if announced_size > FILE_EXPORT_MAX_SIZE:
                            raise UserFacingError(f"ChatGPT file exceeds the {FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB import limit")

                    with destination.open("wb") as output:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            cls._remaining_download_time(deadline)
                            if not chunk:
                                continue
                            bytes_written += len(chunk)
                            if bytes_written > FILE_EXPORT_MAX_SIZE:
                                raise UserFacingError(f"ChatGPT file exceeds the {FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB import limit")
                            output.write(chunk)
                            digest.update(chunk)
                    return bytes_written, digest.hexdigest()
                except requests.RequestException as error:
                    raise UserFacingError(cls._download_failure_message(error)) from None
                finally:
                    response.close()

        raise UserFacingError("ChatGPT file download did not produce a response")

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
        :return: destination identity and immutable source snapshot reference
        """
        source = OpenAIFile.model_validate(file)
        self.project.validate_relative_path(relative_path)
        source_name = source.file_name or source.file_id

        root = Path(self.get_project_root()).resolve()
        destination = (root / relative_path).resolve(strict=False)
        if not destination.is_relative_to(root):
            raise UserFacingError("Destination must remain inside the active project")
        if destination.exists() and not overwrite:
            raise UserFacingError(f"Destination already exists: {relative_path}")
        if destination.exists() and not destination.is_file():
            raise UserFacingError(f"Destination is not a regular file: {relative_path}")

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            original_mode = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else None
            temporary_path = self._create_temporary_upload_path(destination)
        except OSError as error:
            raise UserFacingError(f"Could not prepare upload destination: {error.strerror or error}") from None

        try:
            self._download_to(source, temporary_path)
            source_snapshot = FileSnapshotStore.snapshot(
                temporary_path,
                display_name=source_name,
                description="Immutable ChatGPT upload snapshot retained by Serena",
            )
            if destination.exists() and not overwrite:
                raise UserFacingError(f"Destination already exists: {relative_path}")
            if destination.exists() and original_mode is None:
                original_mode = stat.S_IMODE(destination.stat().st_mode)
            if original_mode is not None:
                temporary_path.chmod(original_mode)
            os.replace(temporary_path, destination)
        except OSError as error:
            raise UserFacingError(f"Could not store uploaded file: {error.strerror or error}") from None
        finally:
            temporary_path.unlink(missing_ok=True)

        return f"OK; uploaded={relative_path}; source_snapshot={source_snapshot.link.uri}"


class FetchMediaFileTool(_McpMediaTool):
    """Returns native project image/audio media.

    In ChatGPT, normally show scientific figures, plots, diagnostics, and other images relevant to the ongoing work inline so the user can inspect them too.
    """

    _MAX_FILE_SIZE = 25 * 1024 * 1024

    def apply(self, relative_path: str) -> _NativeMediaResult:
        """Returns one project image or audio file as native MCP media for inline presentation.

        Use :class:`DownloadFileTool` separately when the original file is also needed as a download.

        :param relative_path: project-relative path to the media file
        :return: native media prepared for direct user-visible presentation
        """
        self.project.validate_relative_path(relative_path)
        path = Path(self.get_project_root(), relative_path)
        if not path.is_file():
            raise UserFacingError(f"File does not exist: {relative_path}")
        try:
            if path.stat().st_size > self._MAX_FILE_SIZE:
                raise UserFacingError(f"Media file exceeds the {self._MAX_FILE_SIZE // (1024 * 1024)} MiB size limit")

            mime_type, _ = mimetypes.guess_type(path.name)
            if mime_type is not None:
                media_type, _, media_format = mime_type.partition("/")
                if media_type in {"image", "audio"}:
                    snapshot = FileSnapshotStore.snapshot(
                        path,
                        display_name=path.name,
                        description=f"Media exported from Serena project {self.project.project_name}",
                        max_size=self._MAX_FILE_SIZE,
                        version_display_name_by_content=True,
                    )
                    if media_type == "image":
                        return _NativeMediaResult(media=Image(path=snapshot.path, format=media_format), file_link=snapshot.link)
                    return _NativeMediaResult(media=Audio(path=snapshot.path, format=media_format), file_link=snapshot.link)
        except OSError as error:
            raise UserFacingError(f"Could not read media file: {error.strerror or error}") from None
        raise UserFacingError("fetch_media_file only accepts image or audio files; use download_file for other files")


class RenderPdfPageTool(_McpMediaTool):
    """Renders one PDF page as native image media.

    In ChatGPT, normally show rendered pages containing scientific figures, plots, diagrams, diagnostics, or other visual results relevant to the ongoing work inline so the user can inspect them too.
    """

    _MIN_DPI = 72
    _MAX_DPI = 300
    _RENDER_TIMEOUT_SECONDS = 30.0
    _MAX_RENDERED_FILE_SIZE = 25 * 1024 * 1024

    def apply(self, relative_path: str, page: int, dpi: int = 150) -> _NativeMediaResult:
        """Renders one PDF page as native MCP image media for inline presentation.

        Use :class:`DownloadFileTool` separately when the source PDF is also needed as a download.

        :param relative_path: project-relative path to the PDF file
        :param page: 1-based page number to render
        :param dpi: rendering resolution in dots per inch, from 72 through 300
        :return: rendered PNG prepared for direct user-visible presentation
        """
        self.project.validate_relative_path(relative_path)
        path = Path(self.get_project_root(), relative_path)
        if not path.is_file():
            raise UserFacingError(f"File does not exist: {relative_path}")
        if path.suffix.lower() != ".pdf":
            raise UserFacingError("render_pdf_page only accepts PDF files")
        if page < 1:
            raise UserFacingError("page must be a 1-based positive integer")
        if not self._MIN_DPI <= dpi <= self._MAX_DPI:
            raise UserFacingError(f"dpi must be between {self._MIN_DPI} and {self._MAX_DPI}")

        renderer = shutil.which("pdftoppm")
        if renderer is None:
            raise UserFacingError("PDF rendering requires 'pdftoppm' (Poppler) to be installed")

        try:
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
                except subprocess.TimeoutExpired:
                    raise UserFacingError(f"PDF page rendering exceeded {self._RENDER_TIMEOUT_SECONDS:g} seconds") from None
                except OSError as error:
                    raise UserFacingError(f"Could not run PDF renderer: {error.strerror or error}") from None

                if result.returncode != 0:
                    detail = result.stderr.strip() or result.stdout.strip() or "unknown renderer error"
                    raise UserFacingError(f"Failed to render PDF page: {detail}")

                temporary_output = output_prefix.with_suffix(".png")
                if not temporary_output.is_file():
                    raise UserFacingError(f"PDF page {page} does not exist or could not be rendered")

                display_name = f"{path.stem}-p{page}-{dpi}dpi.png"
                snapshot = FileSnapshotStore.snapshot(
                    temporary_output,
                    display_name=display_name,
                    description=f"PDF page rendered from Serena project {self.project.project_name}",
                    max_size=self._MAX_RENDERED_FILE_SIZE,
                    version_display_name_by_content=True,
                )
        except OSError as error:
            raise UserFacingError(f"Could not render PDF page: {error.strerror or error}") from None

        return _NativeMediaResult(media=Image(path=snapshot.path, format="png"), file_link=snapshot.link)
