"""Logging utilities. Provides JSON logging and sensitive data sanitization."""

import json
import logging
from typing import Any, Dict, List, Optional

from pythonjsonlogger import json as jsonlogger

SENSITIVE_KEYS = [
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth",
    "token",
    "bearer",
    "password",
    "passwd",
    "secret",
    "credential",
    "credentials",
    "access_token",
    "refresh_token",
    "session_id",
    "cookie",
]

SENSITIVE_HEADER_PREFIXES = [
    "x-api-key",
    "x-ebirdapitoken",
    "x-auth",
    "x-token",
    "x-secret",
    "authorization",
    "cookie",
]


def configure_json_logging(level: str = "INFO", pretty: bool = False) -> None:
    """Configure ALL loggers to use JSON format."""
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    log_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(log_level)

    handler = logging.StreamHandler()
    handler.setLevel(log_level)

    if pretty:
        formatter = _PrettyJsonFormatter()
    else:
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            timestamp=True,
        )

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


class _PrettyJsonFormatter(logging.Formatter):
    """Pretty JSON formatter for local development."""

    def __init__(self, max_string_length: int = 500, max_list_items: int = 10):
        super().__init__()
        self.max_string_length = max_string_length
        self.max_list_items = max_list_items

    def _truncate_value(self, value: Any, depth: int = 0) -> Any:
        if depth > 3:
            return "..."
        if isinstance(value, str):
            if len(value) > self.max_string_length:
                return (
                    value[: self.max_string_length]
                    + f"... (truncated, {len(value)} chars)"
                )
            return value
        elif isinstance(value, dict):
            truncated = {}
            for k, v in list(value.items())[:20]:
                truncated[k] = self._truncate_value(v, depth + 1)
            if len(value) > 20:
                truncated["..."] = f"(truncated, {len(value)} keys)"
            return truncated
        elif isinstance(value, list):
            truncated = [
                self._truncate_value(item, depth + 1)
                for item in value[: self.max_list_items]
            ]
            if len(value) > self.max_list_items:
                truncated.append(f"... (truncated, {len(value)} items)")
            return truncated
        else:
            return value

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "__dict__"):
            for key, value in record.__dict__.items():
                if key not in (
                    "name", "msg", "args", "created", "filename", "funcName",
                    "levelname", "levelno", "lineno", "module", "msecs",
                    "message", "pathname", "process", "processName",
                    "relativeCreated", "thread", "threadName", "exc_info",
                    "exc_text", "stack_info", "asctime", "datefmt",
                ):
                    log_data[key] = self._truncate_value(value)
        try:
            return json.dumps(log_data, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return json.dumps({"message": str(record.getMessage())}, indent=2)


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    return any(sensitive_key in key_lower for sensitive_key in SENSITIVE_KEYS)


def sanitize_dict(data: Any, sensitive_keys: Optional[List[str]] = None) -> Any:
    """Recursively sanitize dictionary values for sensitive keys."""
    if sensitive_keys is None:
        sensitive_keys = SENSITIVE_KEYS
    if isinstance(data, dict):
        sanitized = {}
        for key, value in data.items():
            if _is_sensitive_key(key) or (
                sensitive_keys
                and any(sk.lower() in key.lower() for sk in sensitive_keys)
            ):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_dict(value, sensitive_keys)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_dict(item, sensitive_keys) for item in data]
    else:
        return data


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    sanitized = {}
    for key, value in headers.items():
        key_lower = key.lower()
        if any(
            key_lower.startswith(prefix.lower()) for prefix in SENSITIVE_HEADER_PREFIXES
        ) or _is_sensitive_key(key):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = value
    return sanitized


def sanitize_request_body(body: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(body) if body else {}
        return sanitize_dict(parsed)
    except (json.JSONDecodeError, TypeError):
        return {"raw_body": "[REDACTED]" if len(body) > 0 else ""}


def sanitize_response_body(body: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(body) if body else {}
        return sanitize_dict(parsed)
    except (json.JSONDecodeError, TypeError):
        return {"raw_body": "[REDACTED]" if len(body) > 0 else ""}


def format_request_log(
    request_id: str,
    http_method: str,
    request_path: str,
    headers: Dict[str, str],
    body: str,
    lambda_context: Optional[Any] = None,
) -> Dict[str, Any]:
    log_data = {
        "request_id": request_id,
        "http_method": http_method,
        "request_path": request_path,
        "request_headers": sanitize_headers(headers),
        "request_body": sanitize_request_body(body),
    }
    if lambda_context:
        log_data["lambda_function_name"] = getattr(
            lambda_context, "function_name", None
        )
        log_data["lambda_memory_limit"] = getattr(
            lambda_context, "memory_limit_in_mb", None
        )
        log_data["lambda_remaining_time_ms"] = getattr(
            lambda_context, "get_remaining_time_in_millis", lambda: None
        )()
    return log_data


def format_response_log(
    request_id: str,
    status_code: int,
    headers: Dict[str, str],
    body: str,
    duration_ms: float,
    success: bool = True,
) -> Dict[str, Any]:
    return {
        "request_id": request_id,
        "response_status": status_code,
        "response_headers": sanitize_headers(headers),
        "response_body": sanitize_response_body(body),
        "duration_ms": round(duration_ms, 2),
        "success": success,
    }


def format_jsonrpc_request_log(
    request_id: Optional[Any],
    method: str,
    params: Dict[str, Any],
    is_notification: bool = False,
) -> Dict[str, Any]:
    return {
        "jsonrpc_request_id": request_id,
        "jsonrpc_method": method,
        "jsonrpc_params": sanitize_dict(params),
        "is_notification": is_notification,
    }


def format_jsonrpc_response_log(
    request_id: Optional[Any],
    method: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
    duration_ms: float = 0.0,
) -> Dict[str, Any]:
    log_data = {
        "jsonrpc_request_id": request_id,
        "jsonrpc_method": method,
        "duration_ms": round(duration_ms, 2),
    }
    if error:
        log_data["jsonrpc_error"] = sanitize_dict(error)
        log_data["success"] = False
    else:
        log_data["jsonrpc_result"] = sanitize_dict(result) if result else None
        log_data["success"] = True
    return log_data
