"""Tools for recovering retained oversized tool results."""

import json

from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject, ToolMarkerOptional


class ReadToolOutputTool(Tool, ToolMarkerDoesNotRequireActiveProject, ToolMarkerOptional):
    """Reads one page from a retained oversized tool result."""

    _MAX_PAGE_CHARS = 20_000

    def apply(self, output_id: str, offset: int = 0, max_chars: int = 20_000) -> str:
        """Read an exact retained tool result by the identifier returned with its truncated tail.

        Always pass the ``output_id`` from the original truncated response. Do not substitute the most recent tool call: later
        tool executions do not change which retained result this identifier addresses.

        :param output_id: stable identifier printed in the original truncated tool response
        :param offset: zero-based character offset at which to start this page
        :param max_chars: maximum content characters to return, from 1 through 20000
        :return: JSON containing the exact output identifier, page range, completeness metadata, content, and cursors
        """
        if max_chars <= 0 or max_chars > self._MAX_PAGE_CHARS:
            raise ValueError(f"max_chars must be between 1 and {self._MAX_PAGE_CHARS}")

        page = self.agent.read_tool_output(output_id, offset, max_chars)
        return json.dumps(
            {
                "output_id": page.output_id,
                "tool_name": page.tool_name,
                "total_chars": page.total_chars,
                "offset": page.offset,
                "end_offset": page.end_offset,
                "previous_offset": max(0, page.offset - max_chars) if page.offset > 0 else None,
                "next_offset": page.next_offset,
                "complete": page.complete,
                "truncated": page.truncated,
                "is_open": page.is_open,
                "content": page.content,
            },
            ensure_ascii=False,
        )
