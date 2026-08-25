"""Local dev must reproduce prod's transport behaviour, not approximate it.

Before ``scripts/local_server.py`` was routed through
``UniversalHTTPHandler``, it called ``MCPServer.handle_http_request``
directly. Everything the handler enforces was therefore absent locally:
the Origin allowlist, the ``MCP-Protocol-Version`` check, path and method
validation, and CORS. Those are precisely the protocol MUSTs added for
recent spec revisions, so the hardening could only ever be exercised
against deployed AWS — and could not be regression-tested at all.

These tests drive the SAME handler both entry points use, so a regression
in any of the six cases fails here rather than in production. The case
table mirrors the one verified by hand against local and prod:

    case                              expected
    ping, no Origin                   200
    Origin https://claude.ai          200
    Origin https://evil.example       403
    MCP-Protocol-Version 2026-07-28   400
    MCP-Protocol-Version 2025-11-25   200
    GET /mcp                          405

Run with::

    python -m unittest tests.test_transport_parity
"""

import json
import logging
import unittest
from typing import Any, Dict, List, Optional

import server.http_handler as http_handler_module
from core.interfaces import ToolResult
from core.mcp_server import MCPServer
from server.http_handler import UniversalHTTPHandler
from tests.support import HTTPHandlerIsolation

PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})


class _StubPluginManager:
    plugins: Dict[str, Any] = {}
    tools: Dict[str, Any] = {}

    def get_all_tools(self) -> List[Dict[str, Any]]:
        return []

    def get_instructions(self) -> Optional[str]:
        return None

    async def execute_tool(self, tool_name, arguments) -> ToolResult:
        return ToolResult(content=[], success=True)

    async def shutdown(self) -> None:
        return None


class _TransportCase(HTTPHandlerIsolation, unittest.IsolatedAsyncioTestCase):
    """Drives the real handler with the plugin layer stubbed out.

    The plugin layer is irrelevant to every case here — all six are
    decided before dispatch — and stubbing it keeps the tests off the
    network and independent of config.yaml.
    """

    def setUp(self):
        super().setUp()
        # The handler logs every request as JSON at INFO. Useful in prod,
        # unreadable in a test run.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        http_handler_module._plugin_manager = _StubPluginManager()
        http_handler_module._mcp_server = MCPServer(
            http_handler_module._plugin_manager
        )
        self.handler = UniversalHTTPHandler()

    async def post(
        self,
        headers: Optional[Dict[str, str]] = None,
        path: str = "/mcp",
        method: str = "POST",
        body: str = PING,
    ):
        return await self.handler.handle_request(
            method=method,
            path=path,
            body=body,
            headers=headers or {},
            request_id="test",
        )


class OriginAllowlistTests(_TransportCase):
    """DNS-rebinding defence. The spec makes the 403 a MUST."""

    async def test_no_origin_is_allowed(self):
        """Native clients (Claude Desktop, curl) send no Origin."""
        status, _, _ = await self.post()
        self.assertEqual(status, 200)

    async def test_allowlisted_origin_is_allowed(self):
        for origin in ("https://claude.ai", "https://claude.com"):
            with self.subTest(origin=origin):
                status, headers, _ = await self.post({"origin": origin})
                self.assertEqual(status, 200)
                self.assertEqual(
                    headers["Access-Control-Allow-Origin"], origin
                )

    async def test_mcp_inspector_localhost_is_allowed(self):
        status, _, _ = await self.post({"origin": "http://localhost:6274"})
        self.assertEqual(
            status,
            200,
            "MCP Inspector is how local dev is driven; no dev-mode flag "
            "should be needed to use it.",
        )

    async def test_disallowed_origin_is_refused(self):
        status, _, body = await self.post({"origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["message"], "Forbidden")

    async def test_header_case_is_ignored(self):
        """aiohttp passes 'Origin'; the Lambda adapter lowercases it."""
        status, _, _ = await self.post({"Origin": "https://evil.example"})
        self.assertEqual(
            status, 403, "header lookup must be case-insensitive"
        )


class ProtocolVersionHeaderTests(_TransportCase):
    async def test_supported_version_passes(self):
        status, _, _ = await self.post({"mcp-protocol-version": "2025-11-25"})
        self.assertEqual(status, 200)

    async def test_every_supported_version_passes(self):
        for version in MCPServer.SUPPORTED_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                status, _, _ = await self.post(
                    {"mcp-protocol-version": version}
                )
                self.assertEqual(status, 200)

    async def test_2026_07_28_is_refused_with_400(self):
        """Deliberately 400/-32600, not 2026-07-28's -32022.

        A dual-era client reading a -32022 UnsupportedProtocolVersionError
        would retry the new handshake; reading a plain 400 it falls back
        to initialize and talks to us as the legacy server we are.
        """
        status, _, body = await self.post(
            {"mcp-protocol-version": "2026-07-28"}
        )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], -32600)
        self.assertNotEqual(error["code"], -32022)

    async def test_absent_header_is_fine(self):
        """Older clients omit it; the spec says assume 2025-03-26."""
        status, _, _ = await self.post()
        self.assertEqual(status, 200)


