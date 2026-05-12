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

        is_notification = request.get("id") is None

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
                            "id": request.get("id"),
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
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
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
                        "id": request.get("id"),
                        "error": {"code": -32603, "message": str(e)},
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
