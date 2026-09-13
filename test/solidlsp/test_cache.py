from pathlib import Path
from unittest.mock import Mock

from solidlsp.util import cache


def test_load_cache_rejects_oversized_file_before_deserialising(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "oversized.pkl"
    cache_path.write_bytes(b"oversized")
    load_pickle = Mock(side_effect=AssertionError("oversized cache must not be deserialised"))
    monkeypatch.setattr(cache, "load_pickle", load_pickle)

    result = cache.load_cache(str(cache_path), version=1, max_bytes=cache_path.stat().st_size - 1)

    assert result is None
    load_pickle.assert_not_called()


def test_load_cache_deserialises_file_within_limit(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.pkl"
    expected = {"answer": 42}
    cache.save_cache(str(cache_path), version=3, obj=expected)

    result = cache.load_cache(str(cache_path), version=3, max_bytes=cache_path.stat().st_size)

    assert result == expected
