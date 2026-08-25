# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A hosted Model Context Protocol (MCP) server for the eBird v2 API. Runs as a single AWS Lambda fronted by API Gateway + WAFv2; can also run locally over HTTP for development. Uses a plugin architecture (one fork = one MCP server, enforced by `core/validators.py::validate_plugin_count`).

## Common commands

```powershell
# Local dev (Windows)
copy config-example.yaml config.yaml          # then edit api_key
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/local_server.py                # http://localhost:8000/mcp

# Smoke test the running server (requires jq + Git Bash on Windows)
bash ./scripts/test_streamable_http.sh        # defaults to localhost:8000
bash ./scripts/test_streamable_http.sh https://your-endpoint/mcp

# Deploy
bash ./scripts/setup-backend.sh               # one-time per AWS account
bash ./scripts/deploy.sh --environment staging
bash ./scripts/deploy.sh --environment prod

# Verify a deployed endpoint (staging or prod)
python scripts/smoke_prod.py                  # defaults to prod
python scripts/smoke_prod.py https://<staging-host>/staging/mcp
```

Tests: `python -m unittest discover tests` — plain unittest, no network (upstream calls are stubbed). Also runnable under pytest, which additionally applies the `tests/conftest.py` global-state fixture to every test.

- `test_civic_ai_patterns.py` — caveat precedence and text formatting.
- `test_input_validation.py` — path-traversal rejection (`regionCode`/`speciesCode` flow into upstream URL paths) and numeric clamps.
- `test_mcp_server.py` — JSON-RPC dispatch, protocol negotiation, caller-vs-fault error classification.
- `test_caller_error_logging.py` — WARNING-without-traceback for caller mistakes; an AST sweep keeps numeric coercion of caller args inside `_clamp_int`/`_coerce_float`.
- `test_structured_output.py` — validates REAL tool output against the declared schemas with `jsonschema`, on every awkward branch (zero rows, null `howMany`, cap hit, sparse rows, unparseable dates, clipped text).
- `test_tool_metadata.py`, `test_transport_parity.py`, `test_domain_correctness.py`, `test_config_invariants.py` — title/schema maps, the six transport cases local must match prod on, false-zero/grain checks, and the timeout ladder.

Lint: `python -m ruff check .` — must be clean. Do NOT run `ruff format`; this repo is hand-wrapped at ~79 cols and a format pass produces a thousand-line diff. `jsonschema` is a dev-only dependency (`pyproject.toml` `[project.optional-dependencies].dev`) and is never imported by the Lambda.

The deploy script handles packaging: it builds `.deploy/` with `pip install --platform manylinux2014_x86_64 --python-version 3.11 --only-binary :all:` (or `uv` if present), zips to `lambda-deployment.zip`, and copies that plus `config.yaml` into `terraform/aws/` where Terraform reads them.

## Architecture

Request flow:

```
Client -> POST /mcp (JSON-RPC 2.0)
  -> [AWS only] WAFv2 + API Gateway REST + usage plan
  -> server.adapters.aws_lambda.lambda_handler           (Lambda)
     or scripts/local_server.py aiohttp app               (local)
     -> server.http_handler.UniversalHTTPHandler         (cloud-agnostic)
        -> core.mcp_server.MCPServer                     (JSON-RPC dispatcher)
           -> core.plugin_manager.PluginManager
              -> plugins.<name>.plugin.<Class>           (one enabled at a time)
                 -> plugins.<name>.<api_client> -> upstream API
```

Key design points future Claude should know before editing:

