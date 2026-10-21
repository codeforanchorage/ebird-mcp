"""Modern-era (MCP 2026-07-28) request handling.

The 2026-07-28 revision drops the initialize handshake: every request
carries its protocol version, client identity and capabilities in
``params._meta`` and is served statelessly; ``server/discover`` replaces
``initialize``; every result carries ``resultType``. These tests pin the
dual-era behaviour -- modern requests served per that revision, legacy
requests exactly as before -- and the Streamable HTTP rules that go with
it (header/body validation, 400/404 status codes, no sessions).

Run with::

    python -m unittest tests.test_modern_protocol
"""

import base64
import json
import logging
import unittest
from typing import Any, Dict, List, Optional

from core.interfaces import ToolResult
from core.mcp_server import (
    ERA_LEGACY,
    ERA_MODERN,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    MCPServer,
)

MODERN = "2026-07-28"


class _StubPluginManager:
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        instructions: Optional[str] = None,
        result: Optional[ToolResult] = None,
    ) -> None:
        self.config = config or {}
        self.tools = tools or []
        self.instructions = instructions
        self.result = result or ToolResult(
            content=[{"type": "text", "text": "ok"}], success=True
        )
        self.calls: List[tuple] = []

    def get_all_tools(self) -> List[Dict[str, Any]]:
        return list(self.tools)

    def get_instructions(self) -> Optional[str]:
        return self.instructions

    async def execute_tool(self, tool_name, arguments) -> ToolResult:
        self.calls.append((tool_name, arguments))
        return self.result


def _server(**kwargs) -> MCPServer:
    return MCPServer(_StubPluginManager(**kwargs))


def _meta(version=MODERN, capabilities=None, client=None):
    meta = {META_PROTOCOL_VERSION: version}
    if capabilities is not None:
        meta[META_CLIENT_CAPABILITIES] = capabilities
    if client is not None:
        meta[META_CLIENT_INFO] = client
    return meta


def _request(method, params=None, rid=1, **meta_kwargs):
    params = dict(params or {})
    params["_meta"] = _meta(**meta_kwargs)
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def _headers(method, name=None, version=MODERN):
    headers = {"mcp-protocol-version": version, "mcp-method": method}
    if name is not None:
        headers["mcp-name"] = name
    return headers


# The exact request claude.ai was observed sending (2026-09-17 on the
# Anchorage GIS fork), minus the trace context: the probe that used to
# get a 400 on every session start.
CLAUDE_AI_DISCOVER = {
    "jsonrpc": "2.0",
    "id": 592859513,
    "method": "server/discover",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {
                "name": "Anthropic/ClaudeAI",
                "version": "1.0.0",
            },
            "io.modelcontextprotocol/clientCapabilities": {
                "extensions": {
                    "io.modelcontextprotocol/ui": {
                        "mimeTypes": ["text/html;profile=mcp-app"]
                    }
                }
            },
        }
    },
}


