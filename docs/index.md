# eBird MCP Server

A hosted [Model Context Protocol](https://modelcontextprotocol.io) server that gives AI assistants access to real bird-observation data from [eBird](https://ebird.org), Cornell Lab of Ornithology's global citizen-science database.

Ask your AI to find recent sightings near a coordinate, list rare-bird reports in a region, look up species codes, or surface the most active birding hotspots — backed by ~1.8 billion observations and updated continuously by birders worldwide.

---

## What is MCP?

The **Model Context Protocol** is an open standard, originally proposed by Anthropic, for connecting AI assistants to external tools and data sources. Instead of every AI vendor inventing a different plugin system, MCP lets a single server expose its capabilities once and be reused by any compatible client — Claude, ChatGPT, Gemini, Microsoft Copilot, and a growing list of others.

This project is one such server: it speaks MCP over HTTP and translates your AI's questions into eBird API calls, then returns observation, hotspot, and taxonomy data back in a structured form the model can reason about.

Spec and reference docs: [modelcontextprotocol.io](https://modelcontextprotocol.io).

---

## Try it now

Endpoint:

```
https://ebird.codeforanchorage.org/mcp
```

No API key required. Point any MCP-capable client at the URL above and you're connected. See **[Connect a client](#connect-a-client)** below for vendor-specific setup.

!!! warning "Please read before you use it"
    This is a free, shared instance run by **[Code for Anchorage](https://codeforanchorage.org)**. eBird itself rate-limits the upstream API to **~1000 calls per day total** across everyone using this server — there is no per-user budget. Be considerate:

    - One question per LLM turn is fine. Don't put it in a loop or behind a cron.
    - Don't use it for scraping, bulk exports, or anything you'd be embarrassed to explain to a volunteer.
    - The server may rate-limit or refuse calls if the daily eBird quota is close to exhaustion. If you see a "daily quota nearly exhausted" message, try again after UTC midnight.
    - WAF blocks single-IP traffic above 10 requests/minute. Conversational use is well under this; automated traffic is not.
    - This endpoint may go down for maintenance or be rebuilt without notice. **Do not depend on it for production workloads.**

    For serious or sustained use, **[self-host your own copy](#self-host)** — the repo includes a one-command Terraform deploy.

---

## Connect a client

MCP support varies across vendors. Claude has the most mature end-user UI; ChatGPT, Gemini, and Copilot are catching up but the configuration paths differ. Pick your client below.

=== "Claude"

    Two paths depending on which Claude surface you're using.

    **A. Claude.ai (web or app) — Connectors UI**

    Available on **Free, Pro, Max, Team, and Enterprise** plans. Free-tier accounts can add **one** custom connector; paid tiers can add more. eBird MCP is one connector, so this works on free.

    1. Open **Settings → Connectors**.
    2. Click **Add Custom Connector**.
    3. Fill in:
        - **Name**: `eBird MCP`
        - **URL**: `https://ebird.codeforanchorage.org/mcp`
    4. Click **Add**, then **Connect** (follow any auth prompts — this server uses no auth, so it should connect immediately).
    5. The connector appears in your chat tools automatically in any new conversation.

    **B. Claude Desktop — JSON config (alternative)**

    1. Open Claude Desktop → **Developer** (left sidebar) → **Edit Config**. This opens `claude_desktop_config.json`:
        - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
        - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
    2. Add an `mcpServers` entry:

        ```json
        {
          "mcpServers": {
            "ebird": {
              "url": "https://ebird.codeforanchorage.org/mcp"
            }
          }
        }
        ```

    3. Save and **fully quit and reopen** Claude Desktop (not just close the window — it needs to restart to pick up the new config).

    !!! tip
        Connected either way, try: *"What rare birds have been reported near Anchorage, Alaska in the past week?"* Claude will route through `ebird__get_nearby_notable_observations` and return real observations with checklist links and review-status flags.

    Reference: [support.claude.com](https://support.claude.com) for the latest Connectors / Claude Desktop docs.

=== "ChatGPT"

    **ChatGPT custom MCP connectors (apps)**

    ChatGPT connects to remote MCP servers — like this one — over Streamable HTTP or SSE, exposed as a *custom app* / *custom connector*. Local stdio MCP servers will not work; you need the hosted HTTPS endpoint above.

    !!! note "Availability"
        Custom MCP connectors are plan- and feature-dependent (typically Team / Enterprise / Edu workspaces, sometimes Plus in deep-research contexts). The exact menu paths shift as OpenAI iterates — check OpenAI's current docs if anything below has moved.

    **Workspace admin — one-time setup:**

    Enable Developer Mode at **Workspace Settings → Permissions & Roles → Connected Data → Developer mode / Create custom MCP connectors**. Without this, members can't create connectors.

    **Then any member can create the connector:**

    1. In ChatGPT, open **Settings → Apps** (named "Connectors" in some plans).
    2. Near **Advanced settings**, click **Create app**.
    3. Fill in:
        - **Name**: `eBird MCP`
        - **MCP server URL**: `https://ebird.codeforanchorage.org/mcp`
        - **Authentication**: None
    4. Save. The connector appears in your workspace's app/tool list. Admin-published connectors are visible to every member of the workspace automatically.

    **Use it in a chat:**

    Start a new conversation, click the **+** button next to the composer, choose **More**, then pick the eBird MCP app. From there: *"What rare birds have been seen near Anchorage this week?"* and ChatGPT will route to the appropriate eBird tool.

    !!! tip "Going further with the Apps SDK"
        If you want a richer in-ChatGPT experience — embedded cards, maps, tables, search widgets — the [OpenAI Apps SDK](https://platform.openai.com/docs) extends MCP with UI components. Start with a data-only connector (the path above), then add Apps-SDK UI later if it earns its place.

    Reference: [platform.openai.com/docs](https://platform.openai.com/docs) for current ChatGPT connector and Apps SDK documentation.

=== "Gemini"

    **Gemini and remote MCP servers**

    Google's consumer Gemini web app (gemini.google.com) doesn't yet expose user-facing MCP server configuration. There are several workable paths today depending on how you use Gemini.

    !!! info "No client-side API key needed"
        This hosted endpoint's eBird API key lives server-side. Ignore any third-party setup instructions that tell you to set an `EBIRD_API_KEY` environment variable on the client — that's only relevant if you self-host your own copy.

    **A. MCP-compatible client running Gemini as the model**

    Several MCP clients let you swap in Gemini as the underlying model while keeping a custom MCP server connection.

    - **Cline** (VS Code extension): in settings, change *API Provider* to **Google AI Studio**, paste a free [Gemini API key](https://aistudio.google.com), then add a new MCP server with:
        - **Connection type**: Streamable HTTP (use SSE if Streamable HTTP isn't offered)
        - **URL**: `https://ebird.codeforanchorage.org/mcp`
        - **Authentication**: None
    - **LobeChat**: add a Gemini API key under *Settings → Language Model → Google*, then add the URL above as a custom MCP server in the Plugins/MCP tab.

    **B. Gemini CLI and Gemini Code Assist**

    - **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** (Google's official command-line tool): add an entry to `mcpServers` in your CLI config pointing at the URL above.
    - **Gemini Code Assist** (VS Code / JetBrains plugin): the MCP servers panel in extension settings accepts a custom URL.

    **C. Python with the Google Gen AI SDK**

    If you're building an agent, the official `google-genai` SDK accepts MCP tools directly. Sketch:

    ```python
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from google import genai
    from google.genai import types

    URL = "https://ebird.codeforanchorage.org/mcp"

    async def main():
        async with streamablehttp_client(URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                gemini = genai.Client(api_key="YOUR_GEMINI_API_KEY")
                response = gemini.models.generate_content(
                    model="gemini-2.5-flash",
                    contents="What rare birds have been seen near Anchorage this week?",
                    config=types.GenerateContentConfig(tools=tools),
                )
                print(response.text)

    asyncio.run(main())
    ```

    SDK versions are evolving fast; if `tools=tools` doesn't accept MCP tools directly in your version, convert MCP tool schemas to Gemini function declarations explicitly.

    **D. Browser extensions (consumer Gemini web app)**

    Third-party extensions like **MCP SuperAssistant** bridge remote MCP servers into the consumer Gemini web UI. Install the extension, paste the URL above into its configuration. Review extension permissions before installing — these tools inject scripts into your browser session.

    Reference: [ai.google.dev](https://ai.google.dev) for Gemini API/SDK docs.

=== "Microsoft Copilot"

    **Microsoft Copilot and remote MCP servers**

    MCP is generally available in both Copilot Studio and Microsoft 365 Declarative Agents. The consumer "Microsoft Copilot" surface (the Bing/Edge chat) does **not** support arbitrary MCP connections — for that, you want one of the agent-builder paths below. Both require a Microsoft 365 license with Copilot access.

    **A. Copilot Studio (low-code)**

    1. Sign in to [Copilot Studio](https://copilotstudio.microsoft.com) and open (or create) your agent.
    2. Turn on **generative orchestration** in the agent settings — required for MCP.
    3. Use the **MCP onboarding wizard** under **Tools → Add a tool → Model Context Protocol** to connect to an existing server:
        - **Server URL**: `https://ebird.codeforanchorage.org/mcp`
        - **Authentication**: None
    4. The wizard auto-discovers the eBird tools and adds them to the agent. Publish to make them available.

    **B. Microsoft 365 Declarative Agents (developer)**

    For a code-based path that lives in a repo:

    1. Install the **Microsoft 365 Agents Toolkit** extension in VS Code.
    2. Scaffold a new declarative agent.
    3. Add an **MCP action**, enter `https://ebird.codeforanchorage.org/mcp`, select the eBird tools you want to expose, and set authentication to none.
    4. Deploy the agent to your M365 tenant.

    !!! note "Transport"
        Copilot Studio supports both Streamable HTTP and SSE transports. SSE has been deprecated in the MCP spec but remains in public preview in Copilot Studio, so either works against this server today.

    Reference: [Microsoft Learn — MCP in Copilot Studio](https://learn.microsoft.com) and the [Microsoft 365 Agents Toolkit docs](https://learn.microsoft.com).

If your client isn't listed: any MCP client that supports **Streamable HTTP** transport will work. Point it at the URL above.

---

## What you can do with it

The server exposes the same tool surface as the JS reference `ebird-mcp-server`, plus civic-AI provenance and caveats on every response:

| Tool | What it returns |
|---|---|
| `get_recent_observations` | Most-recent observation of each species in a region or hotspot |
| `get_recent_observations_for_species` | Recent observations of one species in a region |
| `get_notable_observations` | Rare/unusual bird reports flagged by eBird review filters |
| `get_nearby_observations` | Recent observations within N km of a lat/lng |
| `get_nearby_notable_observations` | Notable observations near a coordinate |
| `get_nearby_observations_for_species` | One species near a coordinate |
| `get_hotspots` | Birding hotspots in a region with species totals and last-observation dates |
| `get_nearby_hotspots` | Hotspots within N km of a coordinate |
| `get_taxonomy` | eBird species codes, scientific and common names, taxonomy |
| `get_taxonomy_forms` | Subspecies and form codes for a species |

!!! note "Tool names in your client"
    Clients show these tools with an `ebird__` prefix (e.g. `ebird__get_recent_observations`) — the server namespaces every tool by plugin automatically.

Every response includes the upstream eBird URL it came from, the parameters sent, and a UTC retrieved-at timestamp — so your AI can attribute the data and you can verify it.

The server also adds civic-AI safety caveats: it flags single-observer claims, absence-of-evidence patterns (eBird is opt-in, "no records" usually means "no birders looked"), region-deduped result misuse, notable-is-local rarity, and taxonomic ambiguity for hybrids and subspecies. These exist because LLMs answering questions about wildlife data are prone to over-confident pattern claims from sparse data.

---

## Going deeper

Most of these tools are atomic — one call, one slice of data. For richer answers, your AI can chain them. Two recipes worth knowing about:

**Active-hotspot snapshot (breadth across a region)**

`get_hotspots(regionCode)` to enumerate locations → sort by `latestObsDt` to skip stale ones → for each active hotspot, `get_recent_observations(locId)` → aggregate. The single region-wide call deduplicates to one record per species; fanning out per-hotspot gives you the per-location feeds it omits.

Try: *"Give me a deep snapshot of what's birding around Anchorage this week — fan out across the active hotspots."*

**Target-species hunt (depth on one species)**

`get_taxonomy(cat="species")` to confirm the speciesCode → `get_taxonomy_forms(speciesCode)` to find subspecies and form codes → `get_recent_observations_for_species` against each form separately. Observers may report under any form code, so a single-code query can miss real sightings.

Try: *"Find every recent Yellow-rumped Warbler sighting in Alaska, including all subspecies."*

---

## Self-host

For production, regular use, or anything mission-critical, run your own copy:

```bash
git clone https://github.com/codeforanchorage/ebird-mcp.git
cd ebird-mcp
cp config-example.yaml config.yaml
# edit config.yaml and paste in your free eBird API key
bash ./scripts/setup-backend.sh               # one-time, per AWS account
bash ./scripts/deploy.sh --environment staging
bash ./scripts/deploy.sh --environment prod
```

You'll need:

- A free [eBird API key](https://ebird.org/api/keygen) (instant signup).
- An AWS account with the [AWS CLI](https://aws.amazon.com/cli/) configured (`aws configure`).
- [Terraform](https://www.terraform.io/downloads) >= 1.0 and Python 3.11+ installed locally.

**Cost:** roughly **$6–8/month** at typical use (mostly the $5 WAF ACL), with a hard ceiling around **$25/month** — the default 50,000 requests/day API Gateway quota caps worst-case spend even under a viral spike or denial-of-wallet attack. Lambda itself stays inside the AWS perpetual free tier at conversational traffic. Full cost table in the [repo README](https://github.com/codeforanchorage/ebird-mcp#cost).

The deploy script handles everything else: it validates your config, bundles the full eBird taxonomy into the package (so common taxonomy lookups never touch your eBird quota), builds the Lambda zip, and runs `terraform apply`. Full architecture and operational notes are in the [repo README](https://github.com/codeforanchorage/ebird-mcp) and `CLAUDE.md`.

!!! tip "Test locally before deploying"
    You don't need AWS to try the code: `pip install -r requirements.txt`, then `python local_server.py` serves the identical MCP endpoint on `http://localhost:8000/mcp`. Point the MCP Inspector or any local client at it.

---

## Fork it for your own data source

eBird is just the first plugin. The framework underneath is a general-purpose hosted-MCP scaffold — plugin discovery, JSON-RPC dispatch, CORS/Origin hardening, WAF + rate limiting, CloudWatch alarms, and one-command Terraform deploy are all data-source-agnostic. If you have a REST API you want to expose to AI assistants (a GIS feature service, an open-data portal, a domain dataset), forking this repo gets you a production-ready hosted MCP server where you only write the tool layer:

1. Fork the repo and add `plugins/<your_name>/plugin.py` exporting a class that inherits from `MCPPlugin` — define your tools in `get_tools()`, dispatch them in `execute_tool()`, and put your API client alongside.
2. In `config.yaml`, set `enabled: true` on your plugin (and only yours — one fork = one MCP server, enforced at load time).
3. Deploy. The framework discovers the plugin, namespaces its tools, and serves it — no other code changes.

The eBird plugin (`plugins/ebird/`) is the worked example: study its input validation (regex-checked path parameters, clamped numeric ranges), response provenance, and LLM-facing caveats before writing your own.

This architecture is itself a fork lineage: it comes from the City of Boston's [OpenContext](https://github.com/CityOfBoston/OpenContext) via [anchorage-gis-mcp](https://github.com/codeforanchorage/anchorage-gis-mcp) — cities and civic-tech brigades adapting the same scaffold to their own data. Yours can be next.

---

## Project

Maintained by [Code for Anchorage](https://codeforanchorage.org). Source code and issue tracker on [GitHub](https://github.com/codeforanchorage/ebird-mcp). Built on the [Model Context Protocol](https://modelcontextprotocol.io) and [eBird API v2](https://documenter.getpostman.com/view/664302/S1ENwy59). eBird data © Cornell Lab of Ornithology, made available under eBird's [terms of use](https://www.birds.cornell.edu/home/ebird-data-access-terms-of-use/).

With thanks to the **City of Boston** — CIO **Santi Garces** and OpenContext author **Srihari Raman** — whose [OpenContext](https://github.com/CityOfBoston/OpenContext) project pioneered the plugin architecture this server is built on, and to **Ciara Adkins**, whose [moonbirdai/ebird-mcp-server](https://github.com/moonbirdai/ebird-mcp-server) defined the original eBird tool surface.
