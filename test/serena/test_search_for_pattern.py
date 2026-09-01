"""Tests for the ``SearchForPatternTool`` overflow shortening chain.

The snippet stage and, in particular, its position in the shortening chain were
previously untested. Test contributed by @AmirF194 in review of PR #1667.
"""

import re
from unittest.mock import MagicMock

from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.tool_output import ToolOutputStore
from serena.tools.file_tools import SearchForPatternTool


def test_search_for_pattern_snippet_stage(tmp_path):
    lines: list[str] = []
    for i in range(60):
        lines += [
            "filler above",
            "filler above",
            f"MATCHME item number {i:04d} " + "payload " * 6,
            "filler below",
            "filler below",
        ]
    (tmp_path / "data.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    project = Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    tool = SearchForPatternTool(agent)

    def run(cap: int) -> str:
        return tool.apply(
            substring_pattern="MATCHME",
            context_lines_before=2,
            context_lines_after=2,
            restrict_search_to_code_files=False,
            max_answer_chars=cap,
        )

    # wide but overflowing cap: the snippet stage (line + matched text) is returned
    snippet = run(7000)
    assert "The answer is too long" in snippet
    assert '"text":' in snippet and "MATCHME item number 0000" in snippet
    assert "Match lines per file" not in snippet  # not the bare-line-numbers stage

    # tighter cap: the chain degrades past the snippet stage to bare line numbers
    bare = run(1000)
    assert "Match lines per file" in bare and '"text":' not in bare


def test_search_overflow_keeps_exact_result_recoverable(tmp_path) -> None:
    lines = [f"MATCHME item number {i:04d} " + "payload " * 12 for i in range(60)]
    (tmp_path / "data.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    project = Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    store = ToolOutputStore()
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    agent.tool_is_active.return_value = True
    agent.retain_tool_output.side_effect = store.retain
    agent.render_tool_output_tail.side_effect = store.render_tail
    tool = SearchForPatternTool(agent)

    try:
        response = tool.apply(
            substring_pattern="MATCHME",
            restrict_search_to_code_files=False,
            max_answer_chars=1_200,
        )
        match = re.search(r"Full output retained as ([0-9a-f]{32})", response)
        assert match is not None
        assert len(response) <= 1_200
        assert "complete=false; truncated=true" in response

        retained = store.read(match.group(1), offset=0, max_chars=100_000)
        assert retained.complete is True
        assert "MATCHME item number 0059" in retained.content
    finally:
        store.close()
