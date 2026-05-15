"""Async HTTP client for the eBird API.

Wraps the endpoints used by the JS stdio reference server (10 tools).

Each public method returns ``(data, url, params)`` — the data, the fully
qualified URL that was hit, and the params dict that was sent. The plugin
echoes those back in tool responses so the LLM cannot omit attribution
or misreport what was asked.
"""

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# Daily soft-gate against the upstream eBird quota (1000 calls/day). Per-warm-
# container counter — multiple Lambda containers each track independently, so
# the *aggregate* allowance is higher than the threshold below. This is
# deliberate: the goal is friendly degradation (clear "try tomorrow" error
# before eBird itself 429s mid-response), not exact quota accounting. WAF and
# the bundled taxonomy do the heavy lifting; this is the last line of defense.
DAILY_QUOTA_SOFT_LIMIT = 900
_quota_state: Dict[str, Any] = {"date": None, "count": 0}


class QuotaExhausted(Exception):
    """Raised before an HTTP call when this container's daily counter has
    hit DAILY_QUOTA_SOFT_LIMIT. The plugin catches this explicitly so the
    user gets a clean 'try tomorrow' message instead of an upstream 429."""


def _quota_check_and_bump() -> None:
    today = _dt.datetime.now(_dt.timezone.utc).date()
    if _quota_state["date"] != today:
        _quota_state["date"] = today
        _quota_state["count"] = 0
    if _quota_state["count"] >= DAILY_QUOTA_SOFT_LIMIT:
        raise QuotaExhausted(
            "This eBird MCP server's daily upstream quota is nearly exhausted "
            "(shared free instance, ~1000 eBird calls/day total). Please try "
            "again after UTC midnight, or self-host your own instance for "
            "uninterrupted access — see "
            "https://github.com/codeforanchorage/ebird-mcp."
        )
    _quota_state["count"] += 1
    # Periodic visibility in CloudWatch without log-spam on every call.
    if _quota_state["count"] % 100 == 0:
        logger.info(
            "eBird upstream call count for this container today: %d / %d",
            _quota_state["count"],
            DAILY_QUOTA_SOFT_LIMIT,
        )

# Bundled taxonomy snapshot, refreshed at deploy time by
# scripts/refresh_taxonomy.py. Serving get_taxonomy from this file saves the
# eBird daily-quota call. eBird publishes new taxonomy ~annually, so a deploy
# cadence is more than fresh enough.
_BUNDLE_PATH = Path(__file__).parent / "data" / "taxonomy.json"
_bundle_cache: Optional[List[Dict[str, Any]]] = None
_bundle_load_attempted = False


