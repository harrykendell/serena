"""Disk-backed retained output for Serena tool executions."""

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
    """One character-addressed page of a finalized retained tool result."""

    output_id: str
    total_chars: int
    offset: int
    content: str
    next_offset: int | None

    @property
    def end_offset(self) -> int:
        """Exclusive character offset reached by this page."""
        return self.offset + len(self.content)

    @property
    def complete(self) -> bool:
        """Whether this page contains the complete retained result."""
        return self.offset == 0 and self.next_offset is None


@dataclass(frozen=True)
class _ToolOutputRecord:
    """Metadata for one finalized retained tool result."""

    path: Path
    total_chars: int


class ToolOutputStore:
    """Persistent disk-backed retention for pageable finalized Serena tool results.

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
            self._records[output_id] = _ToolOutputRecord(path=path, total_chars=total_chars)

    def retain(self, content: str) -> str:
        """Retains one complete finalized tool result and returns its stable opaque identifier."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Tool output store is closed")

            output_id = uuid4().hex
            path = self._directory / f"{output_id}.txt"
            path.touch(mode=0o600, exist_ok=False)
            try:
                path.write_text(content, encoding="utf-8", newline="")
                RetainedTextCompression.compress_file(path)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            self._records[output_id] = _ToolOutputRecord(path=path, total_chars=len(content))
            return output_id

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

            full_content = RetainedTextCompression.read_text(record.path)
            content = full_content[offset : offset + max_chars]
            next_offset_value = offset + len(content)
            next_offset = next_offset_value if next_offset_value < record.total_chars else None
            return ToolOutputPage(
                output_id=output_id,
                total_chars=record.total_chars,
                offset=offset,
                content=content,
                next_offset=next_offset,
            )

    def close(self) -> None:
        """Closes the process-local index while preserving session-owned output files."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._records.clear()
