"""Behaviour tests for retained oversized tool output."""

from pathlib import Path
from unittest.mock import MagicMock

from serena.execution_store import ExecutionStore
from serena.result_presentation import ToolResultPresenter
from serena.tool_output import ToolOutputStore
from serena.tools.memory_tools import ReadMemoryTool
from serena.tools.output_tools import ReadToolOutputTool


def _agent_with_store(store: ToolOutputStore) -> MagicMock:
    agent = MagicMock()
    agent.tool_is_active.return_value = True
    agent.read_tool_output.side_effect = store.read
    return agent


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
    finally:
        store.close()


def test_paging_uses_stable_output_id_after_later_tool_output(tmp_path: Path) -> None:
    store = ToolOutputStore(root=tmp_path / "tool_outputs")
    presenter = ToolResultPresenter(store, max_chars=400)
    agent = _agent_with_store(store)
    read_tool = ReadToolOutputTool(agent)
    first_content = "FIRST-" + "a" * 1_500 + "-FIRST-END"
    second_content = "SECOND-" + "b" * 1_500 + "-SECOND-END"

    try:
        first_presentation = presenter.present(first_content)
        second_presentation = presenter.present(second_content)
        first_id = first_presentation.retained_output_id
        second_id = second_presentation.retained_output_id
        assert first_id is not None
        assert second_id is not None
        assert first_id != second_id

        first_page = read_tool.apply(output_id=first_id, offset=1_200, max_chars=400)
        second_page = read_tool.apply(output_id=second_id, offset=1_200, max_chars=400)

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
    output_id = store.retain(content)

    try:
        first_page = read_tool.apply(output_id=output_id, offset=1, max_chars=5)
        middle_offset = len(content) // 2 - 3
        middle_page = read_tool.apply(output_id=output_id, offset=middle_offset, max_chars=7)
        final_offset = len(content) - 4
        final_page = read_tool.apply(output_id=output_id, offset=final_offset, max_chars=10)

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
    output_id = store.retain("persistent-output")
    execution_store.finish_execution(
        "execution-a",
        succeeded=True,
        result="persistent-output",
        retained_output_id=output_id,
        retained_output_chars=len("persistent-output"),
    )
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
