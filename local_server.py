"""Run the eBird MCP server locally for testing (no Lambda needed).

  python local_server.py

Then point Claude Desktop at http://localhost:8000/mcp via Settings ->
Connectors -> Add custom connector, or hit it with curl / scripts/test_streamable_http.sh.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

# Add project root to sys.path so we can import core.* from anywhere.
project_root = Path(__file__).parent.resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import yaml
from aiohttp import web

from core.logging_utils import configure_json_logging
from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager
from core.validators import get_logging_config

logger = logging.getLogger(__name__)

_config_path = os.environ.get("EBIRD_MCP_CONFIG_FILE", "config.yaml")
with open(_config_path) as f:
    config = yaml.safe_load(f)

logging_config = get_logging_config(config)
configure_json_logging(
    level=logging_config.get("level", "INFO"),
    pretty=True,
)

_plugin_manager = None
_mcp_server = None


async def init_server():
    global _plugin_manager, _mcp_server
    print("Initializing eBird MCP Server locally...")
    _plugin_manager = PluginManager(config)
    await _plugin_manager.load_plugins()
    _mcp_server = MCPServer(_plugin_manager)
    print(f"Loaded plugins: {list(_plugin_manager.plugins.keys())}")
    print(f"Available tools: {len(_plugin_manager.get_all_tools())}")


async def handle_mcp_request(request):
    start_time = time.perf_counter()
    try:
        body = await request.text()
        headers = dict(request.headers)
        session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")

        try:
            request_json = json.loads(body)
            method = request_json.get("method", "unknown")
            tool_name = None
            tool_args = None
            if method == "tools/call":
                params = request_json.get("params", {})
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})
        except (json.JSONDecodeError, AttributeError):
            method = "unknown"
            tool_name = None
            tool_args = None

        logger.info(
            "Incoming MCP request",
            extra={
                "session_id": session_id,
                "method": method,
                "tool_name": tool_name,
                "tool_arguments": tool_args,
            },
        )

        is_initialize = method == "initialize"
        session_id_to_return = str(uuid.uuid4()) if is_initialize else None

        response = await _mcp_server.handle_http_request(body, headers)
        response_headers = dict(response.get("headers", {}))
        if session_id_to_return:
            response_headers["Mcp-Session-Id"] = session_id_to_return

        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "MCP request processed",
            extra={
                "session_id": session_id_to_return or session_id,
                "method": method,
                "tool_name": tool_name,
                "duration_ms": round(duration_ms, 2),
                "status_code": response.get("statusCode", 200),
            },
        )

        return web.Response(
            text=response.get("body", "{}"),
            status=response.get("statusCode", 200),
            headers=response_headers,
        )
    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.error(
            f"Error processing MCP request: {e}",
            extra={"duration_ms": round(duration_ms, 2)},
            exc_info=True,
        )
        return web.Response(
            text=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": str(e)},
                }
            ),
            status=500,
            headers={"Content-Type": "application/json"},
        )


async def start_server():
    await init_server()

    app = web.Application()
    app.router.add_post("/mcp", handle_mcp_request)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 8000)
    await site.start()

    print()
    print("=" * 50)
    print("eBird MCP Server running")
    print("=" * 50)
    print("URL: http://localhost:8000/mcp")
    print()
    print("Connect via Claude Connectors:")
    print("  1. Settings -> Connectors -> Add custom connector")
    print("  2. Name: eBird MCP   URL: http://localhost:8000/mcp")
    print()
    print("Test:")
    print("  ./scripts/test_streamable_http.sh")
    print("  curl -X POST http://localhost:8000/mcp \\")
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
        await _plugin_manager.shutdown()


if __name__ == "__main__":
    asyncio.run(start_server())
