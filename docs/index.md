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

    **Claude.ai (web) or Claude Desktop**

    1. Open **Settings → Connectors** (web) or **Settings → Developer → MCP servers** (Desktop).
    2. Click **Add custom connector**.
    3. Fill in:
        - **Name**: `eBird MCP`
        - **URL**: `https://ebird.codeforanchorage.org/mcp`
    4. Save. The connector becomes available in any new conversation — you'll see eBird tools appear in the tool list.

    !!! tip
        After adding, try: *"What rare birds have been reported near Anchorage, Alaska in the past week?"* Claude will route the request through `ebird__get_nearby_notable_observations` and return real observations with checklist links and review-status flags.

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

    **Gemini CLI / Gemini Code Assist**

    Google's consumer Gemini app doesn't yet expose user-facing MCP server configuration. Developer surfaces do:

    - **Gemini CLI**: add an `mcp_servers` entry pointing at the URL in your Gemini CLI config (`~/.gemini/config.yaml` or equivalent).
    - **Gemini Code Assist** (VS Code / JetBrains): the MCP servers panel in the extension settings accepts a custom URL.
    - **Vertex AI / Google AI Studio**: connect via the MCP tool integration when building an agent.

    Reference: [ai.google.dev](https://ai.google.dev) for current API documentation.

=== "Microsoft Copilot"

    **Copilot Studio (builder)**

    Microsoft 365 Copilot (end-user) doesn't yet ship user-facing MCP server configuration. Builder/developer surfaces do:

    1. Open [Copilot Studio](https://copilotstudio.microsoft.com).
    2. In your agent/topic, go to **Tools → Add a tool → Model Context Protocol**.
    3. Configure the connection with the URL `https://ebird.codeforanchorage.org/mcp` and no authentication.
    4. Publish the agent to make the eBird tools available.

    Reference: [Microsoft Learn — Connect to MCP servers](https://learn.microsoft.com).

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

Every response includes the upstream eBird URL it came from, the parameters sent, and a UTC retrieved-at timestamp — so your AI can attribute the data and you can verify it.

The server also adds civic-AI safety caveats: it flags single-observer claims, absence-of-evidence patterns (eBird is opt-in, "no records" usually means "no birders looked"), region-deduped result misuse, notable-is-local rarity, and taxonomic ambiguity for hybrids and subspecies. These exist because LLMs answering questions about wildlife data are prone to over-confident pattern claims from sparse data.

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
- An AWS account. The stack is API Gateway + Lambda + WAFv2 + CloudWatch — all on the AWS free tier or close to it. Estimated cost under moderate load: a few dollars per month.
- [Terraform](https://www.terraform.io/downloads) and Python 3.11 installed locally.

The deploy script handles packaging, the Lambda zip, and a `terraform apply`. Full architecture and operational notes are in the [repo README](https://github.com/codeforanchorage/ebird-mcp) and `CLAUDE.md`.

---

## Project

Maintained by [Code for Anchorage](https://codeforanchorage.org). Source code and issue tracker on [GitHub](https://github.com/codeforanchorage/ebird-mcp). Built on the [Model Context Protocol](https://modelcontextprotocol.io) and [eBird API v2](https://documenter.getpostman.com/view/664302/S1ENwy59). eBird data © Cornell Lab of Ornithology, made available under eBird's [terms of use](https://www.birds.cornell.edu/home/ebird-data-access-terms-of-use/).
