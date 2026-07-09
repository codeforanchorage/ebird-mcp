"""MCP Server: handles JSON-RPC protocol and routes to Plugin Manager."""

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from core.logging_utils import (
    format_jsonrpc_request_log,
    format_jsonrpc_response_log,
)
from core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class MCPServer:
    """MCP Server that handles JSON-RPC requests."""

    # Protocol revisions this server can speak. The server is stateless per
    # request, so every revision is served identically; negotiation just
    # echoes the client's requested version when we support it. Extend this
    # tuple (newest last) when a new spec revision ships.
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
            # Don't echo exception text to the client — it can leak file
            # paths, library internals, or traceback fragments. Mint a
            # correlation ID and log the full exception to CloudWatch.
            error_id = uuid.uuid4().hex
            error_response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": f"Error ID: {error_id}",
                },
            }
            response_log_data = format_jsonrpc_response_log(
                request_id=request_id,
                method=method,
                error={"code": -32603, "message": "Internal error", "data": str(e)},
                duration_ms=duration_ms,
            )
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            logger.error(
                f"Error handling JSON-RPC request {method} [error_id={error_id}]: {e}",
                extra={
                    **response_log_data,
                    "error_type": type(e).__name__,
                    "error_id": error_id,
                },
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

        if not tool_name:
            raise ValueError("Tool name is required")

        result = await self.plugin_manager.execute_tool(tool_name, arguments)

        if result.success:
            return {"content": result.content}
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
            logger.error(
                f"Invalid JSON in request body: {e}",
                extra={"error_type": "JSONDecodeError"},
                exc_info=True,
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
