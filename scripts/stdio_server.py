"""Run the eBird MCP server over stdio (no HTTP server, no AWS).

  python scripts/stdio_server.py

For MCP clients that launch a server as a subprocess and speak
newline-delimited JSON-RPC over its stdin/stdout. Needs a config.yaml with
your own eBird API key; the config is chosen exactly as for
scripts/local_server.py (OPENCONTEXT_CONFIG, else EBIRD_MCP_CONFIG_FILE,
else config.yaml at the repo root), and the script runs from any directory.

Like local_server.py and the Lambda adapter this is a THIN adapter onto the
core's UniversalHTTPHandler: each stdin line is handed to it as the body of
a POST to the MCP route, and the response body is written back as one
stdout line. Nothing protocol-level is re-implemented here, so era
negotiation, caller-error codes, error scrubbing and the request body cap
are the ones prod runs. What does not carry over is what only exists on the
wire: there are no headers, so no Origin check, no MCP-Protocol-Version
header and no Mcp-Session-Id (a modern-era client names its version in the
request's _meta, which the handler reads from the body).

Two stdio rules this file exists to keep:

- stdout carries protocol messages and NOTHING else. sys.stdout is pointed
  at stderr for the life of the process, so a stray print() in any import
  cannot corrupt the stream; logs already go to stderr.
- A notification is never answered, even with an error.

This replaces stdio_bridge.py, which forwarded stdin to a separately
running HTTP server.
"""

import asyncio
import json
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Optional

project_root = Path(__file__).parent.parent.resolve()


def _is_notification(line: bytes) -> bool:
    """A JSON-RPC object with no ``id`` member. Unparseable is not one."""
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(message, dict) and "id" not in message


async def _answer(handler: Any, mcp_path: str, line: bytes) -> Optional[str]:
    """One request line in, one response line out (or None for no reply)."""
    _status, _headers, body = await handler.handle_request(
        method="POST",
        path=mcp_path,
        body=line,
        headers={},
        request_id=str(uuid.uuid4()),
    )
    if not body or _is_notification(line):
        return None
    if "\n" in body or "\r" in body:
        # One message per line is the framing; never let a body break it.
        body = json.dumps(json.loads(body))
    return body


async def serve(stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Serve until stdin reaches EOF, which is how a client stops us."""
    # Imported here, not at module top: main() must choose the config and
    # protect stdout before the handler module is first loaded.
    from server import http_handler
    from server.http_handler import UniversalHTTPHandler

    handler = UniversalHTTPHandler()
    mcp_path = handler._http_settings()["mcp_paths"][0]

    # Fail at launch, on stderr, for a bad config or a missing API key --
    # not on the client's first request.
    await http_handler._initialize_server()

    # stdin is read on a daemon thread: asyncio has no portable way to
    # read a pipe on Windows, and a thread blocked in readline() must not
    # keep the process alive after Ctrl+C.
    loop = asyncio.get_running_loop()
    lines: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()

    def _pump() -> None:
        try:
            for raw in stdin:
                loop.call_soon_threadsafe(lines.put_nowait, raw)
            loop.call_soon_threadsafe(lines.put_nowait, None)
        except RuntimeError:
            pass  # the loop is gone: we are shutting down

    threading.Thread(target=_pump, daemon=True).start()

    try:
        while True:
            raw = await lines.get()
            if raw is None:
                break
            line = raw.strip()
            if not line:
                continue
            reply = await _answer(handler, mcp_path, line)
            if reply is not None:
                stdout.write(reply.encode("utf-8") + b"\n")
                stdout.flush()
    finally:
        if http_handler._plugin_manager is not None:
            await http_handler._plugin_manager.shutdown()


def main() -> None:
    # Bytes in, bytes out: the protocol is UTF-8 whatever the console
    # code page says (cp1252 on Windows would mangle every caveat's "⚠️"),
    # and binary mode keeps "\n" from becoming "\r\n".
    protocol_in = sys.stdin.buffer
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr

    for path in (str(project_root), str(Path(__file__).parent.resolve())):
        if path not in sys.path:
            sys.path.insert(0, path)

    from _local_config import use_local_config

    use_local_config(project_root)

    from core.logging_utils import configure_json_logging
    from core.validators import get_logging_config
    from server import http_handler

    # The handler configures logging at import with a default level; apply
    # the level from the config requests are actually served with.
    logging_config = get_logging_config(http_handler._load_config())
    configure_json_logging(
        level=logging_config.get("level", "INFO"), pretty=False
    )

    try:
        asyncio.run(serve(protocol_in, protocol_out))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
