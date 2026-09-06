import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ActivityHistoryStore:
    """Persists bounded inline activity runs so old chat panels can rehydrate after a restart."""

    def __init__(self, root: Path | None = None, max_runs: int = 128) -> None:
        self._root = root or self._default_root()
        self._max_runs = max_runs

    @staticmethod
    def _default_root() -> Path:
        configured_home = os.getenv("SERENA_HOME", "").strip()
        serena_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".serena"
        return serena_home / "activity_runs"

    def load(self) -> list[dict[str, Any]]:
        """Returns retained run payloads from oldest to newest."""
        if not self._root.exists():
            return []

        payloads: list[tuple[float, dict[str, Any]]] = []
        for path in self._root.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    payload = json.load(stream)
                if not isinstance(payload, dict) or not isinstance(payload.get("run_id"), str):
                    continue
                payloads.append((float(payload.get("started_at") or path.stat().st_mtime), payload))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

        payloads.sort(key=lambda item: item[0])
        return [payload for _, payload in payloads[-self._max_runs :]]

    def save(self, payload: dict[str, Any]) -> None:
        """Atomically stores one run payload and prunes the oldest retained runs."""
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Activity run payload requires a run_id")

        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._write_path(self._root / f"{run_id}.json", payload)
        self._prune()

    def _prune(self) -> None:
        paths = sorted(self._root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        for path in paths[: max(0, len(paths) - self._max_runs)]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _write_path(path: Path, value: dict[str, Any]) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