class EraClassificationTests(unittest.TestCase):
    def test_initialize_is_legacy_even_with_modern_header(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        self.assertEqual(
            MCPServer.request_era(request, {"mcp-protocol-version": MODERN}),
            ERA_LEGACY,
        )

    def test_modern_meta_is_modern(self):
        self.assertEqual(MCPServer.request_era(CLAUDE_AI_DISCOVER, {}), ERA_MODERN)

    def test_modern_header_without_meta_is_modern(self):
        """A body with no _meta under a modern header is a malformed
        modern request, not a legacy one to quietly serve."""
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertEqual(
            MCPServer.request_era(request, {"mcp-protocol-version": MODERN}),
            ERA_MODERN,
        )

    def test_no_meta_no_header_is_legacy(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertEqual(MCPServer.request_era(request, {}), ERA_LEGACY)
        self.assertEqual(
            MCPServer.request_era(
                request, {"mcp-protocol-version": "2025-11-25"}
            ),
            ERA_LEGACY,
        )

    def test_non_object_params_or_meta_is_legacy(self):
        for params in ("oops", ["x"], {"_meta": "oops"}, {"_meta": None}):
            with self.subTest(params=params):
                request = {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    "params": params,
                }
                self.assertEqual(MCPServer.request_era(request, {}), ERA_LEGACY)


class DiscoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_ai_probe_is_served(self):
        server = _server(
            config={"server_name": "eBird MCP", "server_version": "2.3.0"},
            instructions="Resolve the speciesCode first.",
        )

        response = await server.handle_request(CLAUDE_AI_DISCOVER)

        self.assertEqual(response["id"], 592859513)
        result = response["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"][0], MODERN)
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertEqual(
            result["supportedVersions"],
            list(MCPServer.SUPPORTED_PROTOCOL_VERSIONS),
        )
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["instructions"], "Resolve the speciesCode first.")
        self.assertEqual(
            result["_meta"][META_SERVER_INFO],
            {"name": "eBird MCP", "version": "2.3.0"},
        )
        self.assertEqual(result["ttlMs"], MCPServer.DISCOVER_TTL_MS)
        self.assertEqual(result["cacheScope"], "public")

    async def test_discover_omits_instructions_when_unset(self):
        response = await _server().handle_request(CLAUDE_AI_DISCOVER)
        self.assertNotIn("instructions", response["result"])

    async def test_server_info_falls_back_when_config_is_bare(self):
        response = await _server().handle_request(CLAUDE_AI_DISCOVER)
        self.assertEqual(
            response["result"]["_meta"][META_SERVER_INFO],
            {"name": "ebird-mcp", "version": "1.0.0"},
        )


class ModernToolsListTests(unittest.IsolatedAsyncioTestCase):
    TOOLS = [{"name": "ebird__get_hotspots", "description": "d", "inputSchema": {}}]

    async def test_tools_list_is_cacheable_and_stamped(self):
        server = _server(tools=self.TOOLS, config={"server_name": "eBird MCP"})

        response = await server.handle_request(
            _request("tools/list", capabilities={})
        )

        result = response["result"]
        self.assertEqual(result["tools"], self.TOOLS)
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["ttlMs"], MCPServer.TOOLS_LIST_TTL_MS)
        self.assertEqual(result["cacheScope"], "public")
        self.assertEqual(result["_meta"][META_SERVER_INFO]["name"], "eBird MCP")

    async def test_legacy_tools_list_is_unchanged(self):
        """Legacy clients keep getting exactly what they got before: no
        resultType, no cache hints, no _meta."""
        server = _server(tools=self.TOOLS)

        response = await server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )

        self.assertEqual(response["result"], {"tools": self.TOOLS})


class ModernToolsCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_call_result_is_stamped(self):
        server = _server()

        response = await server.handle_request(
            _request(
                "tools/call",
                {"name": "ebird__get_hotspots", "arguments": {"regionCode": "US-AK"}},
                capabilities={},
                client={"name": "claude-code", "version": "2.1.274"},
            )
        )

        self.assertEqual(
            server.plugin_manager.calls,
            [("ebird__get_hotspots", {"regionCode": "US-AK"})],
        )
        result = response["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["content"], [{"type": "text", "text": "ok"}])
        self.assertIn(META_SERVER_INFO, result["_meta"])

    async def test_structured_content_survives_the_stamp(self):
        server = _server(
            result=ToolResult(
                content=[{"type": "text", "text": "ok"}],
                success=True,
                structured_content={"rows": []},
            )
        )
        response = await server.handle_request(
            _request("tools/call", {"name": "ebird__get_hotspots"}, capabilities={})
        )
        self.assertEqual(response["result"]["structuredContent"], {"rows": []})
        self.assertEqual(response["result"]["resultType"], "complete")

    async def test_tool_execution_error_is_a_complete_result(self):
        """isError results are still resultType complete -- the spec's
        tool-execution-error example carries both."""
        server = _server(
            result=ToolResult(content=[], success=False, error_message="bad field")
        )

        response = await server.handle_request(
            _request("tools/call", {"name": "ebird__get_hotspots"}, capabilities={})
        )

        result = response["result"]
        self.assertIs(result["isError"], True)
        self.assertEqual(result["resultType"], "complete")

    async def test_legacy_tools_call_result_is_unchanged(self):
        server = _server()
        response = await server.handle_request(
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "ebird__get_hotspots", "arguments": {}},
            }
        )
        self.assertEqual(
            response["result"], {"content": [{"type": "text", "text": "ok"}]}
        )


class ModernValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_client_capabilities_is_invalid_params(self):
        response = await _server().handle_request(
            _request("tools/list")  # no clientCapabilities
        )

        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["message"], "Invalid params")
        self.assertIn(META_CLIENT_CAPABILITIES, response["error"]["data"])

    async def test_non_object_client_capabilities_is_invalid_params(self):
        response = await _server().handle_request(
            _request("tools/list", capabilities="yes")
        )
        self.assertEqual(response["error"]["code"], -32602)

    async def test_unknown_modern_version_is_32022(self):
        response = await _server().handle_request(
            _request("tools/list", capabilities={}, version="2027-01-01")
        )

        error = response["error"]
        self.assertEqual(error["code"], -32022)
        self.assertEqual(error["message"], "Unsupported protocol version")
        self.assertEqual(error["data"]["requested"], "2027-01-01")
        self.assertEqual(
            error["data"]["supported"], list(MCPServer.SUPPORTED_PROTOCOL_VERSIONS)
        )

    async def test_legacy_version_in_meta_is_32022(self):
        """Our legacy versions are only served via initialize; declared
        as per-request metadata they are unsupported."""
        response = await _server().handle_request(
            _request("tools/list", capabilities={}, version="2025-11-25")
        )

        self.assertEqual(response["error"]["code"], -32022)

    async def test_version_is_checked_before_capabilities(self):
        """A client on the wrong version gets told so, not told about a
        field it may not even have in that version."""
        response = await _server().handle_request(
            _request("tools/list", version="2027-01-01")
        )
        self.assertEqual(response["error"]["code"], -32022)

    async def test_methods_not_in_this_era_are_method_not_found(self):
        """ping is gone from this revision; resources are not served; and
        with no listChanged capability there is nothing to listen for."""
        for method in (
            "ping", "resources/list", "subscriptions/listen", "prompts/list",
        ):
            with self.subTest(method=method):
                response = await _server().handle_request(
                    _request(method, capabilities={})
                )
                self.assertEqual(response["error"]["code"], -32601)

    async def test_initialize_with_meta_is_still_the_legacy_handshake(self):
        """An ``initialize`` request always selects the legacy era, even
        when it carries modern ``_meta`` (spec: era is chosen from how the
        client opens). It is served as a handshake, never a 404."""
        response = await _server().handle_request(
            _request("initialize", {"protocolVersion": "2025-11-25"},
                     capabilities={})
        )
        self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")
        self.assertNotIn("resultType", response["result"])

    async def test_initialize_forced_modern_is_method_not_found(self):
        """When the transport has already classified the exchange as
        modern, ``initialize`` is not a method of that era."""
        response = await _server().handle_request(
            _request("initialize", capabilities={}), era=ERA_MODERN
        )
        self.assertEqual(response["error"]["code"], -32601)

    async def test_modern_notification_is_dropped(self):
        request = _request("notifications/anything", capabilities={})
        del request["id"]

        self.assertIsNone(await _server().handle_request(request))

    async def test_modern_errors_are_caller_errors(self):
        """WARNING, no traceback, no scrubbed Error ID: a bad envelope is
        the client's to fix, not ours to investigate."""
        server = _server()
        for request in (
            _request("tools/list"),
            _request("tools/list", capabilities={}, version="2027-01-01"),
            _request("ping", capabilities={}),
        ):
            with self.subTest(method=request["method"]):
                with self.assertLogs("core.mcp_server", level="DEBUG") as cap:
                    response = await server.handle_request(request)
                warnings = [
                    r for r in cap.records if r.levelno >= logging.WARNING
                ]
                self.assertTrue(warnings)
                self.assertTrue(
                    all(r.levelno == logging.WARNING for r in warnings)
                )
                self.assertTrue(all(r.exc_info is None for r in warnings))
                self.assertNotIn("Error ID", str(response["error"]["data"]))


