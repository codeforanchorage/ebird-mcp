"""MCP Server: handles JSON-RPC protocol and routes to Plugin Manager."""

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from core.interfaces import InvalidToolParamsError, UnknownToolError
from core.logging_utils import (
    format_jsonrpc_request_log,
    format_jsonrpc_response_log,
)
from core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


def _describe_invalid_params(exc: "InvalidToolParamsError") -> Tuple[int, str, str]:
    # The request never described a valid call, so `data` says which part
    # of the CallToolRequest schema it failed.
    return -32602, "Invalid params", str(exc)


def _describe_unknown_tool(exc: UnknownToolError) -> Tuple[int, str, str]:
    # Shape follows the tools spec's own example:
    # {"code": -32602, "message": "Unknown tool: <name>"}.
    data = f"Available tools: {exc.available}" if exc.available else str(exc)
    return -32602, str(exc), data


# Caller errors: the client's mistake, not a server fault. Each gets its own
# JSON-RPC code, a WARNING-level log with no traceback, and — unlike a
# genuine fault — a `data` payload the caller can act on, because there is
# nothing sensitive in "you named a tool that does not exist".
#
# Deliberately a mapping rather than a chain of conditionals: the next
# caller-error code should be one more row here, not another nested branch.
# Order matters only if two entries could match the same exception.
_CALLER_ERRORS: Tuple[Tuple[type, Any], ...] = (
    (UnknownToolError, _describe_unknown_tool),
    (InvalidToolParamsError, _describe_invalid_params),
)


def _classify_error(exc: Exception) -> Tuple[int, str, Optional[str], bool]:
    """Map an exception to (code, message, data, is_caller_error).

    ``data`` is None for genuine faults; the caller substitutes a scrubbed
    correlation ID so exception text never reaches the client.
    """
    for exc_type, describe in _CALLER_ERRORS:
        if isinstance(exc, exc_type):
            code, message, data = describe(exc)
            return code, message, data, True
    return -32603, "Internal error", None, False


