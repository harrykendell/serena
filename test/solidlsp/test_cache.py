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


def test_serialized_cache_budget_rejects_growth_beyond_limit() -> None:
    budget = cache.SerializedCacheBudget(max_bytes=64)
    budget.admit("small", "small")

    try:
        budget.admit("large", "x" * 1024)
    except cache.CacheSizeLimitExceeded as error:
        assert error.current_bytes == budget.total_bytes
        assert error.max_bytes == 64
    else:
        raise AssertionError("oversized cache entry was admitted")


def test_serialized_cache_budget_replacement_reuses_previous_capacity() -> None:
    budget = cache.SerializedCacheBudget(max_bytes=128)
    budget.admit("entry", "x" * 40)
    first_size = budget.total_bytes

    budget.admit("entry", "y" * 50)

    assert first_size < budget.total_bytes <= 128