class PathAndMethodTests(_TransportCase):
    async def test_get_is_not_allowed(self):
        """Streamable HTTP allows GET for SSE; we do not implement it."""
        status, headers, _ = await self.post(method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    async def test_delete_is_not_allowed(self):
        """Spec session termination; this server is stateless."""
        status, _, _ = await self.post(method="DELETE")
        self.assertEqual(status, 405)

    async def test_unknown_path_is_404(self):
        status, _, _ = await self.post(path="/mcp/extra")
        self.assertEqual(status, 404)


class CorsPreflightTests(_TransportCase):
    async def test_preflight_echoes_an_allowlisted_origin(self):
        status, headers, _ = self.handler.handle_options(
            request_id="test", request_origin="https://claude.ai"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            headers["Access-Control-Allow-Origin"], "https://claude.ai"
        )

    async def test_preflight_allows_the_protocol_version_header(self):
        """Clients on 2025-06-18+ send it on every request.

        If it is not in Access-Control-Allow-Headers, every browser
        preflight fails and the connector never works.
        """
        _, headers, _ = self.handler.handle_options(request_id="test")
        self.assertIn(
            "mcp-protocol-version", headers["Access-Control-Allow-Headers"]
        )
        self.assertIn(
            "mcp-session-id", headers["Access-Control-Allow-Headers"]
        )


class SessionHeaderTests(_TransportCase):
    async def test_initialize_mints_a_session_id(self):
        _, headers, _ = await self.post(
            body=json.dumps(
                {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
            )
        )
        self.assertIn("Mcp-Session-Id", headers)

    async def test_non_initialize_does_not_mint_one(self):
        """Stateless: clients may call tools/* with no handshake at all."""
        _, headers, _ = await self.post()
        self.assertNotIn("Mcp-Session-Id", headers)


class GlobalStateIsolationTests(unittest.TestCase):
    """Paired guard: break the isolation helper and this fails.

    Without it the protection could silently rot — the leak it prevents
    only manifests as a failure in an unrelated file, and only under some
    file orderings.
    """

    def test_mixin_restores_globals_it_found(self):
        from tests import support

        sentinel = object()
        http_handler_module._plugin_manager = sentinel

        class _Leaky(HTTPHandlerIsolation, unittest.TestCase):
            def test_leak(inner):
                http_handler_module._plugin_manager = "clobbered"

        result = unittest.TestResult()
        _Leaky("test_leak").run(result)

        self.assertEqual(result.errors + result.failures, [])
        self.assertIs(
            http_handler_module._plugin_manager,
            sentinel,
            "HTTPHandlerIsolation must restore what it found, or a stub "
            "from one test file will break an unrelated one depending on "
            "alphabetical order.",
        )
        support.reset_http_handler_globals()

    def test_mixin_starts_each_test_from_a_clean_slate(self):
        http_handler_module._plugin_manager = "stale"
        seen = {}

        class _Observer(HTTPHandlerIsolation, unittest.TestCase):
            def test_observe(inner):
                seen["value"] = http_handler_module._plugin_manager

        result = unittest.TestResult()
        _Observer("test_observe").run(result)
        self.assertEqual(result.errors + result.failures, [])
        self.assertIsNone(
            seen["value"],
            "a test must not inherit another file's plugin manager",
        )


if __name__ == "__main__":
    unittest.main()
