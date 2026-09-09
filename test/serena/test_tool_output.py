"""Behaviour tests for retained oversized tool output."""

import json
import re
from unittest.mock import MagicMock

import pytest

from serena.agent import SerenaAgent
from serena.execution import bind_execution_id, reset_execution_id
from serena.tool_output import ToolOutputStore
from serena.tools.memory_tools import ReadMemoryTool
from serena.tools.output_tools import ReadToolOutputTool
from serena.tools.tools_base import Tool


class OverflowProbeTool(Tool):
    """Test-only tool exposing the normal result-length behaviour."""

    def apply(self, content: str, max_answer_chars: int) -> str:
        return self._limit_length(content, max_answer_chars)


def _agent_with_store(store: ToolOutputStore) -> MagicMock:
    agent = MagicMock()
    agent.tool_is_active.return_value = True
    agent.serena_config.default_max_tool_answer_tokens = 100
    agent.retain_tool_output.side_effect = store.retain
    agent.retain_tool_output_with_tail.side_effect = store.retain_with_tail
    agent.render_tool_output_tail.side_effect = store.render_tail
    agent.read_tool_output.side_effect = store.read
    return agent


def _output_id(response: str) -> str:
    match = re.search(r"Full output retained as ([0-9a-f]{32})", response)
    assert match is not None
    return match.group(1)


def test_overflow_returns_identified_tail_and_full_output_can_be_paged() -> None:
    store = ToolOutputStore()
    agent = _agent_with_store(store)
    overflow_tool = OverflowProbeTool(agent)
    read_tool = ReadToolOutputTool(agent)
    content = "start-" + "x" * 2_000 + "-useful-tail"

    try:
        response = overflow_tool.apply(content, max_answer_chars=500)
        output_id = _output_id(response)

        assert len(response) <= 500
        assert "-useful-tail" in response
        assert "Showing tail from character" in response
        assert "complete=false; truncated=true" in response
        assert f"read_tool_output(output_id='{output_id}'" in response

        first_page = json.loads(read_tool.apply(output_id=output_id, offset=0, max_chars=700))
        assert first_page["output_id"] == output_id
        assert first_page["offset"] == 0
        assert first_page["end_offset"] == 700
        assert first_page["next_offset"] == 700
        assert first_page["complete"] is False
        assert first_page["truncated"] is True
        assert first_page["is_open"] is False
        assert first_page["content"] == content[:700]

        full_page = json.loads(read_tool.apply(output_id=output_id, offset=0, max_chars=len(content)))
        assert full_page["complete"] is True
        assert full_page["truncated"] is False
    finally:
        store.close()


def test_implicit_budget_uses_approximate_tokens_with_canonical_retained_paging() -> None:
    store = ToolOutputStore()
    agent = _agent_with_store(store)
    overflow_tool = OverflowProbeTool(agent)
    content = "x" * 500

    try:
        retained_response = overflow_tool.apply(content, max_answer_chars=-1)
        assert "Full output retained as" in retained_response
        assert overflow_tool.apply(content, max_answer_chars=500) == content
    finally:
        store.close()


def test_read_memory_uses_retained_output_when_content_exceeds_budget() -> None:
    store = ToolOutputStore()
    agent = _agent_with_store(store)
    project = MagicMock()
    project.memory_manager.load_memory.return_value = "memory-start-" + "x" * 1_000 + "-memory-end"
    agent.get_active_project_or_raise.return_value = project
    tool = ReadMemoryTool(agent)

    try:
        response = tool.apply("large-memory")
        output_id = _output_id(response)
        retained = store.read(output_id, offset=0, max_chars=2_000)

        assert "Full output retained as" in response
        assert retained.complete is True
        assert retained.content.startswith("memory-start-")
        assert retained.content.endswith("-memory-end")
    finally:
        store.close()


def test_paging_uses_stable_output_id_after_later_tool_output() -> None:
    store = ToolOutputStore(max_records=4)
    agent = _agent_with_store(store)
    overflow_tool = OverflowProbeTool(agent)
    read_tool = ReadToolOutputTool(agent)
    first_content = "FIRST-" + "a" * 1_500 + "-FIRST-END"
    second_content = "SECOND-" + "b" * 1_500 + "-SECOND-END"

    try:
        first_id = _output_id(overflow_tool.apply(first_content, max_answer_chars=400))
        second_id = _output_id(overflow_tool.apply(second_content, max_answer_chars=400))
        assert first_id != second_id

        first_page = json.loads(read_tool.apply(output_id=first_id, offset=1_200, max_chars=400))
        second_page = json.loads(read_tool.apply(output_id=second_id, offset=1_200, max_chars=400))

        assert "FIRST" in first_page["content"]
        assert "SECOND" not in first_page["content"]
        assert "SECOND" in second_page["content"]
        assert "FIRST" not in second_page["content"]
    finally:
        store.close()


def test_expired_output_id_fails_instead_of_returning_a_different_result() -> None:
    store = ToolOutputStore(max_records=1)
    agent = _agent_with_store(store)
    overflow_tool = OverflowProbeTool(agent)
    read_tool = ReadToolOutputTool(agent)

    try:
        expired_id = _output_id(overflow_tool.apply("old-" + "x" * 1_000, max_answer_chars=350))
        _output_id(overflow_tool.apply("new-" + "y" * 1_000, max_answer_chars=350))

        with pytest.raises(ValueError, match="not available"):
            read_tool.apply(output_id=expired_id)
    finally:
        store.close()


def test_live_output_can_be_read_by_exact_execution_before_completion() -> None:
    store = ToolOutputStore(max_records=4)
    execution_id = "execution-17"

    try:
        with store.open("execute_shell_command", execution_id=execution_id) as writer:
            writer.write("first chunk\n")
            first_page = store.read_execution_tail(execution_id, max_chars=200)
            descriptor = store.describe_execution(execution_id)

            assert first_page is not None
            assert descriptor is not None
            assert first_page.output_id == writer.output_id
            assert first_page.content == "first chunk\n"
            assert first_page.complete is False
            assert first_page.truncated is False
            assert first_page.is_open is True
            assert descriptor.total_chars == len("first chunk\n")
            assert descriptor.is_open is True

            writer.write("second chunk\n")
            second_page = store.read_execution_tail(execution_id, max_chars=200)
            assert second_page is not None
            assert second_page.output_id == writer.output_id
            assert second_page.content == "first chunk\nsecond chunk\n"

        completed = store.describe_execution(execution_id)
        assert completed is not None
        assert completed.output_id == writer.output_id
        assert completed.is_open is False
    finally:
        store.close()


def test_agent_retained_outputs_correlate_with_current_execution() -> None:
    store = ToolOutputStore(max_records=4)
    agent = MagicMock(spec=SerenaAgent)
    agent._tool_output_store = store

    try:
        complete_token = bind_execution_id("execution-complete")
        try:
            complete_output_id = SerenaAgent.retain_tool_output(agent, "probe", "complete output")
        finally:
            reset_execution_id(complete_token)

        tail_token = bind_execution_id("execution-tail")
        try:
            SerenaAgent.retain_tool_output_with_tail(agent, "probe", "x" * 200, 80)
        finally:
            reset_execution_id(tail_token)

        complete_descriptor = store.describe_execution("execution-complete")
        tail_descriptor = store.describe_execution("execution-tail")
        assert complete_descriptor is not None
        assert complete_descriptor.output_id == complete_output_id
        assert tail_descriptor is not None
        assert tail_descriptor.total_chars == 200
    finally:
        store.close()
