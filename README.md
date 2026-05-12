# eBird MCP Server (AWS Lambda)

**Talk to Claude about birds.** A hosted [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude (and other MCP clients) query the [eBird v2 API](https://documenter.getpostman.com/view/664302/S1ENwy59) conversationally — recent sightings, notable rarities, hotspots, and the full eBird taxonomy, exposed as 10 callable tools.

Ask Claude *"What rare birds have been seen in Alaska this week?"* and it picks the right tool, calls eBird, and answers in plain English with species, locations, and dates. No screen-scraping, no separate UI — just Claude with new abilities.

Runs on AWS Lambda + API Gateway + WAF. Deployable to your own AWS account in about 10 minutes with one `terraform apply`. Hard cost ceiling at ~$25/month even under viral load, ~$8/month at typical use.

Built by [Code for Anchorage](https://codeforanchorage.org), modeled on the [OpenContext](https://github.com/CityOfBoston/OpenContext) / [anchorage-gis-mcp](https://github.com/codeforanchorage/anchorage-gis-mcp) plugin architecture. Ports the tool surface of the upstream npm [`ebird-mcp-server`](https://www.npmjs.com/package/ebird-mcp-server) stdio reference into a hosted HTTP endpoint so anyone can connect Claude via a URL — no local install required.

## Try it (no deploy needed)

Code for Anchorage runs a public instance:

> **<https://ebird.codeforanchorage.org/mcp>**

In Claude (web or desktop) → **Settings → Connectors → Add custom connector**, paste that URL, and you're done. The 10 eBird tools light up immediately. Then ask:

- *"What's been seen at birding hotspots near Anchorage in the last 3 days?"*
- *"Have any Snowy Owls been reported in Alaska this winter?"*
- *"Show me notable sightings within 25 km of Homer, Alaska."*
- *"What's the scientific name for the eBird code 'amecro'? Any hybrids tracked for Mallards?"*
- *"Find the top 10 hotspots in Southeast Alaska by species count."*

Claude picks the right tool, calls eBird, and answers in plain English with species, locations, observer counts, and timestamps.

Prefer to verify it's up before adding a connector?

```bash
curl -sS https://ebird.codeforanchorage.org/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
# {"jsonrpc": "2.0", "id": 1, "result": {"status": "ok"}}
```

The public endpoint is rate-limited (currently 50,000 requests/day, 20 rps sustained per the production tfvars). If you expect heavy usage or want isolation from other users sharing the rate limits, deploy your own copy — instructions below.

## When you'd want to deploy your own copy

- You want a different eBird API key (rate-limit isolation, separate usage tracking)
- You want to lock the connector to your own domain
- You want to extend it with other data sources via the plugin architecture (one fork = one MCP server)
- You're learning AWS / MCP / Terraform and this is a small, complete reference

## What you get

- **Single public `POST /mcp` endpoint** — Streamable HTTP JSON-RPC. Paste the URL into Claude and the eBird tools light up automatically.
- **10 tools** covering recent observations, notable rarities, nearby observations (with species filters), hotspots, nearby hotspots, taxonomy, and species forms — full table below.
- **AWS Lambda + API Gateway REST + WAFv2** — per-IP rate limiting, daily quota, CloudWatch alarms (Lambda errors, throttles, p95 duration, API Gateway 4xx/5xx probing), X-Ray tracing, JSON access logs.
- **One Terraform apply** — `./scripts/deploy.sh --environment prod` builds the deployment zip, plans, prompts, and applies.
- **Stateless and horizontally scalable** — `Mcp-Session-Id` is for log correlation only; no per-session storage.
- **Hard cost ceiling** — default `api_quota_limit` in `prod.tfvars` is 50k requests/day, capping worst-case AWS spend around $25/month even under a denial-of-wallet attack.

## Architecture

```
Claude / MCP client
  -> POST <api-gateway>/mcp  (JSON-RPC 2.0)
     -> WAFv2 (rate limit, managed rules)
     -> API Gateway REST stage  (throttle, usage plan)
        -> Lambda  (Python 3.11)
           server.adapters.aws_lambda.lambda_handler
              -> server.http_handler.UniversalHTTPHandler
                 -> core.mcp_server.MCPServer  (JSON-RPC dispatcher)
                    -> core.plugin_manager.PluginManager
                       -> plugins.ebird.plugin.EBirdPlugin
                          -> plugins.ebird.ebird_client.EBirdClient -> api.ebird.org
```

The hand-rolled JSON-RPC dispatcher implements MCP `initialize`, `tools/list`, `tools/call`, `ping`, and `notifications/initialized`. No MCP SDK; no Lambda Web Adapter; no SSE. Each Lambda invocation handles one request/response cycle. `Mcp-Session-Id` is generated on initialize and echoed back for log correlation only — the server is stateless.

## Repo layout

```
core/                 framework: interfaces, MCP server, plugin manager, validators, logging
plugins/ebird/        eBird plugin: tool definitions, eBird API client, output formatting
server/
  http_handler.py     universal (cloud-agnostic) HTTP handler
  adapters/aws_lambda.py  Lambda event -> universal handler
terraform/aws/        Terraform IaC: Lambda, API GW, WAF, alarms, access logs
scripts/
  deploy.sh           build .deploy/, zip, terraform apply
  setup-backend.sh    create S3 + DynamoDB backend (one-time)
  test_streamable_http.sh  smoke test: initialize, list, call
local_server.py       aiohttp wrapper for local testing on http://localhost:8000/mcp
stdio_bridge.py       stdio<->HTTP bridge for stdio-only MCP clients
config.yaml           local config (gitignored; copy from config-example.yaml)
config-example.yaml   template
mcp-server/           original stdio reference (kept for reference)
```

## Prerequisites

- Python **3.11+**
- Terraform **>= 1.0**
- AWS CLI configured (`aws configure`)
- `zip` or PowerShell (for packaging on Windows)
- An eBird API key — free, instant: <https://ebird.org/api/keygen>
- (Optional) `uv` for faster dependency installs

## Quickstart — local

```powershell
# 1. Configure
copy config-example.yaml config.yaml
# Edit config.yaml: set plugins.ebird.api_key to your eBird key

# 2. Install dev deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Run locally
python local_server.py
# Listening on http://localhost:8000/mcp

# 4. Smoke test (in another shell, requires jq)
bash ./scripts/test_streamable_http.sh
```

## Quickstart — deploy to AWS

```bash
# Once per account/region: create S3 state bucket + DynamoDB lock table
./scripts/setup-backend.sh        # writes terraform/aws/backend.tf

# Edit config.yaml and set plugins.ebird.api_key

# Staging
./scripts/deploy.sh --environment staging
# Prod
./scripts/deploy.sh --environment prod
```

The script will:
1. Validate that exactly one plugin is enabled and that the eBird key is set.
2. Build `.deploy/` with all dependencies installed for `x86_64-manylinux2014` / Python 3.11.
3. Zip into `lambda-deployment.zip` and copy it (and `config.yaml`) into `terraform/aws/`.
4. Run `terraform plan` with the chosen `*.tfvars`, prompt for confirmation, then `terraform apply`.
5. Print the public MCP endpoint URL.

Paste that URL into Claude (Settings → Connectors → Add custom connector). Done.

## Configuration

`config.yaml` is loaded at Terraform plan time, JSON-encoded, and baked into the Lambda's `EBIRD_MCP_CONFIG` environment variable. At runtime, the handler reads it (or falls back to `config.yaml` if the env var is missing, which is the local-dev path).

```yaml
server_name: "eBird MCP"
plugins:
  ebird:
    enabled: true
    api_key: "REPLACE_ME"
    base_url: "https://api.ebird.org/v2"
    timeout: 30
    default_max_results: 100
    default_back: 14
aws:
  region: "us-west-2"
  lambda_name: "ebird-mcp"
  lambda_memory: 512
  lambda_timeout: 30
logging:
  level: "INFO"
  format: "json"
```

> The eBird API key lives in `config.yaml` and is visible to anyone with `lambda:GetFunctionConfiguration` on the function. Acceptable for a low-cost API key; if you want stronger isolation, switch the `EBIRD_MCP_CONFIG` env var for an SSM `SecureString` lookup in `server/http_handler.py::_load_config` and grant the Lambda role `ssm:GetParameter`.

## Tools

| Tool name (after MCP namespacing) | What it does |
|---|---|
| `ebird__get_recent_observations` | Recent observations in a region (country/state/county/hotspot) |
| `ebird__get_recent_observations_for_species` | Recent observations of a species in a region |
| `ebird__get_notable_observations` | Notable/rare birds in a region |
| `ebird__get_nearby_observations` | Recent observations near a lat/lng |
| `ebird__get_nearby_notable_observations` | Notable birds near a lat/lng |
| `ebird__get_nearby_observations_for_species` | Species observations near a lat/lng |
| `ebird__get_hotspots` | Hotspots in a region |
| `ebird__get_nearby_hotspots` | Hotspots near a lat/lng |
| `ebird__get_taxonomy` | eBird taxonomy (filtered by `cat`, locale) |
| `ebird__get_taxonomy_forms` | Subspecies/hybrid forms for a species code |

## Operational notes

- **Stateless.** `Mcp-Session-Id` is for log correlation; the server stores no per-session state. Horizontally scalable.
- **CORS.** The Lambda's `UniversalHTTPHandler` allowlist accepts `https://claude.ai`, `https://console.anthropic.com`, and the MCP Inspector on `localhost:6274`. Edit `server/http_handler.py::ALLOWED_ORIGINS` to add more.
- **Rate limits.** Defaults in `prod.tfvars`: WAF 300 req/IP/5min, API GW 5 rps / 10 burst / 3000/day. Tune for your traffic.
- **Cold-start cleanup.** The Lambda adapter shuts down the plugin manager after every invocation to avoid `"Event loop is closed"` errors with httpx. This means each invocation re-initializes the eBird client (one `taxonomy/forms` smoke call). At Lambda concurrency this is fine; if cold-starts become a problem, keep the client at module scope and remove the shutdown in `_run_with_cleanup`.
- **MCP protocol version advertised.** `"2025-03-26"` — bump in `core/mcp_server.py::_handle_initialize` if Claude Connectors moves on.

## Cost

At AWS pricing as of mid-2026, with the default `prod.tfvars` settings (`api_quota_limit = 50000`, `lambda_reserved_concurrency = 50`):

| Traffic | Approx. monthly cost |
|---|---|
| Quiet (~100 req/day) | ~$6 (mostly the $5 WAF ACL) |
| Typical use (~1k req/day) | ~$8 |
| Trending (~10k req/day) | ~$12 |
| Cap hit every day (50k/day) | ~$25 worst case |

The daily quota acts as a hard cost ceiling — a denial-of-wallet attack or a runaway viral spike can't push the bill past ~$25 because excess requests get 429d at the API Gateway. Lambda is essentially free at conversational traffic levels (the AWS perpetual free tier covers 1M requests + 400k GB-seconds/month).

If you expect more than 50k req/day of real traffic, raise `api_quota_limit` deliberately in `prod.tfvars` — and consider asking eBird about acceptable use before publicizing the connector.

## Adding more tools / plugins

The framework auto-discovers any `plugins/<name>/plugin.py` that exports a class inheriting from `MCPPlugin`. To add tools to the eBird plugin, extend `plugins/ebird/plugin.py::EBirdPlugin.get_tools()` and the matching branch in `execute_tool()`.

To run a different data source instead of eBird (one MCP server per fork is enforced — see `core/validators.py::validate_plugin_count`), drop a sibling plugin into `plugins/<your_name>/` and toggle `enabled: true` only on that one in `config.yaml`.

## Pushing to GitHub safely

The following files contain secrets or account-specific data and are gitignored:

| File | What's in it | Committed substitute |
|---|---|---|
| `config.yaml` | eBird API key, AWS region/Lambda name | `config-example.yaml` |
| `.mcp.json` | eBird API key (stdio fallback) | `.mcp.json.example` |
| `terraform/aws/backend.tf` | Your AWS account ID, S3 bucket | `terraform/aws/backend.tf.example` (regenerated by `scripts/setup-backend.sh`) |
| `terraform/aws/config.yaml`, `lambda-deployment.zip` | Build artifacts | rebuilt by `scripts/deploy.sh` |
| `.terraform/`, `*.tfstate*`, `tfplan` | Local Terraform state | state lives in S3 |

Before your first push, run this from Git Bash to verify nothing slipped through:

```bash
# Stage everything, then look at what would be committed
git init
git add .
git status

# Sanity grep — should print NOTHING:
git ls-files | xargs grep -l "$(grep '^    api_key:' config.yaml | sed 's/.*"\(.*\)".*/\1/')" 2>/dev/null
```

If the grep prints any file, that file contains your real key and needs to be either edited or added to `.gitignore` before commit.

## Credits

- Architecture cloned from [codeforanchorage/anchorage-gis-mcp](https://github.com/codeforanchorage/anchorage-gis-mcp) and [CityOfBoston/OpenContext](https://github.com/CityOfBoston/OpenContext). MIT-licensed.
- Tool surface ported from [`ebird-mcp-server`](https://www.npmjs.com/package/ebird-mcp-server).
- Data from the [eBird API](https://documenter.getpostman.com/view/664302/S1ENwy59) — please follow eBird's [terms of use](https://ebird.org/news/please-share-your-data-with-ebird).

## License

MIT.
