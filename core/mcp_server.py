"""MCP Server: handles JSON-RPC protocol and routes to Plugin Manager.

This is a DUAL-ERA server (spec 2026-07-28, "Versioning and Compatibility"):

* Legacy era (2025-11-25 and earlier): the client opens with an
  ``initialize`` handshake, the transport mints an ``Mcp-Session-Id`` for
  log correlation, and every later request rides on that session.
* Modern era (2026-07-28 and later): there is no handshake. Every request
  carries its protocol version, client identity and capabilities in
  ``params._meta`` and is served statelessly; ``server/discover`` replaces
  ``initialize`` as the way to learn what the server supports.

The era is chosen per request from how the client opens: an ``initialize``
request is legacy; a request whose ``_meta`` carries
``io.modelcontextprotocol/protocolVersion`` is modern. Both are served on
the same endpoint, which is what lets a dual-era client (claude.ai, Claude
Code) skip the "probe with 2026-07-28, get 400, fall back to initialize"
round trip every session used to start with. Legacy behaviour is
unchanged: a legacy client sees exactly what it saw before.
"""

import base64
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

# Reserved `_meta` keys (spec 2026-07-28 "General fields > _meta").
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

ERA_LEGACY = "legacy"
ERA_MODERN = "modern"

_JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


class MethodNotFoundError(Exception):
    """The request names a method this server does not implement.

    JSON-RPC -32601 ("Method not found"), never -32603: clients probe for
    optional MCP methods (resources/list, etc.) and rely on the
    distinction. On the modern HTTP transport the spec additionally
    requires HTTP 404 for this case; legacy stays 200.
    """


class MalformedRequestError(Exception):
    """A modern request missing a required ``_meta`` field.

    Spec: "A request missing any required field is malformed; the server
    MUST reject it with -32602 (Invalid params). On HTTP, the response
    status MUST be 400." Kept distinct from InvalidToolParamsError, which
    is also -32602 but stays HTTP 200 -- a bad tool argument is not a
    malformed envelope, and a 400 with a non-modern-specific body is what
    makes a dual-era client think it is talking to a legacy server.
    """


class UnsupportedProtocolVersionError(Exception):
    """The request declares a protocol version this server does not serve.

    Mapped to -32022 with ``data.supported`` / ``data.requested`` so the
    client can retry with a mutually supported version (spec "Protocol
    Version Negotiation"). HTTP 400.
    """

    def __init__(self, requested: Optional[str]) -> None:
        super().__init__(f"Unsupported protocol version '{requested}'")
        self.requested = requested


def _describe_invalid_params(exc: "InvalidToolParamsError") -> Tuple[int, str, Any]:
    # The request never described a valid call, so `data` says which part
    # of the CallToolRequest schema it failed.
    return -32602, "Invalid params", str(exc)


def _describe_unknown_tool(exc: UnknownToolError) -> Tuple[int, str, Any]:
    # Shape follows the tools spec's own example:
    # {"code": -32602, "message": "Unknown tool: <name>"}.
    data = f"Available tools: {exc.available}" if exc.available else str(exc)
    return -32602, str(exc), data


def _describe_method_not_found(exc: MethodNotFoundError) -> Tuple[int, str, Any]:
    return -32601, "Method not found", str(exc)


def _describe_malformed_request(exc: MalformedRequestError) -> Tuple[int, str, Any]:
    return -32602, "Invalid params", str(exc)


def _describe_unsupported_version(
    exc: UnsupportedProtocolVersionError,
) -> Tuple[int, str, Any]:
    return (
        -32022,
        "Unsupported protocol version",
        {
            "supported": list(MCPServer.SUPPORTED_PROTOCOL_VERSIONS),
            "requested": exc.requested,
        },
    )


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
    (MethodNotFoundError, _describe_method_not_found),
    (MalformedRequestError, _describe_malformed_request),
    (UnsupportedProtocolVersionError, _describe_unsupported_version),
)

# HTTP status a caller error carries in the MODERN era (spec 2026-07-28
# Streamable HTTP). Legacy responses are always 200: there a JSON-RPC
# error is still a successful HTTP exchange, and changing that would make
# a legacy client's probe look like a transport failure.
_MODERN_HTTP_STATUS: Tuple[Tuple[type, int], ...] = (
    (MethodNotFoundError, 404),
    (MalformedRequestError, 400),
    (UnsupportedProtocolVersionError, 400),
)


