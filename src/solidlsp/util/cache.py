import logging
from pathlib import Path
from typing import Any, Optional

from sensai.util.pickle import dump_pickle, load_pickle

log = logging.getLogger(__name__)


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
