import logging
import pickle
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from sensai.util.pickle import dump_pickle, load_pickle

log = logging.getLogger(__name__)


class CacheSizeLimitExceeded(Exception):
    """Indicates that admitting one cache entry would exceed a configured serialized-size budget."""

    def __init__(self, current_bytes: int, max_bytes: int) -> None:
        self.current_bytes = current_bytes
        self.max_bytes = max_bytes
        super().__init__(f"cache size would exceed {max_bytes} bytes")


class _SizeLimitedWriter:
    """Counts pickle output and aborts as soon as one size limit is exceeded."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        if self.bytes_written > self.max_bytes:
            raise CacheSizeLimitExceeded(current_bytes=self.bytes_written, max_bytes=self.max_bytes)
        return len(data)


class SerializedCacheBudget:
    """Tracks a bounded cache using the serialized size of each independently stored value."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._entry_sizes: dict[str, int] = {}
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        """Returns the currently admitted serialized-size estimate."""
        return self._total_bytes

    @staticmethod
    def _measure(value: Any, max_bytes: int) -> int:
        writer = _SizeLimitedWriter(max_bytes)
        pickle.Pickler(writer, protocol=pickle.HIGHEST_PROTOCOL).dump(value)
        return writer.bytes_written

    def reset(self, entries: Mapping[str, Any]) -> None:
        """Rebuilds accounting from an existing cache, enforcing the same live limit."""
        entry_sizes: dict[str, int] = {}
        total_bytes = 0
        for key, value in entries.items():
            remaining_bytes = self.max_bytes - total_bytes
            try:
                entry_size = self._measure(value, remaining_bytes)
            except CacheSizeLimitExceeded:
                raise CacheSizeLimitExceeded(current_bytes=total_bytes, max_bytes=self.max_bytes) from None
            entry_sizes[key] = entry_size
            total_bytes += entry_size

        self._entry_sizes = entry_sizes
        self._total_bytes = total_bytes

    def admit(self, key: str, value: Any) -> None:
        """Accounts for one insertion or replacement, rejecting growth beyond the configured limit."""
        previous_size = self._entry_sizes.get(key, 0)
        base_size = self._total_bytes - previous_size
        remaining_bytes = self.max_bytes - base_size
        try:
            entry_size = self._measure(value, remaining_bytes)
        except CacheSizeLimitExceeded:
            raise CacheSizeLimitExceeded(current_bytes=base_size, max_bytes=self.max_bytes) from None

        self._entry_sizes[key] = entry_size
        self._total_bytes = base_size + entry_size


def load_cache(path: str, version: Any, *, max_bytes: int | None = None) -> Optional[Any]:
    """Loads a versioned pickle cache, optionally refusing oversized files before unpickling.

    :param path: Cache file path.
    :param version: Expected cache format version.
    :param max_bytes: Optional maximum on-disk cache size. Oversized caches are ignored without being deserialised.
    :return: The cached object when present and compatible, otherwise ``None``.
    """
    if max_bytes is not None:
        cache_size = Path(path).stat().st_size
        if cache_size > max_bytes:
            log.warning(
                "Cache at %s is %.1f MiB, exceeding the %.1f MiB load limit; ignoring it without deserialising.",
                path,
                cache_size / (1024 * 1024),
                max_bytes / (1024 * 1024),
            )
            return None

    data = load_pickle(path)
    if not isinstance(data, dict) or "__cache_version" not in data:
        log.info("Cache is outdated (expected version %s). Ignoring cache at %s", version, path)
        return None
    saved_version = data["__cache_version"]
    if saved_version != version:
        log.info("Cache is outdated (expected version %s, got %s). Ignoring cache at %s", version, saved_version, path)
        return None
    return data["obj"]


def save_cache(path: str, version: Any, obj: Any) -> None:
    data = {"__cache_version": version, "obj": obj}
    dump_pickle(data, path)