def _load_bundled_taxonomy() -> Optional[List[Dict[str, Any]]]:
    """Module-level lazy load. Survives warm invocations within one Lambda
    container; cold starts pay a one-time ~50–200 ms read+parse. Returns None
    if the bundle is missing or unreadable, so callers can fall through to the
    live API without a hard failure."""
    global _bundle_cache, _bundle_load_attempted
    if _bundle_cache is not None:
        return _bundle_cache
    if _bundle_load_attempted:
        return None
    _bundle_load_attempted = True

    if not _BUNDLE_PATH.exists():
        logger.info("Bundled taxonomy not present at %s; will use live API", _BUNDLE_PATH)
        return None
    try:
        with open(_BUNDLE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            logger.warning("Bundled taxonomy has unexpected shape; ignoring")
            return None
        _bundle_cache = data
        logger.info("Loaded bundled eBird taxonomy: %d entries", len(data))
        return _bundle_cache
    except Exception as e:
        logger.warning("Failed to load bundled taxonomy: %s", e)
        return None

# A response envelope from the eBird API. Carrying the URL and params alongside
# the data lets the plugin attach provenance without reconstructing the request.
ApiResponse = Tuple[Any, str, Dict[str, Any]]


class EBirdClient:
    """Thin async client over the eBird v2 REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.ebird.org/v2",
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-eBirdApiToken": api_key},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        accept_json: bool = True,
    ) -> ApiResponse:
        """Run a GET against the eBird API.

        Returns ``(body, full_url, clean_params)``. ``body`` is parsed JSON
        when ``accept_json`` is True, otherwise the raw response text.
        """
        # Counted here so bundle-served paths (which never reach _get) don't
        # consume the budget. Raises QuotaExhausted before any network I/O.
        _quota_check_and_bump()

        clean_params: Dict[str, Any] = {}
        if params:
            for key, value in params.items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    clean_params[key] = "true" if value else "false"
                else:
                    clean_params[key] = value

        accept = "application/json" if accept_json else "text/plain"
        response = await self._client.get(
            endpoint, params=clean_params, headers={"Accept": accept}
        )
        response.raise_for_status()
        body = response.json() if accept_json else response.text
        return body, f"{self.base_url}{endpoint}", clean_params

    # ---- Observations -----------------------------------------------------

    async def get_recent_observations(
        self,
        region_code: str,
        back: Optional[int] = None,
        max_results: Optional[int] = None,
        include_provisional: Optional[bool] = None,
        hotspot: Optional[bool] = None,
        detail: str = "simple",
    ) -> ApiResponse:
        # The /detailed endpoint returns 400 in several regions; the reference
        # JS server falls back to simple. Mirror that behavior.
        endpoint = (
            f"/data/obs/{region_code}/recent/detailed"
            if detail == "full"
            else f"/data/obs/{region_code}/recent"
        )
        return await self._get(
            endpoint,
            params={
                "back": back,
                "maxResults": max_results,
                "includeProvisional": include_provisional,
                "hotspot": hotspot,
            },
        )

    async def get_recent_observations_for_species(
        self,
        region_code: str,
        species_code: str,
        back: Optional[int] = None,
        max_results: Optional[int] = None,
        include_provisional: Optional[bool] = None,
        hotspot: Optional[bool] = None,
    ) -> ApiResponse:
        endpoint = f"/data/obs/{region_code}/recent/{species_code}"
        return await self._get(
            endpoint,
            params={
                "back": back,
                "maxResults": max_results,
                "includeProvisional": include_provisional,
                "hotspot": hotspot,
            },
        )

    async def get_notable_observations(
        self,
        region_code: str,
        back: Optional[int] = None,
        max_results: Optional[int] = None,
        detail: str = "simple",
    ) -> ApiResponse:
        endpoint = (
            f"/data/obs/{region_code}/recent/notable/detailed"
            if detail == "full"
            else f"/data/obs/{region_code}/recent/notable"
        )
        return await self._get(
            endpoint, params={"back": back, "maxResults": max_results}
        )

    async def get_nearby_observations(
        self,
        lat: float,
        lng: float,
        dist: Optional[int] = None,
        back: Optional[int] = None,
        max_results: Optional[int] = None,
        include_provisional: Optional[bool] = None,
        hotspot: Optional[bool] = None,
    ) -> ApiResponse:
        return await self._get(
            "/data/obs/geo/recent",
            params={
                "lat": lat,
                "lng": lng,
                "dist": dist,
                "back": back,
                "maxResults": max_results,
                "includeProvisional": include_provisional,
                "hotspot": hotspot,
            },
        )

    async def get_nearby_notable_observations(
        self,
        lat: float,
        lng: float,
        dist: Optional[int] = None,
        back: Optional[int] = None,
        max_results: Optional[int] = None,
    ) -> ApiResponse:
        return await self._get(
            "/data/obs/geo/recent/notable",
            params={
                "lat": lat,
                "lng": lng,
                "dist": dist,
                "back": back,
                "maxResults": max_results,
            },
        )

    async def get_nearby_observations_for_species(
        self,
        lat: float,
        lng: float,
        species_code: str,
        dist: Optional[int] = None,
        back: Optional[int] = None,
        max_results: Optional[int] = None,
        include_provisional: Optional[bool] = None,
    ) -> ApiResponse:
        return await self._get(
            f"/data/obs/geo/recent/{species_code}",
            params={
                "lat": lat,
                "lng": lng,
                "dist": dist,
                "back": back,
                "maxResults": max_results,
                "includeProvisional": include_provisional,
            },
        )

    # ---- Hotspots ---------------------------------------------------------

    async def get_hotspots(
        self,
        region_code: str,
        back: Optional[int] = None,
    ) -> ApiResponse:
        # The /ref/hotspot/{region} endpoint returns CSV by default; force JSON.
        return await self._get(
            f"/ref/hotspot/{region_code}",
            params={"back": back, "fmt": "json"},
        )

    async def get_nearby_hotspots(
        self,
        lat: float,
        lng: float,
        dist: Optional[int] = None,
        back: Optional[int] = None,
    ) -> ApiResponse:
        params = {
            "lat": lat,
            "lng": lng,
            "dist": dist,
            "back": back,
            "fmt": "json",
        }
        try:
            return await self._get("/ref/hotspot/geo", params=params)
        except Exception as e:
            # The endpoint occasionally returns CSV even with fmt=json. Fall back
            # to parsing the text response into a minimal hotspot shape.
            logger.warning(
                f"Falling back to text response for nearby hotspots: {e}"
            )
            params_text = {k: v for k, v in params.items() if k != "fmt"}
            text, url, sent = await self._get(
                "/ref/hotspot/geo", params=params_text, accept_json=False
            )
            return _parse_hotspot_text(text, lat, lng), url, sent

    # ---- Taxonomy ---------------------------------------------------------

    async def get_taxonomy(
        self,
        locale: str = "en",
        cat: str = "species",
        fmt: str = "json",
    ) -> ApiResponse:
        # Serve the common case (English JSON) from the bundled snapshot to
        # save daily eBird quota. The bundle holds every category, so we filter
        # in-process by `cat`. Non-English locales and CSV format fall through
        # to the live API since the bundle is locale=en / fmt=json only.
        if locale == "en" and fmt == "json":
            bundle = _load_bundled_taxonomy()
            if bundle is not None:
                wanted = {c.strip() for c in cat.split(",") if c.strip()}
                filtered = (
                    [e for e in bundle if e.get("category") in wanted]
                    if wanted
                    else list(bundle)
                )
                return (
                    filtered,
                    f"{self.base_url}/ref/taxonomy/ebird",
                    {"locale": locale, "cat": cat, "fmt": fmt},
                )

        return await self._get(
            "/ref/taxonomy/ebird",
            params={"locale": locale, "cat": cat, "fmt": fmt},
        )

    async def get_taxonomy_forms(self, species_code: str) -> ApiResponse:
        # eBird's endpoint is /ref/taxon/forms — NOT /ref/taxonomy/forms
        # (the latter 404s for every species code).
        return await self._get(f"/ref/taxon/forms/{species_code}")


def _parse_hotspot_text(text: str, lat: float, lng: float) -> List[Dict[str, Any]]:
    """Best-effort parser for the eBird hotspot CSV/text fallback.

    Each line of the CSV is:
        locId,countryCode,subnational1Code,subnational2Code,lat,lng,locName,latestObsDt,numSpeciesAllTime
    Older variants only return locId. We accept either.
    """
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(",")]
        loc_id = fields[0]
        record: Dict[str, Any] = {
            "locId": loc_id,
            "locName": fields[6] if len(fields) > 6 else f"Hotspot {loc_id}",
            "lat": float(fields[4]) if len(fields) > 4 and fields[4] else lat,
            "lng": float(fields[5]) if len(fields) > 5 and fields[5] else lng,
        }
        if len(fields) > 8 and fields[8]:
            try:
                record["numSpeciesAllTime"] = int(fields[8])
            except ValueError:
                pass
        out.append(record)
    return out
