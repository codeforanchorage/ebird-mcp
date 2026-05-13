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
python local_server.py                        # http://localhost:8000/mcp

# Smoke test the running server (requires jq + Git Bash on Windows)
bash ./scripts/test_streamable_http.sh        # defaults to localhost:8000
bash ./scripts/test_streamable_http.sh https://your-endpoint/mcp

# Deploy
bash ./scripts/setup-backend.sh               # one-time per AWS account
bash ./scripts/deploy.sh --environment staging
bash ./scripts/deploy.sh --environment prod
```

There is no test suite, linter, or formatter wired up — `pyproject.toml` only declares metadata, no dev tooling.

The deploy script handles packaging: it builds `.deploy/` with `pip install --platform manylinux2014_x86_64 --python-version 3.11 --only-binary :all:` (or `uv` if present), zips to `lambda-deployment.zip`, and copies that plus `config.yaml` into `terraform/aws/` where Terraform reads them.

## Architecture

Request flow:

```
Client -> POST /mcp (JSON-RPC 2.0)
  -> [AWS only] WAFv2 + API Gateway REST + usage plan
  -> server.adapters.aws_lambda.lambda_handler           (Lambda)
     or local_server.py aiohttp app                       (local)
     -> server.http_handler.UniversalHTTPHandler         (cloud-agnostic)
        -> core.mcp_server.MCPServer                     (JSON-RPC dispatcher)
           -> core.plugin_manager.PluginManager
              -> plugins.<name>.plugin.<Class>           (one enabled at a time)
                 -> plugins.<name>.<api_client> -> upstream API
```

Key design points future Claude should know before editing:

- **Hand-rolled JSON-RPC, no MCP SDK.** `core/mcp_server.py` implements `initialize`, `tools/list`, `tools/call`, `ping`, and `notifications/initialized`. The advertised protocol version is `"2025-03-26"` (bump in `_handle_initialize` if Claude Connectors moves on).
- **Stateless.** `Mcp-Session-Id` is minted on `initialize` and echoed for log correlation only. No per-session storage; horizontally scalable.
- **Plugin discovery.** `PluginManager.discover_plugins()` walks `plugins/` (and optional `custom_plugins/`) looking for any subdir with `plugin.py` exporting a class that inherits from `MCPPlugin`. Tools are namespaced `<plugin>__<tool>` automatically — never include the prefix in `ToolDefinition.name`.
- **One enabled plugin per deployment, enforced at load time.** Toggle `enabled: true` on exactly one plugin in `config.yaml` — `validate_plugin_count` raises `ConfigurationError` otherwise.
- **Config is baked into the Lambda env var.** `terraform/aws/main.tf` reads `config.yaml` at plan time, `jsonencode`s it, and injects it as `EBIRD_MCP_CONFIG`. `server/http_handler.py::_load_config` reads that env var first, falling back to `config.yaml` for local dev. To rotate the eBird API key, edit `config.yaml` and redeploy.
- **Per-invocation plugin shutdown on Lambda.** `server/adapters/aws_lambda.py::_run_with_cleanup` calls `plugin_manager.shutdown()` after every request to avoid `"Event loop is closed"` errors from `httpx` when the Lambda execution context is reused. Each warm invocation re-initializes the eBird client. If cold-starts ever become a real problem, move the client to module scope and remove the shutdown — but understand the httpx/asyncio interaction first.
- **Adapter responsibilities split.** `aws_lambda.py` handles event shape (API GW v1, v2, Function URL), base64 bodies, the 64 KB body cap, and OPTIONS preflight. Everything below it (CORS allowlist, JSON-RPC, plugin dispatch) is cloud-agnostic and lives in `server/http_handler.py` and `core/`.
- **Hardening in the eBird plugin.** `plugins/ebird/plugin.py::_clamp_and_validate` enforces regex-validated `regionCode`/`speciesCode`/`locale` (path-injection defense), clamps numeric args (`back`, `maxResults`, `dist`, `lat`/`lng`) silently into eBird's accepted ranges, and rejects unknown `cat`/`fmt` values. Retries (`_RETRY`) are limited to two attempts on transport/read-timeout errors only — 4xx/5xx flow straight through.
- **Error responses are scrubbed.** `core/mcp_server.py::handle_request` mints a UUID `error_id`, logs the full exception to CloudWatch under that ID, and returns only `{"code": -32603, "message": "Internal error", "data": "Error ID: ..."}` to the client. Do not regress this by surfacing `str(e)` in responses.

## Files that matter

- `core/mcp_server.py` — JSON-RPC dispatcher; bump `protocolVersion` here.
- `core/plugin_manager.py` — discovery + tool registration; tool prefixing happens here.
- `core/validators.py` — one-plugin rule; config loading.
- `server/http_handler.py` — `ALLOWED_ORIGINS` CORS allowlist; config env-var loader.
- `server/adapters/aws_lambda.py` — Lambda entry point; body size cap; per-invocation cleanup.
- `plugins/ebird/plugin.py` — tool catalog (`get_tools`) + dispatch (`execute_tool`); hardening helpers.
- `plugins/ebird/ebird_client.py` — thin httpx wrapper over eBird v2.
- `terraform/aws/main.tf` — Lambda + IAM; reads `config.yaml` at plan time.
- `terraform/aws/{api_gateway,waf,cloudwatch_alarms,access_logs}.tf` — the rest of the stack.
- `terraform/aws/{staging,prod}.tfvars` — per-env quota, rate limits, concurrency.
- `local_server.py` — aiohttp wrapper that exposes `UniversalHTTPHandler` on localhost.
- `stdio_bridge.py` — thin stdio<->HTTP shim for stdio-only MCP clients (forwards to a running HTTP server).

## Files NOT under version control (rebuilt or local-only)

`config.yaml`, `.mcp.json`, `terraform/aws/backend.tf`, `terraform/aws/config.yaml`, `lambda-deployment.zip`, `.deploy/`, `.terraform/`, `*.tfstate*`, `tfplan`. Their `.example` counterparts are committed; `scripts/setup-backend.sh` regenerates `backend.tf`; `scripts/deploy.sh` regenerates the zip and the copy of `config.yaml`.

## Adding tools or new plugins

- Adding a tool to eBird: extend `plugins/ebird/plugin.py::EBirdPlugin.get_tools()` with a new `ToolDefinition`, then add the matching branch in `execute_tool()`. Add an upstream call to `plugins/ebird/ebird_client.py`. Validate any new path-flowing args in `_clamp_and_validate`.
- Adding a different data source: drop a sibling `plugins/<name>/plugin.py` exporting an `MCPPlugin` subclass, set its `plugin_name`, and toggle `enabled: true` only on that one in `config.yaml`. The framework picks it up with no other code changes.
