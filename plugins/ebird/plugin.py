"""eBird plugin implementation.

Provides MCP tools backed by the eBird v2 REST API. Mirrors the tool surface
of the upstream stdio reference server (`ebird-mcp-server`).

Civic-AI principles (civicaitools.org/learn): every tool response is wrapped by
``_finalize_response`` so the upstream URL + echoed query lead and a UTC
retrieved-at timestamp closes. Caveats fire between the header and the body.

eBird's load-bearing hallucination vectors are NOT truncation or coded values;
they are *effort bias* and *absence-of-evidence misreading*. Birders are
unevenly distributed in space, time, and skill — "no records of X" almost
always means "no one looked," not "X is absent." The headline caveats are
LOW SURVEY EFFORT, ABSENCE-OF-EVIDENCE framing (in the body, not as a
caveat — descriptions get ignored), SINGLE-OBSERVER PROVENANCE, and
NOTABLE-IS-LOCAL. Every observation also carries observer/checklist
provenance and a review-status label so unreviewed reports cannot pass for
authoritative records.
"""

import datetime as dt
import json
import logging
import math
import re
from typing import Any, Dict, List, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.interfaces import (
    MCPPlugin,
    PluginType,
    ToolDefinition,
    ToolInputError,
    ToolResult,
)
from plugins.ebird.config_schema import EBirdPluginConfig
from plugins.ebird.ebird_client import EBirdClient, QuotaExhausted
from plugins.ebird.schemas import (
    CAVEAT_ABSENCE_OF_EVIDENCE,
    CAVEAT_COUNT_NOT_REPORTED,
    CAVEAT_LOW_SURVEY_EFFORT,
    CAVEAT_NOTABLE_IS_LOCAL,
    CAVEAT_ONE_RECORD_PER_SPECIES,
    CAVEAT_POSSIBLY_TRUNCATED,
    CAVEAT_RESPONSE_SIZE_CEILING,
    CAVEAT_SINGLE_OBSERVER,
    CAVEAT_SINGLE_RECORD,
    CAVEAT_SMALL_SAMPLE,
    CAVEAT_TAXONOMIC_AMBIGUITY,
    CAVEAT_UNCOMPARABLE_SPECIES_TOTALS,
    CAVEAT_WINDOW_STALENESS,
    HOTSPOTS_SCHEMA,
    OBSERVATIONS_SCHEMA,
)

logger = logging.getLogger(__name__)


# Strict regexes for arguments that flow into URL paths. Defense against
# path traversal / unintended endpoint hits at the eBird API.
_REGION_RE = re.compile(r"^([A-Z]{2}(-[A-Z0-9]+)*|L\d+)$")
_LOCID_RE = re.compile(r"^L\d+$")
_SPECIES_RE = re.compile(r"^[a-z0-9]+$")
_LOCALE_RE = re.compile(r"^[a-z]{2}(_[A-Z]{2})?$")
_TAXONOMY_CAT = frozenset({"species", "issf", "hybrid", "slash", "spuh", "domestic", "form"})
_TAXONOMY_FMT = frozenset({"json", "csv"})


# Tool grouping — drives caveat dispatch and per-tool body decisions.

_OBSERVATION_TOOLS = frozenset({
    "get_recent_observations",
    "get_recent_observations_for_species",
    "get_notable_observations",
    "get_nearby_observations",
    "get_nearby_notable_observations",
    "get_nearby_observations_for_species",
})

_NEARBY_OBSERVATION_TOOLS = frozenset({
    "get_nearby_observations",
    "get_nearby_notable_observations",
    "get_nearby_observations_for_species",
})

_NOTABLE_TOOLS = frozenset({
    "get_notable_observations",
    "get_nearby_notable_observations",
})

# Region-scoped endpoints that collapse to the single most-recent observation
# of each species. Their per-record locId is "where that species was last seen
# in the whole region," not a per-location feed — filtering their results by
# location silently undercounts. (The /geo/ nearby variants share this shape
# but are already spatially scoped, so the misuse doesn't arise.)
_REGION_DEDUPED_TOOLS = frozenset({
    "get_recent_observations",
    "get_notable_observations",
})

_SPECIES_SPECIFIC_TOOLS = frozenset({
    "get_recent_observations_for_species",
    "get_nearby_observations_for_species",
})

_HOTSPOT_TOOLS = frozenset({
    "get_hotspots",
    "get_nearby_hotspots",
})


# Sampling-caveat thresholds. Tuned conservatively — a false alarm on
# heavily-birded areas (Central Park, Cape May) trains users to ignore the
# warning entirely. Adjust here, not at call sites.
_SMALL_SAMPLE_THRESHOLD = 10           # below this many records → SMALL SAMPLE
_LOW_EFFORT_CHECKLIST_THRESHOLD = 5    # below this many unique checklists → LOW SURVEY EFFORT
_STALE_WINDOW_MIN_DAYS = 3             # below this, freshness noise dominates

# Response-size controls. eBird itself accepts maxResults up to 10000, but at
# ~100-350 bytes per rendered record that is a multi-megabyte response no MCP
# client has a good use for. The schema max and clamp ceiling live here; the
# byte ceiling is a backstop independent of record count (long location names,
# observer fields) so a single response can never balloon past it.
_MAX_RESULTS_CEILING = 1000            # schema maximum + clamp ceiling for maxResults
_COMPACT_FORMAT_THRESHOLD = 20         # above this many records → compact table
_MAX_BODY_BYTES = 200 * 1024           # formatted-body byte ceiling (truncates at a record boundary)

# Marker the size backstop stamps into the TEXT body. Shared so the caveat
# builder can detect the clip without re-deriving byte sizes, and so the
# wording can change without breaking that detection. Only the text is ever
# clipped; structuredContent always carries the complete row set.
_SIZE_CEILING_MARKER = "⚠️ RESPONSE SIZE CEILING:"


# Human-readable display names. tools/list otherwise carries only the
# prefixed wire name (ebird__get_nearby_notable_observations), which is a
# stable identifier but reads badly anywhere a client shows tools to a
# person. Clients resolve a display name as title -> annotations.title ->
# name; `name` itself is unchanged, so nothing that dispatches on it moves.
#
# Kept as a map rather than a `title=` on each ToolDefinition so the whole
# display surface is legible in one place — and so the tests below can check
# it in both directions (a tool with no entry, and an entry for a tool that
# no longer exists, both fail).
TOOL_TITLES = {
    "get_recent_observations": "Recent Observations",
    "get_recent_observations_for_species": "Observations by Species",
    "get_notable_observations": "Notable Observations",
    "get_nearby_observations": "Nearby Observations",
    "get_nearby_notable_observations": "Nearby Notable Observations",
    "get_nearby_observations_for_species": "Nearby Observations by Species",
    "get_hotspots": "Hotspots",
    "get_nearby_hotspots": "Nearby Hotspots",
    "get_taxonomy": "Taxonomy Lookup",
    "get_taxonomy_forms": "Taxonomy Forms",
}


# Tools that declare an outputSchema, and therefore MUST populate
# structuredContent on every path they can return through — including
# zero-result ones. Membership here is what `_build_structured` dispatches
# on, so a tool cannot advertise a schema it has no builder for.
TOOL_OUTPUT_SCHEMAS = {
    "get_recent_observations": OBSERVATIONS_SCHEMA,
    "get_recent_observations_for_species": OBSERVATIONS_SCHEMA,
    "get_notable_observations": OBSERVATIONS_SCHEMA,
    "get_nearby_observations": OBSERVATIONS_SCHEMA,
    "get_nearby_notable_observations": OBSERVATIONS_SCHEMA,
    "get_nearby_observations_for_species": OBSERVATIONS_SCHEMA,
    "get_hotspots": HOTSPOTS_SCHEMA,
    "get_nearby_hotspots": HOTSPOTS_SCHEMA,
}


_RETRY = retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((httpx.TransportError, httpx.ReadTimeout)),
)


