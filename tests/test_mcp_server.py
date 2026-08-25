"""JSON-RPC dispatcher behaviour, especially error classification.

The theme these tests pin: a -32603 "Internal error" plus a logged
traceback is a claim that the SERVER broke. Spending one on "you named a
tool that doesn't exist" misleads whoever reads the error log and tells
the calling model nothing it can act on. Caller mistakes get their own
JSON-RPC code, an actionable ``data`` payload, and a WARNING-level log
with no traceback; genuine faults keep -32603, the scrubbed correlation
ID, and the full trace.

Run with::

    python -m unittest tests.test_mcp_server
"""

import logging
import unittest
from typing import Any, Dict, List, Optional

from core.interfaces import ToolResult, UnknownToolError
from core.mcp_server import MCPServer


class _StubPluginManager:
    """Minimal PluginManager stand-in.

    ``execute_tool`` mirrors the real one's contract: unknown names raise
    UnknownToolError, and a configured ``raises`` is thrown to simulate a
    genuine server fault.
    """

    def __init__(self, tools: Optional[List[str]] = None, raises=None) -> None:
        self.tools = tools if tools is not None else ["ebird__get_hotspots"]
        self.raises = raises
        self.calls: List[tuple] = []

    def get_all_tools(self) -> List[Dict[str, Any]]:
        return [
            {"name": name, "description": "", "inputSchema": {}}
            for name in self.tools
        ]

    def get_instructions(self) -> Optional[str]:
        return None

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        self.calls.append((tool_name, arguments))
        if self.raises is not None:
            raise self.raises
        if tool_name not in self.tools:
            raise UnknownToolError(tool_name, ", ".join(sorted(self.tools)))
        return ToolResult(
            content=[{"type": "text", "text": "ok"}], success=True
        )


def _call(name: str = "ebird__get_hotspots", **params) -> Dict[str, Any]:
    request: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, **params},
    }
    return request


class ProtocolNegotiationTests(unittest.IsolatedAsyncioTestCase):
    """Version negotiation, including the revision we deliberately refuse."""

    async def _initialize(self, requested: Optional[str]) -> Dict[str, Any]:
        server = MCPServer(_StubPluginManager())
        params: Dict[str, Any] = {}
        if requested is not None:
            params["protocolVersion"] = requested
        response = await server.handle_request(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": params}
        )
        return response["result"]

    async def test_supported_versions_are_echoed_back(self):
        for version in MCPServer.SUPPORTED_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                result = await self._initialize(version)
                self.assertEqual(result["protocolVersion"], version)

    async def test_2026_07_28_is_not_advertised_as_supported(self):
        """It replaces initialize with server/discover — a real migration.

        Listing it would be a claim we implement a handshake we do not.
        """
        self.assertNotIn("2026-07-28", MCPServer.SUPPORTED_PROTOCOL_VERSIONS)

    async def test_unsupported_version_downgrades_to_newest_supported(self):
        result = await self._initialize("2026-07-28")
        self.assertEqual(
            result["protocolVersion"],
            MCPServer.SUPPORTED_PROTOCOL_VERSIONS[-1],
            "An unsupported request must be answered with the newest "
            "revision we really speak, so the client can decide.",
        )

    async def test_newest_supported_is_last_in_the_tuple(self):
        """The downgrade path indexes [-1]; ordering is load-bearing."""
        versions = list(MCPServer.SUPPORTED_PROTOCOL_VERSIONS)
        self.assertEqual(
            versions,
            sorted(versions),
            "SUPPORTED_PROTOCOL_VERSIONS must stay in ascending date order",
        )

    async def test_missing_version_still_initializes(self):
        result = await self._initialize(None)
        self.assertEqual(
            result["protocolVersion"],
            MCPServer.SUPPORTED_PROTOCOL_VERSIONS[-1],
        )


class UnknownToolTests(unittest.IsolatedAsyncioTestCase):
    """An unknown tool name is a caller error (-32602), not a fault."""

    async def test_unknown_tool_returns_invalid_params(self):
        server = MCPServer(_StubPluginManager())
        response = await server.handle_request(_call("ebird__nope"))
        error = response["error"]
        self.assertEqual(
            error["code"],
            -32602,
            "Unknown tool must be -32602 (Invalid params), not -32603 "
            "Internal error — it is the caller's mistake, not ours.",
        )

    async def test_message_follows_the_spec_example_shape(self):
        """The tools spec's own example is 'Unknown tool: <name>'."""
        server = MCPServer(_StubPluginManager())
        response = await server.handle_request(_call("ebird__nope"))
        self.assertEqual(response["error"]["message"], "Unknown tool: ebird__nope")

    async def test_available_tools_travel_in_data(self):
        """A model can self-correct only if it is told what does exist."""
        server = MCPServer(
            _StubPluginManager(tools=["ebird__get_hotspots", "ebird__get_taxonomy"])
        )
        response = await server.handle_request(_call("ebird__nope"))
        data = response["error"]["data"]
        self.assertIn("ebird__get_hotspots", data)
        self.assertIn("ebird__get_taxonomy", data)

    async def test_no_error_id_minted_for_a_caller_error(self):
        """The scrubbed correlation ID is for faults we must investigate.

        Handing one out for a malformed request invites someone to go
        looking in CloudWatch for a server bug that does not exist.
        """
        server = MCPServer(_StubPluginManager())
        response = await server.handle_request(_call("ebird__nope"))
        self.assertNotIn("Error ID", response["error"]["data"])

    async def test_logged_as_warning_without_a_traceback(self):
        server = MCPServer(_StubPluginManager())
        with self.assertLogs("core.mcp_server", level="DEBUG") as captured:
            await server.handle_request(_call("ebird__nope"))
        records = [
            r for r in captured.records if r.levelno >= logging.WARNING
        ]
        self.assertTrue(records, "expected a WARNING-level record")
        self.assertTrue(
            all(r.levelno == logging.WARNING for r in records),
            "a caller error must not log at ERROR",
        )
        self.assertTrue(
            all(r.exc_info is None for r in records),
            "a caller error must not carry a traceback",
        )


