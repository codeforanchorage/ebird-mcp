#!/usr/bin/env python3
"""Production smoke test for the eBird MCP server.

  python scripts/smoke_prod.py                      # prod
  python scripts/smoke_prod.py <base-url>           # e.g. staging

Read-only. Exercises the JSON-RPC transport and the tool surface
end-to-end against a deployed endpoint, with a regression check for every
behaviour fixed in this repo's protocol/hygiene work.

WHAT THIS ASSERTS, AND WHAT IT DELIBERATELY DOES NOT
----------------------------------------------------
eBird data changes hourly. Nothing here pins an observation count, a
subId, a species list, or which hotspots come back -- those would fail
against perfectly correct behaviour, which is worse than not checking at
all because it trains you to ignore the result.

What it pins instead is the CAPABILITY: that a call succeeds, that its
structured half is present and internally consistent, that every row is
accounted for in the summary, and that a code taken from real data can be
chained into the next tool the way the server's own instructions say.

REQUEST BUDGET
--------------
Prod sits behind a WAF rate rule of 50 requests per 5 minutes per IP,
aggregated on (IP, Host). This script makes ~24 requests and paces them,
so a single run is comfortable but two back-to-back runs inside five
minutes can trip it -- a burst of 403s means the WAF, not a regression.
It also spends ~6 calls against the eBird daily quota (1000/day); the
taxonomy checks are served from the bundled snapshot and cost nothing.
"""

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://ebird.codeforanchorage.org/mcp"
URL = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
if not URL.endswith("/mcp"):
    URL = URL.rstrip("/") + "/mcp"

# Pacing. Prod's API Gateway allows 20 rps and the WAF 50 per 5 minutes;
# the gateway is not the binding constraint, the WAF is.
PACE_SECONDS = 0.4

_id = 0
_requests = 0
results = []


def raw(body, headers=None, method="POST"):
    """Send a request and return (status, parsed-or-None, raw-text)."""
    global _requests
    _requests += 1
    request = urllib.request.Request(
        URL,
        data=body.encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode()
    finally:
        time.sleep(PACE_SECONDS)
    try:
        return status, json.loads(text), text
    except json.JSONDecodeError:
        return status, None, text


def rpc(method, params=None, headers=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    _, parsed, text = raw(json.dumps(payload), headers)
    if parsed is None:
        raise AssertionError(f"non-JSON response: {text[:200]}")
    return parsed


def call_tool(name, args, headers=None):
    return rpc(
        "tools/call", {"name": f"ebird__{name}", "arguments": args}, headers
    )


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))


def structured(response):
    return response.get("result", {}).get("structuredContent")


def text_of(response):
    return response["result"]["content"][0]["text"]


print(f"Smoke testing {URL}\n")
print("-- transport --------------------------------------------------")

# ping: the spec defines the result as an empty object. The liveness
# signal is the successful response itself, not its body.
try:
    r = rpc("ping")
    check("ping returns an empty-object result",
          r.get("result") == {} and "error" not in r, json.dumps(r.get("result")))
except Exception as e:
    check("ping returns an empty-object result", False, repr(e))

try:
    r = rpc("initialize", {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "1.0"},
    })
    result = r["result"]
    check("initialize echoes a supported protocol version",
          result["protocolVersion"] == "2025-11-25", result["protocolVersion"])
    check("initialize carries server instructions",
          bool(result.get("instructions")),
          f"{len(result.get('instructions', ''))} chars")
except Exception as e:
    check("initialize", False, repr(e))

try:
    _, parsed, _ = raw(json.dumps(
        {"jsonrpc": "2.0", "id": 99, "method": "initialize",
         "params": {"protocolVersion": "2026-07-28"}}))
    check("unsupported protocolVersion downgrades rather than failing",
          parsed.get("result", {}).get("protocolVersion") not in (None, "2026-07-28"),
          parsed.get("result", {}).get("protocolVersion"))
except Exception as e:
    check("unsupported protocolVersion downgrades", False, repr(e))

