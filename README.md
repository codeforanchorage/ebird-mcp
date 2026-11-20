# eBird MCP Server

**Talk to Claude about birds.** A hosted [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude (and other MCP clients) query the [eBird v2 API](https://documenter.getpostman.com/view/664302/S1ENwy59) conversationally — recent sightings, notable rarities, hotspots, and the full eBird taxonomy, exposed as 10 callable tools.

Ask Claude *"What rare birds have been seen in Alaska this week?"* and it picks the right tool, calls eBird, and answers in plain English with species, locations, and dates. No screen-scraping, no separate UI — just Claude with new abilities.

The same server runs two ways, from the same plugin code:

| | Hosted (AWS Lambda) | Local (stdio) |
|---|---|---|
| What it is | A public HTTPS endpoint: Lambda + API Gateway + WAF | A subprocess your MCP client launches |
| Entry point | `server.adapters.aws_lambda.lambda_handler` | `scripts/stdio_server.py` |
| Who it serves | Anyone you give the URL to | You, on your machine |
| Needs | An AWS account, Terraform, an eBird API key | Python, an eBird API key |
| Start here | [Quickstart — deploy to AWS](#quickstart--deploy-to-aws) | [Quickstart — local stdio server](#quickstart--local-stdio-server) |

Hosted is deployable to your own AWS account in about 10 minutes with one `terraform apply`. Hard cost ceiling at ~$25/month even under viral load, ~$8/month at typical use. Local costs nothing and needs no AWS at all.

Built by [Code for Anchorage](https://codeforanchorage.org), modeled on the [OpenContext](https://github.com/CityOfBoston/OpenContext) / [anchorage-gis-mcp](https://github.com/codeforanchorage/anchorage-gis-mcp) plugin architecture. Ports the tool surface of the upstream npm [`ebird-mcp-server`](https://www.npmjs.com/package/ebird-mcp-server) stdio reference into a hosted HTTP endpoint so anyone can connect Claude via a URL — no local install required.

**Overview and client setup guide:** <https://codeforanchorage.org/ebird-mcp/> — includes step-by-step instructions for connecting Claude, ChatGPT, Gemini, and Microsoft Copilot, plus fair-use guidance for the shared public endpoint.

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
# {"jsonrpc": "2.0", "id": 1, "result": {}}
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
- **Civic-AI response design** — every response carries provenance (the upstream eBird URL, the parameters actually sent, a retrieved-at timestamp) plus epistemic caveats that keep LLMs honest about sparse data: absence-of-evidence framing, single-observer provenance, sampling-size warnings, notable-is-local rarity, taxonomic ambiguity.
- **Sized for LLM context windows** — `maxResults` caps at 1,000 per call; result sets above 20 records render as a compact table instead of verbose blocks; a 200 KB response ceiling truncates at a record boundary with an explicit notice, never silently.
- **AWS Lambda + API Gateway REST + WAFv2** — per-IP rate limiting, daily quota, CloudWatch alarms (Lambda errors, throttles, p95 duration, API Gateway 5xx), X-Ray tracing, JSON access logs.
- **One Terraform apply** — `./scripts/deploy.sh --environment prod` builds the deployment zip, plans, prompts, and applies.
- **Stateless and horizontally scalable** — `Mcp-Session-Id` is for log correlation only; no per-session storage.
- **Hard cost ceiling** — default `api_quota_limit` in `prod.tfvars` is 50k requests/day, capping worst-case AWS spend around $25/month even under a denial-of-wallet attack.

## Architecture

```
Claude / MCP client
  -> POST <api-gateway>/mcp  (JSON-RPC 2.0)
     -> WAFv2 (rate limit, managed rules)
     -> API Gateway REST stage  (throttle, usage plan)
        -> Lambda  (Python 3.14)
           server.adapters.aws_lambda.lambda_handler
              -> server.http_handler.UniversalHTTPHandler
                 -> core.mcp_server.MCPServer  (JSON-RPC dispatcher)
                    -> core.plugin_manager.PluginManager
                       -> plugins.ebird.plugin.EBirdPlugin
                          -> plugins.ebird.ebird_client.EBirdClient -> api.ebird.org
```

`core.*` and `server.*` come from the [`opencontext-core`](https://github.com/codeforanchorage/opencontext-core) package, shared by every Code for Anchorage MCP server; this repo contributes the eBird plugin. The dispatcher is hand-rolled JSON-RPC: no MCP SDK, no Lambda Web Adapter, no SSE. Each Lambda invocation handles one request/response cycle, and the server is stateless.

It serves two generations of the protocol, chosen per request:

- **Legacy, `2024-11-05` through `2025-11-25`** — the `initialize` handshake, then `tools/list`, `tools/call`, `ping` and `notifications/initialized`. The server echoes the client's requested version when it is one of these, otherwise answers with the newest of them. `Mcp-Session-Id` is minted on `initialize` and echoed for log correlation only; clients may skip `initialize` and call `tools/*` directly. `initialize` also returns an `instructions` field — an LLM-facing usage guide sourced from the plugin's `get_instructions()`.
- **Modern, `2026-07-28`** — no handshake and no session. A request names its version in `params._meta`, `server/discover` replaces `initialize`, and every result carries `resultType` and the server's identity.

Streamable HTTP transport rules are enforced in the shared handler: requests with an Origin header outside the allowlist get 403, an `MCP-Protocol-Version` header naming a version the server does not implement gets 400 (absent is fine), and JSON-RPC batch arrays get 400 (batching was removed from the spec in 2025-06-18).

## Repo layout

```
plugins/ebird/        eBird plugin: tool definitions, eBird API client, output formatting
                      (the only server code in this repo; used by every mode below)

(core/ and server/ are NOT in this repo: the JSON-RPC dispatcher, the HTTP
handler and the Lambda adapter come from the opencontext-core package,
pinned in requirements.txt and pyproject.toml.)

Hosted (AWS Lambda)
  terraform/aws/        Terraform IaC: Lambda, API GW, WAF, alarms, access logs
  scripts/deploy.sh     build .deploy/ from uv.lock, zip, terraform apply
  scripts/setup-backend.sh    create S3 + DynamoDB backend (one-time)
  scripts/refresh_taxonomy.py fetch the full eBird taxonomy into plugins/ebird/data/taxonomy.json
  scripts/smoke_prod.py       capability smoke test against a deployed endpoint

Local
  scripts/stdio_server.py  stdio MCP server: a client launches it as a subprocess
  scripts/local_server.py  the same server over HTTP on localhost, for development
  scripts/test_streamable_http.sh  HTTP smoke test: initialize, list, call

tests/                unit tests (no network; upstream calls are stubbed)
uv.lock               exact dependency versions: what CI tests and deploy.sh ships
config.yaml           local config (gitignored; copy from config-example.yaml)
config-example.yaml   template
.mcp.json             Claude Code MCP config (gitignored; copy from .mcp.json.example)
.mcp.json.example     points Claude Code at the hosted endpoint — no API key needed
```

## Prerequisites

- Python **3.11+**
- Terraform **>= 1.0**
- AWS CLI configured (`aws configure`)
- `zip` or PowerShell (for packaging on Windows)
- An eBird API key — free, instant: <https://ebird.org/api/keygen>
- `uv` (on PATH, or `pip install uv`) so deploys install the locked versions in `uv.lock`

## Quickstart — local stdio server

For MCP clients that launch a server as a subprocess (Claude Desktop's
config file, Claude Code, most IDE integrations). No AWS, no HTTP server.

```powershell
# 1. Configure
copy config-example.yaml config.yaml
# Edit config.yaml: set plugins.ebird.api_key to your eBird key

# 2. Install the runtime dependencies into .venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# or, with uv, the exact locked versions:  uv sync --no-dev
```

Then register it with your client, using the venv's Python and the full
path to the script (the script finds `config.yaml` and its plugins from any
working directory):

```json
{
  "mcpServers": {
    "ebird": {
      "command": "C:\\path\\to\\ebird-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\ebird-mcp\\scripts\\stdio_server.py"]
    }
  }
}
```

On macOS/Linux the command is `/path/to/ebird-mcp/.venv/bin/python`. To use
a config file somewhere else, add
`"env": {"EBIRD_MCP_CONFIG_FILE": "/path/to/config.yaml"}`.

It is the same server as the hosted one: the same ten tools, protocol
negotiation and error handling, served through the same request handler.
Logs go to stderr; stdout carries only protocol messages. `get_taxonomy` is
answered from a local bundle if you run `python scripts/refresh_taxonomy.py`
once, and from the live eBird API otherwise.

A stdio-only client that should use the **hosted** endpoint instead (no
eBird key of your own) can go through a generic bridge such as
`npx mcp-remote https://ebird.codeforanchorage.org/mcp`.

## Quickstart — local HTTP server (development)

```powershell
# 1. Configure
copy config-example.yaml config.yaml
# Edit config.yaml: set plugins.ebird.api_key to your eBird key

# 2. Install dev deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# 3. Run the unit tests (no network — upstream calls are stubbed)
python -m pytest tests

# 4. Run locally
python scripts/local_server.py
# Listening on http://localhost:8000/mcp
# Port 8000 taken? python scripts/local_server.py --port 8931
# (or set EBIRD_MCP_PORT), then pass the URL to the smoke test below.

# 5. Smoke test (in another shell, requires jq)
bash ./scripts/test_streamable_http.sh
```

### Connecting Claude Code to this server

`.mcp.json.example` points at the deployed endpoint, which needs no API key
and no local setup:

```bash
cp .mcp.json.example .mcp.json     # copy .mcp.json.example .mcp.json  on Windows
```

To test **your own changes** instead of the deployed server, start
`python scripts/local_server.py` and point the URL at it:

```json
{
  "mcpServers": {
    "ebird": { "type": "http", "url": "http://localhost:8000/mcp" }
  }
}
```

Keep only one of these entries enabled at a time — two would register two
copies of the same ten tools. Either way, restart Claude Code (or run
`claude mcp list`) after editing so the tool list is re-fetched.

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

Commit before you deploy: the script refuses a working tree with uncommitted changes, so that what is live always matches a commit (`--allow-dirty` overrides, for emergencies).

The script will:
1. Validate that exactly one plugin is enabled and that the eBird key is set.
2. Refresh the bundled eBird taxonomy (`scripts/refresh_taxonomy.py` → `plugins/ebird/data/taxonomy.json`) so common taxonomy lookups are served from the deployment package instead of burning eBird API quota. A refresh failure is non-fatal — the plugin falls back to live API calls.
3. Build `.deploy/` with the dependency versions in `uv.lock` (the ones CI tests) installed for `x86_64-manylinux2014` / Python 3.14 (the Lambda runtime), and stamp the package with a `BUILD_INFO.json` (commit, dirty, locked). Without `uv` it falls back to pip and `requirements.txt`, which is not locked.
4. Zip into `lambda-deployment.zip` and copy it (and `config.yaml`) into `terraform/aws/`.
5. Run `terraform plan` with the chosen `*.tfvars`, prompt for confirmation, then `terraform apply`.
6. Print the public MCP endpoint URL.

Paste that URL into Claude (Settings → Connectors → Add custom connector). Done.

## Configuration

`config.yaml` is loaded at Terraform plan time, JSON-encoded, and baked into the Lambda's `OPENCONTEXT_CONFIG` environment variable. At runtime, the handler reads it (or falls back to `config.yaml` if the env var is missing, which is the local-dev path).

```yaml
server_name: "eBird MCP"
plugins:
  ebird:
    enabled: true
    api_key: "REPLACE_ME"
    base_url: "https://api.ebird.org/v2"
    timeout: 20              # upstream HTTP timeout; must stay under lambda_timeout
    default_max_results: 100
    default_back: 14
aws:
  region: "us-west-2"
  lambda_name: "ebird-mcp"
  lambda_memory: 512
  lambda_timeout: 28         # must stay under API Gateway's fixed 29s
logging:
  level: "INFO"
  format: "json"
```

> The eBird API key lives in `config.yaml` and is visible to anyone with `lambda:GetFunctionConfiguration` on the function. Acceptable for a low-cost API key; if you want stronger isolation, switch the `OPENCONTEXT_CONFIG` env var for an SSM `SecureString` lookup in `server/http_handler.py::_load_config` (in the `opencontext-core` package) and grant the Lambda role `ssm:GetParameter`.

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
- **CORS / Origin validation.** The Lambda's `UniversalHTTPHandler` allowlist accepts `https://claude.ai`, `https://claude.com`, `https://console.anthropic.com`, and the MCP Inspector on `localhost:6274`. Browser requests from any other Origin get 403; non-browser clients (including claude.ai's backend connector, which sends no Origin header) are unaffected. To change the list, set `server_allowed_origins` in `config.yaml` (it replaces the default list, so include every origin you want).
- **Rate limits.** Defaults in `prod.tfvars`: WAF 50 req/IP per 5 min (sized against the upstream eBird 1000-calls/day quota, not AWS capacity), API GW 20 rps sustained / 40 burst / 50,000 per day. Tune for your traffic. Code for Anchorage's own instance sets `use_shared_waf = true`, which attaches a web ACL shared by its whole MCP fleet; the per-IP limit for that instance is then set in the `mcp-stats` repo, not here. Leave `use_shared_waf` false to get a dedicated ACL governed by `waf_rate_limit_per_5min`.
- **Input validation, in two layers.** `regionCode`/`speciesCode`/`locale` flow into upstream URL paths, so the plugin regex-validates them before any URL is constructed (`plugins/ebird/plugin.py::_clamp_and_validate`, pinned by `tests/test_input_validation.py`). On prod, the WAF's managed rule sets (`CommonRuleSet`, `KnownBadInputs`) independently 403 traversal-shaped payloads at the edge before they reach the Lambda. Numeric args (`back`, `dist`, `maxResults`) are clamped into valid ranges, and the **effective** values are echoed in the response's `Query:` line so callers always see what actually ran.
- **Response-size controls.** `maxResults` caps at 1,000 per call (eBird itself accepts 10,000, but that renders to 3+ MB of text no client can use). Result sets above 20 records switch from the readable per-record block format to a compact pipe-delimited table, and a 200 KB body ceiling truncates at a record boundary with an explicit `RESPONSE SIZE CEILING` notice.
- **Bundled taxonomy.** The deploy script bakes the full eBird taxonomy into the Lambda package; `get_taxonomy` calls with `locale=en` / `fmt=json` are served from the bundle instead of the live API. This is the single biggest saver against the 1000-calls/day eBird quota. Non-English locales and CSV fall through to the live API.
- **Cold-start cleanup.** The Lambda adapter shuts down the plugin manager after every invocation to avoid `"Event loop is closed"` errors with httpx. This means each invocation re-initializes the eBird client. At Lambda concurrency this is fine; if cold-starts become a problem, keep the client at module scope and remove the shutdown in `_run_with_cleanup`.
- **MCP protocol versions.** `2024-11-05` through `2025-11-25` (legacy) and `2026-07-28` (modern) are served; see Architecture above. The version lists live in the `opencontext-core` package (`core/mcp_server.py`), so a new revision is added there and arrives here by moving the pin in `requirements.txt` and `pyproject.toml`, then `uv lock`.

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
| `.mcp.json` | Which endpoint Claude Code connects to | `.mcp.json.example` |
| `terraform/aws/backend.tf` | Your AWS account ID, S3 bucket | `terraform/aws/backend.tf.example` (regenerated by `scripts/setup-backend.sh`) |
| `terraform/aws/config.yaml`, `lambda-deployment.zip` | Build artifacts | rebuilt by `scripts/deploy.sh` |
| `plugins/ebird/data/taxonomy.json` | Bundled eBird taxonomy (~7 MB) | rebuilt by `scripts/refresh_taxonomy.py` (deploy runs it automatically) |
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

## Credits and thanks

- A huge thank you to the **City of Boston** — especially CIO **Santi Garces**, whose push to open city data to AI agents via MCP ([OpenContext](https://github.com/CityOfBoston/OpenContext)) showed cities how to do this, and **Srihari Raman**, OpenContext's author at Boston's Department of Innovation and Technology. This project's plugin architecture is cloned directly from OpenContext (via [codeforanchorage/anchorage-gis-mcp](https://github.com/codeforanchorage/anchorage-gis-mcp)). MIT-licensed.
- Tool surface ported from [moonbirdai/ebird-mcp-server](https://github.com/moonbirdai/ebird-mcp-server) by Ciara Adkins (published on npm as [`ebird-mcp-server`](https://www.npmjs.com/package/ebird-mcp-server)) — thank you for the original stdio implementation this hosted port is built on.
- Data from the [eBird API](https://documenter.getpostman.com/view/664302/S1ENwy59), by the Cornell Lab of Ornithology — please follow eBird's [terms of use](https://ebird.org/news/please-share-your-data-with-ebird).

## License

MIT.
