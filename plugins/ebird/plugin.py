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

from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult
from plugins.ebird.config_schema import EBirdPluginConfig
from plugins.ebird.ebird_client import EBirdClient, QuotaExhausted

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


# Sampling-caveat thresholds. Tuned conservatively — a false alarm on
# heavily-birded areas (Central Park, Cape May) trains users to ignore the
# warning entirely. Adjust here, not at call sites.
_SMALL_SAMPLE_THRESHOLD = 10           # below this many records → SMALL SAMPLE
_LOW_EFFORT_CHECKLIST_THRESHOLD = 5    # below this many unique checklists → LOW SURVEY EFFORT
_STALE_WINDOW_MIN_DAYS = 3             # below this, freshness noise dominates


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
            "description": "Maximum number of results to return (1-10000). Default: 100.",
            "minimum": 1,
            "maximum": 10000,
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

        return [
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
        except ValueError as e:
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
        except KeyError as e:
            return ToolResult(
                content=[],
                success=False,
                error_message=f"Missing required argument: {e.args[0]}",
            )
        except Exception as e:
            logger.exception(f"Error in {tool_name}")
            return ToolResult(content=[], success=False, error_message=str(e))

        body = _format_body(tool_name, data, arguments, self.plugin_config)
        caveats = _build_caveats(tool_name, data, arguments, self.plugin_config)
        text = _finalize_response(
            url=url, params=params_sent, body=body, caveats=caveats
        )
        return _ok(text)

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
    if key not in args or args[key] in (None, ""):
        raise KeyError(key)
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
            raise ValueError(
                f"Invalid regionCode: {v!r}. Expected e.g. 'US', 'US-NY', "
                f"'US-NY-109', or a hotspot location ID like 'L12345'."
            )

    if "speciesCode" in out and out["speciesCode"] is not None:
        v = out["speciesCode"]
        if not isinstance(v, str) or not _SPECIES_RE.match(v):
            raise ValueError(
                f"Invalid speciesCode: {v!r}. Expected lowercase alphanumeric, "
                f"e.g. 'amecro' for American Crow. Note: these are 6-letter "
                f"eBird internal codes, NOT 4-letter banding alpha codes."
            )

    if "cat" in out and out["cat"] is not None:
        v = out["cat"]
        if v not in _TAXONOMY_CAT:
            raise ValueError(
                f"Invalid cat: {v!r}. Allowed: {sorted(_TAXONOMY_CAT)}."
            )

    if "fmt" in out and out["fmt"] is not None:
        v = out["fmt"]
        if v not in _TAXONOMY_FMT:
            raise ValueError(f"Invalid fmt: {v!r}. Allowed: {sorted(_TAXONOMY_FMT)}.")

    if "locale" in out and out["locale"] is not None:
        v = out["locale"]
        if not isinstance(v, str) or not _LOCALE_RE.match(v):
            raise ValueError(
                f"Invalid locale: {v!r}. Expected ISO codes like 'en', 'es', 'pt_BR'."
            )

    if out.get("back") is not None:
        out["back"] = _clamp_int(out["back"], 1, 30, "back")
    if out.get("maxResults") is not None:
        out["maxResults"] = _clamp_int(out["maxResults"], 1, 10000, "maxResults")
    if out.get("dist") is not None:
        out["dist"] = _clamp_int(out["dist"], 0, 50, "dist")

    if out.get("lat") is not None:
        lat = _coerce_float(out["lat"], "lat")
        if not (-90 <= lat <= 90):
            raise ValueError(f"Invalid lat: {lat}. Must be between -90 and 90.")
        out["lat"] = lat
    if out.get("lng") is not None:
        lng = _coerce_float(out["lng"], "lng")
        if not (-180 <= lng <= 180):
            raise ValueError(f"Invalid lng: {lng}. Must be between -180 and 180.")
        out["lng"] = lng

    return out


def _clamp_int(value: Any, lo: int, hi: int, name: str) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}: {value!r}. Expected integer.")
    return max(lo, min(hi, i))


def _coerce_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}: {value!r}. Expected number.")


