#!/usr/bin/env python3
"""Check that the duplicated Kendell UI foundation matches its sibling repository."""

from __future__ import annotations

import difflib
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
SERENA_ROOT = Path(os.environ.get("KENDELL_SERENA_ROOT", WORKSPACE / "serena"))
ARTIQ_TOOL_ROOT = Path(os.environ.get("KENDELL_ARTIQ_TOOL_ROOT", WORKSPACE / "artiq-tool"))

SHARED_FILES = (
    "kendell-tokens.css",
    "kendell-shell.css",
    "kendell-components.css",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def main() -> int:
    serena_dir = SERENA_ROOT / "src/serena/resources/kendell_dashboard"
    artiq_dir = ARTIQ_TOOL_ROOT / "site"

    missing = [path for path in (serena_dir, artiq_dir) if not path.is_dir()]
    if missing:
        for path in missing:
            print(f"missing Kendell UI directory: {path}", file=sys.stderr)
        return 2

    mismatched = False
    for name in SHARED_FILES:
        left = serena_dir / name
        right = artiq_dir / name
        if not left.is_file() or not right.is_file():
            print(f"MISSING {name}: Serena={left.is_file()} ARTIQ-tool={right.is_file()}")
            mismatched = True
            continue

        left_bytes = left.read_bytes()
        right_bytes = right.read_bytes()
        if left_bytes == right_bytes:
            print(f"OK      {name}  sha256={_sha256(left_bytes)}")
            continue

        mismatched = True
        print(
            f"DIFF    {name}  Serena={_sha256(left_bytes)} ARTIQ-tool={_sha256(right_bytes)}",
            file=sys.stderr,
        )
        left_lines = left_bytes.decode("utf-8").splitlines(keepends=True)
        right_lines = right_bytes.decode("utf-8").splitlines(keepends=True)
        sys.stderr.writelines(
            difflib.unified_diff(
                left_lines,
                right_lines,
                fromfile=f"serena/{name}",
                tofile=f"artiq-tool/{name}",
            )
        )

    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