- **Hand-rolled JSON-RPC, no MCP SDK.** `core/mcp_server.py` implements `initialize`, `tools/list`, `tools/call`, `ping`, and `notifications/initialized`. Protocol version is negotiated: `_handle_initialize` echoes the client's requested version if it's in `MCPServer.SUPPORTED_PROTOCOL_VERSIONS` (2024-11-05 through 2025-11-25), otherwise answers with the newest supported. Append to that tuple (newest last) when a new spec revision ships — but first check the new revision's transport MUSTs against `server/http_handler.py` (2025-11-25 required the Origin-403 and MCP-Protocol-Version-400 guards there). JSON-RPC batch arrays get 400/-32600 (batching was removed in 2025-06-18 and was never supported here); unknown methods get -32601; notifications get 202 with an empty body per the Streamable HTTP spec. Requests with an Origin header not in `ALLOWED_ORIGINS` get 403; an unsupported `MCP-Protocol-Version` header gets 400 (absent is fine).
- **Stateless.** `Mcp-Session-Id` is minted on `initialize` and echoed for log correlation only — clients may also skip `initialize` entirely and call `tools/*` directly with no session header, as newer spec revisions allow. No per-session storage; horizontally scalable.
- **Server instructions come from the plugin.** `initialize` includes an `instructions` field (LLM-facing usage guide) sourced from `MCPPlugin.get_instructions()` — an optional override, default `None`. The eBird implementation lives in `plugins/ebird/plugin.py`; keep it consistent with the workflow/caveat text in the tool descriptions.
- **Plugin discovery.** `PluginManager.discover_plugins()` walks `plugins/` (and optional `custom_plugins/`) looking for any subdir with `plugin.py` exporting a class that inherits from `MCPPlugin`. Tools are namespaced `<plugin>__<tool>` automatically — never include the prefix in `ToolDefinition.name`.
- **One enabled plugin per deployment, enforced at load time.** Toggle `enabled: true` on exactly one plugin in `config.yaml` — `validate_plugin_count` raises `ConfigurationError` otherwise.
- **Config is baked into the Lambda env var.** `terraform/aws/main.tf` reads `config.yaml` at plan time, `jsonencode`s it, and injects it as `EBIRD_MCP_CONFIG`. `server/http_handler.py::_load_config` reads that env var first, falling back to `config.yaml` for local dev. To rotate the eBird API key, edit `config.yaml` and redeploy.
- **Two AWS sizing values are read from `config.yaml` in preference to `terraform/aws/*.tfvars`** — `lambda_memory` and `lambda_timeout` (see the `locals` block in `terraform/aws/main.tf`). Editing them in the tfvars alone silently does nothing. `lambda_name` uses the opposite precedence, so check `main.tf` per variable rather than assuming. `tests/test_config_invariants.py` pins the two files against drift.
- **`terraform/aws/config.yaml` is a BUILD ARTIFACT, not a source file.** `scripts/deploy.sh` (Step 3) copies the repo-root `config.yaml` over it during packaging, and it is gitignored. Two consequences: edits made directly to it vanish on the next deploy, and a bare `terraform plan` run inside `terraform/aws/` (without the packaging steps) reads the STALE copy — so a config change shows up as nothing but a code-hash diff, and a "timeout fix" can appear to apply while changing nothing. Diff the two files before trusting such a plan, or go through `./scripts/deploy.sh` which repackages first.
- **Timeout ladder** — each layer must sit under the one above it, and `tests/test_config_invariants.py` fails if one drifts:

  | Layer | Value | Why |
  |---|---|---|
  | API Gateway integration | 29s | hard REST limit, not adjustable |
  | Lambda (`aws.lambda_timeout`) | 28s | self-terminates before the gateway gives up, so the timeout lands in Lambda's own metrics and the reserved-concurrency slot frees at once |
  | Plugin HTTP (`plugins.ebird.timeout`) | 20s | a hung eBird call returns a readable tool error instead of the Lambda being killed mid-flight (opaque 502) |

  The `lambda-duration-near-timeout` alarm derives its threshold as 80% of `lambda_timeout`, so aligning the ladder is what makes it fire (~22.4s) *before* the gateway starts returning 504s.
