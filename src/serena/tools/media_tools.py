"""Tools for returning project media through MCP-native content blocks."""

import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import Audio, Image
from mcp.types import CallToolResult

from serena.tools.tools_base import Tool

MEDIA_VIEWER_URI = "ui://serena/media-viewer"
MEDIA_VIEWER_MIME_TYPE = "text/html;profile=mcp-app"
MEDIA_VIEWER_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html,body{margin:0;padding:0;background:transparent}body{font-family:system-ui,sans-serif}
#root{display:flex;justify-content:center;align-items:center;min-height:24px}
img{display:block;max-width:100%;height:auto}audio{width:min(100%,720px)}
</style>
</head>
<body>
<div id="root"></div>
<script>
(() => {
  const root = document.getElementById("root");
  let lastData = null;

  function render(media) {
    if (!media || !media.data || media.data === lastData) return;
    lastData = media.data;
    root.replaceChildren();
    const url = `data:${media.mimeType};base64,${media.data}`;
    if (media.type === "image") {
      const image = document.createElement("img");
      image.src = url;
      image.alt = "Serena media preview";
      root.appendChild(image);
    } else if (media.type === "audio") {
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.src = url;
      root.appendChild(audio);
    }
  }

  function readOpenAI() {
    render(window.openai?.toolResponseMetadata?.["serena/media"]);
  }

  window.addEventListener("openai:set_globals", readOpenAI);
  window.addEventListener("message", (event) => {
    const message = event.data;
    if (message?.method === "ui/notifications/tool-result") {
      const direct = message.params?.content?.find((item) => item?.type === "image" || item?.type === "audio");
      if (direct) render(direct);
      else render(message.params?._meta?.["serena/media"]);
    }
  });

  readOpenAI();
  const timer = setInterval(readOpenAI, 100);
  setTimeout(() => clearInterval(timer), 10000);
})();
</script>
</body>
</html>"""


class _McpMediaTool(Tool):
    """Base for tools whose results are MCP-native unstructured media content."""

    @classmethod
    def get_apply_fn_metadata_from_cls(cls, structured_output: bool | None = None):
        # MCP media helpers are content blocks, not JSON-structured outputs
        return super().get_apply_fn_metadata_from_cls(structured_output=False)

    @classmethod
    def get_mcp_tool_meta(cls) -> dict[str, object]:
        """Returns ChatGPT/MCP Apps metadata for the shared media viewer."""
        return {
            "ui": {"resourceUri": MEDIA_VIEWER_URI},
            "openai/outputTemplate": MEDIA_VIEWER_URI,
        }

    def prepare_mcp_result(self, result: object) -> CallToolResult:
        """Wraps native media with widget-only preview metadata for ChatGPT."""
        if isinstance(result, Image):
            content = result.to_image_content()
        elif isinstance(result, Audio):
            content = result.to_audio_content()
        else:
            raise TypeError(f"Unexpected MCP media result: {type(result).__name__}")

        return CallToolResult.model_validate(
            {
                "content": [content],
                "_meta": {
                    "serena/media": {
                        "type": content.type,
                        "data": content.data,
                        "mimeType": content.mimeType,
                    }
                },
            }
        )


class FetchMediaFileTool(_McpMediaTool):
    """Returns an image or audio file from the active project as native MCP media."""

    _MAX_FILE_SIZE = 25 * 1024 * 1024

    def apply(self, relative_path: str) -> Image | Audio:
        """Returns an image or audio file as native MCP media content.

        :param relative_path: project-relative path to the media file
        :return: the media file as an MCP-native image or audio content block
        """
        self.project.validate_relative_path(relative_path)
        path = Path(self.get_project_root(), relative_path)
        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {relative_path}")
        if path.stat().st_size > self._MAX_FILE_SIZE:
            raise ValueError(f"Media file exceeds the {self._MAX_FILE_SIZE // (1024 * 1024)} MiB size limit")

        # determine the MCP media helper from the MIME type
        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type is None:
            raise ValueError(f"Could not determine media type from file extension: {relative_path}")
        if mime_type.startswith("image/"):
            return Image(path=path)
        if mime_type.startswith("audio/"):
            return Audio(path=path)
        raise ValueError(f"Unsupported media type '{mime_type}'; expected an image or audio file")


class RenderPdfPageTool(_McpMediaTool):
    """Renders one page of a project PDF and returns it as a native MCP image."""

    _MIN_DPI = 72
    _MAX_DPI = 300

    def apply(self, relative_path: str, page: int, dpi: int = 150) -> Image:
        """Renders one PDF page to PNG for visual inspection.

        :param relative_path: project-relative path to the PDF file
        :param page: 1-based page number to render
        :param dpi: rendering resolution in dots per inch, from 72 through 300
        :return: the rendered page as an MCP-native image content block
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

            output_path = output_prefix.with_suffix(".png")
            if not output_path.is_file():
                raise RuntimeError(f"PDF page {page} does not exist or could not be rendered")
            return Image(data=output_path.read_bytes(), format="png")
