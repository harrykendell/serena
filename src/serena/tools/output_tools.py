"""Tools for recovering retained oversized tool results."""

import json

from serena.errors import UserFacingError
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject


class ReadToolOutputTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """Reads one page from a retained oversized tool result."""

    _MAX_PAGE_CHARS = 20_000

    def apply(self, output_id: str, offset: int = 0, max_chars: int = 20_000) -> str:
        """Reads one exact page from a retained oversized tool result.

        Always pass the ``output_id`` from the original truncated response. Later tool executions do not change which retained
        result this identifier addresses.

        :param output_id: stable identifier printed in the original truncated response
        :param offset: zero-based character offset at which to start this page
        :param max_chars: maximum content characters to return, from 1 through 20000
        :return: compact JSON containing exact paging state and content
        """
        if max_chars <= 0 or max_chars > self._MAX_PAGE_CHARS:
            raise UserFacingError(f"max_chars must be between 1 and {self._MAX_PAGE_CHARS}")

        page = self.agent.read_tool_output(output_id, offset, max_chars)
        return json.dumps(
            {
                "output_id": page.output_id,
                "total_chars": page.total_chars,
                "offset": page.offset,
                "end_offset": page.end_offset,
                "next_offset": page.next_offset,
                "complete": page.complete,
                "content": page.content,
            },
            ensure_ascii=False,
        )
