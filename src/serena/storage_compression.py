"""Transparent Zstandard compression for Serena's retained text artifacts."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tempfile
from pathlib import Path

import zstandard


class RetainedTextCompression:
    """Encodes retained text artifacts with a self-identifying Zstandard envelope."""

    _HEADER = b"SERENA-ZSTD-1\x00"
    _LEVEL = 3

    @classmethod
    def compress_file(cls, path: Path) -> None:
        """Compresses one file in place with Serena's level-3 Zstandard envelope."""
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".zstd-tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output, path.open("rb") as source:
                output.write(cls._HEADER)
                with zstandard.ZstdCompressor(level=cls._LEVEL).stream_writer(output, closefd=False) as compressor:
                    shutil.copyfileobj(source, compressor, length=1024 * 1024)
            os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @classmethod
    def decode_if_compressed(cls, data: bytes, *, expected_sha256: str | None = None) -> bytes:
        """Returns transparently decompressed bytes when ``data`` has Serena's envelope.

        When ``expected_sha256`` is supplied, the decompressed content must match that
        original-content digest. Invalid or coincidental envelope prefixes are therefore
        treated as ordinary raw data.
        """
        if not data.startswith(cls._HEADER):
            return data
        try:
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data[len(cls._HEADER) :])) as reader:
                decoded = reader.read()
        except zstandard.ZstdError:
            return data
        if expected_sha256 is not None and hashlib.sha256(decoded).hexdigest() != expected_sha256:
            return data
        return decoded

    @classmethod
    def is_compressed(cls, path: Path) -> bool:
        """Returns whether one retained artifact uses Serena's Zstandard envelope."""
        try:
            with path.open("rb") as stream:
                return stream.read(len(cls._HEADER)) == cls._HEADER
        except OSError:
            return False

    @classmethod
    def read_bytes(cls, path: Path, *, expected_sha256: str | None = None) -> bytes:
        """Reads one raw or Serena-compressed artifact transparently."""
        return cls.decode_if_compressed(path.read_bytes(), expected_sha256=expected_sha256)

    @classmethod
    def read_text(cls, path: Path) -> str:
        """Reads one UTF-8 artifact transparently from raw or compressed storage."""
        return cls.read_bytes(path).decode("utf-8")

    @staticmethod
    def is_text_mime_type(mime_type: str) -> bool:
        """Returns whether a MIME type represents text suitable for compression."""
        if mime_type.startswith("text/"):
            return True
        return mime_type in {
            "application/json",
            "application/ld+json",
            "application/xml",
            "application/javascript",
            "application/x-javascript",
            "application/x-yaml",
            "application/yaml",
        }
