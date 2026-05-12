"""eBird plugin implementation.

Provides MCP tools backed by the eBird v2 REST API. Mirrors the tool surface
of the upstream stdio reference server (`ebird-mcp-server`).
"""

import datetime as dt
import json
import logging
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
from plugins.ebird.ebird_client import EBirdClient

logger = logging.getLogger(__name__)

_RETRY = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    # Only retry network errors and 5xx; never retry 4xx (bad params).
    retry=retry_if_exception_type((httpx.TransportError, httpx.ReadTimeout)),
)


class EBirdPlugin(MCPPlugin):
    """Plugin for accessing the eBird v2 REST API."""

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
            # No cold-start smoke call. The eBird API has no cheap, always-200
            # health endpoint; a failing tool call will surface auth/network
            # errors with the actual upstream message.
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

    # ---- Tool catalog -----------------------------------------------------

    def get_tools(self) -> List[ToolDefinition]:
        region_code = {
            "type": "string",
            "description": (
                "eBird region or location code: country (US), subnational1 (US-NY), "
                "subnational2 (US-NY-109), or hotspot location ID (e.g. L99381)."
            ),
        }
        species_code = {
            "type": "string",
            "description": "eBird species code (e.g. 'amecro' for American Crow).",
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

        return [
            ToolDefinition(
                name="get_recent_observations",
                description=(
                    "Get recent bird observations in an eBird region (country, state/province, "
                    "county, or hotspot). Returns species, location, count, and timestamp for each "
                    "checklist entry."
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
                    "Useful for tracking where a target species has been seen."
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
                    "eBird flags these based on regional checklist filters."
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
                    "Get recent bird observations near a lat/lng coordinate within a radius."
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
                    "Get notable (rare or unusual) bird observations near a lat/lng coordinate."
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
                    "Get recent observations of a specific species near a lat/lng coordinate."
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
                    "coordinates, and species totals."
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
                    "Get birding hotspots near a lat/lng coordinate within a radius."
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
                    "names, and taxonomic ordering. Large response — narrow with `cat` if possible."
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
                    "Get taxonomic subforms (subspecies, hybrids, etc.) for a given species code."
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

        # Map detail='full' -> 'simple' if upstream rejects it (mirrors JS reference).
        detail = arguments.get("detail", "simple")
        if detail not in ("simple", "full"):
            detail = "simple"

        try:
            if tool_name == "get_recent_observations":
                data = await _RETRY(self.client.get_recent_observations)(
                    region_code=_require(arguments, "regionCode"),
                    back=arguments.get("back", self.plugin_config.default_back),
                    max_results=arguments.get(
                        "maxResults", self.plugin_config.default_max_results
                    ),
                    include_provisional=arguments.get("includeProvisional"),
                    hotspot=arguments.get("hotspot"),
                    detail=detail,
                )
                return _ok(_format_observations(data))

            if tool_name == "get_recent_observations_for_species":
                data = await _RETRY(self.client.get_recent_observations_for_species)(
                    region_code=_require(arguments, "regionCode"),
                    species_code=_require(arguments, "speciesCode"),
                    back=arguments.get("back", self.plugin_config.default_back),
                    max_results=arguments.get(
                        "maxResults", self.plugin_config.default_max_results
                    ),
                    include_provisional=arguments.get("includeProvisional"),
                    hotspot=arguments.get("hotspot"),
                )
                return _ok(_format_observations(data))

            if tool_name == "get_notable_observations":
                data = await _RETRY(self.client.get_notable_observations)(
                    region_code=_require(arguments, "regionCode"),
                    back=arguments.get("back", self.plugin_config.default_back),
                    max_results=arguments.get(
                        "maxResults", self.plugin_config.default_max_results
                    ),
                    detail=detail,
                )
                return _ok(_format_observations(data))

            if tool_name == "get_nearby_observations":
                data = await _RETRY(self.client.get_nearby_observations)(
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
                return _ok(_format_observations(data))

            if tool_name == "get_nearby_notable_observations":
                data = await _RETRY(self.client.get_nearby_notable_observations)(
                    lat=_require(arguments, "lat"),
                    lng=_require(arguments, "lng"),
                    dist=arguments.get("dist"),
                    back=arguments.get("back", self.plugin_config.default_back),
                    max_results=arguments.get(
                        "maxResults", self.plugin_config.default_max_results
                    ),
                )
                return _ok(_format_observations(data))

            if tool_name == "get_nearby_observations_for_species":
                data = await _RETRY(self.client.get_nearby_observations_for_species)(
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
                return _ok(_format_observations(data))

            if tool_name == "get_hotspots":
                data = await _RETRY(self.client.get_hotspots)(
                    region_code=_require(arguments, "regionCode"),
                    back=arguments.get("back"),
                )
                return _ok(_format_hotspots(data))

            if tool_name == "get_nearby_hotspots":
                data = await _RETRY(self.client.get_nearby_hotspots)(
                    lat=_require(arguments, "lat"),
                    lng=_require(arguments, "lng"),
                    dist=arguments.get("dist"),
                    back=arguments.get("back"),
                )
                return _ok(_format_hotspots(data))

            if tool_name == "get_taxonomy":
                data = await _RETRY(self.client.get_taxonomy)(
                    locale=arguments.get("locale", "en"),
                    cat=arguments.get("cat", "species"),
                    fmt=arguments.get("fmt", "json"),
                )
                return _ok(_format_taxonomy(data))

            if tool_name == "get_taxonomy_forms":
                data = await _RETRY(self.client.get_taxonomy_forms)(
                    species_code=_require(arguments, "speciesCode"),
                )
                return _ok(json.dumps(data, indent=2))

            return ToolResult(
                content=[],
                success=False,
                error_message=f"Unknown tool: {tool_name}",
            )

        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response is not None else ""
            msg = f"eBird API HTTP {e.response.status_code}: {body}"
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


def _require(args: Dict[str, Any], key: str) -> Any:
    if key not in args or args[key] in (None, ""):
        raise KeyError(key)
    return args[key]


def _ok(text: str) -> ToolResult:
    return ToolResult(
        content=[{"type": "text", "text": text}],
        success=True,
    )


# ---- Output formatting ----------------------------------------------------


def _format_observations(observations: List[Dict[str, Any]]) -> str:
    if not observations:
        return "No observations found."

    lines: List[str] = []
    for obs in observations:
        how_many = (
            f"Count: {obs['howMany']}"
            if obs.get("howMany") is not None
            else "Present"
        )
        date_str = _format_date(obs.get("obsDt"))
        coords = (
            f"{obs['lat']}, {obs['lng']}"
            if obs.get("lat") is not None and obs.get("lng") is not None
            else "Not available"
        )
        observer = (
            f"\nObserver: {obs['userDisplayName']}"
            if obs.get("userDisplayName")
            else ""
        )
        lines.append(
            f"Species: {obs.get('comName', '?')} ({obs.get('sciName', '?')})\n"
            f"Location: {obs.get('locName', '?')} ({obs.get('locId', '?')})\n"
            f"{how_many}\n"
            f"Date: {date_str}\n"
            f"Coordinates: {coords}{observer}"
        )
    return "\n\n".join(lines)


def _format_hotspots(hotspots: List[Dict[str, Any]]) -> str:
    if not hotspots:
        return "No hotspots found."

    lines: List[str] = []
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
            else h.get("numSpecies", "Unknown")
        )
        latest = h.get("latestObsDt")
        latest_line = f"\nLatest observation: {_format_date(latest)}" if latest else ""
        lines.append(
            f"Hotspot: {name}\n"
            f"Location ID: {h.get('locId', '?')}\n"
            f"Coordinates: {coords}\n"
            f"Species total: {species}{latest_line}"
        )
    return "\n\n".join(lines)


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


def _format_date(value: Any) -> str:
    if not value:
        return "Unknown"
    if not isinstance(value, str):
        return str(value)
    # eBird returns dates like "2024-05-11 14:23" or "2024-05-11".
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            return parsed.strftime("%Y-%m-%d %H:%M") if "%H" in fmt else parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value