- **Per-invocation plugin shutdown on Lambda.** `server/adapters/aws_lambda.py::_run_with_cleanup` calls `plugin_manager.shutdown()` after every request to avoid `"Event loop is closed"` errors from `httpx` when the Lambda execution context is reused. Each warm invocation re-initializes the eBird client. If cold-starts ever become a real problem, move the client to module scope and remove the shutdown — but understand the httpx/asyncio interaction first.
- **Adapter responsibilities split.** `aws_lambda.py` handles event shape (API GW v1, v2, Function URL), base64 bodies, the 64 KB body cap, and OPTIONS preflight. Everything below it (CORS allowlist, JSON-RPC, plugin dispatch) is cloud-agnostic and lives in `server/http_handler.py` and `core/`.
- **Hardening in the eBird plugin.** `plugins/ebird/plugin.py::_clamp_and_validate` enforces regex-validated `regionCode`/`speciesCode`/`locale` (path-injection defense), clamps numeric args (`back`, `maxResults`, `dist`, `lat`/`lng`) silently into accepted ranges, and rejects unknown `cat`/`fmt` values. Retries (`_RETRY`) are limited to two attempts on transport/read-timeout errors only — 4xx/5xx flow straight through.
- **Bundled taxonomy.** `scripts/refresh_taxonomy.py` fetches the full eBird taxonomy (every `cat`) once at deploy time and writes it to `plugins/ebird/data/taxonomy.json` (gitignored). `ebird_client.py::get_taxonomy` serves the common case (`locale=en`, `fmt=json`) from that bundle via a module-level lazy cache, filtering by `cat` in-process. Non-English locales and `fmt=csv` fall through to the live API. The bundle is the single biggest eBird-quota saver — taxonomy lookups would otherwise be a per-conversation drain against the 1000/day cap. If the file is missing the plugin transparently falls back to live calls, so a refresh failure during deploy is non-fatal.
- **Error responses are scrubbed — but only genuine faults.** `core/mcp_server.py::handle_request` classifies every exception through the `_CALLER_ERRORS` mapping. A genuine fault mints a UUID `error_id`, logs the full exception to CloudWatch under that ID, and returns only `{"code": -32603, "message": "Internal error", "data": "Error ID: ..."}`; do not regress that by surfacing `str(e)`. A *caller* error is the opposite: it gets its own JSON-RPC code, an actionable `data` payload (there is nothing sensitive in "you named a tool that does not exist"), a WARNING-level log and no traceback. Add the next caller-error code as a row in that mapping, not another nested conditional.
  - `UnknownToolError` → -32602 `Unknown tool: <name>`, with the available-tool list in `data`.
  - `InvalidToolParamsError` → -32602 `Invalid params`, for a missing `name` or non-object `arguments`. This is checked *before* dispatch, so a plugin can never be handed a shape it cannot process.
  - `ToolInputError` (raised in the plugin) → a tool error with a readable message, logged at WARNING. **Never infer caller-ness from `ValueError`** — `json.JSONDecodeError` subclasses it, so a malformed upstream payload would be misfiled and lose its traceback.
- **Tool metadata.** Every tool carries a top-level `title` (from `TOOL_TITLES`) and an `outputSchema` (from `TOOL_OUTPUT_SCHEMAS`), both emitted by `PluginManager.get_all_tools`. Both maps are pinned in *both* directions by `tests/test_tool_metadata.py`, so adding or removing a tool without updating them fails. `idempotentHint` is deliberately absent — the schema says it is meaningful only when `readOnlyHint == false`, and every tool here is read-only.
- **Structured output.** All ten tools return `structuredContent` alongside the human-readable text, in a shared `query` / `summary` / `rows` / `caveats` envelope declared in `plugins/ebird/schemas.py`. Things to know before editing:
  - **A declared `outputSchema` is binding** — the spec says servers MUST conform and clients SHOULD validate. Never declare a constraint real data can violate; there is no `maximum` anywhere and no `required` on a field eBird can omit, and a test sweeps for both.
  - **`_build_structured` derives from the RAW upstream `data`, in `execute_tool`** — deliberately not from inside the text formatters, which short-circuit on empty results. A builder behind those early returns is exactly how a server advertises a schema and then returns nothing on the zero-result path. Keep it that way.
  - **Caveats come from ONE list.** `_build_caveats` returns coded objects; `_finalize_response` renders their `message` into the text and `_build_structured` emits `{code, message}`. Codes are stable API — reword a message freely, never a code. `_in_body=True` marks a caveat the body already renders (the absence-of-evidence framing) so it is not printed twice.
  - **`total_count` is null only when genuinely unknown** (the result hit the `maxResults` cap). 0 means "known, and none". Conflating them makes a complete answer look unmeasured.
  - **`howMany` null means PRESENT, COUNT NOT REPORTED**, not zero — eBird's "X". The most misreadable field in this API; it carries a schema note, a `COUNT_NOT_REPORTED` caveat and a `summary.counts_not_reported` tally.
  - **Rows ship complete**, even when the text is compacted or clipped at the byte backstop. The exceptions are `get_taxonomy` and `get_hotspots` — the two tools no caller argument bounds — both capped at `_MAX_STRUCTURED_ROWS` (5000) because Lambda's response payload limit is a hard 6 MB. Measured: `cat=species` is 3.60 MB and all categories 6.00 MB; `get_hotspots` on US-NY is 1.90 MB and on US-CA 4.30 MB. Both report the true `total_count`, `truncated: true` and a `ROWS_TRUNCATED` caveat. **If you add a tool whose result set no argument bounds, it needs this cap too** — the observation tools do not, because `maxResults` already holds them at 1000.
