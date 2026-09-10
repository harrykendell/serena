"""Behaviour tests for canonical successful-result presentation."""

import json
from pathlib import Path

from mcp.types import ResourceLink
from pydantic import AnyUrl

from serena.result_presentation import ToolResultPresenter
from serena.tool_output import ToolOutputStore


def _presenter(tmp_path: Path, *, max_chars: int = 500) -> tuple[ToolResultPresenter, ToolOutputStore]:
    store = ToolOutputStore(root=tmp_path / "tool_outputs")
    return ToolResultPresenter(store, max_chars=max_chars), store


def test_oversized_text_is_retained_exactly_and_presented_once(tmp_path: Path) -> None:
    presenter, store = _presenter(tmp_path)
    logical_result = "first line\n" + "middle line\n" * 200 + "last line\n"

    presentation = presenter.present(logical_result, tool_name="synthetic_text", execution_id="execution-text")

    assert isinstance(presentation.transport_value, dict)
    assert presentation.transport_value["truncated"] is True
    assert presentation.transport_value["total_chars"] == len(logical_result)
    assert presentation.transport_value["output_id"] == presentation.retained_output_id
    assert "first line" in presentation.transport_value["result"]
    assert "last line" in presentation.transport_value["result"]
    assert len(json.dumps(presentation.transport_value, ensure_ascii=False, separators=(",", ":"))) <= 500
    assert presentation.persisted_serialization == json.dumps(
        presentation.transport_value,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert presentation.retained_output_id is not None
    retained = store.read(presentation.retained_output_id, 0, len(logical_result))
    assert retained.content == logical_result
    assert retained.complete


def test_oversized_structured_result_stays_structured_and_retains_exact_serialization(tmp_path: Path) -> None:
    presenter, store = _presenter(tmp_path, max_chars=650)
    logical_result = {
        "records": [
            {
                "name": f"symbol_{index}",
                "relative_path": f"src/example_{index}.py",
                "body": "def example():\n" + (f"    return {index}\n" * 100),
            }
            for index in range(12)
        ],
        "count": 12,
    }
    exact_serialization = json.dumps(logical_result, ensure_ascii=False, separators=(",", ":"))

    presentation = presenter.present(logical_result, tool_name="synthetic_structured", execution_id="execution-structured")

    assert isinstance(presentation.transport_value, dict)
    assert presentation.transport_value["truncated"] is True
    assert presentation.transport_value["total_chars"] == len(exact_serialization)
    assert presentation.transport_value["output_id"] == presentation.retained_output_id
    assert isinstance(presentation.transport_value["result"], dict)
    assert len(json.dumps(presentation.transport_value, ensure_ascii=False, separators=(",", ":"))) <= 650
    assert presentation.persisted_serialization == json.dumps(
        presentation.transport_value,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert presentation.retained_output_id is not None
    retained = store.read(presentation.retained_output_id, 0, len(exact_serialization))
    assert retained.content == exact_serialization
    assert retained.complete


def test_small_ordinary_result_is_unchanged_and_not_retained(tmp_path: Path) -> None:
    presenter, _ = _presenter(tmp_path)
    logical_result = {"return_code": 0, "stdout": "done"}

    presentation = presenter.present(logical_result, tool_name="synthetic_small", execution_id="execution-small")

    assert presentation.transport_value == logical_result
    assert presentation.persisted_serialization == '{"return_code":0,"stdout":"done"}'
    assert presentation.retained_output_id is None
    assert presentation.retained_output_chars is None


def test_native_resource_result_bypasses_ordinary_presentation(tmp_path: Path) -> None:
    presenter, _ = _presenter(tmp_path)
    logical_result = ResourceLink(
        type="resource_link",
        name="figure.png",
        uri=AnyUrl("serena-file://export/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        mimeType="image/png",
        size=123,
    )

    presentation = presenter.present(logical_result, tool_name="synthetic_media", execution_id="execution-media")

    assert presentation.transport_value is logical_result
    assert presentation.persisted_serialization is None
    assert presentation.retained_output_id is None
    assert presentation.retained_output_chars is None
