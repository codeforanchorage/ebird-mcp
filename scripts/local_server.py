"""Run the eBird MCP server locally for testing (no Lambda needed).

  python scripts/local_server.py                # port 8000
  python scripts/local_server.py --port 8931    # or EBIRD_MCP_PORT=8931

Then point Claude Desktop at http://localhost:8000/mcp via Settings ->
Connectors -> Add custom connector, or hit it with curl.

The port is configurable because 8000 is a popular default: another local
MCP server already bound to it answers your probes instead, and nothing
about the response says it is the wrong server. Startup therefore fails
loudly if the port is taken rather than carrying on.

This is deliberately a THIN adapter onto UniversalHTTPHandler, mirroring
``server/adapters/aws_lambda.py``. Everything protocol-level — the Origin
allowlist, the MCP-Protocol-Version check, path and method validation,
session IDs, CORS, request logging — lives in the handler, so what runs
here is what runs in prod. The previous version called
``MCPServer.handle_http_request`` directly and skipped all of it, which
meant none of that behaviour could be reproduced or regression-tested
outside deployed AWS.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

# Add project root to sys.path so we can import core.* from anywhere.
project_root = Path(__file__).parent.parent.resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Which config the server runs on. Resolved BEFORE the handler is imported,
# and handed to it through OPENCONTEXT_CONFIG (which accepts a YAML path as
# well as the JSON Terraform injects), so there is exactly one answer.
#
# This used to be read here for the logging level only, while the handler
# went on loading ./config.yaml by itself: EBIRD_MCP_CONFIG_FILE looked
# honoured -- the server started, the log level changed -- and every
# request was still served from the other file, API key and all.
#
# Precedence: OPENCONTEXT_CONFIG if already set (same as prod), then
# EBIRD_MCP_CONFIG_FILE, then config.yaml at the repo root -- NOT the
# process CWD, so the script works from any directory.
_config_path = os.path.abspath(
    os.environ.get("EBIRD_MCP_CONFIG_FILE", str(project_root / "config.yaml"))
)
# The core discovers plugins relative to the CWD (plugins/, custom_plugins/),
# so run from the repo root wherever the script was launched from. Done
# AFTER resolving the config path above, so a relative
# EBIRD_MCP_CONFIG_FILE still means relative to where the user typed it.
os.chdir(project_root)
if not os.environ.get("OPENCONTEXT_CONFIG"):
    if not os.path.isfile(_config_path):
        raise SystemExit(
            f"Config file not found: {_config_path}\n"
            f"Copy config-example.yaml to config.yaml, or point "
            f"EBIRD_MCP_CONFIG_FILE at one."
        )
    os.environ["OPENCONTEXT_CONFIG"] = _config_path

from aiohttp import web  # noqa: E402

from core.logging_utils import configure_json_logging  # noqa: E402
from core.validators import get_logging_config  # noqa: E402
from server import http_handler  # noqa: E402
from server.http_handler import UniversalHTTPHandler  # noqa: E402

logger = logging.getLogger(__name__)

# The handler's own loader, so logging is configured from the very config
# requests are served with.
config = http_handler._load_config()

logging_config = get_logging_config(config)
configure_json_logging(
    level=logging_config.get("level", "INFO"),
    pretty=True,  # Pretty-print JSON for local readability
)

# The single request handler, identical to the one the Lambda adapter uses.
# Deliberately NOT a local PluginManager/MCPServer pair: a second pair of
# module globals is how the two entry points drifted apart in the first
# place.
_handler = UniversalHTTPHandler()


async def init_server():
    """Warm the handler's plugins so startup failures surface immediately.

    UniversalHTTPHandler initializes lazily on the first request; doing it
    here instead means a bad config or a missing API key fails at launch
    rather than on the first curl, and lets us print what actually loaded.
    """
    print("Initializing eBird MCP Server locally...")
    await http_handler._initialize_server()
    plugin_manager = http_handler._plugin_manager
    print("Server initialized successfully")
    print(f"Loaded plugins: {list(plugin_manager.plugins.keys())}")
    print(f"Available tools: {len(plugin_manager.get_all_tools())}")


async def handle_mcp_request(request):
    """Adapt an aiohttp request onto UniversalHTTPHandler."""
    body = await request.text()

    # HTTP header names are case-insensitive; the handler reads them
    # lowercased, the same normalization the Lambda adapter applies.
    headers = {k.lower(): v for k, v in request.headers.items()}

    # Local-only convenience: surface the tool and its arguments up front.
    # The handler logs the request too, but not at this granularity, and
    # seeing the arguments is most of the value of running locally.
    try:
        request_json = json.loads(body)
        if request_json.get("method") == "tools/call":
            params = request_json.get("params", {})
            logger.info(
                "Incoming tool call",
                extra={
                    "method": "tools/call",
                    "tool_name": params.get("name"),
                    "tool_arguments": params.get("arguments") or None,
                },
            )
    except (json.JSONDecodeError, AttributeError):
        pass

    status_code, response_headers, response_body = await _handler.handle_request(
        method=request.method,
        path=request.path,
        body=body,
        headers=headers,
        request_id=str(uuid.uuid4()),
    )
    return web.Response(
        text=response_body, status=status_code, headers=response_headers
    )


async def handle_mcp_options(request):
    """CORS preflight, delegated to the same handler as prod."""
    status_code, response_headers, response_body = _handler.handle_options(
        request_id=str(uuid.uuid4()),
        request_origin=request.headers.get("Origin"),
    )
    return web.Response(
        text=response_body, status=status_code, headers=response_headers
    )


async def handle_other_method(request):
    """GET/DELETE on /mcp.

    The MCP Streamable HTTP spec allows GET for an SSE stream and DELETE
    for session termination; this server supports neither, and the handler
    answers 405 for both. Routing them here rather than letting aiohttp
    return its own 405 keeps the response body and CORS headers identical
    to what API Gateway + Lambda produce.
    """
    return await handle_mcp_request(request)


DEFAULT_PORT = 8000


def _parse_port(argv=None) -> int:
    """--port, else EBIRD_MCP_PORT, else 8000."""
    parser = argparse.ArgumentParser(
        description="Run the eBird MCP server locally."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EBIRD_MCP_PORT", DEFAULT_PORT)),
        help=(
            f"Port to listen on (default: EBIRD_MCP_PORT, "
            f"else {DEFAULT_PORT})."
        ),
    )
    return parser.parse_args(argv).port


async def start_server(port: int = DEFAULT_PORT):
    await init_server()

    app = web.Application()
    app.router.add_post("/mcp", handle_mcp_request)
    app.router.add_options("/mcp", handle_mcp_options)
    app.router.add_get("/mcp", handle_other_method)
    app.router.add_delete("/mcp", handle_other_method)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    try:
        await site.start()
    except OSError as e:
        await runner.cleanup()
        raise SystemExit(
            f"Could not listen on localhost:{port} ({e}). Something else "
            f"is probably using the port; pick another with --port."
        )

    url = f"http://localhost:{port}/mcp"

    print()
    print("=" * 50)
    print("eBird MCP Server running")
    print("=" * 50)
    print(f"URL: {url}")
    print()
    print("Connect via Claude Connectors:")
    print("  1. Settings -> Connectors -> Add custom connector")
    print(f"  2. Name: eBird MCP   URL: {url}")
    print()
    print("Test:")
    print(f"  bash scripts/test_streamable_http.sh {url}")
    print(f"  curl -X POST {url} \\")
    print("    -H 'Content-Type: application/json' \\")
    print("    -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}'")
    print()
    print("Ctrl+C to stop.")
    print("=" * 50)
    print()

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("Shutting down...")
        if http_handler._plugin_manager is not None:
            await http_handler._plugin_manager.shutdown()


if __name__ == "__main__":
    asyncio.run(start_server(_parse_port()))
