"""Disk-backed retained and live output for Serena tool executions."""

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from uuid import uuid4


@dataclass(frozen=True)
class ToolOutputPage:
    """One character-addressed page of a retained tool result."""

    output_id: str
    tool_name: str
    total_chars: int
    offset: int
    content: str
    next_offset: int | None
    is_open: bool = False

    @property
    def end_offset(self) -> int:
        """Exclusive character offset reached by this page."""
        return self.offset + len(self.content)

    @property
    def complete(self) -> bool:
        """Whether this page contains the complete finalized retained result."""
        return not self.is_open and self.offset == 0 and self.next_offset is None

    @property
    def truncated(self) -> bool:
        """Whether retained content exists outside this page."""
        return self.offset > 0 or self.next_offset is not None


@dataclass(frozen=True)
class ToolOutputDescriptor:
    """Lightweight metadata for one retained tool result."""

    output_id: str
    tool_name: str
    total_chars: int
    is_open: bool


@dataclass
class _ToolOutputRecord:
    """Mutable metadata for one retained tool result."""

    tool_name: str
    path: Path
    execution_name: str | None = None
    total_chars: int = 0
    is_open: bool = True


class ToolOutputWriter:
    """Append-only writer for one retained tool result."""

    def __init__(self, store: "ToolOutputStore", output_id: str):
        self._store = store
        self.output_id = output_id
        self._closed = False

    def write(self, content: str) -> None:
        """Append text to the retained result."""
        if self._closed:
            raise RuntimeError("Tool output writer is closed")
        self._store.append(self.output_id, content)

    def close(self) -> None:
        """Mark the retained result complete."""
        if self._closed:
            return
        self._closed = True
        self._store.finish(self.output_id)

    def __enter__(self) -> "ToolOutputWriter":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class ToolOutputStore:
    """Process-local, disk-backed retention for recent and live tool results."""

    def __init__(self, max_records: int = 16):
        if max_records <= 0:
            raise ValueError("max_records must be positive")

        self._max_records = max_records
        self._directory = TemporaryDirectory(prefix="serena-tool-output-")
        self._records: OrderedDict[str, _ToolOutputRecord] = OrderedDict()
        self._output_by_execution: dict[str, str] = {}
        self._lock = RLock()
        self._closed = False

    def open(self, tool_name: str, execution_name: str | None = None) -> ToolOutputWriter:
        """Open one retained result and return its stable append-only writer."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Tool output store is closed")

            # publish a stable identifier before any process output is produced
            output_id = uuid4().hex
            path = Path(self._directory.name) / f"{output_id}.txt"
            path.touch()
            self._records[output_id] = _ToolOutputRecord(tool_name=tool_name, path=path, execution_name=execution_name)
            if execution_name is not None:
                self._output_by_execution[execution_name] = output_id
            self._prune()
            return ToolOutputWriter(self, output_id)

    def retain(self, tool_name: str, content: str) -> str:
        """Retain one complete tool result and return its stable opaque identifier."""
        with self.open(tool_name) as writer:
            writer.write(content)
            return writer.output_id

    def append(self, output_id: str, content: str) -> None:
        """Append one newly available chunk to a live retained result."""
        if not content:
            return
        with self._lock:
            record = self._record(output_id)
            if not record.is_open:
                raise RuntimeError(f"Tool output '{output_id}' is already complete")
            with record.path.open("a", encoding="utf-8", newline="") as output_file:
                output_file.write(content)
            record.total_chars += len(content)

    def finish(self, output_id: str) -> None:
        """Mark a live retained result complete without changing its identity."""
        with self._lock:
            record = self._record(output_id)
            record.is_open = False

    def retain_with_tail(self, tool_name: str, content: str, max_answer_chars: int) -> str:
        """Retain a result and render an identified tail that fits the answer limit."""
        output_id = self.retain(tool_name, content)
        return self.render_tail(output_id, max_answer_chars, answer_chars=len(content))

    def render_tail(
        self,
        output_id: str,
        max_answer_chars: int,
        *,
        answer_chars: int | None = None,
        retained_label: str = "Full output",
        details: str | None = None,
    ) -> str:
        """Render a bounded identified tail for an already retained result."""
        descriptor = self.describe(output_id)
        answer_length = descriptor.total_chars if answer_chars is None else answer_chars
        footer = f"\nUse read_tool_output(output_id='{output_id}', offset=<offset>) to read another page."
        details_text = f"\n{details}" if details else ""
        available = max_answer_chars - len(footer) - len(details_text) - 280
        tail_length = max(0, min(descriptor.total_chars, available))

        while True:
            tail_start = descriptor.total_chars - tail_length
            page = self.read(output_id, tail_start, tail_length) if tail_length else None
            header = (
                f"The answer is too long ({answer_length} characters). {retained_label} retained as {output_id}.\n"
                f"Showing tail from character {tail_start}:{details_text}\n"
                f"complete=false; truncated=true; total_chars={descriptor.total_chars}; "
                f"shown_range={tail_start}:{descriptor.total_chars}\n"
            )
            response = f"{header}{page.content if page is not None else ''}{footer}"
            if len(response) <= max_answer_chars or tail_length == 0:
                return response[:max_answer_chars]
            tail_length = max(0, tail_length - (len(response) - max_answer_chars))

    def describe(self, output_id: str) -> ToolOutputDescriptor:
        """Return lightweight metadata for one retained result."""
        with self._lock:
            record = self._record(output_id)
            return ToolOutputDescriptor(
                output_id=output_id,
                tool_name=record.tool_name,
                total_chars=record.total_chars,
                is_open=record.is_open,
            )

    def describe_execution(self, execution_name: str) -> ToolOutputDescriptor | None:
        """Return retained-output metadata for one exact task execution, if still available."""
        with self._lock:
            output_id = self._output_by_execution.get(execution_name)
            if output_id is None or output_id not in self._records:
                return None
            return self.describe(output_id)

    def read(self, output_id: str, offset: int, max_chars: int) -> ToolOutputPage:
        """Read one page from an explicitly identified retained result."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")

        with self._lock:
            record = self._record(output_id)
            if offset > record.total_chars:
                raise ValueError(f"offset {offset} exceeds retained output length {record.total_chars}")

            # seek by characters rather than bytes so cursors remain correct for arbitrary UTF-8 output
            with record.path.open("r", encoding="utf-8", newline="") as output_file:
                remaining = offset
                while remaining:
                    skipped = output_file.read(min(65_536, remaining))
                    if not skipped:
                        break
                    remaining -= len(skipped)
                content = output_file.read(max_chars)

            next_offset_value = offset + len(content)
            next_offset = next_offset_value if next_offset_value < record.total_chars else None
            return ToolOutputPage(
                output_id=output_id,
                tool_name=record.tool_name,
                total_chars=record.total_chars,
                offset=offset,
                content=content,
                next_offset=next_offset,
                is_open=record.is_open,
            )

    def read_tail(self, output_id: str, max_chars: int) -> ToolOutputPage:
        """Read the newest bounded tail from one retained result."""
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        descriptor = self.describe(output_id)
        return self.read(output_id, max(0, descriptor.total_chars - max_chars), max_chars)

    def read_execution_tail(self, execution_name: str, max_chars: int) -> ToolOutputPage | None:
        """Read the newest bounded tail for one exact task execution."""
        descriptor = self.describe_execution(execution_name)
        if descriptor is None:
            return None
        return self.read_tail(descriptor.output_id, max_chars)

    def close(self) -> None:
        """Remove all retained output files."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._records.clear()
            self._output_by_execution.clear()
            self._directory.cleanup()

    def _record(self, output_id: str) -> _ToolOutputRecord:
        record = self._records.get(output_id)
        if record is None:
            raise ValueError(f"Tool output '{output_id}' is not available. It may have expired or belong to a different Serena process.")
        return record

    def _prune(self) -> None:
        while len(self._records) > self._max_records:
            expired_id, expired = self._records.popitem(last=False)
            if expired.execution_name is not None and self._output_by_execution.get(expired.execution_name) == expired_id:
                del self._output_by_execution[expired.execution_name]
            expired.path.unlink(missing_ok=True)