class MCPServer:
    """MCP Server that handles JSON-RPC requests."""

    # Protocol revisions this server can speak. The server is stateless per
    # request, so every revision is served identically; negotiation just
    # echoes the client's requested version when we support it. Extend this
    # tuple (newest last) when a new spec revision ships — but check the new
    # revision's transport MUSTs against server/http_handler.py first.
    #
    # 2026-07-28 is deliberately NOT listed. It is not a version-string
    # addition like the four below: it replaces the initialize handshake
    # with per-request `_meta` plus a mandatory `server/discover` RPC. That
    # is a dual-era migration needing real work here, not a tuple entry, and
    # claiming support without implementing it would strand clients that
    # take us at our word.
    #
    # Related, in server/http_handler.py: an unrecognized
    # MCP-Protocol-Version returns 400 with -32600 rather than the
    # 2026-07-28 UnsupportedProtocolVersionError (-32022). That is
    # deliberate too — a dual-era client then reads us as a legacy server
    # and falls back to initialize instead of retrying.
    SUPPORTED_PROTOCOL_VERSIONS = (
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    )

    def __init__(self, plugin_manager: PluginManager) -> None:
        self.plugin_manager = plugin_manager

    async def handle_request(
        self,
        request: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        start_time = time.perf_counter()
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        is_notification = request_id is None

        request_log_data = format_jsonrpc_request_log(
            request_id=request_id,
            method=method,
            params=params,
            is_notification=is_notification,
        )
        if session_id:
            request_log_data["mcp_session_id"] = session_id
        logger.info("JSON-RPC request received", extra=request_log_data)

        try:
            if method == "initialize":
                result = await self._handle_initialize(params)
            elif method == "tools/list":
                result = await self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tools_call(params)
            elif method == "ping":
                # Spec: ping result is an empty object.
                result = {}
            elif method == "notifications/initialized":
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "JSON-RPC notification processed",
                    extra={
                        **request_log_data,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                return None
            else:
                if is_notification:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    logger.warning(
                        f"Ignoring unknown notification method: {method}",
                        extra={
                            **request_log_data,
                            "duration_ms": round(duration_ms, 2),
                        },
                    )
                    return None
                # JSON-RPC: unknown method is -32601, not an internal error.
                # Clients probe for optional methods (resources/list, etc.);
                # don't log those probes as server errors.
                duration_ms = (time.perf_counter() - start_time) * 1000
                error = {
                    "code": -32601,
                    "message": "Method not found",
                    "data": f"Unknown method: {method}",
                }
                response_log_data = format_jsonrpc_response_log(
                    request_id=request_id,
                    method=method,
                    error=error,
                    duration_ms=duration_ms,
                )
                if session_id:
                    response_log_data["mcp_session_id"] = session_id
                logger.warning(
                    f"Unknown JSON-RPC method: {method}", extra=response_log_data
                )
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": error,
                }

            if is_notification:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "JSON-RPC notification processed",
                    extra={
                        **request_log_data,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                return None

            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }

            duration_ms = (time.perf_counter() - start_time) * 1000
            response_log_data = format_jsonrpc_response_log(
                request_id=request_id,
                method=method,
                result=result,
                duration_ms=duration_ms,
            )
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            logger.info(
                "JSON-RPC request processed successfully", extra=response_log_data
            )

            return response

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            code, message, data, is_caller_error = _classify_error(e)

            # Genuine faults: don't echo exception text to the client — it
            # can leak file paths, library internals, or traceback
            # fragments. Mint a correlation ID and log the full exception to
            # CloudWatch. Caller errors carry their own actionable `data`
            # and need no correlation ID; the client can fix the request.
            error_id = None
            if not is_caller_error:
                error_id = uuid.uuid4().hex
                data = f"Error ID: {error_id}"

            error_response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": code,
                    "message": message,
                    "data": data,
                },
            }
            response_log_data = format_jsonrpc_response_log(
                request_id=request_id,
                method=method,
                error={"code": code, "message": message, "data": str(e)},
                duration_ms=duration_ms,
            )
            if session_id:
                response_log_data["mcp_session_id"] = session_id

            extra = {**response_log_data, "error_type": type(e).__name__}
            if is_caller_error:
                # No traceback: a malformed request is not a server fault,
                # and a stack trace here is noise that hides real ones.
                logger.warning(
                    f"Caller error handling JSON-RPC request {method}: {e}",
                    extra=extra,
                )
            else:
                extra["error_id"] = error_id
                logger.error(
                    f"Error handling JSON-RPC request {method} "
                    f"[error_id={error_id}]: {e}",
                    extra=extra,
                    exc_info=True,
                )
            if is_notification:
                return None
            return error_response

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # Version negotiation (spec): if we support the client's requested
        # version, echo it back; otherwise answer with the latest we support
        # and let the client decide whether to proceed or disconnect.
        requested = params.get("protocolVersion")
        if requested in self.SUPPORTED_PROTOCOL_VERSIONS:
            negotiated = requested
        else:
            negotiated = self.SUPPORTED_PROTOCOL_VERSIONS[-1]
        result = {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "ebird-mcp",
                "version": "1.0.0",
            },
        }
        instructions = (
            self.plugin_manager.get_instructions() if self.plugin_manager else None
        )
        if instructions:
            result["instructions"] = instructions
        return result

    async def _handle_tools_list(self) -> Dict[str, Any]:
        tools = self.plugin_manager.get_all_tools()
        return {"tools": tools}

    async def _handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        # Validate the request shape before dispatch. Both of these are
        # malformed CallToolRequests, not server faults: without this, a
        # missing name surfaced as -32603 "Internal error", and a non-object
        # `arguments` reached the plugin — which called dict()/.get() on it
        # and handed the caller a raw Python message ("dictionary update
        # sequence element #0 has length 1; 2 is required") dressed up as a
        # tool RESULT with isError: true, as though the tool had run.
        if not tool_name:
            raise InvalidToolParamsError(
                "Missing required parameter 'name' (the tool to call)"
            )
        if not isinstance(arguments, dict):
            raise InvalidToolParamsError(
                f"Parameter 'arguments' must be an object, got "
                f"{type(arguments).__name__}"
            )

        result = await self.plugin_manager.execute_tool(tool_name, arguments)

        if result.success:
            response: Dict[str, Any] = {"content": result.content}
            # `structuredContent` is the machine-readable twin of `content`.
            # Only tools that declare an outputSchema populate it, and the
            # spec requires the value conform to that schema. `content`
            # still carries the human-readable rendering, so clients that
            # ignore structured output are unaffected.
            if result.structured_content is not None:
                response["structuredContent"] = result.structured_content
            return response
        else:
            error_msg = result.error_message or "An unknown error occurred"
            # Include error in content so all clients receive it.
            content = (
                result.content
                if result.content
                else [{"type": "text", "text": error_msg}]
            )
            return {
                "content": content,
                "isError": True,
                "error": error_msg,
            }

    async def handle_http_request(
        self, body: str, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        try:
            request = json.loads(body)
        except json.JSONDecodeError as e:
            # A malformed body is the caller's mistake. -32700 "Parse error"
            # already tells them exactly that; our own parse traceback adds
            # nothing and reads as a server fault.
            logger.warning(
                f"Invalid JSON in request body: {e}",
                extra={"error_type": "JSONDecodeError"},
            )
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json; charset=utf-8"},
                "body": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": "Parse error",
                            "data": str(e),
                        },
                    },
                    ensure_ascii=False,
                ),
            }

        # JSON-RPC batching was removed in protocol revision 2025-06-18 and
        # was never supported here. Reject arrays (and any other non-object
        # payload) with -32600 instead of crashing to a 500.
        if not isinstance(request, dict):
            logger.warning(
                "Rejected non-object JSON-RPC payload (batching is not supported)",
                extra={"payload_type": type(request).__name__},
            )
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json; charset=utf-8"},
                "body": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32600,
                            "message": "Invalid Request",
                            "data": "Request must be a single JSON-RPC object; "
                            "batching is not supported",
                        },
                    },
                    ensure_ascii=False,
                ),
            }

        session_id = None
        if headers:
            session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")

        response = await self.handle_request(request, session_id=session_id)

        if response is None:
            # Streamable HTTP spec: notifications get 202 Accepted, no body.
            return {
                "statusCode": 202,
                "headers": {"Content-Type": "application/json; charset=utf-8"},
                "body": "",
            }

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(response, ensure_ascii=False),
        }
