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


def test_existing_retained_output_metadata_survives_small_transitional_result(tmp_path: Path) -> None:
    presenter, store = _presenter(tmp_path)
    execution_id = "execution-legacy"
    complete_result = "full result " + "x" * 2_000
    output_id = store.retain("legacy_tool", complete_result, execution_id=execution_id)
    descriptor = store.describe_execution(execution_id)
    assert descriptor is not None

    presentation = presenter.present(
        "legacy bounded result",
        tool_name="legacy_tool",
        execution_id=execution_id,
        retained_output=descriptor,
    )

    assert presentation.transport_value == "legacy bounded result"
    assert presentation.persisted_serialization == '"legacy bounded result"'
    assert presentation.retained_output_id == output_id
    assert presentation.retained_output_chars == len(complete_result)


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


def test_adversarial_structured_values_never_exceed_any_positive_budget(tmp_path: Path) -> None:
    logical_result = {
        "unicode": "αβγδ🙂漢字" * 80,
        "deep": {"level": [{"next": {"payload": "line one\n" + "middle\n" * 120 + "line end\n"}}]},
        "records": [
            {
                "name_path": f"Thing{index}/run",
                "relative_path": f"src/pkg/example_{index}.py",
                "kind": "Method",
                "body": "def run():\n" + (f"    value_{index} += 1\n" * 80),
            }
            for index in range(24)
        ],
        "streams": {"stdout": "out\n" * 700, "stderr": "err\n" * 500},
    }
    before = json.dumps(logical_result, ensure_ascii=False, separators=(",", ":"))

    for budget in (1, 2, 3, 7, 16, 31, 64, 97, 128, 257, 511, 1024, 2048):
        presenter, _ = _presenter(tmp_path / str(budget), max_chars=budget)
        first = presenter.present(logical_result, tool_name="adversarial", execution_id=f"execution-{budget}")
        second = presenter.present(logical_result, tool_name="adversarial", execution_id=f"execution-repeat-{budget}")
        first_serialized = json.dumps(first.transport_value, ensure_ascii=False, separators=(",", ":"))
        first_comparable = dict(first.transport_value) if isinstance(first.transport_value, dict) else first.transport_value
        second_comparable = dict(second.transport_value) if isinstance(second.transport_value, dict) else second.transport_value
        if isinstance(first_comparable, dict):
            first_comparable.pop("output_id", None)
        if isinstance(second_comparable, dict):
            second_comparable.pop("output_id", None)

        assert len(first_serialized) <= budget
        assert first_comparable == second_comparable
        assert json.dumps(logical_result, ensure_ascii=False, separators=(",", ":")) == before


def test_structured_fitting_uses_available_budget_and_preserves_collection_breadth(tmp_path: Path) -> None:
    budget = 3_000
    presenter, _ = _presenter(tmp_path, max_chars=budget)
    logical_result = {
        "records": [
            {
                "name": f"symbol_{index}",
                "relative_path": f"src/example_{index}.py",
                "status": "ok",
                "body": "def example():\n" + (f"    return {index}\n" * 120),
            }
            for index in range(20)
        ],
        "count": 20,
    }

    presentation = presenter.present(logical_result, tool_name="wide_records", execution_id="execution-wide")
    serialized = json.dumps(presentation.transport_value, ensure_ascii=False, separators=(",", ":"))
    result = presentation.transport_value["result"]

    assert len(serialized) <= budget
    assert len(serialized) >= int(budget * 0.9)
    assert isinstance(result, dict)
    records = result["records"]
    assert isinstance(records, list)
    assert len(records) == 20
    assert [record["name"] for record in records] == [f"symbol_{index}" for index in range(20)]
    assert all("omitted" in record["body"] or "…" in record["body"] for record in records)


def test_repeated_verbose_fields_receive_fair_preview_space(tmp_path: Path) -> None:
    presenter, _ = _presenter(tmp_path, max_chars=1_800)
    logical_result = {
        "records": [{"name": f"record-{index}", "payload": f"record {index}\n" + ("payload line\n" * 80)} for index in range(10)]
    }

    presentation = presenter.present(logical_result, tool_name="fair_records", execution_id="execution-fair")
    result = presentation.transport_value["result"]
    records = result["records"]
    preview_lengths = [len(record["payload"]) for record in records]

    assert len(records) == 10
    assert min(preview_lengths) > 20
    assert max(preview_lengths) - min(preview_lengths) <= len("payload line\n") + 1


def test_structural_compaction_uses_explicit_omission_markers(tmp_path: Path) -> None:
    list_presenter, _ = _presenter(tmp_path / "list", max_chars=260)
    list_result = {"items": [{"id": index, "label": f"item-{index}"} for index in range(100)]}
    list_presentation = list_presenter.present(list_result, tool_name="many_items", execution_id="execution-list")
    compacted_items = list_presentation.transport_value["result"]["items"]

    assert any(isinstance(item, dict) and "_serena_omitted_items" in item for item in compacted_items)

    dict_presenter, _ = _presenter(tmp_path / "dict", max_chars=260)
    dict_result = {f"field_{index:03d}": index for index in range(100)}
    dict_presentation = dict_presenter.present(dict_result, tool_name="many_fields", execution_id="execution-dict")
    compacted_mapping = dict_presentation.transport_value["result"]

    assert any(key.endswith("_serena_omitted_fields") for key in compacted_mapping)


def test_json_looking_string_remains_text_and_unknown_objects_normalize_safely(tmp_path: Path) -> None:
    presenter, store = _presenter(tmp_path / "json-string", max_chars=320)
    logical_text = '{"records":[' + ",".join(f'{{"value":{index}}}' for index in range(200)) + "]}"

    presentation = presenter.present(logical_text, tool_name="json_looking_text", execution_id="execution-json-text")

    assert isinstance(presentation.transport_value["result"], str)
    assert presentation.retained_output_id is not None
    retained = store.read(presentation.retained_output_id, 0, len(logical_text))
    assert retained.content == logical_text

    class UnknownResult:
        def __str__(self) -> str:
            return "unknown-result:" + "x" * 500

    unknown_presenter, _ = _presenter(tmp_path / "unknown", max_chars=240)
    unknown = unknown_presenter.present(UnknownResult(), tool_name="unknown", execution_id="execution-unknown")

    assert isinstance(unknown.transport_value, dict)
    assert isinstance(unknown.transport_value["result"], str)
    assert len(json.dumps(unknown.transport_value, ensure_ascii=False, separators=(",", ":"))) <= 240