class StreamableHTTPTests(unittest.IsolatedAsyncioTestCase):
    """handle_http_request: header validation and status codes."""

    async def _post(self, server, request, headers):
        return await server.handle_http_request(json.dumps(request), headers)

    async def test_claude_ai_probe_end_to_end(self):
        response = await self._post(
            _server(), CLAUDE_AI_DISCOVER, _headers("server/discover")
        )

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["result"]["supportedVersions"][0], MODERN)

    async def test_missing_mcp_method_header_is_header_mismatch(self):
        response = await self._post(
            _server(), CLAUDE_AI_DISCOVER, {"mcp-protocol-version": MODERN}
        )

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["id"], 592859513)
        self.assertEqual(body["error"]["code"], -32020)
        self.assertIn("Mcp-Method", body["error"]["message"])

    async def test_missing_version_header_on_modern_body_is_header_mismatch(self):
        response = await self._post(
            _server(), CLAUDE_AI_DISCOVER, {"mcp-method": "server/discover"}
        )

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32020)

    async def test_version_header_body_disagreement_is_header_mismatch(self):
        response = await self._post(
            _server(),
            CLAUDE_AI_DISCOVER,
            _headers("server/discover", version="2025-11-25"),
        )

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32020)

    async def test_mcp_method_disagreement_is_header_mismatch(self):
        response = await self._post(
            _server(), CLAUDE_AI_DISCOVER, _headers("tools/list")
        )

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32020)

    async def test_tools_call_requires_matching_mcp_name(self):
        server = _server()
        request = _request(
            "tools/call",
            {"name": "ebird__get_hotspots", "arguments": {}},
            capabilities={},
        )

        missing = await self._post(server, request, _headers("tools/call"))
        self.assertEqual(missing["statusCode"], 400)
        self.assertEqual(json.loads(missing["body"])["error"]["code"], -32020)

        wrong = await self._post(
            server, request, _headers("tools/call", name="ebird__other")
        )
        self.assertEqual(wrong["statusCode"], 400)

        ok = await self._post(
            server, request, _headers("tools/call", name="ebird__get_hotspots")
        )
        self.assertEqual(ok["statusCode"], 200)
        self.assertEqual(server.plugin_manager.calls, [("ebird__get_hotspots", {})])

    async def test_mcp_name_base64_sentinel_is_decoded(self):
        server = _server()
        name = "ebird__get_hotspots"
        encoded = "=?base64?" + base64.b64encode(name.encode()).decode() + "?="
        request = _request(
            "tools/call", {"name": name, "arguments": {}}, capabilities={}
        )

        response = await self._post(
            server, request, _headers("tools/call", name=encoded)
        )

        self.assertEqual(response["statusCode"], 200)

    async def test_mcp_name_invalid_base64_is_header_mismatch(self):
        request = _request(
            "tools/call", {"name": "ebird__get_hotspots"}, capabilities={}
        )
        response = await self._post(
            _server(), request, _headers("tools/call", name="=?base64?!!!?=")
        )
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32020)

    async def test_tools_call_without_name_falls_through_to_invalid_params(self):
        """A missing params.name is the body's fault, not the header's:
        the caller gets the -32602 that names the missing field, not a
        header-mismatch complaint about a header it could not have set."""
        request = _request("tools/call", {"arguments": {}}, capabilities={})

        response = await self._post(_server(), request, _headers("tools/call"))

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32602)

    async def test_unknown_modern_method_is_404(self):
        for method in ("ping", "resources/list"):
            with self.subTest(method=method):
                response = await self._post(
                    _server(),
                    _request(method, capabilities={}),
                    _headers(method),
                )
                self.assertEqual(response["statusCode"], 404)
                self.assertEqual(
                    json.loads(response["body"])["error"]["code"], -32601
                )

    async def test_unknown_legacy_method_stays_200(self):
        """Legacy Streamable HTTP puts JSON-RPC errors in a 200, and the
        server/discover probe from an older-core-era client must keep
        looking like a legacy refusal, not a modern 404."""
        response = await self._post(
            _server(),
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
            {"mcp-protocol-version": "2025-11-25"},
        )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32601)

    async def test_malformed_meta_is_400(self):
        request = _request("tools/list")  # no clientCapabilities

        response = await self._post(_server(), request, _headers("tools/list"))

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32602)

    async def test_legacy_invalid_params_stays_200(self):
        """-32602 for a bad tool call is NOT a malformed envelope; the
        legacy transport keeps it at 200 as it always was."""
        response = await self._post(
            _server(),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
            {},
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32602)

    async def test_unsupported_modern_version_is_400(self):
        request = _request("tools/list", capabilities={}, version="2027-01-01")

        response = await self._post(
            _server(), request, _headers("tools/list", version="2027-01-01")
        )

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["id"], 1)
        self.assertEqual(body["error"]["code"], -32022)

    async def test_modern_header_with_legacy_body_is_header_mismatch(self):
        """The half-migrated case: header says 2026-07-28, body carries no
        _meta. Rejected as a modern request (the client is told which
        header/body pair disagrees) rather than silently served as legacy."""
        response = await self._post(
            _server(),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"mcp-protocol-version": MODERN},
        )

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32020)

    async def test_modern_notification_is_202(self):
        request = _request("notifications/anything", capabilities={})
        del request["id"]
        response = await self._post(
            _server(), request, _headers("notifications/anything")
        )
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(response["body"], "")

    async def test_legacy_notification_is_202(self):
        response = await self._post(
            _server(),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {},
        )

        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(response["body"], "")

    async def test_mixed_case_headers_are_normalised(self):
        """The adapters lowercase headers; the core must not depend on it."""
        response = await _server().handle_http_request(
            json.dumps(CLAUDE_AI_DISCOVER),
            {"MCP-Protocol-Version": MODERN, "Mcp-Method": "server/discover"},
        )
        self.assertEqual(response["statusCode"], 200)

    async def test_non_object_body_is_400(self):
        response = await _server().handle_http_request("[1, 2]", {})

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], -32600)


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_identity_logged_for_both_eras(self):
        server = _server()

        with self.assertLogs("core.mcp_server", level="INFO") as cap:
            await server.handle_request(CLAUDE_AI_DISCOVER)
            await server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "clientInfo": {"name": "claude-code", "version": "2.1.274"},
                    },
                }
            )

        received = [
            r for r in cap.records if r.getMessage() == "JSON-RPC request received"
        ]
        self.assertEqual(
            [r.mcp_era for r in received], [ERA_MODERN, ERA_LEGACY]
        )
        self.assertEqual(
            [r.mcp_client_name for r in received],
            ["Anthropic/ClaudeAI", "claude-code"],
        )
        self.assertEqual(
            [r.mcp_client_version for r in received], ["1.0.0", "2.1.274"]
        )
        self.assertEqual(
            [r.mcp_protocol_version for r in received], [MODERN, "2025-11-25"]
        )

    async def test_identity_fields_absent_when_not_sent(self):
        with self.assertLogs("core.mcp_server", level="INFO") as cap:
            await _server().handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            )
        rec = next(
            r for r in cap.records if r.getMessage() == "JSON-RPC request received"
        )
        self.assertEqual(rec.mcp_era, ERA_LEGACY)
        self.assertFalse(hasattr(rec, "mcp_client_name"))
        self.assertFalse(hasattr(rec, "mcp_protocol_version"))


if __name__ == "__main__":
    unittest.main()
