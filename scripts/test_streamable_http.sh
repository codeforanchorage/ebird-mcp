#!/bin/bash
# Smoke test: initialize, tools/list, tools/call against the MCP server.
#
# Usage: ./scripts/test_streamable_http.sh [BASE_URL]
# Default BASE_URL is http://localhost:8000/mcp.

set -e

if ! command -v jq &> /dev/null; then
    echo "jq required. brew install jq / apt-get install jq"
    exit 1
fi

BASE_URL="${1:-http://localhost:8000/mcp}"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=========================================="
echo "Testing eBird MCP Server"
echo "=========================================="
echo "URL: $BASE_URL"
echo ""

echo -e "${BLUE}Step 1: initialize${NC}"
INIT_REQUEST='{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {"name": "test-client", "version": "1.0.0"}
  }
}'

TEMP_HEADERS=$(mktemp)
TEMP_BODY=$(mktemp)

HTTP_CODE=$(curl -s -o "$TEMP_BODY" -w "%{http_code}" -X POST "$BASE_URL" \
  -H "Content-Type: application/json" \
  -d "$INIT_REQUEST" \
  -D "$TEMP_HEADERS")

SESSION_ID=$(grep -i "mcp-session-id" "$TEMP_HEADERS" | head -n 1 | sed -E 's/^[^:]*:[[:space:]]*(.*)$/\1/' | tr -d '\r\n' || echo "")
BODY=$(cat "$TEMP_BODY")
rm -f "$TEMP_HEADERS" "$TEMP_BODY"

if [ "$HTTP_CODE" != "200" ]; then
    echo -e "${YELLOW}HTTP $HTTP_CODE${NC}"
    echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
    exit 1
fi
echo "$BODY" | jq '.'
[ -n "$SESSION_ID" ] && echo -e "${GREEN}Session: $SESSION_ID${NC}"
echo ""

echo -e "${BLUE}Step 2: tools/list${NC}"
LIST_REQUEST='{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
if [ -n "$SESSION_ID" ]; then
    LIST_RESPONSE=$(curl -s -X POST "$BASE_URL" \
      -H "Content-Type: application/json" \
      -H "Mcp-Session-Id: $SESSION_ID" \
      -d "$LIST_REQUEST")
else
    LIST_RESPONSE=$(curl -s -X POST "$BASE_URL" -H "Content-Type: application/json" -d "$LIST_REQUEST")
fi
echo "$LIST_RESPONSE" | jq '.result.tools[] | .name'
TOOL_COUNT=$(echo "$LIST_RESPONSE" | jq '.result.tools | length')
echo -e "${GREEN}$TOOL_COUNT tools available${NC}"
echo ""

echo -e "${BLUE}Step 3: tools/call ebird__get_taxonomy_forms speciesCode=amecro${NC}"
CALL_REQUEST='{
  "jsonrpc":"2.0","id":3,"method":"tools/call",
  "params":{"name":"ebird__get_taxonomy_forms","arguments":{"speciesCode":"amecro"}}
}'
if [ -n "$SESSION_ID" ]; then
    CALL_RESPONSE=$(curl -s -X POST "$BASE_URL" \
      -H "Content-Type: application/json" \
      -H "Mcp-Session-Id: $SESSION_ID" -d "$CALL_REQUEST")
else
    CALL_RESPONSE=$(curl -s -X POST "$BASE_URL" -H "Content-Type: application/json" -d "$CALL_REQUEST")
fi
echo "$CALL_RESPONSE" | jq '.'

ERROR=$(echo "$CALL_RESPONSE" | jq -r '.error // empty')
if [ -n "$ERROR" ]; then
    echo -e "${YELLOW}Tool returned an error${NC}"
    exit 1
fi

echo ""
echo "=========================================="
echo -e "${GREEN}All tests passed!${NC}"
echo "=========================================="
