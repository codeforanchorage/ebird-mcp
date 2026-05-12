"""AWS Lambda adapter.

Transforms AWS Lambda events (from API Gateway or Function URL) into the
universal HTTP format expected by UniversalHTTPHandler.
"""

import asyncio
import base64
import json
import logging
from typing import Any, Dict, Optional, Protocol

from server.http_handler import UniversalHTTPHandler


class LambdaContext(Protocol):
    aws_request_id: str
    function_name: Optional[str]
    memory_limit_in_mb: Optional[int]


logger = logging.getLogger(__name__)

_handler: Optional[UniversalHTTPHandler] = None


def get_handler() -> UniversalHTTPHandler:
    global _handler
    if _handler is None:
        _handler = UniversalHTTPHandler()
        logger.info("Created new UniversalHTTPHandler instance")
    return _handler


def lambda_handler(
    event: Dict[str, Any], context: Optional[LambdaContext]
) -> Dict[str, Any]:
    """AWS Lambda entry point.

    Supports both API Gateway (v1, v2) and Lambda Function URL event formats.
    """
    try:
        request_id = context.aws_request_id if context else "unknown"
        function_name = getattr(context, "function_name", None) if context else None
        memory_limit = getattr(context, "memory_limit_in_mb", None) if context else None

        logger.info(
            "Lambda invocation started",
            extra={
                "request_id": request_id,
                "function_name": function_name,
                "memory_limit": memory_limit,
            },
        )

        http_method = event.get("requestContext", {}).get("http", {}).get(
            "method"
        ) or event.get("httpMethod", "POST")

        request_path = (
            event.get("rawPath")
            or event.get("requestContext", {}).get("http", {}).get("path")
            or event.get("path", "/")
        )

        if http_method == "OPTIONS":
            handler = get_handler()
            preflight_origin = (event.get("headers") or {}).get(
                "origin"
            ) or (event.get("headers") or {}).get("Origin")
            status_code, headers, body = handler.handle_options(
                request_id=request_id,
                request_origin=preflight_origin,
            )
            logger.info(
                "CORS preflight request handled",
                extra={"request_id": request_id, "status_code": status_code},
            )
            return {
                "statusCode": status_code,
                "headers": headers,
                "body": body,
            }

        body = event.get("body", "{}")

        if event.get("isBase64Encoded", False):
            try:
                body = base64.b64decode(body).decode("utf-8")
            except Exception as e:
                logger.error(
                    f"Failed to decode base64 body: {e}",
                    extra={"request_id": request_id},
                )
                raise ValueError(f"Invalid base64-encoded body: {e}") from e

        if isinstance(body, dict):
            body = json.dumps(body)

        headers = event.get("headers", {})
        if isinstance(headers, dict):
            # Lowercase headers for HTTP/1.1 case-insensitive lookup.
            headers = {k.lower(): v for k, v in headers.items()}
        else:
            headers = {}

        handler = get_handler()

        async def _run_with_cleanup():
            try:
                return await handler.handle_request(
                    method=http_method,
                    path=request_path,
                    body=body,
                    headers=headers,
                    request_id=request_id,
                )
            finally:
                # Close plugin HTTP clients before the event loop closes.
                # Avoids "Event loop is closed" on warm starts when tools use httpx.
                from server import http_handler

                if http_handler._plugin_manager is not None:
                    await http_handler._plugin_manager.shutdown()
                    http_handler._plugin_manager = None
                    http_handler._mcp_server = None

        status_code, response_headers, response_body = asyncio.run(_run_with_cleanup())

        lambda_response = {
            "statusCode": status_code,
            "headers": response_headers,
            "body": response_body,
        }

        logger.info(
            "Lambda invocation completed",
            extra={"request_id": request_id, "status_code": status_code},
        )

        return lambda_response

    except Exception as e:
        request_id = context.aws_request_id if context else "unknown"
        logger.error(
            f"Error in Lambda handler: {e}",
            extra={"request_id": request_id, "error_type": type(e).__name__},
            exc_info=True,
        )
        error_origin = (event.get("headers") or {}).get(
            "origin"
        ) or (event.get("headers") or {}).get("Origin")
        error_cors = UniversalHTTPHandler._get_cors_headers(error_origin)
        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json; charset=utf-8",
                **error_cors,
            },
            "body": json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": f"Request ID: {request_id}",
                    },
                }
            ),
        }
