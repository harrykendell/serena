"""Disk-backed retained and live output for Serena tool executions."""

import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4

from serena.errors import UserFacingError
from serena.execution_store import ExecutionStore
from serena.storage_compression import RetainedTextCompression


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
    execution_id: str | None = None
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
    """Persistent disk-backed retention for pageable Serena tool results.

    Output lifetime is owned by the canonical execution/session store. Files survive Serena restarts,
    and crash-orphaned blobs are removed when Serena's canonical execution store starts.
    """

    def __init__(self, root: Path | None = None, execution_store: ExecutionStore | None = None):
        self._execution_store = execution_store
        if root is None:
            configured_home = os.getenv("SERENA_HOME", "").strip()
            serena_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".serena"
            root = serena_home / "tool_outputs"
        self._directory = root
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._directory, 0o700)
        self._records: dict[str, _ToolOutputRecord] = {}
        self._output_by_execution: dict[str, str] = {}
        self._lock = RLock()
        self._closed = False
        self._rehydrate()

    def _rehydrate(self) -> None:
        """Restores metadata for outputs referenced by retained executions."""
        if self._execution_store is None:
            return
        for execution in self._execution_store.list_executions(newest_first=False):
            output_id = execution.retained_output_id
            if output_id is None:
                continue
            path = self._directory / f"{output_id}.txt"
            if not path.is_file():
                continue
            total_chars = execution.retained_output_chars
            if total_chars is None:
                try:
                    total_chars = len(RetainedTextCompression.read_text(path))
                except (OSError, UnicodeDecodeError):
                    continue
            self._records[output_id] = _ToolOutputRecord(
                tool_name=execution.tool_name,
                path=path,
                execution_id=execution.execution_id,
                total_chars=total_chars,
                is_open=False,
            )
            self._output_by_execution[execution.execution_id] = output_id

    def open(self, tool_name: str, execution_id: str | None = None) -> ToolOutputWriter:
        """Open one retained result and return its stable append-only writer."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Tool output store is closed")

            output_id = uuid4().hex
            path = self._directory / f"{output_id}.txt"
            path.touch(mode=0o600, exist_ok=False)
            self._records[output_id] = _ToolOutputRecord(tool_name=tool_name, path=path, execution_id=execution_id)
            if execution_id is not None:
                self._output_by_execution[execution_id] = output_id
            return ToolOutputWriter(self, output_id)

    def retain(self, tool_name: str, content: str, execution_id: str | None = None) -> str:
        """Retains one complete tool result and returns its stable opaque identifier."""
        with self.open(tool_name, execution_id=execution_id) as writer:
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
        """Mark a live retained result complete and compress its persisted text."""
        with self._lock:
            record = self._record(output_id)
            if not record.is_open:
                return
            record.is_open = False
            RetainedTextCompression.compress_file(record.path)

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

    def describe_execution(self, execution_id: str) -> ToolOutputDescriptor | None:
        """Return retained-output metadata for one exact task execution, if still available."""
        with self._lock:
            output_id = self._output_by_execution.get(execution_id)
            if output_id is None or output_id not in self._records:
                return None
            return self.describe(output_id)

    def read(self, output_id: str, offset: int, max_chars: int) -> ToolOutputPage:
        """Reads one character-addressed page from an explicitly identified retained result."""
        if offset < 0:
            raise UserFacingError("offset must be non-negative")
        if max_chars <= 0:
            raise UserFacingError("max_chars must be positive")

        with self._lock:
            record = self._records.get(output_id)
            if record is None:
                raise UserFacingError(f"Tool output '{output_id}' is unavailable or expired")
            if offset > record.total_chars:
                raise UserFacingError(f"offset {offset} exceeds retained output length {record.total_chars}")

            # finalized output is compressed; live output remains directly seekable UTF-8 text
            if RetainedTextCompression.is_compressed(record.path):
                full_content = RetainedTextCompression.read_text(record.path)
                content = full_content[offset : offset + max_chars]
            else:
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

    def close(self) -> None:
        """Closes the process-local index while preserving session-owned output files."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._records.clear()
            self._output_by_execution.clear()

    def _record(self, output_id: str) -> _ToolOutputRecord:
        """Returns one retained-output record for internal store operations."""
        record = self._records.get(output_id)
        if record is None:
            raise ValueError(f"Tool output '{output_id}' is not available")
        return record
