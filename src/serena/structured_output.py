"""Structure-aware compaction for bounded Serena tool output."""

from __future__ import annotations

import json
from typing import cast

from serena.result_metadata import ResultIdentityText


class StructuredOutputCompactor:
    """Fits JSON-safe values adaptively while preserving useful structure."""

    _ESSENTIAL_TEXT_MAX = 96
    _SMALL_STRUCTURE_MAX = 192
    _OMITTED_ITEMS_KEY = "_serena_omitted_items"
    _OMITTED_FIELDS_KEY = "_serena_omitted_fields"

    def serialize_for_storage(self, value: object, max_chars: int) -> str:
        """Serializes ``value`` within ``max_chars`` without corrupting structured content."""
        if max_chars <= 0:
            return ""

        # normalize first so fitting and serialization operate on one deterministic value
        normalized = self._normalize(value)
        serialized = self._dumps(normalized, pretty=True)
        if len(serialized) <= max_chars:
            return serialized

        compacted = self.compact(normalized, max_chars, pretty=True)
        return self._dumps(compacted, pretty=True)

    def compact(self, value: object, max_chars: int, *, pretty: bool = False) -> object:
        """Returns the richest deterministic JSON-safe value fitting ``max_chars``."""
        if max_chars <= 0:
            return ""

        normalized = self._normalize(value)
        if self._serialized_length(normalized, pretty=pretty) <= max_chars:
            return normalized

        # first preserve the complete mapping/list topology and shorten only bulky text leaves
        candidate = self._fit_text_previews(normalized, max_chars=max_chars, pretty=pretty)
        if candidate is not None:
            return candidate

        # if the structural skeleton is too large, compact collections adaptively with omission markers
        max_list_items = self._max_list_items(normalized)
        if max_list_items > 0:
            candidate = self._largest_fitting_list_shape(
                normalized,
                max_chars=max_chars,
                pretty=pretty,
                max_list_items=max_list_items,
            )
            if candidate is not None:
                return candidate

        # only after collection compaction omit low-value mapping fields
        max_dict_fields = self._max_dict_fields(normalized)
        if max_dict_fields > 0:
            candidate = self._largest_fitting_dict_shape(
                normalized,
                max_chars=max_chars,
                pretty=pretty,
                list_limit=1 if max_list_items > 0 else None,
                max_dict_fields=max_dict_fields,
            )
            if candidate is not None:
                return candidate

        # allow collections to collapse to an explicit marker before abandoning structured output
        if max_list_items > 0:
            candidate = self._fit_text_previews(
                normalized,
                max_chars=max_chars,
                pretty=pretty,
                list_limit=0,
                dict_limit=1 if max_dict_fields > 0 else None,
            )
            if candidate is not None:
                return candidate

        return self._minimal_value(normalized, max_chars=max_chars, pretty=pretty)

    def _largest_fitting_list_shape(
        self,
        value: object,
        *,
        max_chars: int,
        pretty: bool,
        max_list_items: int,
    ) -> object | None:
        """Finds the widest representative collection shape that fits."""
        smallest = self._fit_text_previews(value, max_chars=max_chars, pretty=pretty, list_limit=1)
        if smallest is None:
            return None

        low = 1
        high = max_list_items
        best = smallest
        while low <= high:
            limit = (low + high) // 2
            candidate = self._fit_text_previews(value, max_chars=max_chars, pretty=pretty, list_limit=limit)
            if candidate is not None:
                best = candidate
                low = limit + 1
            else:
                high = limit - 1
        return best

    def _largest_fitting_dict_shape(
        self,
        value: object,
        *,
        max_chars: int,
        pretty: bool,
        list_limit: int | None,
        max_dict_fields: int,
    ) -> object | None:
        """Finds the widest mapping field set that fits after collection compaction."""
        smallest = self._fit_text_previews(
            value,
            max_chars=max_chars,
            pretty=pretty,
            list_limit=list_limit,
            dict_limit=1,
        )
        if smallest is None:
            return None

        low = 1
        high = max_dict_fields
        best = smallest
        while low <= high:
            limit = (low + high) // 2
            candidate = self._fit_text_previews(
                value,
                max_chars=max_chars,
                pretty=pretty,
                list_limit=list_limit,
                dict_limit=limit,
            )
            if candidate is not None:
                best = candidate
                low = limit + 1
            else:
                high = limit - 1
        return best

    def _fit_text_previews(
        self,
        value: object,
        *,
        max_chars: int,
        pretty: bool,
        list_limit: int | None = None,
        dict_limit: int | None = None,
    ) -> object | None:
        """Fits text leaves adaptively while preserving the requested structural shape."""
        shaped = self._shape(value, list_limit=list_limit, dict_limit=dict_limit)
        if self._serialized_length(shaped, pretty=pretty) <= max_chars:
            return shaped

        # shorten only as many of the longest text leaves as necessary to preserve this topology
        all_lengths = self._previewable_text_lengths(shaped, 2)
        if not all_lengths:
            return None
        candidate_lengths = sorted(set(all_lengths))
        low = 0
        high = len(candidate_lengths) - 1
        chosen_min_length: int | None = None
        chosen_lengths: list[int] = []
        while low <= high:
            index = (low + high) // 2
            min_length = candidate_lengths[index]
            preview_lengths = [length for length in all_lengths if length >= min_length]
            smallest = self._apply_text_budget(shaped, len(preview_lengths), min_length=min_length)
            if self._serialized_length(smallest, pretty=pretty) <= max_chars:
                chosen_min_length = min_length
                chosen_lengths = preview_lengths
                low = index + 1
            else:
                high = index - 1

        if chosen_min_length is None:
            return None

        # use the remaining budget fairly across the selected leaves
        low = len(chosen_lengths)
        high = sum(chosen_lengths)
        best = self._apply_text_budget(shaped, low, min_length=chosen_min_length)
        while low <= high:
            text_budget = (low + high) // 2
            candidate = self._apply_text_budget(shaped, text_budget, min_length=chosen_min_length)
            if self._serialized_length(candidate, pretty=pretty) <= max_chars:
                best = candidate
                low = text_budget + 1
            else:
                high = text_budget - 1
        return best

    def _shape(self, value: object, *, list_limit: int | None, dict_limit: int | None) -> object:
        """Copies one value while applying structural collection and mapping limits."""
        if isinstance(value, list):
            items = cast(list[object], value)
            selected: list[object]
            if list_limit is not None and len(items) > list_limit:
                head_count = (list_limit + 1) // 2
                tail_count = list_limit // 2
                omitted = len(items) - head_count - tail_count
                selected = [*items[:head_count], {self._OMITTED_ITEMS_KEY: omitted}]
                if tail_count:
                    selected.extend(items[-tail_count:])
            else:
                selected = items
            return [self._shape(item, list_limit=list_limit, dict_limit=dict_limit) for item in selected]

        if isinstance(value, dict):
            items = list(cast(dict[str, object], value).items())
            omitted_fields = 0
            if dict_limit is not None and len(items) > dict_limit:
                items, omitted_fields = self._select_dict_items(items, dict_limit)

            compacted = {key: self._shape(item, list_limit=list_limit, dict_limit=dict_limit) for key, item in items}
            if omitted_fields:
                marker_key = self._available_marker_key(cast(dict[str, object], value), self._OMITTED_FIELDS_KEY)
                compacted[marker_key] = omitted_fields
            return compacted

        return value

    def _select_dict_items(self, items: list[tuple[str, object]], limit: int) -> tuple[list[tuple[str, object]], int]:
        """Selects mapping fields by generic structural value while preserving source order."""
        if len(items) <= limit:
            return items, 0
        if limit <= 0:
            return [], len(items)

        ranked = sorted(
            enumerate(items),
            key=lambda entry: (self._field_rank(entry[1][1]), entry[0]),
        )
        selected_indices = sorted(index for index, _ in ranked[:limit])
        return [items[index] for index in selected_indices], len(items) - len(selected_indices)

    def _field_rank(self, value: object) -> tuple[int, int]:
        """Ranks fields generically so compact scalar identity survives verbose leaves."""
        if value is None or isinstance(value, bool | int | float):
            return (0, self._serialized_length(value, pretty=False))
        if isinstance(value, ResultIdentityText):
            return (0, len(value))
        if isinstance(value, str):
            if len(value) <= self._ESSENTIAL_TEXT_MAX:
                return (1, len(value))
            return (4, len(value))
        if isinstance(value, dict | list):
            size = self._serialized_length(value, pretty=False)
            return (2 if size <= self._SMALL_STRUCTURE_MAX else 3, size)
        return (3, self._serialized_length(value, pretty=False))

    def _apply_text_budget(self, value: object, total_budget: int, *, min_length: int) -> object:
        """Distributes one aggregate text budget fairly over eligible text leaves."""
        lengths = self._previewable_text_lengths(value, min_length)
        allocations = self._fair_text_allocations(lengths, total_budget)
        allocation_iter = iter(allocations)

        def apply(item: object) -> object:
            if isinstance(item, ResultIdentityText):
                return item
            if isinstance(item, str):
                if len(item) >= min_length:
                    return self.truncate_text(item, next(allocation_iter))
                return item
            if isinstance(item, list):
                return [apply(child) for child in cast(list[object], item)]
            if isinstance(item, dict):
                return {key: apply(child) for key, child in cast(dict[str, object], item).items()}
            return item

        return apply(value)

    @staticmethod
    def _previewable_text_lengths(value: object, min_length: int) -> list[int]:
        """Collects text-leaf lengths eligible for adaptive previewing."""
        lengths: list[int] = []

        def collect(item: object) -> None:
            if isinstance(item, ResultIdentityText):
                return
            if isinstance(item, str):
                if len(item) >= min_length:
                    lengths.append(len(item))
                return
            if isinstance(item, list):
                for child in cast(list[object], item):
                    collect(child)
                return
            if isinstance(item, dict):
                for child in cast(dict[str, object], item).values():
                    collect(child)

        collect(value)
        return lengths

    @staticmethod
    def _fair_text_allocations(lengths: list[int], total_budget: int) -> list[int]:
        """Returns deterministic near-equal allocations summing to ``total_budget`` where possible."""
        if not lengths:
            return []

        minimum = len(lengths)
        budget = max(minimum, min(total_budget, sum(lengths)))
        low = 1
        high = max(lengths)
        cap = 1
        while low <= high:
            candidate = (low + high) // 2
            used = sum(min(length, candidate) for length in lengths)
            if used <= budget:
                cap = candidate
                low = candidate + 1
            else:
                high = candidate - 1

        allocations = [min(length, cap) for length in lengths]
        remainder = budget - sum(allocations)
        for index, length in enumerate(lengths):
            if remainder <= 0:
                break
            if allocations[index] < length:
                allocations[index] += 1
                remainder -= 1
        return allocations

    @classmethod
    def truncate_text(cls, text: str, limit: int) -> str:
        """Returns an adaptive head/tail preview occupying at most ``limit`` characters."""
        if len(text) <= limit:
            return text
        if limit <= 0:
            return ""
        if limit == 1:
            return "…"

        # very tight budgets still make omission explicit without spending space on a long marker
        if limit < 24:
            head = (limit - 1 + 1) // 2
            tail = (limit - 1) // 2
            return text[:head] + "…" + (text[-tail:] if tail else "")

        # use a descriptive omission marker and split the remaining capacity across head and tail
        multiline = "\n" in text
        omitted = max(1, len(text) - limit)
        for _ in range(5):
            marker = cls._omission_marker(omitted, multiline=multiline)
            available = limit - len(marker)
            if available <= 1:
                head = (limit - 1 + 1) // 2
                tail = (limit - 1) // 2
                return text[:head] + "…" + (text[-tail:] if tail else "")

            head_chars = (available + 1) // 2
            tail_chars = available // 2
            if multiline:
                head_chars, tail_chars = cls._prefer_complete_lines(text, head_chars, tail_chars, available)
            omitted = len(text) - head_chars - tail_chars

        marker = cls._omission_marker(omitted, multiline=multiline)
        available = limit - len(marker)
        if available <= 1:
            head = (limit - 1 + 1) // 2
            tail = (limit - 1) // 2
            return text[:head] + "…" + (text[-tail:] if tail else "")

        head_chars = (available + 1) // 2
        tail_chars = available // 2
        if multiline:
            head_chars, tail_chars = cls._prefer_complete_lines(text, head_chars, tail_chars, available)
        omitted = len(text) - head_chars - tail_chars
        marker = cls._omission_marker(omitted, multiline=multiline)

        # one final exact rebalance handles marker-width changes at powers of ten
        available = max(0, limit - len(marker))
        head_chars = (available + 1) // 2
        tail_chars = available // 2
        if multiline:
            head_chars, tail_chars = cls._prefer_complete_lines(text, head_chars, tail_chars, available)
        if multiline:
            core = f"... {max(0, omitted)} chars omitted ..."
            prefix = "" if head_chars and text[head_chars - 1] == "\n" else "\n"
            tail_start = len(text) - tail_chars if tail_chars else len(text)
            suffix = "" if tail_chars and text[tail_start] == "\n" else "\n"
            marker = prefix + core + suffix
        result = text[:head_chars] + marker + (text[-tail_chars:] if tail_chars else "")
        return result[:limit] if len(result) > limit else result

    @staticmethod
    def _prefer_complete_lines(text: str, head_chars: int, tail_chars: int, available: int) -> tuple[int, int]:
        """Moves preview boundaries to complete lines and only adds further complete lines."""
        head_break = text.rfind("\n", 0, min(len(text), head_chars) + 1)
        head = head_break + 1 if head_break >= 0 else head_chars

        tail_start = max(0, len(text) - tail_chars)
        tail_break = text.find("\n", tail_start)
        tail = len(text) - tail_break - 1 if tail_break >= 0 else tail_chars

        head = min(head, available)
        tail = min(tail, available - head)
        remaining = available - head - tail

        # use spare capacity on whole adjacent lines without recreating partial boundaries
        while remaining > 0 and head + tail < len(text):
            changed = False
            omitted_end = len(text) - tail

            next_head_break = text.find("\n", head, omitted_end)
            if next_head_break >= 0:
                addition = next_head_break + 1 - head
                if addition <= remaining:
                    head += addition
                    remaining -= addition
                    changed = True
                    omitted_end = len(text) - tail

            tail_start = len(text) - tail
            previous_tail_break = text.rfind("\n", head, max(head, tail_start - 1))
            if previous_tail_break >= 0:
                addition = tail_start - previous_tail_break - 1
                if 0 < addition <= remaining:
                    tail += addition
                    remaining -= addition
                    changed = True

            if not changed:
                break

        return head, tail

    @staticmethod
    def _omission_marker(omitted: int, *, multiline: bool) -> str:
        marker = f"... {max(0, omitted)} chars omitted ..."
        return f"\n{marker}\n" if multiline else marker

    @classmethod
    def _normalize(cls, value: object) -> object:
        if value is None or isinstance(value, str | int | bool):
            return value
        if isinstance(value, float):
            return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, dict):
            return {str(key): cls._normalize(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [cls._normalize(item) for item in value]
        if isinstance(value, set | frozenset):
            normalized = [cls._normalize(item) for item in value]
            return sorted(normalized, key=cls._stable_sort_key)
        return str(value)

    @classmethod
    def _stable_sort_key(cls, value: object) -> str:
        return cls._dumps(value)

    @classmethod
    def _max_list_items(cls, value: object) -> int:
        maximum = 0
        if isinstance(value, list):
            items = cast(list[object], value)
            maximum = len(items)
            for item in items:
                maximum = max(maximum, cls._max_list_items(item))
        elif isinstance(value, dict):
            for item in cast(dict[str, object], value).values():
                maximum = max(maximum, cls._max_list_items(item))
        return maximum

    @classmethod
    def _max_dict_fields(cls, value: object) -> int:
        maximum = 0
        if isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            maximum = len(mapping)
            for item in mapping.values():
                maximum = max(maximum, cls._max_dict_fields(item))
        elif isinstance(value, list):
            for item in cast(list[object], value):
                maximum = max(maximum, cls._max_dict_fields(item))
        return maximum

    @staticmethod
    def _available_marker_key(mapping: dict[str, object], base: str) -> str:
        key = base
        while key in mapping:
            key = f"_{key}"
        return key

    def _minimal_value(self, value: object, *, max_chars: int, pretty: bool) -> object:
        """Returns the smallest informative JSON value available for pathological budgets."""
        candidates: list[object]
        if isinstance(value, dict):
            candidates = [{"_serena_truncated": True}, {}]
        elif isinstance(value, list):
            marker = [{self._OMITTED_ITEMS_KEY: len(value)}]
            candidates = [marker, []]
        elif isinstance(value, str):
            candidates = [self.truncate_text(value, max_chars)]
        else:
            candidates = [value, None]

        for candidate in candidates:
            if self._serialized_length(candidate, pretty=pretty) <= max_chars:
                return candidate
        return 0

    def _serialized_length(self, value: object, *, pretty: bool) -> int:
        return len(self._dumps(value, pretty=pretty))

    @staticmethod
    def _dumps(value: object, *, pretty: bool = False) -> str:
        if pretty:
            return json.dumps(value, ensure_ascii=False, indent=2)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
