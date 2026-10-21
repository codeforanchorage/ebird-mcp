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

    case                                          expected
    ping, no Origin                               200
    Origin https://claude.ai                      200
    Origin https://evil.example                   403
    MCP-Protocol-Version 2026-07-28, legacy body  400 (-32020)
    MCP-Protocol-Version 2026-07-28, modern body  200
    MCP-Protocol-Version 1999-01-01               400 (-32022)
    MCP-Protocol-Version 2025-11-25               200
    GET /mcp                                      405

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


MODERN_DISCOVER = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
)


class ProtocolVersionHeaderTests(_TransportCase):
    async def test_supported_version_passes(self):
        status, _, _ = await self.post({"mcp-protocol-version": "2025-11-25"})
        self.assertEqual(status, 200)

    async def test_every_legacy_version_passes(self):
        for version in MCPServer.LEGACY_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                status, _, _ = await self.post(
                    {"mcp-protocol-version": version}
                )
                self.assertEqual(status, 200)

    async def test_modern_version_with_modern_body_is_served(self):
        """2026-07-28 is served (dual-era), not bounced to the 400 that
        used to cost every claude.ai / Claude Code session a fallback
        round trip. Header, body and Mcp-Method all agree here."""
        status, headers, body = await self.post(
            {
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "server/discover",
                "mcp-session-id": "stale-legacy-session",
            },
            body=MODERN_DISCOVER,
        )
        self.assertEqual(status, 200)
        result = json.loads(body)["result"]
        self.assertEqual(result["supportedVersions"][0], "2026-07-28")
        # No session in the modern era: nothing minted, nothing echoed.
        self.assertNotIn("Mcp-Session-Id", headers)

    async def test_modern_version_with_legacy_body_is_header_mismatch(self):
        """The half-migrated case: header says 2026-07-28, body carries no
        _meta. Still a 400 (so the old case table holds), but now the
        modern -32020 naming the disagreeing header -- it is rejected as
        a modern request, not silently served as legacy."""
        status, _, body = await self.post(
            {"mcp-protocol-version": "2026-07-28"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32020)

    async def test_unknown_version_is_refused_with_32022(self):
        """A revision we implement in neither era: 400 with the modern
        UnsupportedProtocolVersionError listing every version we do
        serve, so the client can retry with one. (Used to be -32600, when
        a plain 400 was what made dual-era clients fall back to
        initialize; we are dual-era now, so that fallback would cost a
        round trip for nothing.)"""
        status, _, body = await self.post(
            {"mcp-protocol-version": "1999-01-01"}
        )
        self.assertEqual(status, 400)
        parsed = json.loads(body)
        error = parsed["error"]
        self.assertEqual(error["code"], -32022)
        self.assertEqual(error["data"]["requested"], "1999-01-01")
        self.assertEqual(error["data"]["supported"][0], "2026-07-28")
        self.assertIn("2025-11-25", error["data"]["supported"])
        # The spec's own UnsupportedProtocolVersionError example echoes
        # the request id; a client with several requests in flight needs
        # it to know which one was refused. This used to be null.
        self.assertEqual(parsed["id"], 1)

    async def test_unknown_version_without_an_id_keeps_null_id(self):
        """A notification (no id) or an unparseable body cannot be
        correlated, so the error keeps a null id rather than inventing
        one."""
        for label, body in (
            (
                "notification",
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            ),
            ("unparseable", "{not json"),
            ("non-object", "[1, 2]"),
        ):
            with self.subTest(body=label):
                status, _, out = await self.post(
                    {"mcp-protocol-version": "1999-01-01"}, body=body
                )
                self.assertEqual(status, 400)
                parsed = json.loads(out)
                self.assertIsNone(parsed["id"])
                self.assertEqual(parsed["error"]["code"], -32022)

    async def test_origin_and_path_errors_keep_null_id(self):
        """Those happen before any body can be trusted; the spec says
        they MAY have no id, and we do not go looking for one."""
        status, _, body = await self.post({"origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIsNone(json.loads(body)["id"])
        status, _, body = await self.post(path="/nope")
        self.assertEqual(status, 404)
        self.assertIsNone(json.loads(body)["id"])

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

    async def test_get_with_null_body_is_still_405(self):
        """API Gateway gives the Lambda adapter `"body": null` on a GET
        (aiohttp gives ""). The handler pre-parses the body before the
        method check, and json.loads(None) is a TypeError -- which made
        GET /mcp a 500 on prod while every local run said 405. The two
        entry points must agree, and they must agree on 405."""
        status, headers, body = await self.post(method="GET", body=None)
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")
        self.assertEqual(json.loads(body)["error"]["message"], "Method Not Allowed")

    async def test_post_with_null_body_is_a_parse_error(self):
        """Same shape on a POST: a missing body is the caller's -32700,
        not our 500."""
        status, _, body = await self.post(body=None)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)

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

    async def test_preflight_allows_the_modern_mirror_headers(self):
        """2026-07-28 clients send Mcp-Method on every request and
        Mcp-Name on tools/call; a browser preflight must allow both."""
        _, headers, _ = self.handler.handle_options(request_id="test")
        allowed = headers["Access-Control-Allow-Headers"]
        self.assertIn("mcp-method", allowed)
        self.assertIn("mcp-name", allowed)


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
