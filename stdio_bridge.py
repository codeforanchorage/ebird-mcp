"""Stdio-to-HTTP bridge for connecting stdio MCP clients to the HTTP eBird MCP server.

Reads JSON-RPC messages from stdin, forwards them to the local HTTP server,
and writes responses to stdout. Lets you point Claude Desktop's stdio config
at this script while the server itself is HTTP (local or deployed).
"""

import json
import sys
import urllib.error
import urllib.request

SERVER_URL = "http://localhost:8000/mcp"


def _jsonrpc_error_body(error):
    """The JSON-RPC error object carried by an HTTP error, or None.

    None for anything that is not one (an HTML gateway page, a WAF
    ``{"message": "Forbidden"}``, an empty body), so the caller falls back
    to reporting the bare status.
    """
    try:
        body = json.loads(error.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body
    return None


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else SERVER_URL
    if not url.endswith("/mcp"):
        url = url.rstrip("/") + "/mcp"

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                ),
                flush=True,
            )
            continue

        # A non-object body (a batch array, a bare string) has no id to
        # read. Forward it anyway: the server answers with a proper
        # -32600, which beats this process dying on an AttributeError.
        request_id = request.get("id") if isinstance(request, dict) else None
        is_notification = isinstance(request, dict) and request_id is None

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(request).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")

            if not body:
                if is_notification:
                    continue
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32603, "message": "Empty response"},
                        }
                    ),
                    flush=True,
                )
                continue

            response = json.loads(body)
            if is_notification:
                continue
            print(json.dumps(response), flush=True)

        except urllib.error.HTTPError as e:
            if is_notification:
                continue
            # The server answers caller mistakes with a real JSON-RPC error
            # under a 4xx status (-32602 invalid params, -32601 unknown
            # method, -32022 unsupported version). Forward that body: a
            # generic "HTTP 400" throws away the one part the client can
            # act on.
            forwarded = _jsonrpc_error_body(e)
            if forwarded is not None:
                print(json.dumps(forwarded), flush=True)
                continue
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": f"HTTP {e.code}"},
                    }
                ),
                flush=True,
            )
        except Exception as e:
            if is_notification:
                continue
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": str(e)},
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
