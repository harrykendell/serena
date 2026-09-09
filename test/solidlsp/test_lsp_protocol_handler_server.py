"""Tests for the small JSON-RPC message builders used by SolidLSP."""

from solidlsp.lsp_protocol_handler.server import make_notification, make_request


def test_notification_omits_unsupplied_params() -> None:
    assert make_notification("exit", None) == {"jsonrpc": "2.0", "method": "exit"}


def test_notification_preserves_explicit_params() -> None:
    params = {"uri": "file:///test.py", "languageId": "python"}
    assert make_notification("textDocument/didOpen", params) == {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": params,
    }
    assert make_notification("custom/notify", {})["params"] == {}


def test_request_omits_unsupplied_params() -> None:
    assert make_request("shutdown", request_id=1, params=None) == {
        "jsonrpc": "2.0",
        "method": "shutdown",
        "id": 1,
    }


def test_request_preserves_id_and_explicit_params() -> None:
    params = {"textDocument": {"uri": "file:///test.py"}, "position": {"line": 10, "character": 5}}
    assert make_request("textDocument/hover", request_id="request-42", params=params) == {
        "jsonrpc": "2.0",
        "method": "textDocument/hover",
        "id": "request-42",
        "params": params,
    }
