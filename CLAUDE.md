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
```

Tests: `python -m unittest discover tests` — plain unittest, no network (upstream calls are stubbed). `tests/test_civic_ai_patterns.py` pins the caveat/formatting behavior; `tests/test_input_validation.py` pins the path-traversal rejection (regionCode/speciesCode flow into upstream URL paths) and the numeric clamps. No linter or formatter wired up — `pyproject.toml` only declares metadata.

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
- **Response-size controls.** `maxResults` is capped at 1000 (`_MAX_RESULTS_CEILING` — deliberately below eBird's own 10000, which would render to 3+ MB of text). Observation/hotspot results above 20 records (`_COMPACT_FORMAT_THRESHOLD`) render as a compact pipe-delimited table instead of the verbose block format; small results keep the blocks. A 200 KB byte ceiling (`_MAX_BODY_BYTES`, via `_join_with_size_cap`) backstops the formatted body independent of record count, truncating at a record boundary with an explicit `RESPONSE SIZE CEILING` notice — never silently.
- **Bundled taxonomy.** `scripts/refresh_taxonomy.py` fetches the full eBird taxonomy (every `cat`) once at deploy time and writes it to `plugins/ebird/data/taxonomy.json` (gitignored). `ebird_client.py::get_taxonomy` serves the common case (`locale=en`, `fmt=json`) from that bundle via a module-level lazy cache, filtering by `cat` in-process. Non-English locales and `fmt=csv` fall through to the live API. The bundle is the single biggest eBird-quota saver — taxonomy lookups would otherwise be a per-conversation drain against the 1000/day cap. If the file is missing the plugin transparently falls back to live calls, so a refresh failure during deploy is non-fatal.
- **Error responses are scrubbed.** `core/mcp_server.py::handle_request` mints a UUID `error_id`, logs the full exception to CloudWatch under that ID, and returns only `{"code": -32603, "message": "Internal error", "data": "Error ID: ..."}` to the client. Do not regress this by surfacing `str(e)` in responses.

## Files that matter

- `core/mcp_server.py` — JSON-RPC dispatcher; bump `protocolVersion` here.
- `core/plugin_manager.py` — discovery + tool registration; tool prefixing happens here.
- `core/validators.py` — one-plugin rule; config loading.
- `server/http_handler.py` — `ALLOWED_ORIGINS` CORS allowlist; config env-var loader.
- `server/adapters/aws_lambda.py` — Lambda entry point; body size cap; per-invocation cleanup.
- `plugins/ebird/plugin.py` — tool catalog (`get_tools`) + dispatch (`execute_tool`); hardening helpers.
- `plugins/ebird/ebird_client.py` — thin httpx wrapper over eBird v2. Watch the endpoint paths: eBird is inconsistent — taxonomy lives under `/ref/taxonomy/ebird` but taxonomic *forms* live under `/ref/taxon/forms/{speciesCode}` (`taxon`, not `taxonomy`). The wrong path 404s for every species; don't "correct" `taxon` back to `taxonomy`. Also: `get_taxonomy` serves from `plugins/ebird/data/taxonomy.json` (the deploy-time bundle) when feasible; see the bundled-taxonomy bullet above.
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