class EBirdPlugin(MCPPlugin):
    plugin_name = "ebird"
    plugin_type = PluginType.CUSTOM_API
    plugin_version = "1.0.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config = EBirdPluginConfig(**config)
        self.client: Optional[EBirdClient] = None

    async def initialize(self) -> bool:
        try:
            self.client = EBirdClient(
                api_key=self.plugin_config.api_key,
                base_url=self.plugin_config.base_url,
                timeout=self.plugin_config.timeout,
            )
            self._initialized = True
            logger.info("eBird plugin initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize eBird plugin: {e}", exc_info=True)
            return False

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        self._initialized = False
        logger.info("eBird plugin shut down")

    async def health_check(self) -> bool:
        return self.client is not None and self._initialized

    def get_instructions(self) -> str:
        return (
            "This server exposes the Cornell Lab's eBird v2 API: crowd-sourced "
            "bird observations, hotspots, and taxonomy.\n\n"
            "Workflow — chain tools, never guess identifiers:\n"
            "1. Resolve the canonical 6-letter speciesCode with get_taxonomy "
            "(or get_taxonomy_forms for subspecies/forms).\n"
            "2. Resolve the place: use standard eBird region codes (US, US-AK, "
            "US-AK-020) or find hotspot L-codes with get_hotspots / "
            "get_nearby_hotspots; L-codes work anywhere a regionCode is accepted.\n"
            "3. Query observations with the region/species/nearby tools.\n\n"
            "Data caveats that apply to every tool:\n"
            "- eBird is opt-in observation logging, not a systematic survey. Zero "
            "records means 'not reported in this window', never 'absent'.\n"
            "- 'Notable' means rare for that specific location; do not generalize "
            "to regional or national rarity.\n"
            "- If get_taxonomy_forms reports multiple forms for a species, "
            "observers may have logged sightings under any form code — query each "
            "before aggregating counts."
        )

    # ---- Tool catalog -----------------------------------------------------

    def get_tools(self) -> List[ToolDefinition]:
        region_code = {
            "type": "string",
            "description": (
                "eBird region or location code: country (US), subnational1 (US-NY), "
                "subnational2 (US-NY-109), or hotspot location ID (e.g. L99381). "
                "Use get_hotspots / get_nearby_hotspots first to discover valid "
                "L-codes — do not invent location IDs."
            ),
        }
        species_code = {
            "type": "string",
            "description": (
                "eBird 6-letter species code — lowercase and CASE-SENSITIVE (e.g. "
                "'amerob' for American Robin, 'yerwar1' for Yellow-rumped Warbler). "
                "These are eBird's internal codes, NOT the 4-letter banding alpha "
                "codes (AMRO, YRWA). Use get_taxonomy or get_taxonomy_forms to "
                "confirm the exact code — do not guess; many codes are not "
                "derivable from common names ('norcar' = Northern Cardinal)."
            ),
        }
        lat = {
            "type": "number",
            "description": "Latitude in decimal degrees (-90 to 90).",
            "minimum": -90,
            "maximum": 90,
        }
        lng = {
            "type": "number",
            "description": "Longitude in decimal degrees (-180 to 180).",
            "minimum": -180,
            "maximum": 180,
        }
        back = {
            "type": "integer",
            "description": "Number of days back to search (1-30). Default: 14.",
            "minimum": 1,
            "maximum": 30,
        }
        dist = {
            "type": "integer",
            "description": "Radius in kilometers from lat/lng (0-50). Default: 25.",
            "minimum": 0,
            "maximum": 50,
        }
        max_results = {
            "type": "integer",
            "description": (
                f"Maximum number of results to return (1-{_MAX_RESULTS_CEILING}). "
                f"Default: 100. For larger sets, narrow with back/dist and "
                f"aggregate across calls."
            ),
            "minimum": 1,
            "maximum": _MAX_RESULTS_CEILING,
        }
        include_provisional = {
            "type": "boolean",
            "description": "Include observations not yet reviewed. Default: true.",
        }
        hotspot = {
            "type": "boolean",
            "description": "Only include observations from designated hotspots. Default: false.",
        }
        detail = {
            "type": "string",
            "enum": ["simple", "full"],
            "description": "Detail level. 'full' is unreliable in some regions; defaults to 'simple'.",
        }

        workflow = (
            "Workflow: (1) get_taxonomy or get_taxonomy_forms to find a speciesCode, "
            "(2) get_hotspots / get_nearby_hotspots to find a regionCode or L-code, "
            "(3) call this tool. Never guess speciesCode or location IDs."
        )

        absence_caveat = (
            "IMPORTANT: a zero-record response does NOT mean the species is absent. "
            "It means no birders reported it in this window. eBird is opt-in "
            "observation logs, not a systematic survey — \"absence in eBird\" and "
            "\"absence in reality\" are different things."
        )

        notable_caveat = (
            "NOTE: 'notable' in eBird means rare *for that specific location*. A "
            "notable bird in one county may be common in the next — do not "
            "generalize to regional or national rarity from a notable list."
        )

        tools = [
            ToolDefinition(
                name="get_recent_observations",
                description=(
                    "Get recent bird observations in an eBird region (country, state/province, "
                    "county, or hotspot). Returns the single most recent observation of EACH "
                    "species in the region — one record per species, not a full feed. To find "
                    "what was seen at a specific place, pass that place's hotspot L-code as "
                    "regionCode; do NOT pull a whole county and filter the results by location "
                    "(that undercounts — see the response caveats).\n\n"
                    "For deep regional coverage, chain get_hotspots first to enumerate active "
                    "L-codes, then call this tool with each as regionCode and aggregate.\n\n"
                    + workflow
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "regionCode": region_code,
                        "back": back,
                        "maxResults": max_results,
                        "includeProvisional": include_provisional,
                        "hotspot": hotspot,
                        "detail": detail,
                    },
                    "required": ["regionCode"],
                },
            ),
            ToolDefinition(
                name="get_recent_observations_for_species",
                description=(
                    "Get recent observations of a specific bird species in an eBird region. "
                    "Useful for tracking where a target species has been seen.\n\n"
                    + absence_caveat + "\n\n" + workflow
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "regionCode": region_code,
                        "speciesCode": species_code,
                        "back": back,
                        "maxResults": max_results,
                        "includeProvisional": include_provisional,
                        "hotspot": hotspot,
                    },
                    "required": ["regionCode", "speciesCode"],
                },
            ),
            ToolDefinition(
                name="get_notable_observations",
                description=(
                    "Get notable (rare or unusual) bird observations in an eBird region. "
                    "eBird flags these based on regional checklist filters.\n\n"
                    + notable_caveat + "\n\n" + workflow
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "regionCode": region_code,
                        "back": back,
                        "maxResults": max_results,
                        "detail": detail,
                    },
                    "required": ["regionCode"],
                },
            ),
            ToolDefinition(
                name="get_nearby_observations",
                description=(
                    "Get recent bird observations near a lat/lng coordinate within a "
                    "radius (max 50 km).\n\n" + workflow
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "lat": lat,
                        "lng": lng,
                        "dist": dist,
                        "back": back,
                        "maxResults": max_results,
                        "includeProvisional": include_provisional,
                        "hotspot": hotspot,
                    },
                    "required": ["lat", "lng"],
                },
            ),
            ToolDefinition(
                name="get_nearby_notable_observations",
                description=(
                    "Get notable (rare or unusual) bird observations near a lat/lng "
                    "coordinate.\n\n" + notable_caveat + "\n\n" + workflow
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "lat": lat,
                        "lng": lng,
                        "dist": dist,
                        "back": back,
                        "maxResults": max_results,
                    },
                    "required": ["lat", "lng"],
                },
            ),
            ToolDefinition(
                name="get_nearby_observations_for_species",
                description=(
                    "Get recent observations of a specific species near a lat/lng "
                    "coordinate.\n\n" + absence_caveat + "\n\n" + workflow
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "lat": lat,
                        "lng": lng,
                        "speciesCode": species_code,
                        "dist": dist,
                        "back": back,
                        "maxResults": max_results,
                        "includeProvisional": include_provisional,
                    },
                    "required": ["lat", "lng", "speciesCode"],
                },
            ),
            ToolDefinition(
                name="get_hotspots",
                description=(
                    "Get birding hotspots in an eBird region. Returns location IDs, names, "
                    "coordinates, all-time species totals, and last-observed dates. Use the "
                    "returned locId values as regionCode for the observation tools.\n\n"
                    "NOTE: eBird does not return per-hotspot checklist counts on this endpoint, "
                    "so species totals are not directly comparable across hotspots — a hotspot "
                    "with 200 species and 5000 checklists is different from one with 200 and 50. "
                    "Use the last-observation date to gauge activity.\n\n"
                    "For a deep regional snapshot, fan out to get_recent_observations(locId) per "
                    "active hotspot (sort by latestObsDt to skip stale ones) and aggregate."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "regionCode": region_code,
                        "back": back,
                    },
                    "required": ["regionCode"],
                },
            ),
            ToolDefinition(
                name="get_nearby_hotspots",
                description=(
                    "Get birding hotspots near a lat/lng coordinate within a radius. "
                    "Use the returned locId values as regionCode for the observation tools."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "lat": lat,
                        "lng": lng,
                        "dist": dist,
                        "back": back,
                    },
                    "required": ["lat", "lng"],
                },
            ),
            ToolDefinition(
                name="get_taxonomy",
                description=(
                    "Get the eBird taxonomy. Returns species codes, common names, scientific "
                    "names, and taxonomic ordering. Large response — narrow with `cat` if "
                    "possible. Use this to discover canonical 6-letter speciesCode values "
                    "before calling species-specific observation tools."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "locale": {
                            "type": "string",
                            "description": "Locale code for common names (e.g. 'en', 'es'). Default: 'en'.",
                        },
                        "cat": {
                            "type": "string",
                            "description": (
                                "Taxonomic category filter: 'species', 'issf', 'hybrid', 'slash', "
                                "'spuh', 'domestic', 'form'. Default: 'species'."
                            ),
                        },
                        "fmt": {
                            "type": "string",
                            "enum": ["json", "csv"],
                            "description": "Response format. Default: 'json'.",
                        },
                    },
                },
            ),
            ToolDefinition(
                name="get_taxonomy_forms",
                description=(
                    "Get taxonomic subforms (subspecies, hybrids, etc.) for a given species code. "
                    "If multiple forms are returned, eBird observers may report sightings under "
                    "any of them — the tool surfaces a TAXONOMIC AMBIGUITY warning so the model "
                    "knows to query each form separately before aggregating."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"speciesCode": species_code},
                    "required": ["speciesCode"],
                },
            ),
        ]

        # Attach display titles and output schemas. Done here rather than
        # inline on each ToolDefinition so the maps above stay the single
        # legible inventory of the display and machine-readable surfaces.
        for tool in tools:
            tool.title = TOOL_TITLES.get(tool.name)
            tool.output_schema = TOOL_OUTPUT_SCHEMAS.get(tool.name)
        return tools

    # ---- Tool execution ---------------------------------------------------

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        if self.client is None:
            return ToolResult(
                content=[], success=False, error_message="eBird plugin not initialized"
            )

        try:
            arguments = _clamp_and_validate(arguments)
        except ToolInputError as e:
            # The caller sent something invalid. WARNING with no traceback:
            # a stack trace here would read as a server fault and bury the
            # real ones. A plain ValueError escaping this call is NOT a
            # caller error and deliberately propagates to the generic
            # handler with its trace intact.
            logger.warning("Rejected %s arguments: %s", tool_name, e)
            return ToolResult(content=[], success=False, error_message=str(e))

        detail = arguments.get("detail", "simple")
        if detail not in ("simple", "full"):
            detail = "simple"

        try:
            data, url, params_sent = await self._dispatch(tool_name, arguments, detail)
        except _UnknownTool:
            return ToolResult(
                content=[],
                success=False,
                error_message=f"Unknown tool: {tool_name}",
            )
        except QuotaExhausted as e:
            # Soft-gate trip — friendly message, no stack trace in logs.
            logger.warning("Daily upstream quota soft-gate triggered: %s", e)
            return ToolResult(content=[], success=False, error_message=str(e))
        except httpx.HTTPStatusError as e:
            body_excerpt = e.response.text[:500] if e.response is not None else ""
            msg = f"eBird API HTTP {e.response.status_code}: {body_excerpt}"
            logger.warning(msg)
            return ToolResult(content=[], success=False, error_message=msg)
        except ToolInputError as e:
            # A required argument was missing. Same reasoning as above: the
            # caller can fix this, so it is a WARNING, not a fault. (This
            # replaces an `except KeyError` branch — a KeyError escaping
            # _dispatch now means a genuine bug and keeps its traceback.)
            logger.warning("Rejected %s arguments: %s", tool_name, e)
            return ToolResult(content=[], success=False, error_message=str(e))
        except Exception as e:
            logger.exception(f"Error in {tool_name}")
            return ToolResult(content=[], success=False, error_message=str(e))

        # One timestamp for both channels, so the text footer and
        # structuredContent cannot report different instants.
        retrieved_at = _utcnow_iso()
        body = _format_body(tool_name, data, arguments, self.plugin_config)
        caveats = _build_caveats(
            tool_name, data, arguments, self.plugin_config, body=body
        )
        text = _finalize_response(
            url=url,
            params=params_sent,
            body=body,
            caveats=caveats,
            retrieved_at=retrieved_at,
        )
        structured = _build_structured(
            tool_name,
            data,
            arguments,
            self.plugin_config,
            url=url,
            params=params_sent,
            caveats=caveats,
            retrieved_at=retrieved_at,
        )
        return _ok(text, structured)

    async def _dispatch(
        self, tool_name: str, arguments: Dict[str, Any], detail: str
    ):
        """Route a tool call to the eBird client and return (data, url, params).

        The client itself returns the request URL and the params it actually
        sent, so provenance is recorded at the call site rather than
        reconstructed by the plugin.
        """
        client = self.client
        assert client is not None  # guarded in execute_tool

        if tool_name == "get_recent_observations":
            return await _RETRY(client.get_recent_observations)(
                region_code=_require(arguments, "regionCode"),
                back=arguments.get("back", self.plugin_config.default_back),
                max_results=arguments.get(
                    "maxResults", self.plugin_config.default_max_results
                ),
                include_provisional=arguments.get("includeProvisional"),
                hotspot=arguments.get("hotspot"),
                detail=detail,
            )

        if tool_name == "get_recent_observations_for_species":
            return await _RETRY(client.get_recent_observations_for_species)(
                region_code=_require(arguments, "regionCode"),
                species_code=_require(arguments, "speciesCode"),
                back=arguments.get("back", self.plugin_config.default_back),
                max_results=arguments.get(
                    "maxResults", self.plugin_config.default_max_results
                ),
                include_provisional=arguments.get("includeProvisional"),
                hotspot=arguments.get("hotspot"),
            )

        if tool_name == "get_notable_observations":
            return await _RETRY(client.get_notable_observations)(
                region_code=_require(arguments, "regionCode"),
                back=arguments.get("back", self.plugin_config.default_back),
                max_results=arguments.get(
                    "maxResults", self.plugin_config.default_max_results
                ),
                detail=detail,
            )

        if tool_name == "get_nearby_observations":
            return await _RETRY(client.get_nearby_observations)(
                lat=_require(arguments, "lat"),
                lng=_require(arguments, "lng"),
                dist=arguments.get("dist"),
                back=arguments.get("back", self.plugin_config.default_back),
                max_results=arguments.get(
                    "maxResults", self.plugin_config.default_max_results
                ),
                include_provisional=arguments.get("includeProvisional"),
                hotspot=arguments.get("hotspot"),
            )

        if tool_name == "get_nearby_notable_observations":
            return await _RETRY(client.get_nearby_notable_observations)(
                lat=_require(arguments, "lat"),
                lng=_require(arguments, "lng"),
                dist=arguments.get("dist"),
                back=arguments.get("back", self.plugin_config.default_back),
                max_results=arguments.get(
                    "maxResults", self.plugin_config.default_max_results
                ),
            )

        if tool_name == "get_nearby_observations_for_species":
            return await _RETRY(client.get_nearby_observations_for_species)(
                lat=_require(arguments, "lat"),
                lng=_require(arguments, "lng"),
                species_code=_require(arguments, "speciesCode"),
                dist=arguments.get("dist"),
                back=arguments.get("back", self.plugin_config.default_back),
                max_results=arguments.get(
                    "maxResults", self.plugin_config.default_max_results
                ),
                include_provisional=arguments.get("includeProvisional"),
            )

        if tool_name == "get_hotspots":
            return await _RETRY(client.get_hotspots)(
                region_code=_require(arguments, "regionCode"),
                back=arguments.get("back"),
            )

        if tool_name == "get_nearby_hotspots":
            return await _RETRY(client.get_nearby_hotspots)(
                lat=_require(arguments, "lat"),
                lng=_require(arguments, "lng"),
                dist=arguments.get("dist"),
                back=arguments.get("back"),
            )

        if tool_name == "get_taxonomy":
            return await _RETRY(client.get_taxonomy)(
                locale=arguments.get("locale", "en"),
                cat=arguments.get("cat", "species"),
                fmt=arguments.get("fmt", "json"),
            )

        if tool_name == "get_taxonomy_forms":
            return await _RETRY(client.get_taxonomy_forms)(
                species_code=_require(arguments, "speciesCode"),
            )

        raise _UnknownTool(tool_name)