def _classify_error(exc: Exception) -> Tuple[int, str, Any, bool]:
    """Map an exception to (code, message, data, is_caller_error).

    ``data`` is None for genuine faults; the caller substitutes a scrubbed
    correlation ID so exception text never reaches the client.
    """
    for exc_type, describe in _CALLER_ERRORS:
        if isinstance(exc, exc_type):
            code, message, data = describe(exc)
            return code, message, data, True
    return -32603, "Internal error", None, False


def _modern_http_status(exc: Exception) -> int:
    for exc_type, status in _MODERN_HTTP_STATUS:
        if isinstance(exc, exc_type):
            return status
    return 200


class MCPServer:
    """MCP Server that handles JSON-RPC requests."""

    # Protocol revisions this server implements, newest first.
    #
    # MODERN revisions drop the initialize handshake: version, identity and
    # capabilities ride in every request's `_meta`, `server/discover` is
    # mandatory, `ping`/sessions/GET-SSE are gone, and every result carries
    # `resultType`. LEGACY revisions open with `initialize` and are served
    # exactly as before. The wire format for a tools-only server is
    # otherwise compatible across each group -- the additions each revision
    # brings (icons, tasks, elicitation) are optional and unused here -- so
    # we echo the client's requested version when it's one we recognize.
    #
    # Related, in server/http_handler.py: the transport-level header check
    # accepts anything in SUPPORTED_PROTOCOL_VERSIONS and refuses the rest
    # with -32022 listing this tuple.
    MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)
    LEGACY_PROTOCOL_VERSIONS = (
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
    )
    SUPPORTED_PROTOCOL_VERSIONS = MODERN_PROTOCOL_VERSIONS + LEGACY_PROTOCOL_VERSIONS

    # Cache hints on modern results (spec "CacheableResult"). The tool set
    # and server identity only change on redeploy, so both are safe to
    # cache; tools/list gets the shorter window so a renamed tool is not
    # stale for long after a deploy.
    TOOLS_LIST_TTL_MS = 300_000
    DISCOVER_TTL_MS = 3_600_000

    # Methods whose Mcp-Name header must mirror `params.name` / `params.uri`.
    _NAMED_METHODS = {
        "tools/call": "name",
        "resources/read": "uri",
        "prompts/get": "name",
    }

    def __init__(self, plugin_manager: PluginManager) -> None:
        self.plugin_manager = plugin_manager

    # ── Era classification ────────────────────────────────────────────

    @staticmethod
    def _request_meta(request: Dict[str, Any]) -> Dict[str, Any]:
        """The request's ``params._meta`` object, or {} if absent/invalid."""
        params = request.get("params")
        if not isinstance(params, dict):
            return {}
        meta = params.get("_meta")
        return meta if isinstance(meta, dict) else {}

    @classmethod
    def request_era(
        cls,
        request: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """Which protocol era a request belongs to.

        Spec ("Backward Compatibility with Initialization-Based Versions"):
        a dual-era server selects its behaviour from how the client opens --
        an ``initialize`` request is legacy, a request carrying modern
        per-request ``_meta`` is modern. A body without ``_meta`` under a
        modern ``MCP-Protocol-Version`` header is a malformed modern
        request, so it is classified modern and rejected downstream rather
        than being quietly served as legacy. Header names are looked up
        lowercased (both adapters normalise them that way).
        """
        if request.get("method") == "initialize":
            return ERA_LEGACY
        if META_PROTOCOL_VERSION in cls._request_meta(request):
            return ERA_MODERN
        declared = (headers or {}).get("mcp-protocol-version")
        if declared in cls.MODERN_PROTOCOL_VERSIONS:
            return ERA_MODERN
        return ERA_LEGACY

    # ── Request handling ──────────────────────────────────────────────

    async def handle_request(
        self,
        request: Dict[str, Any],
        session_id: Optional[str] = None,
        era: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC request; None for a notification.

        ``session_id`` is for log correlation only (legacy era; modern
        requests have no session). ``era`` is ERA_LEGACY / ERA_MODERN, or
        None to classify from the request body alone.
        """
        response, _status = await self._handle_request_with_status(
            request, session_id=session_id, era=era
        )
        return response

    async def _handle_request_with_status(
        self,
        request: Dict[str, Any],
        session_id: Optional[str] = None,
        era: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        """handle_request plus the HTTP status the response should carry.

        Legacy responses are always 200 (a JSON-RPC error is still a
        successful HTTP exchange). Modern responses follow the 2026-07-28
        Streamable HTTP rules: 400 for an unsupported version or malformed
        ``_meta``, 404 for an unknown method. A notification is 202.
        """
        start_time = time.perf_counter()
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            params = {}
        if era is None:
            era = self.request_era(request)
        meta = self._request_meta(request)

        is_notification = request_id is None

        request_log_data = format_jsonrpc_request_log(
            request_id=request_id,
            method=method,
            params=params,
            is_notification=is_notification,
        )
        request_log_data["mcp_era"] = era
        # Flatten client identity onto the log line for BOTH eras, so
        # client-family dashboards keep working once claude.ai stops
        # sending initialize (where they used to read clientInfo from).
        if era == ERA_MODERN:
            client_info = meta.get(META_CLIENT_INFO)
            declared_version = meta.get(META_PROTOCOL_VERSION)
        else:
            client_info = (
                params.get("clientInfo") if method == "initialize" else None
            )
            declared_version = params.get("protocolVersion")
        if isinstance(client_info, dict):
            request_log_data["mcp_client_name"] = client_info.get("name")
            request_log_data["mcp_client_version"] = client_info.get("version")
        if declared_version is not None:
            request_log_data["mcp_protocol_version"] = declared_version
        if session_id:
            request_log_data["mcp_session_id"] = session_id
        # W3C trace context, if the client propagates it (spec 2026-07-28
        # "_meta" OpenTelemetry conventions). Flattened so a CloudWatch
        # query can join our request line to the caller's trace. `baggage`
        # is deliberately not copied: it is free-form key/values and could
        # carry anything.
        for key in ("traceparent", "tracestate"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                request_log_data[f"otel_{key}"] = value
        logger.info("JSON-RPC request received", extra=request_log_data)

        http_status = 200
        try:
            if era == ERA_MODERN:
                result = await self._dispatch_modern(
                    method, params, meta, is_notification
                )
            else:
                result = await self._dispatch_legacy(
                    method, params, is_notification
                )

            if result is None or is_notification:
                # Notification (known or unknown): nothing to send back.
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "JSON-RPC notification processed",
                    extra={
                        **request_log_data,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                return None, 202

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
            response_log_data["mcp_era"] = era
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            logger.info(
                "JSON-RPC request processed successfully", extra=response_log_data
            )

            return response, http_status

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            code, message, data, is_caller_error = _classify_error(e)
            if era == ERA_MODERN:
                http_status = _modern_http_status(e)

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
            response_log_data["mcp_era"] = era
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
                return None, 202
            return error_response, http_status

    # ── Legacy era (initialize handshake) ─────────────────────────────

    async def _dispatch_legacy(
        self,
        method: Optional[str],
        params: Dict[str, Any],
        is_notification: bool,
    ) -> Optional[Dict[str, Any]]:
        """Route a legacy-era request. Returns None for notifications."""
        if method == "initialize":
            return await self._handle_initialize(params)
        if method == "tools/list":
            return await self._handle_tools_list()
        if method == "tools/call":
            return await self._handle_tools_call(params)
        if method == "ping":
            # Spec: ping result is an empty object.
            return {}
        if method == "notifications/initialized":
            return None
        if is_notification:
            logger.warning(f"Ignoring unknown notification method: {method}")
            return None
        # JSON-RPC: unknown method is -32601, not an internal error.
        # Clients probe for optional methods (resources/list, etc.);
        # don't log those probes as server errors.
        raise MethodNotFoundError(f"Unknown method: {method}")

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # Version negotiation (spec): if we support the client's requested
        # version, echo it back; otherwise answer with the latest we support
        # and let the client decide whether to proceed or disconnect.
        #
        # Only LEGACY revisions are negotiable here: `initialize` *is* the
        # legacy handshake, so a client asking for 2026-07-28 on it is
        # confused, and echoing that version back would promise stateless
        # semantics on a session-based exchange. It falls back like any
        # other unrecognised version.
        requested = params.get("protocolVersion")
        if requested in self.LEGACY_PROTOCOL_VERSIONS:
            negotiated = requested
        else:
            negotiated = self.LEGACY_PROTOCOL_VERSIONS[0]
        result = {
            "protocolVersion": negotiated,
            "capabilities": self._capabilities(),
            # Pinned, not read from config: legacy clients must keep seeing
            # exactly what they saw before this server became dual-era.
            "serverInfo": {
                "name": "ebird-mcp",
                "version": "1.0.0",
            },
        }
        instructions = self._instructions()
        if instructions:
            result["instructions"] = instructions
        return result

    # ── Modern era (per-request _meta, stateless) ─────────────────────

    async def _dispatch_modern(
        self,
        method: Optional[str],
        params: Dict[str, Any],
        meta: Dict[str, Any],
        is_notification: bool,
    ) -> Optional[Dict[str, Any]]:
        """Route a modern-era request. Returns None for notifications.

        Validates the per-request protocol fields first (spec "_meta >
        Per-request protocol fields"): ``protocolVersion`` and
        ``clientCapabilities`` are required on every request; a version we
        do not serve statelessly is -32022; then dispatches and stamps the
        result with ``resultType`` and our ``serverInfo``.
        """
        if is_notification:
            # This revision defines no client-to-server notifications on
            # Streamable HTTP; accept and drop anything that arrives.
            logger.warning(f"Ignoring unknown notification method: {method}")
            return None

        version = meta.get(META_PROTOCOL_VERSION)
        if not isinstance(version, str) or not version:
            raise MalformedRequestError(
                f"Missing required _meta field '{META_PROTOCOL_VERSION}'"
            )
        if version not in self.MODERN_PROTOCOL_VERSIONS:
            # Includes our own legacy versions: those are only served via
            # the initialize handshake, never as per-request metadata.
            raise UnsupportedProtocolVersionError(version)
        if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
            raise MalformedRequestError(
                f"Missing required _meta field '{META_CLIENT_CAPABILITIES}'"
            )

        if method == "server/discover":
            result = await self._handle_discover()
        elif method == "tools/list":
            result = await self._handle_tools_list()
            result["ttlMs"] = self.TOOLS_LIST_TTL_MS
            result["cacheScope"] = "public"
        elif method == "tools/call":
            result = await self._handle_tools_call(params)
        else:
            # `ping`, `initialize`, `resources/*`, `subscriptions/listen`
            # (we advertise no listChanged) and anything else: not served
            # in this era.
            raise MethodNotFoundError(f"Unknown method: {method}")

        result["resultType"] = "complete"
        result.setdefault("_meta", {})[META_SERVER_INFO] = self._server_info()
        return result

    async def _handle_discover(self) -> Dict[str, Any]:
        """server/discover: our versions, capabilities and identity.

        The modern replacement for ``initialize``'s response. Lists EVERY
        version we implement, newest first, exactly like the spec's own
        example -- a dual-era client picks the newest mutual one; a legacy
        client never calls this.
        """
        result: Dict[str, Any] = {
            "supportedVersions": list(self.SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": self._capabilities(),
            "ttlMs": self.DISCOVER_TTL_MS,
            "cacheScope": "public",
        }
        instructions = self._instructions()
        if instructions:
            result["instructions"] = instructions
        return result

    # ── Shared ────────────────────────────────────────────────────────

    def _server_info(self) -> Dict[str, str]:
        """Identity stamped on every modern result (``_meta.serverInfo``).

        Read from config (``server_name`` / ``server_version``) so each
        deployment names itself; the legacy ``initialize`` response keeps
        its historical hardcoded identity instead -- see there.
        """
        config = getattr(self.plugin_manager, "config", None) or {}
        return {
            "name": str(config.get("server_name") or "ebird-mcp"),
            "version": str(config.get("server_version") or "1.0.0"),
        }

    def _instructions(self) -> Optional[str]:
        if not self.plugin_manager:
            return None
        return self.plugin_manager.get_instructions()

    @staticmethod
    def _capabilities() -> Dict[str, Any]:
        # Tools only; no listChanged, so modern clients have no reason to
        # open a subscriptions/listen stream.
        return {"tools": {}}

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

    # ── Streamable HTTP ───────────────────────────────────────────────

    @staticmethod
    def _decode_header_value(value: str) -> Optional[str]:
        """Undo the ``=?base64?…?=`` sentinel encoding a client MUST use
        for header values that are not plain ASCII (spec "Value
        Encoding"). Returns None if the payload is not valid base64/UTF-8.
        """
        if value.startswith("=?base64?") and value.endswith("?="):
            try:
                return base64.b64decode(
                    value[len("=?base64?") : -len("?=")], validate=True
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
        return value

    @classmethod
    def _modern_header_mismatch(
        cls,
        request: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Optional[str]:
        """Spec "Server Validation": the standard request headers must be
        present and mirror the body. Returns the failure description, or
        None if the request is consistent. Header names are looked up
        lowercased (both adapters normalise them that way).
        """
        method = request.get("method")
        meta = cls._request_meta(request)
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}

        version_header = headers.get("mcp-protocol-version")
        if version_header is None:
            return "required header MCP-Protocol-Version is missing"
        body_version = meta.get(META_PROTOCOL_VERSION)
        if body_version is not None and version_header != body_version:
            return (
                f"MCP-Protocol-Version header value '{version_header}' does "
                f"not match body value '{body_version}'"
            )

        method_header = headers.get("mcp-method")
        if method_header is None:
            return "required header Mcp-Method is missing"
        if method_header != method:
            return (
                f"Mcp-Method header value '{method_header}' does not match "
                f"body value '{method}'"
            )

        name_field = cls._NAMED_METHODS.get(method or "")
        body_name = params.get(name_field) if name_field else None
        if name_field and body_name is not None:
            name_header = headers.get("mcp-name")
            if name_header is None:
                return "required header Mcp-Name is missing"
            decoded = cls._decode_header_value(name_header)
            if decoded != body_name:
                return (
                    f"Mcp-Name header value '{name_header}' does not match "
                    f"body value '{body_name}'"
                )
        return None

    async def handle_http_request(
        self, body: str, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
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
            return self._http_error(400, None, -32700, "Parse error", str(e))

        # JSON-RPC batching was removed in protocol revision 2025-06-18 and
        # was never supported here. Reject arrays (and any other non-object
        # payload) with -32600 instead of crashing to a 500.
        if not isinstance(request, dict):
            logger.warning(
                "Rejected non-object JSON-RPC payload (batching is not supported)",
                extra={"payload_type": type(request).__name__},
            )
            return self._http_error(
                400,
                None,
                -32600,
                "Invalid Request",
                "Request must be a single JSON-RPC object; "
                "batching is not supported",
            )

        era = self.request_era(request, headers)

        session_id = None
        if era == ERA_MODERN:
            # Modern transport: the mirrored headers must agree with the
            # body before we trust either (spec "Server Validation").
            # Sessions do not exist in this era; an Mcp-Session-Id sent by
            # a confused client is ignored, never echoed.
            mismatch = self._modern_header_mismatch(request, headers)
            if mismatch:
                logger.warning(
                    f"400 error: header mismatch: {mismatch}",
                    extra={
                        "error_type": "HeaderMismatchError",
                        "jsonrpc_method": request.get("method"),
                        "mcp_era": era,
                    },
                )
                return self._http_error(
                    400,
                    request.get("id"),
                    -32020,
                    f"Header mismatch: {mismatch}",
                )
        else:
            session_id = headers.get("mcp-session-id")

        response, status_code = await self._handle_request_with_status(
            request, session_id=session_id, era=era
        )

        if response is None:
            # Streamable HTTP spec: notifications get 202 Accepted, no body.
            return {"statusCode": 202, "headers": dict(_JSON_HEADERS), "body": ""}

        return {
            "statusCode": status_code,
            "headers": dict(_JSON_HEADERS),
            "body": json.dumps(response, ensure_ascii=False),
        }

    @staticmethod
    def _http_error(
        status: int,
        request_id: Any,
        code: int,
        message: str,
        data: Any = None,
    ) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {
            "statusCode": status,
            "headers": dict(_JSON_HEADERS),
            "body": json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "error": error},
                ensure_ascii=False,
            ),
        }
