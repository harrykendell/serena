"""Canonical presentation of successful Serena tool results."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum

from mcp.types import CallToolResult, ResourceLink
from pydantic import BaseModel

from serena.structured_output import StructuredOutputCompactor
from serena.tool_output import ToolOutputDescriptor, ToolOutputStore


@dataclass(frozen=True)
class ToolResultPresentation:
    """One canonical transport and persistence representation of a tool result."""

    transport_value: object
    persisted_serialization: str | None
    retained_output_id: str | None = None
    retained_output_chars: int | None = None


class ToolResultPresenter:
    """Presents complete logical tool results within one canonical response budget."""

    def __init__(self, output_store: ToolOutputStore, max_chars: int):
        if max_chars <= 0:
            raise ValueError(f"Result presentation budget must be positive, got: {max_chars}")
        self._output_store = output_store
        self._max_chars = max_chars
        self._structured_compactor = StructuredOutputCompactor()

    def present(
        self,
        logical_result: object,
        *,
        tool_name: str,
        execution_id: str | None = None,
        retained_output: ToolOutputDescriptor | None = None,
    ) -> ToolResultPresentation:
        """Returns the canonical bounded presentation of one complete logical result."""
        # bypass native MCP media/resource results without reducing their content
        if self._is_native_result(logical_result):
            return ToolResultPresentation(transport_value=logical_result, persisted_serialization=None)

        # normalize ordinary values once before measuring or retaining them
        normalized = self._normalize(logical_result)
        complete_serialization = self._serialize_logical_result(normalized)
        if self._transport_length(normalized) <= self._max_chars:
            return ToolResultPresentation(
                transport_value=normalized,
                persisted_serialization=self._serialize_presentation(normalized),
                retained_output_id=retained_output.output_id if retained_output is not None else None,
                retained_output_chars=retained_output.total_chars if retained_output is not None else None,
            )

        # retain the exact complete logical serialization unless an earlier execution stage already owns it
        if retained_output is None:
            output_id = self._output_store.retain(tool_name, complete_serialization, execution_id=execution_id)
            total_chars = len(complete_serialization)
        else:
            output_id = retained_output.output_id
            total_chars = retained_output.total_chars

        # construct one canonical bounded envelope shared by transport and persistence
        if isinstance(normalized, str):
            transport_value = self._fit_text_envelope(normalized, output_id, total_chars)
        else:
            transport_value = self._fit_structured_envelope(normalized, output_id, total_chars)
        return ToolResultPresentation(
            transport_value=transport_value,
            persisted_serialization=self._serialize_presentation(transport_value),
            retained_output_id=output_id,
            retained_output_chars=total_chars,
        )

    def _fit_text_envelope(self, text: str, output_id: str, total_chars: int) -> object:
        """Fits one adaptive text preview inside the canonical truncation envelope."""
        metadata = self._truncation_metadata(output_id, total_chars)
        minimum = metadata | {"result": ""}
        if self._transport_length(minimum) > self._max_chars:
            return self._minimum_envelope(metadata)

        # search the raw preview budget while measuring the actual serialized envelope
        low = 0
        high = min(len(text), self._max_chars)
        best = ""
        while low <= high:
            preview_chars = (low + high) // 2
            preview = self._truncate_text(text, preview_chars)
            envelope = metadata | {"result": preview}
            if self._transport_length(envelope) <= self._max_chars:
                best = preview
                low = preview_chars + 1
            else:
                high = preview_chars - 1
        return metadata | {"result": best}

    def _fit_structured_envelope(self, value: object, output_id: str, total_chars: int) -> object:
        """Fits one valid structured preview inside the canonical truncation envelope."""
        metadata = self._truncation_metadata(output_id, total_chars)
        empty_envelope = metadata | {"result": None}
        fixed_chars = self._transport_length(empty_envelope) - len("null")
        available = self._max_chars - fixed_chars

        # fit the logical structure directly rather than reconstructing it from serialized prose
        if available > 0:
            preview = self._structured_compactor.compact(value, available)
            envelope = metadata | {"result": preview}
            if self._transport_length(envelope) <= self._max_chars:
                return envelope
        return self._minimum_envelope(metadata)

    def _minimum_envelope(self, metadata: dict[str, object]) -> object:
        """Returns the most informative valid fallback that fits a pathological tiny budget."""
        candidates: tuple[object, ...] = (
            metadata | {"result": None},
            {"truncated": True, "output_id": metadata["output_id"]},
            {"truncated": True},
            {},
            0,
        )
        for candidate in candidates:
            if self._transport_length(candidate) <= self._max_chars:
                return candidate
        raise AssertionError("A positive result-presentation budget must fit the one-character fallback")

    @staticmethod
    def _truncation_metadata(output_id: str, total_chars: int) -> dict[str, object]:
        """Builds the canonical retained-result metadata fields."""
        return {
            "truncated": True,
            "total_chars": total_chars,
            "output_id": output_id,
        }

    @staticmethod
    def extend_output_schema(output_schema: dict[str, object] | None) -> dict[str, object] | None:
        """Extends one FastMCP output schema with the canonical truncation envelope."""
        if output_schema is None:
            return None
        return {
            "anyOf": [
                output_schema,
                {
                    "type": "object",
                    "properties": {
                        "truncated": {"const": True, "type": "boolean"},
                        "total_chars": {"type": "integer", "minimum": 0},
                        "output_id": {"type": "string"},
                        "result": {},
                    },
                    "required": ["truncated", "total_chars", "output_id", "result"],
                },
            ]
        }

    @staticmethod
    def _is_native_result(value: object) -> bool:
        """Whether ``value`` must bypass ordinary text/JSON presentation."""
        if isinstance(value, CallToolResult | ResourceLink):
            return True
        return isinstance(getattr(value, "file_link", None), ResourceLink)

    @classmethod
    def _normalize(cls, value: object) -> object:
        """Normalizes an ordinary result deterministically into JSON/MCP-safe Python values."""
        if value is None or isinstance(value, str | int | bool):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, Enum):
            return cls._normalize(value.value)
        if isinstance(value, BaseModel):
            return cls._normalize(value.model_dump(mode="json"))
        if is_dataclass(value) and not isinstance(value, type):
            return cls._normalize(asdict(value))
        if isinstance(value, Mapping):
            return {str(key): cls._normalize(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [cls._normalize(item) for item in value]
        if isinstance(value, set | frozenset):
            normalized_items = [cls._normalize(item) for item in value]
            return sorted(normalized_items, key=cls._serialize_json)
        return str(value)

    @classmethod
    def _serialize_logical_result(cls, value: object) -> str:
        """Serializes a normalized logical result exactly for retained-output paging."""
        if isinstance(value, str):
            return value
        return cls._serialize_json(value)

    @classmethod
    def _serialize_presentation(cls, value: object) -> str:
        """Serializes one presented value deterministically for execution persistence."""
        return cls._serialize_json(value)

    @classmethod
    def _transport_length(cls, value: object) -> int:
        """Measures the canonical model-facing representation of one ordinary value."""
        if isinstance(value, str):
            return len(value)
        return len(cls._serialize_json(value))

    @staticmethod
    def _serialize_json(value: object) -> str:
        """Serializes one normalized value as compact deterministic JSON."""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        """Returns the shared adaptive head/tail text preview."""
        return StructuredOutputCompactor.truncate_text(text, limit)
