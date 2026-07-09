"""Universal HTTP handler for the eBird MCP server.

Cloud-agnostic request processing used by AWS Lambda (and any other adapter).
"""

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from core.logging_utils import (
    configure_json_logging,
    format_request_log,
    format_response_log,
)
from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager
from core.validators import (
    ConfigurationError,
    get_logging_config,
    load_and_validate_config,
)

# Configure JSON logging before any other loggers are created.
try:
    if os.environ.get("EBIRD_MCP_CONFIG"):
        config_json = os.environ.get("EBIRD_MCP_CONFIG")
        config = json.loads(config_json)
        logging_config = get_logging_config(config)
        log_level = logging_config.get("level", "INFO")
    else:
        config = load_and_validate_config("config.yaml")
        logging_config = get_logging_config(config)
        log_level = logging_config.get("level", "INFO")
except Exception:
    log_level = "INFO"

configure_json_logging(level=log_level, pretty=False)
logger = logging.getLogger(__name__)

# Globals for container reuse (warm starts)
_plugin_manager: Optional[PluginManager] = None
_mcp_server: Optional[MCPServer] = None
_config: Optional[Dict[str, Any]] = None


def _load_config() -> Dict[str, Any]:
    """Load configuration from env var (Terraform) or fall back to config.yaml."""
    global _config

    if _config is not None:
        return _config

    config_json = os.environ.get("EBIRD_MCP_CONFIG")
    if config_json:
        try:
            _config = json.loads(config_json)
            logger.info("Loaded configuration from environment variable")
            return _config
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse config from environment: {e}")
            raise

    try:
        _config = load_and_validate_config("config.yaml")
        logger.info("Loaded configuration from config.yaml")
        return _config
    except FileNotFoundError:
        logger.error(
            "No configuration found. Set EBIRD_MCP_CONFIG environment variable "
            "or ensure config.yaml exists."
        )
        raise


async def _initialize_server() -> None:
    """Initialize plugin manager and MCP server (cold-start path)."""
    global _plugin_manager, _mcp_server

    if _plugin_manager is not None and _mcp_server is not None:
        return

    try:
        config = _load_config()
        _plugin_manager = PluginManager(config)
        await _plugin_manager.load_plugins()
        _mcp_server = MCPServer(_plugin_manager)
        logger.info("eBird MCP server initialized successfully")
    except ConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        raise RuntimeError(f"Configuration error: {e}") from e
    except Exception as e:
        logger.error(f"Failed to initialize server: {e}", exc_info=True)
        raise