class _UnknownTool(Exception):
    """Raised by _dispatch when an unrecognized tool_name reaches it."""


# ---- Validation helpers ---------------------------------------------------


def _require(args: Dict[str, Any], key: str) -> Any:
    """Fetch a required argument or reject the call.

    Raises ToolInputError rather than KeyError so a missing argument is
    classified as the caller mistake it is. A genuine KeyError escaping
    _dispatch now means a real bug and keeps its ERROR + traceback.
    """
    if key not in args or args[key] in (None, ""):
        raise ToolInputError(f"Missing required argument: {key}")
    return args[key]


def _clamp_and_validate(args: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp numeric args and validate string args against expected formats.

    Numeric args (back, maxResults, dist) get silently clamped into the range
    eBird accepts — better than a 400 round-trip. String args that flow into
    URL paths or take a known set of values are rejected outright with a
    clear error.
    """
    out = dict(args)

    if "regionCode" in out and out["regionCode"] is not None:
        v = out["regionCode"]
        if not isinstance(v, str) or not _REGION_RE.match(v):
            raise ToolInputError(
                f"Invalid regionCode: {v!r}. Expected e.g. 'US', 'US-NY', "
                f"'US-NY-109', or a hotspot location ID like 'L12345'."
            )

    if "speciesCode" in out and out["speciesCode"] is not None:
        v = out["speciesCode"]
        if not isinstance(v, str) or not _SPECIES_RE.match(v):
            raise ToolInputError(
                f"Invalid speciesCode: {v!r}. Expected lowercase alphanumeric, "
                f"e.g. 'amecro' for American Crow. Note: these are 6-letter "
                f"eBird internal codes, NOT 4-letter banding alpha codes."
            )

    if "cat" in out and out["cat"] is not None:
        v = out["cat"]
        if v not in _TAXONOMY_CAT:
            raise ToolInputError(
                f"Invalid cat: {v!r}. Allowed: {sorted(_TAXONOMY_CAT)}."
            )

    if "fmt" in out and out["fmt"] is not None:
        v = out["fmt"]
        if v not in _TAXONOMY_FMT:
            raise ToolInputError(f"Invalid fmt: {v!r}. Allowed: {sorted(_TAXONOMY_FMT)}.")

    if "locale" in out and out["locale"] is not None:
        v = out["locale"]
        if not isinstance(v, str) or not _LOCALE_RE.match(v):
            raise ToolInputError(
                f"Invalid locale: {v!r}. Expected ISO codes like 'en', 'es', 'pt_BR'."
            )

    if out.get("back") is not None:
        out["back"] = _clamp_int(out["back"], 1, 30, "back")
    if out.get("maxResults") is not None:
        out["maxResults"] = _clamp_int(
            out["maxResults"], 1, _MAX_RESULTS_CEILING, "maxResults"
        )
    if out.get("dist") is not None:
        out["dist"] = _clamp_int(out["dist"], 0, 50, "dist")

    if out.get("lat") is not None:
        lat = _coerce_float(out["lat"], "lat")
        if not (-90 <= lat <= 90):
            raise ToolInputError(f"Invalid lat: {lat}. Must be between -90 and 90.")
        out["lat"] = lat
    if out.get("lng") is not None:
        lng = _coerce_float(out["lng"], "lng")
        if not (-180 <= lng <= 180):
            raise ToolInputError(f"Invalid lng: {lng}. Must be between -180 and 180.")
        out["lng"] = lng

    return out


def _clamp_int(value: Any, lo: int, hi: int, name: str) -> int:
    """Coerce a caller-supplied integer argument, then clamp it.

    The ONLY sanctioned int() over a caller argument. A bare int() at a
    call site would surface Python's own "invalid literal for int() with
    base 10: 'abc'" — useless to the caller and, worse, indistinguishable
    from a server fault in the logs. Raising ToolInputError names the
    argument and the offending value instead.
    ``tests/test_caller_error_logging.py`` sweeps the AST to keep it that
    way.
    """
    try:
        i = int(value)
    except (TypeError, ValueError):
        raise ToolInputError(f"Invalid {name}: {value!r}. Expected integer.")
    return max(lo, min(hi, i))


def _coerce_float(value: Any, name: str) -> float:
    """Coerce a caller-supplied float argument. See _clamp_int."""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ToolInputError(f"Invalid {name}: {value!r}. Expected number.")


def _ok(text: str, structured: Optional[Dict[str, Any]] = None) -> ToolResult:
    return ToolResult(
        content=[{"type": "text", "text": text}],
        structured_content=structured,
        success=True,
    )


# ---- Provenance + time helpers --------------------------------------------


def _utcnow_iso() -> str:
    """Current UTC time as ``2026-05-13T19:24:00Z``. Centralized so tests can
    monkeypatch a single function."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_naive_utc() -> dt.datetime:
    """Current UTC time as a naive datetime, matching the naive datetimes that
    `_parse_ebird_datetime` returns. Patched in tests for stable comparisons."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _pretty_param(v: Any) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def _finalize_response(
    *,
    url: str,
    params: Dict[str, Any],
    body: str,
    caveats: List[Dict[str, Any]],
    retrieved_at: str,
) -> str:
    """Wrap a formatted body with provenance + caveats + retrieved-at footer.

    Renders the ``message`` of each caveat except those marked ``_in_body``,
    whose prose the body already carries — rendering those here would print
    them twice. ``retrieved_at`` is passed in rather than read from the
    clock so the text footer and structuredContent report the same instant.
    """
    parts: List[str] = [f"Source: {url}"]
    if params:
        params_str = ", ".join(f"{k}={_pretty_param(v)}" for k, v in params.items())
        parts.append(f"Query: {params_str}")
    parts.append("")  # blank line after provenance header

    for caveat in caveats:
        if caveat.get("_in_body"):
            continue
        parts.append(caveat["message"])
        parts.append("")

    parts.append(body)
    parts.append("")
    parts.append(f"_Retrieved: {retrieved_at}_")
    return "\n".join(parts)


def _parse_ebird_datetime(value: Any) -> Optional[dt.datetime]:
    """Parse the various date / datetime string shapes eBird returns. Returns
    None on anything unrecognizable so callers can skip the field cleanly."""
    if not value or not isinstance(value, str):
        return None
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _days_ago(value: Any) -> Optional[int]:
    parsed = _parse_ebird_datetime(value)
    if parsed is None:
        return None
    return (_now_naive_utc().date() - parsed.date()).days


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance, kilometers. Mean Earth radius 6371.0088 km."""
    R = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---- Caveats --------------------------------------------------------------


def _text_was_clipped(body: str) -> bool:
    """Did the size backstop drop records from the TEXT rendering?

    Only the text is ever clipped — structuredContent always carries the
    complete row set — so this drives a caveat pointing the reader at the
    machine-readable half rather than letting them read a partial list as
    the whole answer.
    """
    return _SIZE_CEILING_MARKER in body


def _caveat(code: str, message: str, *, in_body: bool = False) -> Dict[str, Any]:
    """Build one caveat.

    ONE list feeds both channels: ``_finalize_response`` renders ``message``
    into the text and the structured builder emits {code, message}. They
    cannot drift because there is no second source.

    ``in_body=True`` marks a caveat whose prose is already woven into the
    body — the absence-of-evidence framing deliberately lives there rather
    than in the caveat block, because a model skims a leading warning list
    but reads the answer. It still travels in structuredContent, so a
    caller can branch on the code without parsing prose.
    """
    return {"code": code, "message": message, "_in_body": in_body}


_NOTABLE_IS_LOCAL_CAVEAT = (
    "⚠️ NOTABLE-IS-LOCAL: in eBird, 'notable' means rare *for this specific "
    "location*. A notable bird in one county may be common in the next — do "
    "not generalize to regional or national rarity from a notable list."
)

_REGION_DEDUPED_CAVEAT = (
    "⚠️ ONE-RECORD-PER-SPECIES: this region pull returns only the single most "
    "recent observation of each species. The location on each record is just "
    "wherever that species was last seen *anywhere in the region* — it is NOT "
    "a complete per-location feed. Do NOT filter this list by location to "
    "answer \"what was seen at <place>\": that silently drops every species "
    "that was present there but last reported elsewhere in the region. "
    "Instead, resolve the place to an eBird hotspot L-code (get_hotspots / "
    "get_nearby_hotspots) and re-call this tool with that L-code as "
    "regionCode, or use get_nearby_observations with the place's lat/lng. "
    "Keep the region-wide call only for the region-wide picture."
)


def _build_caveats(
    tool_name: str,
    data: Any,
    arguments: Dict[str, Any],
    plugin_config: EBirdPluginConfig,
    body: str = "",
) -> List[Dict[str, Any]]:
    """Compute civic-AI caveats as coded objects.

    ONE list drives both output channels: ``_finalize_response`` renders
    each ``message`` into the text block, and ``_build_structured`` emits
    {code, message} into structuredContent. A qualification that used to be
    prose a model had to notice is now something a caller can branch on,
    and the two cannot disagree because there is no second source.

    Best-effort: any unexpected error degrades silently to an empty list so
    a caveat-formatting bug never breaks an otherwise-good response.
    """
    try:
        caveats: List[Dict[str, Any]] = []

        # NOTABLE-IS-LOCAL fires on the tool, not the data — even empty notable
        # responses get it, because a model might read "no notable" as "no rare
        # birds in the area" when it really means "no birds notable HERE."
        if tool_name in _NOTABLE_TOOLS:
            caveats.append(
                _caveat(CAVEAT_NOTABLE_IS_LOCAL, _NOTABLE_IS_LOCAL_CAVEAT)
            )

        # ONE-RECORD-PER-SPECIES fires on the tool + regionCode shape, not the
        # data: it warns about how the result may be *used*, so it must fire
        # even on empty/small results. Suppressed when regionCode is already an
        # L-code (the list is then location-scoped — nothing to misfilter).
        if tool_name in _REGION_DEDUPED_TOOLS:
            region = arguments.get("regionCode")
            if isinstance(region, str) and not _LOCID_RE.match(region):
                caveats.append(
                    _caveat(
                        CAVEAT_ONE_RECORD_PER_SPECIES, _REGION_DEDUPED_CAVEAT
                    )
                )

        if (
            tool_name == "get_taxonomy_forms"
            and isinstance(data, list)
            and len(data) > 1
        ):
            requested = arguments.get("speciesCode", "?")
            caveats.append(
                _caveat(
                    CAVEAT_TAXONOMIC_AMBIGUITY,
                    f"⚠️ TAXONOMIC AMBIGUITY: {len(data)} taxonomic forms "
                    f"exist for '{requested}' (subspecies / hybrids / "
                    f"spuhs). Observers may report sightings under any of "
                    f"these codes — query each form separately before "
                    f"aggregating, or ask the user which form they meant.",
                )
            )

        # Hotspot species totals invite a comparison eBird cannot support.
        # Fires on the tool, like NOTABLE-IS-LOCAL: the misreading is
        # available as soon as the numbers are.
        if tool_name in _HOTSPOT_TOOLS and isinstance(data, list) and data:
            caveats.append(
                _caveat(
                    CAVEAT_UNCOMPARABLE_SPECIES_TOTALS,
                    "⚠️ UNCOMPARABLE TOTALS: all-time species counts are NOT "
                    "comparable across hotspots. eBird returns no per-hotspot "
                    "checklist count, so a site with 200 species over 5000 "
                    "checklists is indistinguishable here from one with 200 "
                    "over 50. Rank by days_since_last_obs for activity, not "
                    "by numSpeciesAllTime for quality.",
                )
            )

        if _text_was_clipped(body):
            caveats.append(
                _caveat(
                    CAVEAT_RESPONSE_SIZE_CEILING,
                    f"⚠️ TEXT CLIPPED: the rendered text hit this server's "
                    f"{_MAX_BODY_BYTES // 1024} KB backstop and shows only "
                    f"part of the result. The complete row set is in this "
                    f"response's structuredContent — read that rather than "
                    f"concluding the remainder does not exist.",
                )
            )

        if tool_name not in _OBSERVATION_TOOLS or not isinstance(data, list):
            return caveats

        count = len(data)
        if count == 0:
            # The absence-of-evidence framing is deliberately rendered in the
            # BODY, not the caveat block: a model skims a leading warning list
            # but reads the answer. It still travels in structuredContent
            # under a stable code so a caller can branch without parsing.
            caveats.append(
                _caveat(
                    CAVEAT_ABSENCE_OF_EVIDENCE,
                    _empty_observation_body(tool_name, arguments),
                    in_body=True,
                )
            )
            return caveats

        unique_subIds = _unique_subIds(data)
        max_results = arguments.get("maxResults", plugin_config.default_max_results)
        hit_cap = isinstance(max_results, int) and count >= max_results

        # Sampling-size caveats are mutually exclusive — pick the most specific
        # one. SINGLE-RECORD > SINGLE-OBSERVER > LOW SURVEY EFFORT > SMALL
        # SAMPLE. Truncation suppresses LOW SURVEY EFFORT (when we hit the
        # cap, the small unique-checklist count could simply reflect that many
        # obs from a few birders saturated maxResults — not real low effort).
        if count == 1:
            caveats.append(
                _caveat(
                    CAVEAT_SINGLE_RECORD,
                    "⚠️ SINGLE-RECORD CLAIM: only 1 observation returned. Do "
                    "not report this as a trend or pattern — N=1 is an "
                    "anecdote, not a frequency claim. Check the review status "
                    "before treating as established.",
                )
            )
        elif unique_subIds == 1:
            caveats.append(
                _caveat(
                    CAVEAT_SINGLE_OBSERVER,
                    f"⚠️ SINGLE-OBSERVER PROVENANCE: all {count} observations "
                    f"come from one checklist (one birder, one trip). Verify "
                    f"the review status on each record before treating as "
                    f"established — a single-checklist rare-bird record could "
                    f"be a misidentification.",
                )
            )
        elif not hit_cap and 1 < unique_subIds < _LOW_EFFORT_CHECKLIST_THRESHOLD:
            caveats.append(
                _caveat(
                    CAVEAT_LOW_SURVEY_EFFORT,
                    f"⚠️ LOW SURVEY EFFORT: only {unique_subIds} distinct "
                    f"checklists contributed to these {count} observations. "
                    f"Absence of OTHER species in this area/window is much "
                    f"weaker evidence than in well-birded regions — it "
                    f"usually means no one looked, not no birds.",
                )
            )
        elif count < _SMALL_SAMPLE_THRESHOLD:
            caveats.append(
                _caveat(
                    CAVEAT_SMALL_SAMPLE,
                    f"⚠️ SMALL SAMPLE: only {count} observations returned. "
                    f"Trends, 'most common', and frequency claims are "
                    f"unreliable below {_SMALL_SAMPLE_THRESHOLD} records.",
                )
            )

        if hit_cap:
            caveats.append(
                _caveat(
                    CAVEAT_POSSIBLY_TRUNCATED,
                    f"⚠️ POSSIBLY TRUNCATED: returned {count} observations, "
                    f"which hits the maxResults cap ({max_results}). The true "
                    f"total may be larger — re-run with a higher maxResults, "
                    f"a smaller back, or a smaller dist to confirm.",
                )
            )

        # NULL COUNTS. eBird lets an observer record presence as "X" instead
        # of a number, which arrives as a missing howMany. Treating that as 0
        # in a sum silently undercounts, and it is the single most misread
        # field in this API — so it gets a code, not just a schema note.
        missing_counts = sum(1 for obs in data if obs.get("howMany") is None)
        if missing_counts:
            caveats.append(
                _caveat(
                    CAVEAT_COUNT_NOT_REPORTED,
                    f"⚠️ COUNTS NOT REPORTED: {missing_counts} of {count} "
                    f"observations record presence without a number (eBird "
                    f"'X'). Their howMany is null, which means PRESENT, NOT "
                    f"ZERO. Any total summed over these rows is a lower "
                    f"bound — do not present it as a population count.",
                )
            )

        stale = _window_staleness_caveat(
            data,
            arguments.get("back"),
            plugin_config.default_back,
        )
        if stale:
            caveats.append(stale)

        return caveats
    except Exception:
        logger.exception(
            "Failed to build caveats for %s; continuing without", tool_name
        )
        return []


def _unique_subIds(observations: List[Dict[str, Any]]) -> int:
    """Count of distinct eBird checklist IDs. Each subId is one birding outing
    by one observer, so this is the natural 'effort' proxy when full-detail
    observer fields aren't present."""
    return len({obs.get("subId") for obs in observations if obs.get("subId")})


def _window_staleness_caveat(
    observations: List[Dict[str, Any]],
    requested_back: Any,
    default_back: int,
) -> Optional[Dict[str, Any]]:
    """Fires when the most recent observation in the result is much older than
    expected for the requested ``back`` window. Implies low birding effort
    rather than species turnover."""
    back = requested_back if isinstance(requested_back, int) else default_back
    if not isinstance(back, int) or back <= 0:
        return None

    max_date: Optional[dt.datetime] = None
    for obs in observations:
        parsed = _parse_ebird_datetime(obs.get("obsDt"))
        if parsed is None:
            continue
        if max_date is None or parsed > max_date:
            max_date = parsed
    if max_date is None:
        return None

    days_old = (_now_naive_utc().date() - max_date.date()).days
    if days_old >= _STALE_WINDOW_MIN_DAYS and days_old > back / 2:
        return _caveat(
            CAVEAT_WINDOW_STALENESS,
            f"⚠️ WINDOW STALENESS: most recent observation is {days_old} days "
            f"old (you requested back={back}). No fresh observations in this "
            f"window — suggests low birding effort recently, not necessarily "
            f"species turnover.",
        )
    return None


# ---- Structured output ----------------------------------------------------
#
# Built from the RAW upstream `data`, in one place, rather than from inside
# the text formatters. That is deliberate: the formatters short-circuit on
# empty results (`_empty_observation_body`, the no-hotspots branch), and a
# structured builder living behind those early returns is exactly how a
# server ends up advertising an outputSchema and then returning no
# structuredContent on the zero-result path. Deriving from `data` makes the
# empty case fall out as `rows: []` with no special handling to forget.


def _structured_caveats(caveats: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Strip the internal render flag; emit only the wire fields."""
    return [
        {"code": c["code"], "message": c["message"]} for c in caveats
    ]


def _total_count(returned: int, cap: Any) -> Optional[int]:
    """How many exist upstream, or None when that is genuinely unknown.

    eBird returns no total alongside a result set. If the result came back
    short of the cap, it is complete and `returned` IS the total — 0
    included, which is a real answer meaning "none reported in this
    window". If it came back AT the cap, more may exist and we do not know
    how many, so the honest value is null. Reporting `returned` there would
    dress a capped sample up as a complete census.
    """
    if isinstance(cap, int) and returned >= cap:
        return None
    return returned


def _observation_rows(
    observations: List[Dict[str, Any]],
    *,
    query_lat: Optional[float],
    query_lng: Optional[float],
) -> List[Dict[str, Any]]:
    """eBird's own fields, unclipped and uncompacted.

    The text may render these as a compact table or drop some at the byte
    backstop; these rows never are. Truncating the machine-readable half
    would defeat the point of having one.
    """
    rows: List[Dict[str, Any]] = []
    for obs in observations:
        raw_dt = obs.get("obsDt")
        parsed = _parse_ebird_datetime(raw_dt)
        row: Dict[str, Any] = {
            "speciesCode": obs.get("speciesCode"),
            "comName": obs.get("comName"),
            "sciName": obs.get("sciName"),
            # Raw first, ours second and clearly labelled, so a caller can
            # tell eBird's value from our normalization of it.
            "obsDt": raw_dt,
            "obsDtIso": parsed.isoformat() if parsed else None,
            # NOT defaulted to 0: a missing howMany means "present, count
            # not reported", which is a different claim from zero.
            "howMany": obs.get("howMany"),
            "lat": obs.get("lat"),
            "lng": obs.get("lng"),
            "locId": obs.get("locId"),
            "locName": obs.get("locName"),
            "obsValid": obs.get("obsValid"),
            "obsReviewed": obs.get("obsReviewed"),
            "subId": obs.get("subId"),
            "distance_km": None,
        }
        if (
            query_lat is not None
            and query_lng is not None
            and obs.get("lat") is not None
            and obs.get("lng") is not None
        ):
            try:
                row["distance_km"] = round(
                    _haversine_km(
                        float(query_lat),
                        float(query_lng),
                        float(obs["lat"]),
                        float(obs["lng"]),
                    ),
                    3,
                )
            except (TypeError, ValueError):
                pass
        # Every key is always present, even when null. Two reasons: a
        # caller gets one predictable row shape instead of having to probe
        # for optional keys, and null is itself information here — howMany
        # null means "present, count not reported", distance_km null means
        # "not a proximity query". Correspondingly every field is declared
        # nullable in the schema, because eBird genuinely omits fields
        # across endpoints and detail levels, and a schema must not be
        # violated by real data.
        rows.append(row)
    return rows


def _hotspot_rows(
    hotspots: List[Dict[str, Any]],
    *,
    query_lat: Optional[float],
    query_lng: Optional[float],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for h in hotspots:
        latest = h.get("latestObsDt")
        row: Dict[str, Any] = {
            "locId": h.get("locId"),
            "locName": h.get("locName"),
            "lat": h.get("lat"),
            "lng": h.get("lng"),
            "numSpeciesAllTime": h.get("numSpeciesAllTime"),
            "latestObsDt": latest,
            # The activity signal that species totals cannot provide.
            "days_since_last_obs": _days_ago(latest) if latest else None,
            "distance_km": None,
        }
        if (
            query_lat is not None
            and query_lng is not None
            and h.get("lat") is not None
            and h.get("lng") is not None
        ):
            try:
                row["distance_km"] = round(
                    _haversine_km(
                        float(query_lat),
                        float(query_lng),
                        float(h["lat"]),
                        float(h["lng"]),
                    ),
                    3,
                )
            except (TypeError, ValueError):
                pass
        rows.append(row)
    return rows


def _build_structured(
    tool_name: str,
    data: Any,
    arguments: Dict[str, Any],
    plugin_config: EBirdPluginConfig,
    *,
    url: str,
    params: Dict[str, Any],
    caveats: List[Dict[str, Any]],
    retrieved_at: str,
) -> Optional[Dict[str, Any]]:
    """Assemble the declared envelope, or None for tools without a schema.

    Never returns None for a tool that declares an outputSchema — including
    when `data` is empty. A declared schema is binding: advertising one and
    then omitting structuredContent is a conformance break a validating
    client would be right to reject.
    """
    if tool_name not in TOOL_OUTPUT_SCHEMAS:
        return None
    if not isinstance(data, list):
        data = []

    envelope: Dict[str, Any] = {
        "query": {"source": url, "params": dict(params)},
        "caveats": _structured_caveats(caveats),
    }

    if tool_name in _OBSERVATION_TOOLS:
        nearby = tool_name in _NEARBY_OBSERVATION_TOOLS
        rows = _observation_rows(
            data,
            query_lat=arguments.get("lat") if nearby else None,
            query_lng=arguments.get("lng") if nearby else None,
        )
        cap = arguments.get("maxResults", plugin_config.default_max_results)
        envelope["rows"] = rows
        envelope["summary"] = {
            "returned": len(rows),
            "total_count": _total_count(len(rows), cap),
            "truncated": False,
            "retrieved_at": retrieved_at,
            # Grain matters: "how many species" is not "how many rows", and
            # "how much evidence" is checklists, not rows. Reporting all
            # three means a caller never has to guess which one a bare
            # count meant.
            "distinct_species": len(
                {r.get("speciesCode") for r in rows if r.get("speciesCode")}
            ),
            "distinct_checklists": len(
                {r.get("subId") for r in rows if r.get("subId")}
            ),
            "distinct_locations": len(
                {r.get("locId") for r in rows if r.get("locId")}
            ),
            "counts_not_reported": sum(
                1 for r in rows if r.get("howMany") is None
            ),
        }
        return envelope

    # Hotspots. get_hotspots/get_nearby_hotspots take no maxResults, so the
    # returned set is always complete and total_count is never null.
    nearby = tool_name == "get_nearby_hotspots"
    rows = _hotspot_rows(
        data,
        query_lat=arguments.get("lat") if nearby else None,
        query_lng=arguments.get("lng") if nearby else None,
    )
    envelope["rows"] = rows
    envelope["summary"] = {
        "returned": len(rows),
        "total_count": len(rows),
        "truncated": False,
        "retrieved_at": retrieved_at,
        "active_hotspots": sum(
            1 for r in rows if r.get("latestObsDt")
        ),
    }
    return envelope


# ---- Body formatters ------------------------------------------------------


def _format_body(
    tool_name: str,
    data: Any,
    arguments: Dict[str, Any],
    plugin_config: EBirdPluginConfig,
) -> str:
    if tool_name in _OBSERVATION_TOOLS:
        return _format_observations(
            data,
            include_observer=plugin_config.include_observer_name,
            tool_name=tool_name,
            arguments=arguments,
        )
    if tool_name == "get_hotspots":
        return _format_hotspots(data, arguments=arguments, nearby=False)
    if tool_name == "get_nearby_hotspots":
        return _format_hotspots(data, arguments=arguments, nearby=True)
    if tool_name == "get_taxonomy":
        return _format_taxonomy(data)
    if tool_name == "get_taxonomy_forms":
        return _format_taxonomy_forms(data)
    return json.dumps(data, indent=2)


def _format_observations(
    observations: List[Dict[str, Any]],
    *,
    include_observer: bool,
    tool_name: str,
    arguments: Dict[str, Any],
) -> str:
    if not observations:
        return _empty_observation_body(tool_name, arguments)

    query_lat = arguments.get("lat") if tool_name in _NEARBY_OBSERVATION_TOOLS else None
    query_lng = arguments.get("lng") if tool_name in _NEARBY_OBSERVATION_TOOLS else None

    if len(observations) > _COMPACT_FORMAT_THRESHOLD:
        return _format_observations_compact(
            observations,
            include_observer=include_observer,
            query_lat=query_lat,
            query_lng=query_lng,
        )

    blocks = [
        _format_obs_block(
            obs,
            include_observer=include_observer,
            query_lat=query_lat,
            query_lng=query_lng,
        )
        for obs in observations
    ]
    return _join_with_size_cap(blocks, sep="\n\n")


def _format_obs_block(
    obs: Dict[str, Any],
    *,
    include_observer: bool,
    query_lat: Optional[float],
    query_lng: Optional[float],
) -> str:
    com = obs.get("comName", "?")
    sci = obs.get("sciName", "?")
    code = obs.get("speciesCode")

    species_line = f"Species: {com} ({sci})"
    if code:
        species_line += f" [{code}]"

    how_many = (
        f"Count: {obs['howMany']}"
        if obs.get("howMany") is not None
        else "Count: present (no number reported)"
    )
    date_str = _format_date(obs.get("obsDt"))
    location_line = f"Location: {obs.get('locName', '?')} ({obs.get('locId', '?')})"
    coords = (
        f"{obs['lat']}, {obs['lng']}"
        if obs.get("lat") is not None and obs.get("lng") is not None
        else "Not available"
    )

    lines: List[str] = [
        species_line,
        f"Date: {date_str}",
        location_line,
        how_many,
        f"Coordinates: {coords}",
    ]

    if (
        query_lat is not None
        and query_lng is not None
        and obs.get("lat") is not None
        and obs.get("lng") is not None
    ):
        try:
            km = _haversine_km(
                float(query_lat),
                float(query_lng),
                float(obs["lat"]),
                float(obs["lng"]),
            )
            lines.append(f"Distance: {km:.1f} km from query point")
        except (TypeError, ValueError):
            pass

    lines.append(f"Review: {_review_label(obs)}")

    sub_id = obs.get("subId")
    if sub_id:
        lines.append(f"Checklist: https://ebird.org/checklist/{sub_id}")

    if include_observer:
        name = obs.get("userDisplayName") or "anonymous"
        lines.append(f"Observer: {name}")

    flag = _taxonomy_flag_for(com)
    if flag:
        lines.append(flag)

    return "\n".join(lines)


def _review_label(obs: Dict[str, Any]) -> str:
    """Translate obsReviewed / obsValid into a user-facing label.

    Most eBird observations have obsReviewed=False (auto-passed without
    explicit review). Notable / rare-bird records are more likely to have
    obsReviewed=True. obsValid=False on a reviewed record means a reviewer
    rejected it — typically a misidentification.
    """
    reviewed = obs.get("obsReviewed")
    valid = obs.get("obsValid")
    if reviewed is True and valid is True:
        return "✓ Reviewer-confirmed"
    if reviewed is True and valid is False:
        return "✗ Rejected by reviewer"
    if reviewed is False:
        return "⏳ Not yet reviewed (may be a misidentification)"
    return "Review status unknown"


def _taxonomy_flag_for(com_name: Any) -> str:
    """Best-effort taxonomic-category flag inferred from the common name.

    Without a session-level taxonomy cache, we still flag hybrids, spuhs,
    slashes, and subspecies/form records by their common-name pattern. eBird
    reserves parenthetical suffixes for taxonomic forms — false positives are
    unlikely. If a flag is wrong, the body is still correct; only the
    advisory is misplaced.
    """
    if not com_name or not isinstance(com_name, str):
        return ""
    lower = com_name.lower()
    if "(hybrid)" in lower:
        return (
            "⚠️ TAXONOMY: hybrid record — do NOT count toward species totals; "
            "this is a cross between two species."
        )
    if lower.endswith(" sp."):
        return (
            "⚠️ TAXONOMY: 'sp.' (spuh) record — observer could not identify to "
            "species; ambiguous between multiple possibilities."
        )
    if "/" in com_name:
        return (
            "⚠️ TAXONOMY: slash-code record — observer reported two possible "
            "species without confirming which; do not count as confirmed."
        )
    if "(" in com_name and ")" in com_name:
        return (
            "⚠️ TAXONOMY: subspecies / form record — this is one form of a "
            "broader species; other forms may be reported separately under "
            "different codes."
        )
    return ""


# ---- Compact rendering + size backstop -------------------------------------


def _join_with_size_cap(blocks: List[str], *, sep: str) -> str:
    """Join pre-formatted record blocks, truncating at a record boundary if
    the joined text would exceed ``_MAX_BODY_BYTES``. Truncation is never
    silent — a house-style notice replaces the dropped records."""
    total = len(blocks)
    sep_bytes = len(sep.encode("utf-8"))
    out: List[str] = []
    size = 0
    for block in blocks:
        added = len(block.encode("utf-8")) + (sep_bytes if out else 0)
        if size + added > _MAX_BODY_BYTES and out:
            shown = len(out)
            out.append(
                f"{_SIZE_CEILING_MARKER} showing {shown} of {total} records "
                f"— the formatted response hit this server's "
                f"{_MAX_BODY_BYTES // 1024} KB backstop. The remaining "
                f"{total - shown} records were returned by eBird but not "
                f"rendered here; they ARE present in full in this response's "
                f"structuredContent. Re-run with a smaller back, dist, or "
                f"maxResults for a complete, narrower text rendering."
            )
            break
        out.append(block)
        size += added
    return sep.join(out)


def _table_cell(value: Any) -> str:
    """Render a value for a pipe-delimited table row: pipes and newlines in
    field values would corrupt the row structure, so replace them."""
    return str(value).replace("|", "/").replace("\n", " ")


def _review_label_compact(obs: Dict[str, Any]) -> str:
    """Short review labels for table rows; same semantics as _review_label."""
    reviewed = obs.get("obsReviewed")
    valid = obs.get("obsValid")
    if reviewed is True and valid is True:
        return "confirmed"
    if reviewed is True and valid is False:
        return "REJECTED"
    if reviewed is False:
        return "unreviewed"
    return "?"


def _format_observations_compact(
    observations: List[Dict[str, Any]],
    *,
    include_observer: bool,
    query_lat: Optional[float],
    query_lng: Optional[float],
) -> str:
    """Pipe-delimited table for large observation sets. The block format at
    ~325 bytes/record makes big responses multi-megabyte; rows keep the same
    facts (species, date, count, location, coords, review, checklist) at a
    fraction of the size. Small results stay on the block format, which
    reads better."""
    has_distance = query_lat is not None and query_lng is not None

    columns = ["Species [code]", "Date", "Count", "Location (locId)", "Lat,Lng"]
    if has_distance:
        columns.append("Km")
    columns += ["Review", "Checklist"]
    if include_observer:
        columns.append("Observer")

    rows: List[str] = []
    any_taxonomy_flag = False
    for obs in observations:
        com = obs.get("comName", "?")
        code = obs.get("speciesCode")
        species = f"{com} [{code}]" if code else str(com)
        if _taxonomy_flag_for(com):
            any_taxonomy_flag = True

        count = obs["howMany"] if obs.get("howMany") is not None else "present"
        coords = (
            f"{obs['lat']},{obs['lng']}"
            if obs.get("lat") is not None and obs.get("lng") is not None
            else "?"
        )
        cells: List[Any] = [
            species,
            _format_date(obs.get("obsDt")),
            count,
            f"{obs.get('locName', '?')} ({obs.get('locId', '?')})",
            coords,
        ]
        if has_distance:
            km = "?"
            if obs.get("lat") is not None and obs.get("lng") is not None:
                try:
                    km = "{:.1f}".format(
                        _haversine_km(
                            float(query_lat),
                            float(query_lng),
                            float(obs["lat"]),
                            float(obs["lng"]),
                        )
                    )
                except (TypeError, ValueError):
                    pass
            cells.append(km)
        cells.append(_review_label_compact(obs))
        cells.append(obs.get("subId") or "?")
        if include_observer:
            cells.append(obs.get("userDisplayName") or "anonymous")

        rows.append(" | ".join(_table_cell(c) for c in cells))

    header: List[str] = [
        f"{len(observations)} observations — compact table, one record per "
        f"line. Checklist column: append the ID to "
        f"https://ebird.org/checklist/ for the full checklist. Review "
        f"column: 'unreviewed' records may be misidentifications; 'REJECTED' "
        f"means a reviewer rejected the record."
    ]
    if any_taxonomy_flag:
        header.append(
            "⚠️ TAXONOMY: rows whose species name contains '(hybrid)', "
            "'sp.', a slash, or a parenthetical form are not confirmed "
            "single-species records — do not count them toward species totals."
        )
    header.append("")
    header.append(" | ".join(columns))
    return "\n".join(header) + "\n" + _join_with_size_cap(rows, sep="\n")


def _empty_observation_body(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Body text for an empty observation result. Frames absence as
    sampling, not biology — eBird is opt-in, so 'no records' usually means
    'no one looked.' This is the headline civic-AI move for eBird and must
    live in the BODY (descriptions get ignored)."""
    species = arguments.get("speciesCode")
    region = arguments.get("regionCode")
    back = arguments.get("back")
    window = f"the last {back} days" if isinstance(back, int) else "the requested window"

    if tool_name in _SPECIES_SPECIFIC_TOOLS:
        target = f"'{species}'" if species else "this species"
        where = f"in '{region}'" if region else "near the query point"
        return (
            f"No eBird records of {target} {where} during {window}.\n\n"
            f"⚠️ ABSENCE-OF-EVIDENCE: this does NOT mean {target} is absent. "
            f"eBird is opt-in observation logs, not a systematic survey — "
            f"\"no records\" usually means no birders reported a sighting in "
            f"this window, not that no birds were present. Try: expanding "
            f"back, increasing dist (for nearby queries), checking "
            f"get_taxonomy_forms for related subspecies codes, or checking "
            f"get_hotspots for active birding locations nearby."
        )

    where = f"in '{region}'" if region else "near the query point"
    return (
        f"No eBird observations reported {where} during {window}.\n\n"
        f"⚠️ ABSENCE-OF-EVIDENCE: this almost always means low or no birding "
        f"effort in this period, not that no birds are present. eBird is "
        f"opt-in observation logs, not a systematic survey. Try expanding "
        f"back, increasing dist, or checking get_hotspots / "
        f"get_nearby_hotspots for active birding locations."
    )


def _format_hotspots(
    hotspots: List[Dict[str, Any]],
    *,
    arguments: Dict[str, Any],
    nearby: bool,
) -> str:
    if not hotspots:
        if nearby:
            return (
                "No hotspots found near the query point.\n\n"
                "eBird hotspots are observer-curated locations. Try increasing "
                "dist, or query get_nearby_observations directly — there may "
                "be active birding here that isn't yet a designated hotspot."
            )
        region = arguments.get("regionCode")
        where = f"in '{region}'" if region else "in the requested region"
        return (
            f"No hotspots registered {where}.\n\n"
            f"eBird hotspots are observer-curated locations. A region with no "
            f"hotspots may still have active birding — try get_recent_observations "
            f"by region, or get_nearby_hotspots from a known lat/lng inside it."
        )

    query_lat = arguments.get("lat") if nearby else None
    query_lng = arguments.get("lng") if nearby else None

    if len(hotspots) > _COMPACT_FORMAT_THRESHOLD:
        return _format_hotspots_compact(
            hotspots, query_lat=query_lat, query_lng=query_lng
        )

    blocks: List[str] = []
    for h in hotspots:
        name = h.get("locName") or f"Hotspot {h.get('locId', '?')}"
        coords = (
            f"{h['lat']}, {h['lng']}"
            if h.get("lat") is not None and h.get("lng") is not None
            else "Not available"
        )
        species = (
            h.get("numSpeciesAllTime")
            if h.get("numSpeciesAllTime") is not None
            else h.get("numSpecies", "unknown")
        )

        lines: List[str] = [
            f"Hotspot: {name}",
            f"Location ID: {h.get('locId', '?')}",
            f"Coordinates: {coords}",
            f"Species total (all-time): {species}",
        ]

        if (
            query_lat is not None
            and query_lng is not None
            and h.get("lat") is not None
            and h.get("lng") is not None
        ):
            try:
                km = _haversine_km(
                    float(query_lat),
                    float(query_lng),
                    float(h["lat"]),
                    float(h["lng"]),
                )
                lines.append(f"Distance: {km:.1f} km from query point")
            except (TypeError, ValueError):
                pass

        latest = h.get("latestObsDt")
        if latest:
            days = _days_ago(latest)
            if days is None:
                lines.append(f"Last observation: {_format_date(latest)}")
            elif days <= 0:
                lines.append(f"Last observation: {_format_date(latest)} (today)")
            elif days == 1:
                lines.append(f"Last observation: {_format_date(latest)} (1 day ago)")
            else:
                lines.append(
                    f"Last observation: {_format_date(latest)} ({days} days ago)"
                )
        else:
            lines.append("Last observation: no records (inactive hotspot)")

        blocks.append("\n".join(lines))

    return _join_with_size_cap(blocks, sep="\n\n")


def _format_hotspots_compact(
    hotspots: List[Dict[str, Any]],
    *,
    query_lat: Optional[float],
    query_lng: Optional[float],
) -> str:
    """Pipe-delimited table for large hotspot sets (a whole state can return
    thousands). Same size rationale as _format_observations_compact."""
    has_distance = query_lat is not None and query_lng is not None

    columns = ["Hotspot (locId)", "Lat,Lng", "Species all-time"]
    if has_distance:
        columns.append("Km")
    columns.append("Last obs")

    rows: List[str] = []
    for h in hotspots:
        name = h.get("locName") or f"Hotspot {h.get('locId', '?')}"
        coords = (
            f"{h['lat']},{h['lng']}"
            if h.get("lat") is not None and h.get("lng") is not None
            else "?"
        )
        species = (
            h.get("numSpeciesAllTime")
            if h.get("numSpeciesAllTime") is not None
            else h.get("numSpecies", "unknown")
        )
        cells: List[Any] = [f"{name} ({h.get('locId', '?')})", coords, species]
        if has_distance:
            km = "?"
            if h.get("lat") is not None and h.get("lng") is not None:
                try:
                    km = "{:.1f}".format(
                        _haversine_km(
                            float(query_lat),
                            float(query_lng),
                            float(h["lat"]),
                            float(h["lng"]),
                        )
                    )
                except (TypeError, ValueError):
                    pass
            cells.append(km)
        latest = h.get("latestObsDt")
        if latest:
            days = _days_ago(latest)
            last = _format_date(latest)
            if days is not None and days > 0:
                last += f" ({days}d ago)"
        else:
            last = "none (inactive)"
        cells.append(last)

        rows.append(" | ".join(_table_cell(c) for c in cells))

    header = [
        f"{len(hotspots)} hotspots — compact table, one per line. Species "
        f"totals are all-time counts and are NOT comparable across hotspots "
        f"(eBird does not return per-hotspot checklist counts); use the "
        f"last-observation date to gauge activity.",
        "",
        " | ".join(columns),
    ]
    return "\n".join(header) + "\n" + _join_with_size_cap(rows, sep="\n")


def _format_taxonomy(taxonomy: List[Dict[str, Any]], limit: int = 20) -> str:
    if not taxonomy:
        return "No taxonomy data found."

    shown = taxonomy[:limit]
    lines: List[str] = []
    for entry in shown:
        lines.append(
            f"Common Name: {entry.get('comName', '?')}\n"
            f"Scientific Name: {entry.get('sciName', '?')}\n"
            f"Species Code: {entry.get('speciesCode', '?')}\n"
            f"Order: {entry.get('order', '?')}\n"
            f"Family: {entry.get('familyComName', '?')} ({entry.get('familySciName', '?')})"
        )
    out = "\n\n".join(lines)
    if len(taxonomy) > limit:
        out += f"\n\n[Showing {limit} of {len(taxonomy)} entries]"
    return out


def _format_taxonomy_forms(forms: Any) -> str:
    if not isinstance(forms, list) or not forms:
        return (
            "No additional taxonomic forms recognized. Only the base species "
            "code is in eBird's taxonomy."
        )
    return "Recognized form codes (use any of these as speciesCode):\n" + "\n".join(
        f"- {c}" for c in forms
    )


def _format_date(value: Any) -> str:
    """Normalize eBird date strings to ISO 8601.

    eBird returns dates like ``2024-05-11 14:23`` (space separator), epoch-ms
    on some fields, or plain ``2024-05-11``. Midnight-aligned values render as
    date-only; everything else renders as ``YYYY-MM-DDTHH:MM`` so real
    observation timestamps don't get silently rounded.
    """
    if value is None or value == "":
        return "Unknown"

    if isinstance(value, (int, float)):
        try:
            parsed = dt.datetime.fromtimestamp(value / 1000.0, dt.timezone.utc).replace(
                tzinfo=None
            )
            return _render_datetime(parsed)
        except (OverflowError, OSError, ValueError):
            return str(value)

    if not isinstance(value, str):
        return str(value)

    parsed = _parse_ebird_datetime(value)
    if parsed is None:
        return value
    return _render_datetime(parsed)


def _render_datetime(parsed: dt.datetime) -> str:
    if parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%Y-%m-%dT%H:%M")
