"""Structure-aware compaction for bounded Serena tool output."""

from __future__ import annotations

import json
from typing import Any, cast


class StructuredOutputCompactor:
    """Preserves useful JSON structure while reducing oversized display values."""

    _STRING_LIMITS = (4096, 2048, 1024, 512, 256, 128, 64)
    _LIST_LIMITS = (64, 32, 16, 8, 4, 2, 1)
    _DICT_LIMITS = (64, 32, 16, 8, 4)
    _PRIORITY_KEYS = (
        "name_path",
        "name",
        "kind",
        "relative_path",
        "path",
        "file",
        "body_location",
        "location",
        "status",
        "return_code",
        "exit_code",
        "job_id",
        "output_id",
        "line",
        "start_line",
        "end_line",
        "count",
        "total",
        "message",
    )

    def serialize_for_storage(self, value: object, max_chars: int) -> str:
        """Serializes ``value`` within ``max_chars`` without corrupting structured content."""
        try:
            serialized = self._dumps(value, pretty=True, default=str)
        except (TypeError, ValueError):
            text = str(value)
            return text if len(text) <= max_chars else self._truncate_text(text, max_chars)
        if len(serialized) <= max_chars:
            return serialized

        # recover JSON-producing tools before compacting so their outer structure remains useful
        normalized = self._normalize(self._decode_structured_string(value))
        compacted = self.compact(normalized, max_chars, pretty=True)
        return self._dumps(compacted, pretty=True)

    def render_retained_json_preview(self, result: str, output_id: str, max_chars: int) -> str | None:
        """Returns a bounded valid-JSON retained-output envelope when ``result`` is structured JSON."""
        structured = self._decode_structured_string(result)
        if not isinstance(structured, dict | list):
            return None

        metadata: dict[str, Any] = {
            "truncated": True,
            "total_chars": len(result),
            "output_id": output_id,
        }
        empty_envelope = self._dumps(metadata | {"result": None})
        available = max_chars - len(empty_envelope) - 8
        if available < 64:
            return None

        # leave the exact full value in retained output while exposing the richest valid structure that fits
        for _ in range(4):
            compacted = self.compact(self._normalize(structured), available)
            response = self._dumps(metadata | {"result": compacted})
            if len(response) <= max_chars:
                return response
            available -= len(response) - max_chars + 16
            if available < 64:
                return None
        return None

    def compact(self, value: object, max_chars: int, *, pretty: bool = False) -> object:
        """Returns the richest deterministic structure whose JSON representation fits ``max_chars``."""
        normalized = self._normalize(value)
        if self._serialized_length(normalized, pretty=pretty) <= max_chars:
            return normalized

        # first sacrifice only verbose leaf strings, retaining the complete object/list topology
        for string_limit in self._STRING_LIMITS:
            candidate = self._shrink(normalized, string_limit=string_limit)
            if self._serialized_length(candidate, pretty=pretty) <= max_chars:
                return candidate

        # next retain representative beginning/end entries of oversized arrays
        for list_limit in self._LIST_LIMITS:
            candidate = self._shrink(normalized, string_limit=64, list_limit=list_limit)
            if self._serialized_length(candidate, pretty=pretty) <= max_chars:
                return candidate

        # only as a final structured stage omit dictionary fields, preferring identifiers and locations
        for dict_limit in self._DICT_LIMITS:
            candidate = self._shrink(normalized, string_limit=64, list_limit=1, dict_limit=dict_limit)
            if self._serialized_length(candidate, pretty=pretty) <= max_chars:
                return candidate

        fallback = {
            "_serena_truncated": True,
            "original_type": type(normalized).__name__,
            "note": "Structured value exceeded the retained display budget; use retained output for the exact result.",
        }
        if self._serialized_length(fallback, pretty=pretty) <= max_chars:
            return fallback
        return "... output omitted ..."

    def _shrink(
        self,
        value: object,
        *,
        string_limit: int,
        list_limit: int | None = None,
        dict_limit: int | None = None,
    ) -> object:
        if isinstance(value, str):
            return self._truncate_text(value, string_limit)
        if isinstance(value, list):
            items = cast(list[object], value)
            selected: list[object]
            if list_limit is not None and len(items) > list_limit:
                head_count = (list_limit + 1) // 2
                tail_count = list_limit // 2
                omitted = len(items) - head_count - tail_count
                selected = [*items[:head_count], {"_serena_omitted_items": omitted}]
                if tail_count:
                    selected.extend(items[-tail_count:])
            else:
                selected = items
            return [self._shrink(item, string_limit=string_limit, list_limit=list_limit, dict_limit=dict_limit) for item in selected]
        if isinstance(value, dict):
            selected_items = list(cast(dict[str, object], value).items())
            omitted_fields = 0
            if dict_limit is not None and len(selected_items) > dict_limit:
                selected_items, omitted_fields = self._select_dict_items(selected_items, dict_limit)
            compacted = {
                key: self._shrink(item, string_limit=string_limit, list_limit=list_limit, dict_limit=dict_limit)
                for key, item in selected_items
            }
            if omitted_fields:
                compacted["_serena_omitted_fields"] = omitted_fields
            return compacted
        return value

    def _select_dict_items(self, items: list[tuple[str, object]], limit: int) -> tuple[list[tuple[str, object]], int]:
        if len(items) <= limit:
            return items, 0

        priority = {key: index for index, key in enumerate(self._PRIORITY_KEYS)}
        selected_indices = [index for index, (key, _) in enumerate(items) if key in priority]
        selected_indices.sort(key=lambda index: priority[items[index][0]])
        selected = selected_indices[:limit]

        # use remaining capacity on both ends, which usually preserves context plus terminal status/count fields
        candidates: list[int] = []
        left, right = 0, len(items) - 1
        while left <= right:
            candidates.append(left)
            if right != left:
                candidates.append(right)
            left += 1
            right -= 1
        for index in candidates:
            if len(selected) >= limit:
                break
            if index not in selected:
                selected.append(index)

        selected.sort()
        return [items[index] for index in selected], len(items) - len(selected)

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text

        # preserve complete lines for code, logs and other multiline output whenever the budget permits
        if "\n" in text and limit >= 64:
            available = max(0, limit - len(f"... {len(text)} chars omitted ...\n"))
            for _ in range(3):
                head_budget = (available + 1) // 2
                tail_budget = available // 2

                head_candidate = text[:head_budget]
                head_break = head_candidate.rfind("\n")
                head = head_candidate[: head_break + 1] if head_break >= 0 else head_candidate

                tail = ""
                if tail_budget:
                    tail_start = max(0, len(text) - tail_budget)
                    tail_break = text.find("\n", tail_start)
                    tail = text[tail_break + 1 :] if 0 <= tail_break < len(text) - 1 else text[-tail_budget:]

                omitted = len(text) - len(head) - len(tail)
                separator = "" if head.endswith("\n") else "\n"
                result = head + separator + f"... {omitted} chars omitted ...\n" + tail
                if len(result) <= limit:
                    return result
                available = max(0, available - (len(result) - limit))

        # fall back to exact character budgeting for single-line or exceptionally tight values
        marker_template = "\n... {omitted} chars omitted ...\n"
        marker = marker_template.format(omitted=0)
        available = max(0, limit - len(marker))
        head = (available + 1) // 2
        tail = available // 2
        omitted = len(text) - head - tail
        marker = marker_template.format(omitted=omitted)
        available = max(0, limit - len(marker))
        head = (available + 1) // 2
        tail = available // 2
        omitted = len(text) - head - tail
        marker = marker_template.format(omitted=omitted)
        if tail:
            return text[:head] + marker + text[-tail:]
        return text[:head] + marker

    @classmethod
    def _normalize(cls, value: object) -> object:
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, dict):
            return {str(key): cls._normalize(item) for key, item in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [cls._normalize(item) for item in value]
        return str(value)

    @staticmethod
    def _decode_structured_string(value: object) -> object:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text or text[0] not in "[{":
            return value
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, dict | list) else value

    def _serialized_length(self, value: object, *, pretty: bool) -> int:
        return len(self._dumps(value, pretty=pretty))

    @staticmethod
    def _dumps(value: object, *, pretty: bool = False, default: Any = None) -> str:
        if pretty:
            return json.dumps(value, ensure_ascii=False, indent=2, default=default)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=default)
