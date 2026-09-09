"""
This file provides the implementation of the JSON-RPC client, that launches and
communicates with the language server.

The initial implementation of this file was obtained from
https://github.com/predragnikolic/OLSP under the MIT License with the following terms:

MIT License

Copyright (c) 2023 Предраг Николић

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import dataclasses
import json
import logging
import os
from typing import Any, Union

from .lsp_types import ErrorCodes

StringDict = dict[str, Any]
PayloadLike = Union[list[StringDict], StringDict, None, bool]
CONTENT_LENGTH = "Content-Length: "
ENCODING = "utf-8"
log = logging.getLogger(__name__)


@dataclasses.dataclass
class ProcessLaunchInfo:
    """
    This class is used to store the information required to launch a (language server) process.
    """

    cmd: list[str]
    """The argv vector used to launch the process."""

    env: dict[str, str] = dataclasses.field(default_factory=dict)
    """
    the environment variables to set for the process
    """

    cwd: str = os.getcwd()
    """
    the working directory for the process
    """


class LSPError(Exception):
    def __init__(self, code: ErrorCodes, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_lsp(self) -> StringDict:
        return {"code": self.code, "message": super().__str__()}

    @classmethod
    def from_lsp(cls, d: StringDict) -> "LSPError":
        return LSPError(d["code"], d["message"])

    def __str__(self) -> str:
        return f"{super().__str__()} ({self.code})"


def make_response(request_id: Any, params: PayloadLike) -> StringDict:
    return {"jsonrpc": "2.0", "id": request_id, "result": params}


def make_error_response(request_id: Any, err: LSPError) -> StringDict:
    return {"jsonrpc": "2.0", "id": request_id, "error": err.to_lsp()}


def _build_params_field(params: PayloadLike) -> StringDict:
    """Return the optional JSON-RPC ``params`` field when parameters were supplied."""
    return {} if params is None else {"params": params}


def make_notification(method: str, params: PayloadLike) -> StringDict:
    """Create a JSON-RPC 2.0 notification message."""
    return {"jsonrpc": "2.0", "method": method, **_build_params_field(params)}


def make_request(method: str, request_id: Any, params: PayloadLike) -> StringDict:
    """Create a JSON-RPC 2.0 request message."""
    return {"jsonrpc": "2.0", "method": method, "id": request_id, **_build_params_field(params)}


def create_message(payload: PayloadLike) -> tuple[bytes, bytes]:
    body = json.dumps(payload, check_circular=False, ensure_ascii=False, separators=(",", ":")).encode(ENCODING)
    # NOTE: only Content-Length is sent. Content-Type is optional per the LSP
    # spec (default: application/vscode-jsonrpc; charset=utf-8), and Godot's
    # GDScript LSP parser only handles Content-Length — any additional header
    # makes it silently drop the message.
    return (
        f"Content-Length: {len(body)}\r\n\r\n".encode(ENCODING),
        body,
    )


class MessageType:
    error = 1
    warning = 2
    info = 3
    log = 4


def content_length(line: bytes) -> int | None:
    if line.startswith(b"Content-Length: "):
        _, value = line.split(b"Content-Length: ")
        value = value.strip()
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"Invalid Content-Length header: {value!r}")
    return None
