import base64
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import Image
from mcp.types import CallToolResult

from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.tools import FetchMediaFileTool, RenderPdfPageTool
from serena.tools.media_tools import MEDIA_VIEWER_URI

_ONE_PIXEL_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def _make_tool(tool_cls, project: Project):
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    return tool_cls(agent)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))


def _write_minimal_pdf(path: Path) -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode())
        data.extend(body)
        data.extend(b"\nendobj\n")
    xref_offset = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    path.write_bytes(data)


def test_fetch_media_file_returns_native_mcp_image(project: Project, tmp_path: Path) -> None:
    (tmp_path / "pixel.png").write_bytes(_ONE_PIXEL_PNG)

    result = _make_tool(FetchMediaFileTool, project).apply("pixel.png")

    assert isinstance(result, Image)


def test_fetch_media_file_preserves_svg_mime_type(project: Project, tmp_path: Path) -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><circle cx=".5" cy=".5" r=".5"/></svg>'
    (tmp_path / "icon.svg").write_bytes(svg)
    tool = _make_tool(FetchMediaFileTool, project)

    result = tool.prepare_mcp_result(tool.apply("icon.svg"))

    assert isinstance(result, CallToolResult)
    assert result.content[0].type == "image"
    assert result.content[0].mimeType == "image/svg+xml"
    assert base64.b64decode(result.content[0].data) == svg
    assert result.meta is not None
    assert result.meta["serena/media"]["mimeType"] == "image/svg+xml"


def test_media_tool_prepares_chatgpt_preview_result(project: Project, tmp_path: Path) -> None:
    (tmp_path / "pixel.png").write_bytes(_ONE_PIXEL_PNG)
    tool = _make_tool(FetchMediaFileTool, project)

    result = tool.prepare_mcp_result(tool.apply("pixel.png"))

    assert isinstance(result, CallToolResult)
    assert result.content[0].type == "image"
    assert result.meta is not None
    assert result.meta["serena/media"]["mimeType"] == "image/png"
    assert tool.get_mcp_tool_meta() == {
        "ui": {"resourceUri": MEDIA_VIEWER_URI},
        "openai/outputTemplate": MEDIA_VIEWER_URI,
    }


def test_fetch_media_file_rejects_non_media(project: Project, tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not media")

    with pytest.raises(ValueError, match="Unsupported media type"):
        _make_tool(FetchMediaFileTool, project).apply("notes.txt")


def test_fetch_media_file_rejects_path_outside_project(project: Project) -> None:
    with pytest.raises(ValueError):
        _make_tool(FetchMediaFileTool, project).apply("../outside.png")


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler is not installed")
def test_render_pdf_page_returns_native_mcp_image(project: Project, tmp_path: Path) -> None:
    _write_minimal_pdf(tmp_path / "one-page.pdf")

    result = _make_tool(RenderPdfPageTool, project).apply("one-page.pdf", page=1, dpi=72)

    assert isinstance(result, Image)


def test_render_pdf_page_bounds_resolution(project: Project, tmp_path: Path) -> None:
    _write_minimal_pdf(tmp_path / "one-page.pdf")

    with pytest.raises(ValueError, match="dpi must be between"):
        _make_tool(RenderPdfPageTool, project).apply("one-page.pdf", page=1, dpi=600)