- **Response-size controls apply to the TEXT only.** `maxResults` is capped at 1000 (`_MAX_RESULTS_CEILING` — deliberately below eBird's own 10000). Observation/hotspot results above 20 records (`_COMPACT_FORMAT_THRESHOLD`) render as a compact pipe-delimited table; small results keep the blocks. A 200 KB byte ceiling (`_MAX_BODY_BYTES`, via `_join_with_size_cap`) backstops the formatted body, truncating at a record boundary with an explicit notice and a `RESPONSE_SIZE_CEILING` caveat — never silently. `structuredContent` is unaffected by all of it.

## Files that matter

- `core/mcp_server.py` — JSON-RPC dispatcher; bump `protocolVersion` here.
- `core/plugin_manager.py` — discovery + tool registration; tool prefixing happens here.
- `core/validators.py` — one-plugin rule; config loading.
- `server/http_handler.py` — `ALLOWED_ORIGINS` CORS allowlist; config env-var loader.
- `server/adapters/aws_lambda.py` — Lambda entry point; body size cap; per-invocation cleanup.
- `plugins/ebird/plugin.py` — tool catalog (`get_tools`) + dispatch (`execute_tool`); hardening helpers; caveat builder and structured-output builder.
- `plugins/ebird/schemas.py` — the declared `outputSchema`s and the stable caveat codes. Codes are API; messages are not.
- `core/interfaces.py` — `ToolDefinition` (`title`, `output_schema`), `ToolResult` (`structured_content`), and the caller-error exception types.
- `plugins/ebird/ebird_client.py` — thin httpx wrapper over eBird v2. Watch the endpoint paths: eBird is inconsistent — taxonomy lives under `/ref/taxonomy/ebird` but taxonomic *forms* live under `/ref/taxon/forms/{speciesCode}` (`taxon`, not `taxonomy`). The wrong path 404s for every species; don't "correct" `taxon` back to `taxonomy`. Also: `get_taxonomy` serves from `plugins/ebird/data/taxonomy.json` (the deploy-time bundle) when feasible; see the bundled-taxonomy bullet above.
- `scripts/smoke_prod.py` — capability-level smoke test against a deployed endpoint; takes a base URL so it runs against staging too. Asserts invariants, never volatile eBird data. Budget ~20 requests against prod's 50-per-5-min WAF rule.
- `scripts/refresh_taxonomy.py` — fetches the full eBird taxonomy and writes the bundle. Run automatically by `scripts/deploy.sh` (Step 1.5). Can also be run manually for local dev: `python scripts/refresh_taxonomy.py`.
- `terraform/aws/main.tf` — Lambda + IAM; reads `config.yaml` at plan time.
- `terraform/aws/{api_gateway,waf,cloudwatch_alarms,access_logs}.tf` — the rest of the stack.
- `terraform/aws/{staging,prod}.tfvars` — per-env quota, rate limits, concurrency.
- `scripts/local_server.py` — thin aiohttp adapter onto `UniversalHTTPHandler`, the mirror of `server/adapters/aws_lambda.py`. Local dev therefore exercises the same Origin allowlist, `MCP-Protocol-Version` check, path/method validation and CORS that prod runs; `tests/test_transport_parity.py` pins that.
- `stdio_bridge.py` — thin stdio<->HTTP shim for stdio-only MCP clients (forwards to a running HTTP server).

## Files NOT under version control (rebuilt or local-only)

`config.yaml`, `.mcp.json`, `terraform/aws/backend.tf`, `terraform/aws/config.yaml`, `lambda-deployment.zip`, `.deploy/`, `.terraform/`, `*.tfstate*`, `tfplan`, `plugins/ebird/data/taxonomy.json`. Their `.example` counterparts are committed (where applicable); `scripts/setup-backend.sh` regenerates `backend.tf`; `scripts/deploy.sh` regenerates the zip, the copy of `config.yaml`, and refreshes `taxonomy.json` via `scripts/refresh_taxonomy.py`.

## Adding tools or new plugins

- Adding a tool to eBird: extend `plugins/ebird/plugin.py::EBirdPlugin.get_tools()` with a new `ToolDefinition`, then add the matching branch in `execute_tool()`. Add an upstream call to `plugins/ebird/ebird_client.py`. Validate any new path-flowing args in `_clamp_and_validate`.
- Adding a different data source: drop a sibling `plugins/<name>/plugin.py` exporting an `MCPPlugin` subclass, set its `plugin_name`, and toggle `enabled: true` only on that one in `config.yaml`. The framework picks it up with no other code changes.
