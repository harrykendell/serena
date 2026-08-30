"""Tools for returning project files and media through MCP-native content blocks."""

import base64
import hashlib
import ipaddress
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from mcp.server.fastmcp import Audio, FastMCP, Image
from mcp.types import CallToolResult, ContentBlock, ResourceLink, TextContent
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


class _ProjectFileExport:
    """Creates and resolves private MCP links for project files."""

    @classmethod
    def _file_metadata(cls, project, relative_path: str) -> tuple[Path, str, int]:
        """Validates one project file and returns its path, MIME type, and size."""
        project.validate_relative_path(relative_path)
        path = Path(project.project_root, relative_path)
        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {relative_path}")

        size = path.stat().st_size
        if size > _FILE_EXPORT_MAX_SIZE:
            raise ValueError(f"File exceeds the {_FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB export limit")

        mime_type, _ = mimetypes.guess_type(path.name)
        return path, mime_type or "application/octet-stream", size

    @classmethod
    def create_link(cls, project, relative_path: str) -> ResourceLink:
        """Creates the private MCP resource link retained for dashboard/media handling."""
        path, mime_type, size = cls._file_metadata(project, relative_path)
        payload = json.dumps(
            {"project": project.project_name, "path": relative_path},
            separators=(",", ":"),
        ).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

        return ResourceLink(
            type="resource_link",
            name=path.name,
            uri=AnyUrl(f"serena-file://export/{token}"),
            mimeType=mime_type,
            size=size,
            description=f"File exported from Serena project {project.project_name}",
        )

    @classmethod
    def _resolve_registered_path(cls, project_name: str, relative_path: str) -> Path:
        """Resolves a registered project file while rechecking confinement and size."""
        from serena.config.serena_config import SerenaConfig

        config = SerenaConfig.from_config_file()
        registered_project = config.get_registered_project(project_name)
        if registered_project is None:
            raise FileNotFoundError(f"Serena project is no longer registered: {project_name}")

        project = registered_project.get_project_instance(config)
        project.validate_relative_path(relative_path)
        path = Path(project.project_root, relative_path)
        if not path.is_file():
            raise FileNotFoundError(f"File no longer exists: {relative_path}")
        if path.stat().st_size > _FILE_EXPORT_MAX_SIZE:
            raise ValueError(f"File exceeds the {_FILE_EXPORT_MAX_SIZE // (1024 * 1024)} MiB export limit")
        return path

    @classmethod
    def read(cls, token: str) -> bytes:
        """Reads one legacy/private MCP resource token."""
        padding = "=" * (-len(token) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(token + padding).decode("utf-8"))
            project_name = payload["project"]
            relative_path = payload["path"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid Serena file resource") from exc

        return cls._resolve_registered_path(project_name, relative_path).read_bytes()


def register_file_export_resource(mcp: FastMCP) -> None:
    """Registers the lazy binary resource backing exported project files."""

    @mcp.resource(
        _FILE_RESOURCE_URI_TEMPLATE,
        name="Serena exported project file",
        description="Reads a file previously exported from a registered Serena project.",
        mime_type="application/octet-stream",
    )
    def read_exported_file(token: str) -> bytes:
        return _ProjectFileExport.read(token)


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
    return _ProjectFileExport.read(uri.removeprefix(prefix))


class _McpMediaTool(Tool, ToolMarkerOptional):
    """Base for tools returning native media while retaining an internal dashboard file reference."""

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

        # prepare the native media block and an invocation-specific model hint for images
        media = result.media
        if isinstance(media, Image):
            media_content = media.to_image_content()
            display_hint = TextContent(
                type="text",
                text=(
                    "If showing this image is relevant to the user's request, embed the materialized file in the assistant "
                    "response using normal Markdown image syntax."
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
        return _ProjectFileExport.create_link(self.project, relative_path)

    def prepare_mcp_result(self, result: object) -> CallToolResult:
        """Returns the exported project file as a standard MCP resource link."""
        if not isinstance(result, ResourceLink):
            raise TypeError(f"Unexpected exported file result: {type(result).__name__}")

        return CallToolResult(content=[result])


class UploadFileTool(Tool, ToolMarkerCanEdit, ToolMarkerOptional):
    """Uploads one ChatGPT file into the active Serena project."""

    _MAX_REDIRECTS = 4

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
    def _download_to(cls, source: OpenAIFile, destination: Path) -> tuple[int, str]:
        """Downloads one temporary ChatGPT file URL to ``destination`` with bounded redirects and size."""
        current_url = source.download_url
        bytes_written = 0
        digest = hashlib.sha256()

        with requests.Session() as session:
            for redirect_index in range(cls._MAX_REDIRECTS + 1):
                cls._validate_download_url(current_url)
                response = session.get(current_url, stream=True, allow_redirects=False, timeout=(10, 60))
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
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".serena-import",
            dir=destination.parent,
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            byte_count, sha256 = self._download_to(source, temporary_path)
            if destination.exists() and not overwrite:
                raise FileExistsError(f"Destination already exists: {relative_path}")
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)

        source_name = source.file_name or source.file_id
        return f"Uploaded {source_name} to {relative_path} ({byte_count} bytes, sha256={sha256})"


class FetchMediaFileTool(_McpMediaTool):
    """Returns project image/audio media for inspection. In ChatGPT, when an image is materialized and visually relevant, embed the materialized file in the assistant response using normal Markdown image syntax."""

    _MAX_FILE_SIZE = 25 * 1024 * 1024

    def apply(self, relative_path: str) -> _NativeMediaResult:
        """Returns native image/audio content for one project media file.

        In ChatGPT, if the host materializes an image file from this result and showing the image is relevant to the user's request, embed that materialized file in the assistant response with normal Markdown image syntax.

        :param relative_path: project-relative path to the media file
        :return: native media with an internal file reference used by Serena's dashboard
        """
        self.project.validate_relative_path(relative_path)
        path = Path(self.get_project_root(), relative_path)
        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {relative_path}")
        if path.stat().st_size > self._MAX_FILE_SIZE:
            raise ValueError(f"Media file exceeds the {self._MAX_FILE_SIZE // (1024 * 1024)} MiB size limit")

        # retain the original file reference internally for dashboard preview/download handling
        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type is not None:
            media_type, _, media_format = mime_type.partition("/")
            if media_type == "image":
                return _NativeMediaResult(
                    media=Image(path=path, format=media_format),
                    file_link=_ProjectFileExport.create_link(self.project, relative_path),
                )
            if media_type == "audio":
                return _NativeMediaResult(
                    media=Audio(path=path, format=media_format),
                    file_link=_ProjectFileExport.create_link(self.project, relative_path),
                )
        raise ValueError("fetch_media_file only accepts image or audio files; use download_file for other files")


class RenderPdfPageTool(_McpMediaTool):
    """Renders one PDF page for inspection. In ChatGPT, when the rendered PNG is materialized and visually relevant, embed it in the assistant response using normal Markdown image syntax."""

    _MIN_DPI = 72
    _MAX_DPI = 300

    def _get_cached_render(self, source_path: Path, relative_path: str, page: int, dpi: int) -> tuple[Path, str]:
        """Returns the stable cache path for a rendered source revision and page."""
        stat = source_path.stat()
        fingerprint_source = f"{relative_path}\0{stat.st_mtime_ns}\0{stat.st_size}\0{page}\0{dpi}".encode()
        fingerprint = hashlib.sha256(fingerprint_source).hexdigest()[:12]
        render_dir = Path(self.get_project_root(), ".serena", "chat_renders")
        render_dir.mkdir(parents=True, exist_ok=True)
        render_path = render_dir / f"{source_path.stem}-p{page}-{dpi}dpi-{fingerprint}.png"
        render_relative_path = render_path.relative_to(Path(self.get_project_root())).as_posix()
        return render_path, render_relative_path

    def apply(self, relative_path: str, page: int, dpi: int = 150) -> _NativeMediaResult:
        """Renders one PDF page to a native image.

        In ChatGPT, if the host materializes the rendered PNG and showing the page is relevant to the user's request, embed that materialized PNG in the assistant response with normal Markdown image syntax.

        :param relative_path: project-relative path to the PDF file
        :param page: 1-based page number to render
        :param dpi: rendering resolution in dots per inch, from 72 through 300
        :return: the rendered PNG as native media with an internal dashboard file reference
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

        # use Poppler when available, avoiding a Serena-side PDF dependency
        renderer = shutil.which("pdftoppm")
        if renderer is None:
            raise RuntimeError("PDF rendering requires 'pdftoppm' (Poppler) to be installed")

        # cache the rendered page inside the project so ChatGPT can fetch the same PNG as a file
        output_path, output_relative_path = self._get_cached_render(path, relative_path, page, dpi)
        if not output_path.is_file():
            with tempfile.TemporaryDirectory(prefix="serena-pdf-") as tmp_dir:
                output_prefix = Path(tmp_dir, "page")
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
                )
                if result.returncode != 0:
                    detail = result.stderr.strip() or result.stdout.strip() or "unknown renderer error"
                    raise RuntimeError(f"Failed to render PDF page: {detail}")

                temporary_output = output_prefix.with_suffix(".png")
                if not temporary_output.is_file():
                    raise RuntimeError(f"PDF page {page} does not exist or could not be rendered")
                output_path.write_bytes(temporary_output.read_bytes())

        return _NativeMediaResult(
            media=Image(path=output_path, format="png"),
            file_link=_ProjectFileExport.create_link(self.project, output_relative_path),
        )
