"""Declared output schemas and caveat codes for the eBird tools.

A declared ``outputSchema`` is BINDING: the MCP spec says servers MUST
return conforming structured results and clients SHOULD validate them. So
nothing in here declares a constraint that real eBird data can violate —
notably there is no ``maximum`` anywhere, and no ``required`` on a field
eBird can legitimately omit.

Everything shares one envelope::

    {"query": {...}, "summary": {...}, "rows": [...], "caveats": [...]}

``caveats`` is the point of the exercise. A qualification that used to be
prose the model had to notice ("all 40 observations come from one
checklist") is now an object with a stable ``code`` a caller can branch
on. The rendered prose and the structured array are built from ONE list in
``plugin.py`` so they cannot drift apart.
"""

from typing import Any, Dict

# ---- Caveat codes ---------------------------------------------------------
#
# Stable identifiers. The prose attached to each may be reworded; these must
# not change, because callers branch on them.

CAVEAT_ABSENCE_OF_EVIDENCE = "ABSENCE_OF_EVIDENCE"
CAVEAT_COUNT_NOT_REPORTED = "COUNT_NOT_REPORTED"
CAVEAT_LOW_SURVEY_EFFORT = "LOW_SURVEY_EFFORT"
CAVEAT_NOTABLE_IS_LOCAL = "NOTABLE_IS_LOCAL"
CAVEAT_ONE_RECORD_PER_SPECIES = "ONE_RECORD_PER_SPECIES"
CAVEAT_POSSIBLY_TRUNCATED = "POSSIBLY_TRUNCATED"
CAVEAT_RESPONSE_SIZE_CEILING = "RESPONSE_SIZE_CEILING"
CAVEAT_SINGLE_OBSERVER = "SINGLE_OBSERVER_PROVENANCE"
CAVEAT_SINGLE_RECORD = "SINGLE_RECORD_CLAIM"
CAVEAT_SMALL_SAMPLE = "SMALL_SAMPLE"
CAVEAT_TAXONOMIC_AMBIGUITY = "TAXONOMIC_AMBIGUITY"
CAVEAT_UNCOMPARABLE_SPECIES_TOTALS = "UNCOMPARABLE_SPECIES_TOTALS"
CAVEAT_WINDOW_STALENESS = "WINDOW_STALENESS"

ALL_CAVEAT_CODES = frozenset(
    {
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
    }
)


# ---- Shared envelope pieces -----------------------------------------------

_CAVEATS = {
    "type": "array",
    "description": (
        "Qualifications that change how the rows must be read. Each has a "
        "stable `code` a caller can branch on; the `message` wording may "
        "change. The same text appears in the human-readable content block."
    ),
    "items": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "enum": sorted(ALL_CAVEAT_CODES),
                "description": "Stable identifier for this qualification.",
            },
            "message": {
                "type": "string",
                "description": "Human-readable rendering of the caveat.",
            },
        },
        "required": ["code", "message"],
    },
}

# The echoed request. Parameter names differ per tool (regionCode vs
# lat/lng), so this is an open object — the only form JSON Schema can
# validate for a varying key set.
_QUERY = {
    "type": "object",
    "description": (
        "Provenance: the upstream URL that was called and the parameters "
        "actually sent to it, after clamping."
    ),
    "properties": {
        "source": {
            "type": "string",
            "description": "Fully qualified eBird API URL that was called.",
        },
        "params": {
            "type": "object",
            "description": "Parameters sent upstream, after clamping.",
            "additionalProperties": True,
        },
    },
    "required": ["source", "params"],
}


def _summary(extra: Dict[str, Any] = None) -> Dict[str, Any]:
    """Row-count bookkeeping.

    ``total_count`` is deliberately nullable. null means "eBird does not
    report a total and this result hit the requested cap, so more may
    exist" — a different claim from 0, which means "the count is known and
    it is none". Conflating them makes a complete answer look unmeasured.
    """
    properties: Dict[str, Any] = {
        "returned": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of rows in `rows`.",
        },
        "total_count": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": (
                "Total available upstream, when knowable. Null when the "
                "result hit the maxResults cap: eBird returns no total, so "
                "the true count is unknown rather than equal to `returned`. "
                "0 is a real, complete answer and is NOT the same as null."
            ),
        },
        "truncated": {
            "type": "boolean",
            "description": (
                "True when rows were dropped from `rows` — i.e. the "
                "machine-readable channel itself is incomplete."
            ),
        },
        "retrieved_at": {
            "type": "string",
            "description": "UTC timestamp of this response (ISO 8601).",
        },
    }
    if extra:
        properties.update(extra)
    return {
        "type": "object",
        "properties": properties,
        "required": ["returned", "total_count", "truncated", "retrieved_at"],
    }


def _envelope(rows: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": _QUERY,
            "summary": summary,
            "rows": rows,
            "caveats": _CAVEATS,
        },
        "required": ["query", "summary", "rows", "caveats"],
    }


# ---- Observation rows -----------------------------------------------------

