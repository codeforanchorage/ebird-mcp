"""stdio_bridge.py must relay what the server said, not a summary of it.

The server reports caller mistakes as a real JSON-RPC error under a 4xx
status. The bridge used to flatten every one of those to
``-32603 "HTTP 400"``, discarding the code and the actionable ``data``
(for an unknown tool, the list of tools that do exist). It also died with
an AttributeError on any body that was not a JSON object.

Run with::

    python -m unittest tests.test_stdio_bridge
"""

import io
import json
import unittest
import urllib.error
from typing import Any, List
from unittest import mock

import stdio_bridge


def _http_error(status: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://localhost:8000/mcp",
        status,
        "error",
        {},  # type: ignore[arg-type]
        io.BytesIO(body.encode("utf-8")),
    )


class _OkResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> "_OkResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _run(lines: List[str], outcome: Any) -> List[Any]:
    """Feed lines to the bridge; return the JSON objects it printed."""
    def _urlopen(req: Any, timeout: Any = None) -> Any:
        # A callable builds a fresh outcome per request: an HTTPError's
        # body is a stream, and a second read of the same one is empty.
        result = outcome() if callable(outcome) else outcome
        if isinstance(result, BaseException):
            raise result
        return _OkResponse(result)

    stdout = io.StringIO()
    with (
        mock.patch.object(stdio_bridge.sys, "stdin", io.StringIO("\n".join(lines))),
        mock.patch.object(stdio_bridge.sys, "stdout", stdout),
        mock.patch.object(stdio_bridge.sys, "argv", ["stdio_bridge.py"]),
        mock.patch.object(stdio_bridge.urllib.request, "urlopen", _urlopen),
    ):
        stdio_bridge.main()
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


_CALL = json.dumps(
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}
)


class HTTPErrorForwardingTests(unittest.TestCase):
    def test_jsonrpc_error_body_is_forwarded_verbatim(self) -> None:
        server_error = {
            "jsonrpc": "2.0",
            "id": 7,
            "error": {
                "code": -32602,
                "message": "Unknown tool: ebird__nope",
                "data": "Available tools: ebird__get_hotspots",
            },
        }
        out = _run([_CALL], _http_error(400, json.dumps(server_error)))
        self.assertEqual(out, [server_error])

    def test_non_jsonrpc_error_body_falls_back_to_the_status(self) -> None:
        for body in ('{"message": "Forbidden"}', "<html>502</html>", ""):
            with self.subTest(body=body):
                out = _run([_CALL], _http_error(403, body))
                self.assertEqual(len(out), 1)
                self.assertEqual(out[0]["id"], 7)
                self.assertEqual(
                    out[0]["error"],
                    {"code": -32603, "message": "HTTP 403"},
                )

    def test_notification_errors_stay_silent(self) -> None:
        notification = json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertEqual(_run([notification], _http_error(400, "{}")), [])


class NonObjectBodyTests(unittest.TestCase):
    def test_batch_array_is_forwarded_not_fatal(self) -> None:
        server_error = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
        out = _run(
            ['[{"jsonrpc":"2.0","id":1,"method":"ping"}]', _CALL],
            lambda: _http_error(400, json.dumps(server_error)),
        )
        # Both lines answered: the array did not kill the loop.
        self.assertEqual(out, [server_error, server_error])

    def test_success_is_relayed(self) -> None:
        ok = {"jsonrpc": "2.0", "id": 7, "result": {}}
        self.assertEqual(_run([_CALL], json.dumps(ok)), [ok])


if __name__ == "__main__":
    unittest.main()