def _ok(text: str) -> ToolResult:
    return ToolResult(content=[{"type": "text", "text": text}], success=True)


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
    caveats: List[str],
) -> str:
    """Wrap a formatted body with provenance + caveats + retrieved-at footer."""
    parts: List[str] = [f"Source: {url}"]
    if params:
        params_str = ", ".join(f"{k}={_pretty_param(v)}" for k, v in params.items())
        parts.append(f"Query: {params_str}")
    parts.append("")  # blank line after provenance header

    for caveat in caveats:
        parts.append(caveat)
        parts.append("")

    parts.append(body)
    parts.append("")
    parts.append(f"_Retrieved: {_utcnow_iso()}_")
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
) -> List[str]:
    """Compute civic-AI caveats. Best-effort: any unexpected error degrades
    silently to an empty list so a caveat-formatting bug never breaks an
    otherwise-good response."""
    try:
        caveats: List[str] = []

        # NOTABLE-IS-LOCAL fires on the tool, not the data — even empty notable
        # responses get it, because a model might read "no notable" as "no rare
        # birds in the area" when it really means "no birds notable HERE."
        if tool_name in _NOTABLE_TOOLS:
            caveats.append(_NOTABLE_IS_LOCAL_CAVEAT)

        # ONE-RECORD-PER-SPECIES fires on the tool + regionCode shape, not the
        # data: it warns about how the result may be *used*, so it must fire
        # even on empty/small results. Suppressed when regionCode is already an
        # L-code (the list is then location-scoped — nothing to misfilter).
        if tool_name in _REGION_DEDUPED_TOOLS:
            region = arguments.get("regionCode")
            if isinstance(region, str) and not _LOCID_RE.match(region):
                caveats.append(_REGION_DEDUPED_CAVEAT)

        if tool_name == "get_taxonomy_forms" and isinstance(data, list) and len(data) > 1:
            requested = arguments.get("speciesCode", "?")
            caveats.append(
                f"⚠️ TAXONOMIC AMBIGUITY: {len(data)} taxonomic forms exist for "
                f"'{requested}' (subspecies / hybrids / spuhs). Observers may "
                f"report sightings under any of these codes — query each form "
                f"separately before aggregating, or ask the user which form "
                f"they meant."
            )

        if tool_name not in _OBSERVATION_TOOLS or not isinstance(data, list):
            return caveats

        count = len(data)
        # Empty result: the absence-of-evidence message lives in the body, not
        # the caveats block. No sample-size caveats apply when count is 0.
        if count == 0:
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
                "⚠️ SINGLE-RECORD CLAIM: only 1 observation returned. Do not "
                "report this as a trend or pattern — N=1 is an anecdote, not a "
                "frequency claim. Check the review status before treating as "
                "established."
            )
        elif unique_subIds == 1:
            caveats.append(
                f"⚠️ SINGLE-OBSERVER PROVENANCE: all {count} observations come "
                f"from one checklist (one birder, one trip). Verify the review "
                f"status on each record before treating as established — a "
                f"single-checklist rare-bird record could be a misidentification."
            )
        elif not hit_cap and 1 < unique_subIds < _LOW_EFFORT_CHECKLIST_THRESHOLD:
            caveats.append(
                f"⚠️ LOW SURVEY EFFORT: only {unique_subIds} distinct checklists "
                f"contributed to these {count} observations. Absence of OTHER "
                f"species in this area/window is much weaker evidence than in "
                f"well-birded regions — it usually means no one looked, not no "
                f"birds."
            )
        elif count < _SMALL_SAMPLE_THRESHOLD:
            caveats.append(
                f"⚠️ SMALL SAMPLE: only {count} observations returned. Trends, "
                f"'most common', and frequency claims are unreliable below "
                f"{_SMALL_SAMPLE_THRESHOLD} records."
            )

        if hit_cap:
            caveats.append(
                f"⚠️ POSSIBLY TRUNCATED: returned {count} observations, which "
                f"hits the maxResults cap ({max_results}). The true total may "
                f"be larger — re-run with a higher maxResults, a smaller back, "
                f"or a smaller dist to confirm."
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
) -> Optional[str]:
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
        return (
            f"⚠️ WINDOW STALENESS: most recent observation is {days_old} days "
            f"old (you requested back={back}). No fresh observations in this "
            f"window — suggests low birding effort recently, not necessarily "
            f"species turnover."
        )
    return None


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

    blocks = [
        _format_obs_block(
            obs,
            include_observer=include_observer,
            query_lat=query_lat,
            query_lng=query_lng,
        )
        for obs in observations
    ]
    return "\n\n".join(blocks)


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

    return "\n\n".join(blocks)


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