# Raw eBird field names and values, not a prose-rendered subset. A caller
# that wants what the text shows can read the text; the point of this
# channel is the data as eBird actually returned it.
_OBSERVATION_ROW = {
    "type": "object",
    "properties": {
        "speciesCode": {
            "type": ["string", "null"],
            "description": (
                "eBird's 6-letter internal code — the identifier to feed "
                "back into species-specific tools. NOT a banding alpha code."
            ),
        },
        "comName": {"type": ["string", "null"], "description": "Common name."},
        "sciName": {"type": ["string", "null"], "description": "Scientific name."},
        "obsDt": {
            "type": ["string", "null"],
            "description": (
                "Observation date/time EXACTLY as eBird returned it — "
                "usually 'YYYY-MM-DD HH:MM', sometimes date-only when the "
                "observer reported no time. Not normalized; see obsDtIso."
            ),
        },
        "obsDtIso": {
            "type": ["string", "null"],
            "description": (
                "Our ISO-8601 normalization of obsDt, or null when the "
                "upstream value did not parse. Shipped alongside the raw "
                "value rather than replacing it so a caller can tell which "
                "is eBird's and which is ours."
            ),
        },
        "howMany": {
            "type": ["integer", "null"],
            "description": (
                "Number of individuals reported. NULL MEANS 'PRESENT, "
                "COUNT NOT REPORTED' — it does NOT mean zero. eBird lets "
                "observers record presence with an 'X' instead of a "
                "number, and summing this field while treating null as 0 "
                "undercounts. See the COUNT_NOT_REPORTED caveat."
            ),
        },
        "lat": {
            "type": ["number", "null"],
            "description": "Latitude as reported by eBird.",
        },
        "lng": {
            "type": ["number", "null"],
            "description": "Longitude as reported by eBird.",
        },
        "locId": {
            "type": ["string", "null"],
            "description": (
                "eBird location ID. Usable as regionCode on the region "
                "tools when it is a hotspot (L-prefixed) code."
            ),
        },
        "locName": {"type": ["string", "null"], "description": "Location name."},
        "obsValid": {
            "type": ["boolean", "null"],
            "description": (
                "False on a reviewed record means a reviewer REJECTED it, "
                "typically as a misidentification."
            ),
        },
        "obsReviewed": {
            "type": ["boolean", "null"],
            "description": (
                "False means auto-passed without explicit review, which is "
                "the common case — not a quality signal on its own."
            ),
        },
        "subId": {
            "type": ["string", "null"],
            "description": (
                "Checklist ID. Append to https://ebird.org/checklist/ for "
                "the source checklist. Distinct subIds are the natural "
                "proxy for survey effort behind a set of observations."
            ),
        },
        "distance_km": {
            "type": ["number", "null"],
            "description": (
                "Great-circle distance from the query point. Present only "
                "on the lat/lng-based tools; null elsewhere."
            ),
        },
    },
    # No `required`: eBird omits fields inconsistently across endpoints and
    # detail levels, and a declared schema must not be violated by real data.
}

OBSERVATIONS_SCHEMA = _envelope(
    rows={
        "type": "array",
        "description": (
            "One object per observation, carrying eBird's own field names "
            "and values. Complete: unlike the text rendering, these rows "
            "are never compacted or clipped for readability."
        ),
        "items": _OBSERVATION_ROW,
    },
    summary=_summary(
        {
            "distinct_species": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Distinct speciesCode values among the rows. Differs "
                    "from `returned` whenever a species was reported more "
                    "than once — count this, not rows, for 'how many "
                    "species'."
                ),
            },
            "distinct_checklists": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Distinct subId values: how many separate birding "
                    "outings produced these rows. The effort denominator. "
                    "A large row count from one checklist is one trip, not "
                    "broad evidence."
                ),
            },
            "distinct_locations": {
                "type": "integer",
                "minimum": 0,
                "description": "Distinct locId values among the rows.",
            },
            "counts_not_reported": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Rows whose howMany is null. Sum howMany over the rows "
                    "only if this is 0; otherwise the sum is a lower bound."
                ),
            },
        }
    ),
)


# ---- Hotspot rows ---------------------------------------------------------

_HOTSPOT_ROW = {
    "type": "object",
    "properties": {
        "locId": {
            "type": ["string", "null"],
            "description": (
                "Hotspot location ID. Pass as regionCode to the "
                "observation tools to scope a query to this place."
            ),
        },
        "locName": {"type": ["string", "null"], "description": "Hotspot name."},
        "lat": {"type": ["number", "null"], "description": "Latitude."},
        "lng": {"type": ["number", "null"], "description": "Longitude."},
        "numSpeciesAllTime": {
            "type": ["integer", "null"],
            "description": (
                "All-time species total. NOT comparable across hotspots: "
                "eBird returns no per-hotspot checklist count, so a "
                "hotspot with 200 species over 5000 checklists and one "
                "with 200 over 50 look identical here. See the "
                "UNCOMPARABLE_SPECIES_TOTALS caveat."
            ),
        },
        "latestObsDt": {
            "type": ["string", "null"],
            "description": (
                "Date of the most recent observation, as eBird returned "
                "it. Null means no records — an inactive hotspot."
            ),
        },
        "days_since_last_obs": {
            "type": ["integer", "null"],
            "description": (
                "Days since latestObsDt, or null when absent/unparseable. "
                "The usable activity signal, given species totals are not "
                "comparable."
            ),
        },
        "distance_km": {
            "type": ["number", "null"],
            "description": (
                "Great-circle distance from the query point; present only "
                "on get_nearby_hotspots."
            ),
        },
    },
}

HOTSPOTS_SCHEMA = _envelope(
    rows={
        "type": "array",
        "description": "One object per hotspot, with eBird's field names.",
        "items": _HOTSPOT_ROW,
    },
    summary=_summary(
        {
            "active_hotspots": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Rows with any recorded observation date. The rest are "
                    "registered but dormant."
                ),
            }
        }
    ),
)
