"""Behavioural tests for complete pattern-search results."""

from unittest.mock import MagicMock

from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.tools.file_tools import SearchForPatternTool


def _tool(tmp_path) -> SearchForPatternTool:
    project = Project.load(str(tmp_path), serena_config=SerenaConfig(web_dashboard=False))
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    return SearchForPatternTool(agent)


def test_search_for_pattern_returns_complete_matches(tmp_path) -> None:
    lines = [f"MATCHME item number {i:04d} " + "payload " * 12 for i in range(60)]
    (tmp_path / "data.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    tool = _tool(tmp_path)

    result = tool.apply(
        substring_pattern="MATCHME",
        restrict_search_to_code_files=False,
    )

    assert len(result["data.txt"]) == 60
    assert "MATCHME item number 0000" in result["data.txt"][0]
    assert "MATCHME item number 0059" in result["data.txt"][-1]


def test_search_for_pattern_preserves_complete_requested_context(tmp_path) -> None:
    (tmp_path / "data.txt").write_text("above\nMATCHME target\nbelow\n", encoding="utf-8")
    tool = _tool(tmp_path)

    result = tool.apply(
        substring_pattern="MATCHME",
        context_lines_before=1,
        context_lines_after=1,
        restrict_search_to_code_files=False,
    )

    assert result == {"data.txt": ["...   0:above\n  >   1:MATCHME target\n...   2:below"]}


def test_search_deduplicates_multiple_matches_on_one_source_line(tmp_path) -> None:
    (tmp_path / "data.txt").write_text("MATCHME and MATCHME on one line\n", encoding="utf-8")
    tool = _tool(tmp_path)

    result = tool.apply(
        substring_pattern="MATCHME",
        restrict_search_to_code_files=False,
    )

    assert len(result["data.txt"]) == 1
