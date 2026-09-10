"""Behaviour tests for retained oversized tool output."""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

from serena.agent import SerenaAgent
from serena.execution import bind_execution_id, reset_execution_id
from serena.execution_store import ExecutionStore
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
    match = re.search(r"output_id=([0-9a-f]{32})", response)
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
        assert response.startswith(f"truncated=true; total_chars={len(content)}; output_id={output_id}\nshown_range=")
        assert "read_tool_output" not in response

        first_page = json.loads(read_tool.apply(output_id=output_id, offset=0, max_chars=700))
        assert first_page == {
            "output_id": output_id,
            "total_chars": len(content),
            "offset": 0,
            "end_offset": 700,
            "next_offset": 700,
            "complete": False,
            "content": content[:700],
        }

        full_page = json.loads(read_tool.apply(output_id=output_id, offset=0, max_chars=len(content)))
        assert full_page["complete"] is True
        assert full_page["content"] == content
    finally:
        store.close()


def test_overflow_preserves_structured_json_for_model_consumption() -> None:
    store = ToolOutputStore()
    agent = _agent_with_store(store)
    overflow_tool = OverflowProbeTool(agent)
    content = json.dumps(
        [
            {
                "name_path": "Thing/run",
                "kind": "Method",
                "relative_path": "src/thing.py",
                "body_location": {"start_line": 10, "end_line": 900},
                "body": "def run():\n" + "    value += 1\n" * 800,
            }
        ]
    )

    try:
        response = overflow_tool.apply(content, max_answer_chars=700)
        payload = json.loads(response)
        result = payload["result"]

        assert payload["truncated"] is True
        assert payload["total_chars"] == len(content)
        assert isinstance(result, list)
        assert result[0]["name_path"] == "Thing/run"
        assert result[0]["kind"] == "Method"
        assert result[0]["relative_path"] == "src/thing.py"
        assert result[0]["body_location"] == {"start_line": 10, "end_line": 900}
        assert "chars omitted" in result[0]["body"]
        body_lines = result[0]["body"].splitlines()
        marker_index = next(index for index, line in enumerate(body_lines) if "chars omitted" in line)
        assert body_lines[marker_index - 1].strip() == "value += 1"
        assert body_lines[marker_index + 1].strip() == "value += 1"

        retained = store.read(payload["output_id"], offset=0, max_chars=len(content))
        assert retained.complete is True
        assert retained.content == content
    finally:
        store.close()


def test_implicit_budget_uses_approximate_tokens_with_canonical_retained_paging() -> None:
    store = ToolOutputStore()
    agent = _agent_with_store(store)
    overflow_tool = OverflowProbeTool(agent)
    content = "x" * 500

    try:
        retained_response = overflow_tool.apply(content, max_answer_chars=-1)
        assert "truncated=true; total_chars=" in retained_response
        assert overflow_tool.apply(content, max_answer_chars=500) == content
    finally:
        store.close()


def test_read_memory_returns_complete_content_without_tool_local_retention() -> None:
    store = ToolOutputStore()
    agent = _agent_with_store(store)
    project = MagicMock()
    content = "memory-start-" + "x" * 1_000 + "-memory-end"
    project.memory_manager.load_memory.return_value = content
    agent.get_active_project_or_raise.return_value = project
    tool = ReadMemoryTool(agent)

    try:
        response = tool.apply("large-memory")

        assert response == content
        agent.retain_tool_output.assert_not_called()
    finally:
        store.close()


def test_paging_uses_stable_output_id_after_later_tool_output() -> None:
    store = ToolOutputStore()
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


def test_unicode_paging_uses_character_offsets_and_lengths() -> None:
    store = ToolOutputStore()
    agent = _agent_with_store(store)
    read_tool = ReadToolOutputTool(agent)
    content = "A🙂漢字éβZ" * 80
    output_id = store.retain("unicode_probe", content)

    try:
        first_page = json.loads(read_tool.apply(output_id=output_id, offset=1, max_chars=5))
        middle_offset = len(content) // 2 - 3
        middle_page = json.loads(read_tool.apply(output_id=output_id, offset=middle_offset, max_chars=7))
        final_offset = len(content) - 4
        final_page = json.loads(read_tool.apply(output_id=output_id, offset=final_offset, max_chars=10))

        assert first_page["total_chars"] == len(content)
        assert first_page["content"] == content[1:6]
        assert middle_page["content"] == content[middle_offset : middle_offset + 7]
        assert final_page["content"] == content[final_offset:]
        assert final_page["end_offset"] == len(content)
        assert final_page["next_offset"] is None
    finally:
        store.close()


def test_retained_output_survives_store_restart(tmp_path: Path) -> None:
    execution_store = ExecutionStore(tmp_path / "execution-store")
    execution_store.start_execution(
        execution_id="execution-a",
        session_id="chat-a",
        project_name="project-a",
        tool_name="overflow_probe",
        arguments="{}",
    )
    store = ToolOutputStore(tmp_path / "tool-outputs", execution_store=execution_store)
    output_id = store.retain("overflow_probe", "persistent-output", execution_id="execution-a")
    execution_store.finish_execution("execution-a", succeeded=True, result="persistent-output")
    execution_store.set_retained_output("execution-a", output_id, len("persistent-output"))
    store.close()

    restored_execution_store = ExecutionStore(tmp_path / "execution-store")
    restored = ToolOutputStore(tmp_path / "tool-outputs", execution_store=restored_execution_store)
    try:
        execution = restored_execution_store.get_execution("execution-a")
        assert execution is not None
        assert execution.retained_output_id == output_id
        assert execution.retained_output_chars == len("persistent-output")

        page = restored.read(output_id, 0, 100)
        assert page.content == "persistent-output"
        assert page.complete
    finally:
        restored.close()


def test_live_output_can_be_read_by_exact_execution_before_completion() -> None:
    store = ToolOutputStore()
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
    store = ToolOutputStore()
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