class MalformedCallToolRequestTests(unittest.IsolatedAsyncioTestCase):
    """A request that never described a valid call is -32602, not -32603.

    The `arguments` case is the nastier of the two: before the fix a
    non-object reached the plugin, which called dict()/.get() on it, and
    the caller got a raw Python message back as a tool RESULT with
    isError: true — as though the tool had run and failed.
    """

    async def _post(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.manager = _StubPluginManager()
        server = MCPServer(self.manager)
        return await server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        )

    async def test_missing_name_is_invalid_params(self):
        response = await self._post({"arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["message"], "Invalid params")

    async def test_empty_name_is_invalid_params(self):
        response = await self._post({"name": "", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)

    async def test_string_arguments_is_invalid_params(self):
        response = await self._post(
            {"name": "ebird__get_hotspots", "arguments": "oops"}
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("must be an object", response["error"]["data"])
        self.assertIn("str", response["error"]["data"])

    async def test_list_arguments_is_invalid_params(self):
        response = await self._post(
            {"name": "ebird__get_hotspots", "arguments": [1, 2]}
        )
        self.assertEqual(response["error"]["code"], -32602)

    async def test_malformed_arguments_never_reaches_the_plugin(self):
        """The plugin must not be handed a shape it cannot process.

        This is what turned a protocol error into a fake tool failure.
        """
        await self._post({"name": "ebird__get_hotspots", "arguments": "oops"})
        self.assertEqual(
            self.manager.calls,
            [],
            "execute_tool must not be called for a malformed request",
        )

    async def test_no_python_internals_leak_to_the_caller(self):
        response = await self._post(
            {"name": "ebird__get_hotspots", "arguments": "oops"}
        )
        blob = str(response)
        self.assertNotIn("dictionary update sequence", blob)
        self.assertNotIn("has no attribute", blob)

    async def test_omitting_arguments_entirely_is_valid(self):
        """Absent `arguments` defaults to {} — that IS a valid call."""
        response = await self._post({"name": "ebird__get_hotspots"})
        self.assertNotIn("error", response)
        self.assertEqual(self.manager.calls, [("ebird__get_hotspots", {})])

    async def test_malformed_request_logged_as_warning(self):
        manager = _StubPluginManager()
        server = MCPServer(manager)
        with self.assertLogs("core.mcp_server", level="DEBUG") as captured:
            await server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "ebird__get_hotspots", "arguments": "x"},
                }
            )
        errors = [r for r in captured.records if r.levelno >= logging.ERROR]
        self.assertEqual(errors, [], "a malformed request is not a server fault")


class GenuineFaultTests(unittest.IsolatedAsyncioTestCase):
    """The mapping must not quietly swallow real failures."""

    async def test_runtime_error_still_returns_internal_error(self):
        server = MCPServer(_StubPluginManager(raises=RuntimeError("boom")))
        response = await server.handle_request(_call())
        self.assertEqual(response["error"]["code"], -32603)
        self.assertEqual(response["error"]["message"], "Internal error")

    async def test_fault_text_is_scrubbed_behind_an_error_id(self):
        """CLAUDE.md: never surface str(e) to the client."""
        server = MCPServer(
            _StubPluginManager(raises=RuntimeError("secret /path/detail"))
        )
        response = await server.handle_request(_call())
        data = response["error"]["data"]
        self.assertIn("Error ID:", data)
        self.assertNotIn("secret", data)
        self.assertNotIn("/path/detail", data)

    async def test_fault_logged_as_error_with_traceback(self):
        server = MCPServer(_StubPluginManager(raises=RuntimeError("boom")))
        with self.assertLogs("core.mcp_server", level="DEBUG") as captured:
            await server.handle_request(_call())
        errors = [r for r in captured.records if r.levelno == logging.ERROR]
        self.assertTrue(errors, "a genuine fault must log at ERROR")
        self.assertTrue(
            any(r.exc_info for r in errors),
            "a genuine fault must keep its traceback",
        )

    async def test_value_error_is_not_mistaken_for_a_caller_error(self):
        """Only the declared caller-error types get -32602.

        Inferring "caller error" from ValueError would misfile real
        failures — json.JSONDecodeError subclasses it, so a malformed
        upstream payload would lose its traceback.
        """
        server = MCPServer(_StubPluginManager(raises=ValueError("upstream junk")))
        response = await server.handle_request(_call())
        self.assertEqual(response["error"]["code"], -32603)


if __name__ == "__main__":
    unittest.main()