class UniversalHTTPHandler:
    """Universal HTTP handler for cloud-agnostic request processing."""

    # Browser origins allowed to call /mcp via cross-origin requests.
    # Native MCP clients (Claude Desktop, Claude Code) ignore CORS entirely.
    ALLOWED_ORIGINS = frozenset(
        {
            "https://claude.ai",
            "https://claude.com",  # claude.ai -> claude.com domain migration
            "https://console.anthropic.com",
            "http://localhost:6274",
            "http://127.0.0.1:6274",
        }
    )
    DEFAULT_CORS_ORIGIN = "https://claude.ai"

    def __init__(self) -> None:
        logger.info("UniversalHTTPHandler initialized")

    @classmethod
    def _get_cors_headers(
        cls, request_origin: Optional[str] = None
    ) -> Dict[str, str]:
        if request_origin and request_origin in cls.ALLOWED_ORIGINS:
            allow_origin = request_origin
        else:
            allow_origin = cls.DEFAULT_CORS_ORIGIN
        return {
            "Access-Control-Allow-Origin": allow_origin,
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            # mcp-protocol-version: sent on every request by clients speaking
            # protocol revision 2025-06-18+; must be allowed or browser
            # preflights fail.
            "Access-Control-Allow-Headers": (
                "content-type, accept, mcp-session-id, mcp-protocol-version"
            ),
            "Access-Control-Expose-Headers": "x-request-id, mcp-session-id",
        }

    async def handle_request(
        self,
        method: str,
        path: str,
        body: str,
        headers: Dict[str, str],
        request_id: Optional[str] = None,
    ) -> Tuple[int, Dict[str, str], str]:
        start_time = time.perf_counter()
        request_id = request_id or "unknown"

        # Normalize header names: the Lambda adapter lowercases them, but the
        # local aiohttp server passes them through as sent (e.g. "Origin").
        headers = {k.lower(): v for k, v in headers.items()} if headers else {}

        incoming_session_id = headers.get("mcp-session-id")
        request_origin = headers.get("origin")

        # Spec (Streamable HTTP): servers MUST validate the Origin header to
        # prevent DNS-rebinding attacks and MUST respond 403 when it is
        # present and invalid. Non-browser clients send no Origin header.
        if request_origin and request_origin not in self.ALLOWED_ORIGINS:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.warning(
                f"403: Origin '{request_origin}' not in allowlist",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "origin": request_origin,
                    "duration_ms": duration_ms,
                },
            )
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Forbidden",
                        "data": "Origin not allowed",
                    },
                }
            )
            error_headers = {"Content-Type": "application/json; charset=utf-8"}
            error_headers.update(self._get_cors_headers(request_origin))
            return (403, error_headers, error_body)

        # Spec (Streamable HTTP): a request carrying an invalid or
        # unsupported MCP-Protocol-Version header MUST get 400 Bad Request.
        # An absent header is fine (assume 2025-03-26 per spec; this server
        # behaves identically across revisions).
        protocol_version_header = headers.get("mcp-protocol-version")
        if (
            protocol_version_header
            and protocol_version_header not in MCPServer.SUPPORTED_PROTOCOL_VERSIONS
        ):
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.warning(
                f"400: Unsupported MCP-Protocol-Version '{protocol_version_header}'",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "mcp_protocol_version": protocol_version_header,
                    "duration_ms": duration_ms,
                },
            )
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Bad Request",
                        "data": (
                            f"Unsupported MCP-Protocol-Version: "
                            f"{protocol_version_header}. Supported: "
                            f"{', '.join(MCPServer.SUPPORTED_PROTOCOL_VERSIONS)}"
                        ),
                    },
                }
            )
            error_headers = {"Content-Type": "application/json; charset=utf-8"}
            error_headers.update(self._get_cors_headers(request_origin))
            return (400, error_headers, error_body)

        if path != "/mcp":
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32601,
                        "message": "Not Found",
                        "data": f"Path '{path}' not found. Expected '/mcp'",
                    },
                }
            )
            logger.warning(
                f"404 error: Path '{path}' not found",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "duration_ms": duration_ms,
                },
            )
            error_headers = {"Content-Type": "application/json; charset=utf-8"}
            error_headers.update(self._get_cors_headers(request_origin))
            return (404, error_headers, error_body)

        if method != "POST":
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32601,
                        "message": "Method Not Allowed",
                        "data": f"Method '{method}' not allowed. Expected 'POST'",
                    },
                }
            )
            logger.warning(
                f"405 error: Method '{method}' not allowed",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "duration_ms": duration_ms,
                },
            )
            error_headers = {"Content-Type": "application/json; charset=utf-8", "Allow": "POST"}
            error_headers.update(self._get_cors_headers(request_origin))
            return (405, error_headers, error_body)

        # Pre-parse JSON only to detect initialize and mint a session ID.
        try:
            request_json = json.loads(body)
            is_initialize = request_json.get("method") == "initialize"
        except (json.JSONDecodeError, AttributeError):
            is_initialize = False

        session_id = None
        if is_initialize:
            session_id = str(uuid.uuid4())
            logger.info(
                f"Initialize request detected, generating session ID: {session_id}",
                extra={"request_id": request_id, "mcp_session_id": session_id},
            )

        effective_session_id = session_id or incoming_session_id

        request_log_data = format_request_log(
            request_id=request_id,
            http_method=method,
            request_path=path,
            headers=headers,
            body=body,
            lambda_context=None,
        )
        if effective_session_id:
            request_log_data["mcp_session_id"] = effective_session_id
        logger.info("Incoming HTTP request", extra=request_log_data)

        try:
            await _initialize_server()
            response = await _mcp_server.handle_http_request(body, headers)

            status_code = response.get("statusCode", 200)
            response_body = response.get("body", "")
            response_headers = response.get("headers", {}).copy()

            if session_id:
                response_headers["Mcp-Session-Id"] = session_id
            response_headers["X-Request-ID"] = request_id

            if "Content-Type" not in response_headers:
                response_headers["Content-Type"] = "application/json"
            response_headers.update(self._get_cors_headers(request_origin))

            duration_ms = (time.perf_counter() - start_time) * 1000
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=status_code,
                headers=response_headers,
                body=response_body,
                duration_ms=duration_ms,
                success=True,
            )
            if effective_session_id:
                response_log_data["mcp_session_id"] = effective_session_id
            logger.info("HTTP request processed successfully", extra=response_log_data)

            return (status_code, response_headers, response_body)

        except ConfigurationError as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Server configuration error",
                        "data": f"Request ID: {request_id}",
                    },
                }
            )
            error_headers = {"Content-Type": "application/json; charset=utf-8"}
            error_headers.update(self._get_cors_headers(request_origin))
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=500,
                headers=error_headers,
                body=error_body,
                duration_ms=duration_ms,
                success=False,
            )
            if effective_session_id:
                response_log_data["mcp_session_id"] = effective_session_id
            logger.error(
                f"Configuration error in request {request_id}: {e}",
                extra={**response_log_data, "error_type": "ConfigurationError"},
                exc_info=True,
            )
            return (500, error_headers, error_body)

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": f"Request ID: {request_id}",
                    },
                }
            )
            error_headers = {"Content-Type": "application/json; charset=utf-8"}
            error_headers.update(self._get_cors_headers(request_origin))
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=500,
                headers=error_headers,
                body=error_body,
                duration_ms=duration_ms,
                success=False,
            )
            if effective_session_id:
                response_log_data["mcp_session_id"] = effective_session_id
            logger.error(
                f"Error processing request {request_id}: {e}",
                extra={**response_log_data, "error_type": type(e).__name__},
                exc_info=True,
            )
            return (500, error_headers, error_body)

    def handle_options(
        self,
        request_id: Optional[str] = None,
        request_origin: Optional[str] = None,
    ) -> Tuple[int, Dict[str, str], str]:
        request_id = request_id or "unknown"
        cors_headers = {
            **self._get_cors_headers(request_origin),
            "Access-Control-Max-Age": "86400",
            "Content-Type": "application/json; charset=utf-8",
            "X-Request-ID": request_id,
        }
        logger.info(
            "CORS preflight OPTIONS request handled",
            extra={"request_id": request_id},
        )
        return (200, cors_headers, "")