# tools/list: names, titles, schemas.
try:
    tools = rpc("tools/list")["result"]["tools"]
    check("tools/list returns the full tool surface", len(tools) == 10, f"{len(tools)} tools")
    check("every tool name is plugin-prefixed",
          all(t["name"].startswith("ebird__") for t in tools))
    untitled = [t["name"] for t in tools if not t.get("title")]
    check("every tool advertises a display title", not untitled, str(untitled))
    unschema = [t["name"] for t in tools if not t.get("outputSchema")]
    check("every tool advertises an outputSchema", not unschema, str(unschema))
    hinted = [t["name"] for t in tools
              if "idempotentHint" in (t.get("annotations") or {})]
    check("no tool advertises idempotentHint", not hinted, str(hinted))
except Exception as e:
    check("tools/list", False, repr(e))
    tools = []

print("\n-- caller errors are not server faults -------------------------")

try:
    r = call_tool("no_such_tool", {})
    error = r.get("error", {})
    check("unknown tool returns -32602, not -32603",
          error.get("code") == -32602, str(error.get("code")))
    check("unknown tool message follows the spec shape",
          error.get("message", "").startswith("Unknown tool:"), error.get("message"))
    check("unknown tool lists what does exist, so a model can self-correct",
          "get_hotspots" in str(error.get("data")))
    check("unknown tool does not mint a server-fault Error ID",
          "Error ID" not in str(error.get("data")))
except Exception as e:
    check("unknown tool", False, repr(e))

try:
    r = rpc("tools/call", {"arguments": {}})
    check("tools/call with no name returns -32602",
          r.get("error", {}).get("code") == -32602, str(r.get("error", {}).get("code")))
except Exception as e:
    check("tools/call with no name", False, repr(e))

try:
    r = rpc("tools/call", {"name": "ebird__get_hotspots", "arguments": "oops"})
    error = r.get("error", {})
    check("non-object arguments returns -32602 rather than a fake tool result",
          error.get("code") == -32602 and "result" not in r, str(error.get("code")))
    blob = json.dumps(r)
    check("no Python internals leak to the caller",
          "has no attribute" not in blob and "dictionary update sequence" not in blob)
except Exception as e:
    check("non-object arguments", False, repr(e))

try:
    r = rpc("get_nonexistent_method")
    check("unknown method returns -32601",
          r.get("error", {}).get("code") == -32601, str(r.get("error", {}).get("code")))
except Exception as e:
    check("unknown method", False, repr(e))

try:
    status, parsed, _ = raw("{not json")
    check("malformed JSON returns 400 / -32700",
          status == 400 and parsed.get("error", {}).get("code") == -32700, str(status))
except Exception as e:
    check("malformed JSON", False, repr(e))

try:
    status, parsed, _ = raw(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]))
    check("JSON-RPC batch is refused with 400 / -32600",
          status == 400 and parsed.get("error", {}).get("code") == -32600, str(status))
except Exception as e:
    check("batch rejection", False, repr(e))

print("\n-- transport guards -------------------------------------------")

PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
for label, headers, expected in (
    ("disallowed Origin is refused with 403",
     {"Origin": "https://evil.example"}, 403),
    ("allowlisted Origin is accepted",
     {"Origin": "https://claude.ai"}, 200),
    ("unsupported MCP-Protocol-Version is refused with 400",
     {"MCP-Protocol-Version": "2026-07-28"}, 400),
    ("supported MCP-Protocol-Version is accepted",
     {"MCP-Protocol-Version": "2025-11-25"}, 200),
):
    try:
        status, _, _ = raw(PING, headers)
        check(label, status == expected, f"got {status}, want {expected}")
    except Exception as e:
        check(label, False, repr(e))

try:
    status, _, _ = raw(None, method="GET")
    check("GET /mcp is refused with 405", status == 405, str(status))
except Exception as e:
    check("GET /mcp", False, repr(e))

print("\n-- tool capability (spends eBird quota) ------------------------")

# A populated query. US-NY back=1 is reliably non-empty without pinning
# WHAT comes back -- the capability is "a region query returns records
# and accounts for all of them", not any particular bird.
first_species = None
try:
    r = call_tool("get_recent_observations",
                  {"regionCode": "US-NY", "back": 1, "maxResults": 25})
    payload = structured(r)
    check("populated region query returns structuredContent", payload is not None)
    if payload:
        rows, summary = payload["rows"], payload["summary"]
        check("populated query returns at least one row", len(rows) >= 1, f"{len(rows)} rows")
        check("every row is accounted for in the summary",
              summary["returned"] == len(rows),
              f"returned={summary['returned']} rows={len(rows)}")
        check("distinct species never exceeds row count",
              summary["distinct_species"] <= summary["returned"])
        check("every row carries a speciesCode",
              all(row.get("speciesCode") for row in rows))
        check("caveats are coded objects, not prose",
              all(set(c) == {"code", "message"} for c in payload["caveats"]),
              f"{len(payload['caveats'])} caveats")
        check("caveat text also appears in the human-readable block",
              all(c["message"] in text_of(r) for c in payload["caveats"]))
        check("body leads with the grain counts",
              f"{summary['distinct_species']} species" in text_of(r))
        if rows:
            first_species = rows[0]["speciesCode"]
except Exception as e:
    check("populated region query", False, repr(e))

# A query that returns nothing. This is where a declared outputSchema
# gets broken: an early return that skips the formatter advertises a
# schema and then sends no structuredContent.
try:
    r = call_tool("get_nearby_observations",
                  {"lat": -40.0, "lng": -140.0, "dist": 1, "back": 1})
    payload = structured(r)
    check("EMPTY query still returns structuredContent", payload is not None)
    if payload:
        check("empty result has zero rows", payload["rows"] == [])
        check("empty result reports total_count 0, not null",
              payload["summary"]["total_count"] == 0,
              repr(payload["summary"]["total_count"]))
        check("empty result frames absence as sampling",
              "ABSENCE-OF-EVIDENCE" in text_of(r))
except Exception as e:
    check("empty query", False, repr(e))

try:
    r = call_tool("get_hotspots", {"regionCode": "US-NY"})
    payload = structured(r)
    check("hotspots query returns structuredContent", payload is not None)
    if payload:
        check("hotspots query returns rows", len(payload["rows"]) >= 1,
              f"{len(payload['rows'])} hotspots")
        check("hotspot rows carry usable locIds",
              all(str(row.get("locId", "")).startswith("L") for row in payload["rows"]))
        check("hotspot totals come with the uncomparability caveat",
              any(c["code"] == "UNCOMPARABLE_SPECIES_TOTALS" for c in payload["caveats"]))
except Exception as e:
    check("hotspots query", False, repr(e))

# Taxonomy is served from the bundled snapshot, so this costs no quota.
try:
    r = call_tool("get_taxonomy", {"cat": "species"})
    payload = structured(r)
    check("taxonomy returns structuredContent", payload is not None)
    if payload:
        summary = payload["summary"]
        check("taxonomy reports the TRUE total, not the capped one",
              summary["total_count"] > summary["returned"],
              f"total={summary['total_count']} returned={summary['returned']}")
        check("taxonomy row truncation is declared, never silent",
              summary["truncated"] is True
              and any(c["code"] == "ROWS_TRUNCATED" for c in payload["caveats"]))
        check("taxonomy rows resolve to species codes",
              all(row.get("speciesCode") for row in payload["rows"]))
except Exception as e:
    check("taxonomy", False, repr(e))

# The chaining capability the server's own instructions describe:
# a code taken from real data feeds the next tool.
if first_species:
    try:
        r = call_tool("get_taxonomy_forms", {"speciesCode": first_species})
        check(f"a speciesCode from live data chains into get_taxonomy_forms "
              f"({first_species})",
              "error" not in r and structured(r) is not None)
    except Exception as e:
        check("speciesCode chaining", False, repr(e))
else:
    check("speciesCode chaining", False, "no species code available upstream")

# Argument validation. Deliberately NOT a traversal-shaped payload: prod's
# WAF blocks those at the edge, so such a probe tests the WAF rather than
# the plugin and comes back as an opaque gateway 403.
try:
    r = call_tool("get_recent_observations", {"regionCode": "us-ny"})
    result = r.get("result", {})
    check("an invalid regionCode is rejected with a readable message",
          result.get("isError") is True and "regionCode" in str(result.get("error")),
          str(result.get("error"))[:80])
except Exception as e:
    check("regionCode validation", False, repr(e))

print("\n" + "-" * 63)
failed = [label for label, ok in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} checks passed "
      f"in {_requests} requests")
if failed:
    print("\nFAILED:")
    for label in failed:
        print(f"  - {label}")
    sys.exit(1)
print("All checks passed.")
