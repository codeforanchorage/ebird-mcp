"""Tests for civic-AI patterns in the eBird plugin.

The eBird-tailored patterns center on *effort bias* — "no records" usually
means "no one looked," not "no birds." These tests pin each caveat to a
"fires when expected" + "silent when not applicable" pair. False alarms on
heavily-birded areas (Cape May, Central Park) erode trust faster than false
silences, so the precedence chain — SINGLE-RECORD > SINGLE-OBSERVER >
LOW SURVEY EFFORT > SMALL SAMPLE — is exercised explicitly.

Pattern guide: civicaitools.org/learn.

Run with::

    python -m unittest tests.test_civic_ai_patterns
"""

import datetime as dt
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from plugins.ebird import plugin as plugin_module
from plugins.ebird.plugin import EBirdPlugin


_BASE_URL = "https://api.ebird.org/v2"

# All time-sensitive tests pin "now" here so observation dates ("yesterday",
# "10 days ago") stay stable across days. Default test obsDt = day-before.
_FIXED_NOW = dt.datetime(2026, 5, 13, 12, 0, 0)
_DEFAULT_OBS_DT = "2026-05-12 09:15"


def _make_plugin(include_observer_name: bool = False) -> EBirdPlugin:
    """Build a plugin without touching the real eBird API."""
    p = EBirdPlugin({
        "enabled": True,
        "api_key": "test-key",
        "include_observer_name": include_observer_name,
    })

    class _StubClient:
        base_url = _BASE_URL

        async def aclose(self) -> None:  # pragma: no cover
            pass

    p.client = _StubClient()  # type: ignore[assignment]
    p._initialized = True
    return p


def _fake_obs(
    n: int,
    *,
    sub_ids: Optional[List[str]] = None,
    com_name: str = "American Crow",
    sci_name: str = "Corvus brachyrhynchos",
    species_code: str = "amecro",
    obs_dt: str = _DEFAULT_OBS_DT,
    obs_reviewed: bool = True,
    obs_valid: bool = True,
    lat: float = 40.7128,
    lng: float = -74.0060,
    user_display_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build n fake observation records. By default each gets a unique subId
    (S0..S{n-1}); pass `sub_ids` to control checklist provenance."""
    if sub_ids is None:
        sub_ids = [f"S{i}" for i in range(n)]
    if len(sub_ids) < n:
        sub_ids = list(sub_ids) + [f"S{i}" for i in range(len(sub_ids), n)]
    sub_ids = sub_ids[:n]

    out: List[Dict[str, Any]] = []
    for i in range(n):
        rec: Dict[str, Any] = {
            "comName": com_name,
            "sciName": sci_name,
            "speciesCode": species_code,
            "locName": "Test Park",
            "locId": "L1",
            "obsDt": obs_dt,
            "howMany": 2,
            "lat": lat,
            "lng": lng,
            "obsValid": obs_valid,
            "obsReviewed": obs_reviewed,
            "subId": sub_ids[i],
        }
        if user_display_name is not None:
            rec["userDisplayName"] = user_display_name
        out.append(rec)
    return out


async def _run(
    plugin: EBirdPlugin,
    tool_name: str,
    arguments: Dict[str, Any],
    data: Any,
    *,
    url: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Stub the upstream call (`_dispatch`) and let the real formatters run."""
    if url is None:
        url = f"{_BASE_URL}/data/obs/US-NY/recent"
    if params is None:
        params = {}

    async def _stub_dispatch(self, name, args, detail):  # noqa: ARG001
        return data, url, params

    with patch.object(EBirdPlugin, "_dispatch", _stub_dispatch):
        result = await plugin.execute_tool(tool_name, arguments)
    if result.success:
        return result.content[0]["text"]
    return result.error_message or ""


class _TimeFrozenTest(unittest.IsolatedAsyncioTestCase):
    """Pin `_now_naive_utc` so observation timestamps stay stable across days."""

    def setUp(self) -> None:
        patcher = patch.object(plugin_module, "_now_naive_utc", return_value=_FIXED_NOW)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---- Provenance -----------------------------------------------------------


class ProvenanceTests(_TimeFrozenTest):
    """Source line + echoed query + retrieved-at footer."""

    async def test_source_line_first(self) -> None:
        p = _make_plugin()
        url = f"{_BASE_URL}/data/obs/US-NY/recent"
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(20), url=url, params={"back": 14},
        )
        self.assertTrue(text.startswith(f"Source: {url}"), text[:200])

    async def test_query_echoed(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(20),
            params={"back": 14, "maxResults": 100, "includeProvisional": "true"},
        )
        self.assertIn(
            "Query: back=14, maxResults=100, includeProvisional=true", text
        )

    async def test_retrieved_timestamp_at_end(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"}, _fake_obs(20),
        )
        self.assertRegex(text, r"_Retrieved: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z_$")


# ---- Sampling-caveat precedence -------------------------------------------


class SamplingCaveatTests(_TimeFrozenTest):
    """SINGLE-RECORD > SINGLE-OBSERVER > LOW SURVEY EFFORT > SMALL SAMPLE."""

    async def test_single_record_at_n1(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"}, _fake_obs(1),
        )
        self.assertIn("SINGLE-RECORD CLAIM", text)
        # Don't double-fire.
        self.assertNotIn("SINGLE-OBSERVER", text)
        self.assertNotIn("LOW SURVEY EFFORT", text)

    async def test_single_observer_when_all_same_checklist(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(5, sub_ids=["SAME"] * 5),
        )
        self.assertIn("SINGLE-OBSERVER PROVENANCE", text)
        self.assertNotIn("LOW SURVEY EFFORT", text)
        self.assertNotIn("SMALL SAMPLE", text)

    async def test_single_observer_silent_with_multiple_checklists(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(5, sub_ids=["S1", "S2", "S3", "S4", "S5"]),
        )
        self.assertNotIn("SINGLE-OBSERVER", text)

    async def test_low_survey_effort_when_few_unique_checklists(self) -> None:
        p = _make_plugin()
        # 4 obs across 3 unique checklists → 1 < 3 < 5 → LOW SURVEY EFFORT
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(4, sub_ids=["S1", "S1", "S2", "S3"]),
        )
        self.assertIn("LOW SURVEY EFFORT", text)
        self.assertIn("3 distinct checklists", text)
        self.assertNotIn("SMALL SAMPLE", text)

    async def test_low_survey_effort_silent_when_many_checklists(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(8),  # 8 unique subIds (≥5)
        )
        self.assertNotIn("LOW SURVEY EFFORT", text)

    async def test_low_survey_effort_silent_when_truncated(self) -> None:
        # Hitting maxResults could explain low unique-checklist count
        # (many obs from few birders saturating the cap) — suppress LOW EFFORT.
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(100, sub_ids=["S1", "S2"] * 50),
        )
        self.assertIn("POSSIBLY TRUNCATED", text)
        self.assertNotIn("LOW SURVEY EFFORT", text)

    async def test_small_sample_when_count_low_but_unique_normal(self) -> None:
        p = _make_plugin()
        # 7 obs from 7 distinct checklists: SMALL SAMPLE fires, LOW EFFORT does not.
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(7),
        )
        self.assertIn("SMALL SAMPLE", text)
        self.assertNotIn("LOW SURVEY EFFORT", text)

    async def test_small_sample_silent_at_threshold(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(10),
        )
        self.assertNotIn("SMALL SAMPLE", text)

    async def test_no_sampling_caveats_for_normal_result(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(50),
        )
        for caveat in (
            "SINGLE-RECORD",
            "SINGLE-OBSERVER",
            "LOW SURVEY EFFORT",
            "SMALL SAMPLE",
            "POSSIBLY TRUNCATED",
            "WINDOW STALENESS",
        ):
            self.assertNotIn(caveat, text, f"unexpected {caveat} in normal result")


# ---- Truncation -----------------------------------------------------------


class TruncationTests(_TimeFrozenTest):
    async def test_fires_when_count_equals_max(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(100),
        )
        self.assertIn("POSSIBLY TRUNCATED", text)
        self.assertIn("maxResults cap (100)", text)

    async def test_silent_below_max(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(50),
        )
        self.assertNotIn("POSSIBLY TRUNCATED", text)


# ---- Absence-of-evidence framing ------------------------------------------


class AbsenceFramingTests(_TimeFrozenTest):
    """The headline eBird pattern: empty results must NOT read like "absent."""

    async def test_species_specific_empty_names_the_species(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations_for_species",
            {"regionCode": "US-NY", "speciesCode": "snowyl1", "back": 14}, [],
        )
        self.assertIn("ABSENCE-OF-EVIDENCE", text)
        self.assertIn("snowyl1", text)
        self.assertIn("opt-in", text)

    async def test_general_empty_includes_effort_framing(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-AK"}, [],
        )
        self.assertIn("ABSENCE-OF-EVIDENCE", text)
        self.assertIn("US-AK", text)
        self.assertIn("opt-in", text)

    async def test_nearby_species_empty_suggests_alternatives(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_nearby_observations_for_species",
            {"lat": 64.0, "lng": -150.0, "speciesCode": "snowyl1"}, [],
        )
        self.assertIn("ABSENCE-OF-EVIDENCE", text)
        # Should point at expanding window/dist or hotspots — actionable hints.
        self.assertIn("get_hotspots", text)


# ---- Notable-is-local -----------------------------------------------------


class NotableIsLocalTests(_TimeFrozenTest):
    async def test_fires_for_notable_region_tool(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_notable_observations", {"regionCode": "US-NY"},
            _fake_obs(20),
        )
        self.assertIn("NOTABLE-IS-LOCAL", text)

    async def test_fires_for_notable_nearby_tool(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_nearby_notable_observations", {"lat": 40.0, "lng": -73.0},
            _fake_obs(20),
        )
        self.assertIn("NOTABLE-IS-LOCAL", text)

    async def test_fires_even_when_empty(self) -> None:
        # Empty notable response could read as "no rare birds anywhere" — fire.
        p = _make_plugin()
        text = await _run(
            p, "get_notable_observations", {"regionCode": "US-NY"}, [],
        )
        self.assertIn("NOTABLE-IS-LOCAL", text)

    async def test_silent_for_general_observation_tools(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(20),
        )
        self.assertNotIn("NOTABLE-IS-LOCAL", text)


# ---- Observation rendering enrichment -------------------------------------


class ObservationRenderingTests(_TimeFrozenTest):
    async def test_species_code_in_brackets(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(1, species_code="amerob"),
        )
        self.assertIn("[amerob]", text)

    async def test_checklist_url(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(1, sub_ids=["S12345"]),
        )
        self.assertIn("Checklist: https://ebird.org/checklist/S12345", text)

    async def test_review_label_confirmed(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(1, obs_reviewed=True, obs_valid=True),
        )
        self.assertIn("Reviewer-confirmed", text)

    async def test_review_label_pending(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(1, obs_reviewed=False),
        )
        self.assertIn("Not yet reviewed", text)

    async def test_review_label_rejected(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(1, obs_reviewed=True, obs_valid=False),
        )
        self.assertIn("Rejected by reviewer", text)

    async def test_observer_hidden_by_default(self) -> None:
        # Privacy default: include_observer_name=False suppresses observer line.
        p = _make_plugin(include_observer_name=False)
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(1, user_display_name="Jane Doe"),
        )
        self.assertNotIn("Jane Doe", text)
        self.assertNotIn("Observer:", text)

    async def test_observer_shown_when_configured(self) -> None:
        p = _make_plugin(include_observer_name=True)
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(1, user_display_name="Jane Doe"),
        )
        self.assertIn("Observer: Jane Doe", text)

    async def test_observer_anonymous_when_no_name(self) -> None:
        p = _make_plugin(include_observer_name=True)
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(1),  # no userDisplayName field
        )
        self.assertIn("Observer: anonymous", text)


# ---- Compact table format for large results --------------------------------


class CompactFormatTests(_TimeFrozenTest):
    """Above _COMPACT_FORMAT_THRESHOLD records, observations render as a
    pipe-delimited table; at or below it, the readable block format stays."""

    async def test_block_format_at_threshold(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(20),
        )
        self.assertIn("Species: American Crow", text)
        self.assertNotIn("compact table", text)

    async def test_compact_format_above_threshold(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(21),
        )
        self.assertIn("21 observations — compact table", text)
        self.assertIn("Species [code] | Date | Count | Location (locId)", text)
        self.assertIn("American Crow [amecro] | 2026-05-12T09:15 | 2", text)
        # Block-format labels must not leak into the table.
        self.assertNotIn("Species: American Crow", text)

    async def test_compact_preserves_provenance_and_caveats(self) -> None:
        p = _make_plugin()
        url = f"{_BASE_URL}/data/obs/US-NY/recent"
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "maxResults": 100},
            _fake_obs(100), url=url, params={"back": 14, "maxResults": 100},
        )
        self.assertTrue(text.startswith(f"Source: {url}"), text[:200])
        self.assertIn("Query: back=14, maxResults=100", text)
        self.assertIn("POSSIBLY TRUNCATED", text)
        self.assertIn("ONE-RECORD-PER-SPECIES", text)
        self.assertRegex(text, r"_Retrieved: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z_$")

    async def test_compact_distance_column_for_nearby(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_nearby_observations",
            {"lat": 40.7128, "lng": -74.0060},
            _fake_obs(25, lat=40.7128, lng=-73.0000),
        )
        self.assertIn("| Km |", text)
        self.assertRegex(text, r"\| \d+\.\d \| confirmed \|")

    async def test_compact_observer_column_when_configured(self) -> None:
        p = _make_plugin(include_observer_name=True)
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(25, user_display_name="Jane Doe"),
        )
        self.assertIn("| Observer", text)
        self.assertIn("| Jane Doe", text)

    async def test_compact_observer_hidden_by_default(self) -> None:
        p = _make_plugin(include_observer_name=False)
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(25, user_display_name="Jane Doe"),
        )
        self.assertNotIn("Jane Doe", text)

    async def test_compact_checklist_ids_with_url_recipe(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(25, sub_ids=[f"S{i}" for i in range(25)]),
        )
        self.assertIn("https://ebird.org/checklist/", text)  # recipe in header
        self.assertIn("| S24", text)

    async def test_compact_taxonomy_legend_when_flagged_records(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(25, com_name="Greater/Lesser Yellowlegs"),
        )
        self.assertIn("⚠️ TAXONOMY", text)
        self.assertIn("do not count them toward species totals", text)

    async def test_compact_no_taxonomy_legend_for_clean_species(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(25),
        )
        self.assertNotIn("⚠️ TAXONOMY", text)

    async def test_hotspots_compact_above_threshold(self) -> None:
        p = _make_plugin()
        hotspots = [
            {
                "locId": f"L{i}",
                "locName": f"Spot {i}",
                "lat": 40.0,
                "lng": -73.0,
                "numSpeciesAllTime": 100 + i,
                "latestObsDt": "2026-05-10 09:00",
            }
            for i in range(30)
        ]
        text = await _run(p, "get_hotspots", {"regionCode": "US-NY"}, hotspots)
        self.assertIn("30 hotspots — compact table", text)
        self.assertIn("Hotspot (locId) | Lat,Lng | Species all-time | Last obs", text)
        self.assertIn("Spot 3 (L3) | 40.0,-73.0 | 103 | 2026-05-10T09:00 (3d ago)", text)

    async def test_hotspots_block_format_at_threshold(self) -> None:
        p = _make_plugin()
        hotspots = [
            {"locId": f"L{i}", "locName": f"Spot {i}", "lat": 40.0, "lng": -73.0}
            for i in range(20)
        ]
        text = await _run(p, "get_hotspots", {"regionCode": "US-NY"}, hotspots)
        self.assertIn("Hotspot: Spot 0", text)
        self.assertNotIn("compact table", text)

    def test_pipe_in_field_values_escaped(self) -> None:
        self.assertEqual(plugin_module._table_cell("a|b\nc"), "a/b c")


# ---- Response byte ceiling ---------------------------------------------------


class SizeCeilingTests(_TimeFrozenTest):
    """The byte backstop truncates at a record boundary with an explicit
    notice — never silently, never mid-record."""

    async def test_ceiling_truncates_with_notice(self) -> None:
        p = _make_plugin()
        with patch.object(plugin_module, "_MAX_BODY_BYTES", 2000):
            text = await _run(
                p, "get_recent_observations", {"regionCode": "US-NY"},
                _fake_obs(100),
            )
        self.assertIn("RESPONSE SIZE CEILING", text)
        self.assertRegex(text, r"showing \d+ of 100 records")
        # Provenance survives truncation.
        self.assertIn("Source:", text)
        self.assertRegex(text, r"_Retrieved: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z_$")

    async def test_ceiling_applies_to_block_format_too(self) -> None:
        p = _make_plugin()
        with patch.object(plugin_module, "_MAX_BODY_BYTES", 600):
            text = await _run(
                p, "get_recent_observations", {"regionCode": "US-NY"},
                _fake_obs(10),  # ≤20 → block format
            )
        self.assertIn("RESPONSE SIZE CEILING", text)

    async def test_no_notice_under_ceiling(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"},
            _fake_obs(100),
        )
        self.assertNotIn("RESPONSE SIZE CEILING", text)

    def test_first_record_always_shown_even_if_oversized(self) -> None:
        # A single block larger than the cap must still render (with no
        # infinite loop and no empty body).
        with patch.object(plugin_module, "_MAX_BODY_BYTES", 10):
            out = plugin_module._join_with_size_cap(["x" * 100], sep="\n")
        self.assertEqual(out, "x" * 100)


# ---- Distance for nearby tools --------------------------------------------


class DistanceTests(_TimeFrozenTest):
    async def test_distance_rendered_for_nearby(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_nearby_observations",
            {"lat": 40.7128, "lng": -74.0060},
            _fake_obs(1, lat=40.7128, lng=-73.0000),  # ~84 km east
        )
        self.assertRegex(text, r"Distance: \d+\.\d km from query point")

    async def test_distance_absent_for_region_tool(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations", {"regionCode": "US-NY"}, _fake_obs(1),
        )
        self.assertNotIn("Distance:", text)


# ---- Taxonomic-category inline flag ---------------------------------------


class TaxonomyFlagTests(unittest.TestCase):
    def test_hybrid_flag(self) -> None:
        out = plugin_module._taxonomy_flag_for("Mallard x American Black Duck (hybrid)")
        self.assertIn("hybrid", out.lower())

    def test_spuh_flag(self) -> None:
        out = plugin_module._taxonomy_flag_for("hawk sp.")
        self.assertIn("spuh", out.lower())

    def test_slash_flag(self) -> None:
        out = plugin_module._taxonomy_flag_for("Greater/Lesser Yellowlegs")
        self.assertIn("slash", out.lower())

    def test_subspecies_flag(self) -> None:
        out = plugin_module._taxonomy_flag_for("Yellow-rumped Warbler (Myrtle)")
        self.assertIn("subspecies", out.lower())

    def test_normal_species_silent(self) -> None:
        self.assertEqual(plugin_module._taxonomy_flag_for("American Robin"), "")

    def test_non_string_silent(self) -> None:
        self.assertEqual(plugin_module._taxonomy_flag_for(None), "")
        self.assertEqual(plugin_module._taxonomy_flag_for(42), "")


# ---- Hotspot formatting ---------------------------------------------------


class HotspotFormattingTests(_TimeFrozenTest):
    async def test_days_ago_rendered(self) -> None:
        p = _make_plugin()
        # latestObsDt 2026-05-10, now 2026-05-13 → 3 days ago
        text = await _run(
            p, "get_hotspots", {"regionCode": "US-NY"},
            [{
                "locId": "L1",
                "locName": "Test",
                "lat": 40.0,
                "lng": -73.0,
                "numSpeciesAllTime": 100,
                "latestObsDt": "2026-05-10 09:00",
            }],
        )
        self.assertIn("3 days ago", text)

    async def test_one_day_ago_singular(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_hotspots", {"regionCode": "US-NY"},
            [{
                "locId": "L1",
                "locName": "Test",
                "lat": 40.0,
                "lng": -73.0,
                "latestObsDt": "2026-05-12 09:00",
            }],
        )
        self.assertIn("1 day ago", text)

    async def test_inactive_hotspot_labeled(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_hotspots", {"regionCode": "US-NY"},
            [{"locId": "L1", "locName": "Empty", "lat": 40, "lng": -73}],
        )
        self.assertIn("inactive hotspot", text)

    async def test_empty_response_has_actionable_hint(self) -> None:
        p = _make_plugin()
        text = await _run(p, "get_hotspots", {"regionCode": "US-NY"}, [])
        self.assertIn("US-NY", text)
        self.assertIn("get_recent_observations", text)

    async def test_distance_rendered_for_nearby_hotspots(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_nearby_hotspots",
            {"lat": 40.7128, "lng": -74.0060},
            [{"locId": "L1", "locName": "X", "lat": 40.7128, "lng": -73.0}],
        )
        self.assertRegex(text, r"Distance: \d+\.\d km from query point")


# ---- Window staleness -----------------------------------------------------


class WindowStalenessTests(_TimeFrozenTest):
    async def test_fires_when_most_recent_old(self) -> None:
        # back=14, latest obs 10 days old → 10 > 14/2 → fires
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "back": 14},
            _fake_obs(20, obs_dt="2026-05-03 09:00"),
        )
        self.assertIn("WINDOW STALENESS", text)
        self.assertIn("10 days old", text)

    async def test_silent_when_fresh(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "back": 14},
            _fake_obs(20, obs_dt="2026-05-12 09:00"),  # 1 day old
        )
        self.assertNotIn("WINDOW STALENESS", text)

    async def test_silent_below_min_days(self) -> None:
        # back=2, obs 1 day old → ratio is high but absolute < 3 → silent
        p = _make_plugin()
        text = await _run(
            p, "get_recent_observations",
            {"regionCode": "US-NY", "back": 2},
            _fake_obs(20, obs_dt="2026-05-12 09:00"),
        )
        self.assertNotIn("WINDOW STALENESS", text)


# ---- Taxonomy_forms ambiguity ---------------------------------------------


class AmbiguityCaveatTests(_TimeFrozenTest):
    async def test_fires_when_multiple_forms(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_taxonomy_forms", {"speciesCode": "amecro"},
            ["amecro", "amecro1", "amecro2"],
            url=f"{_BASE_URL}/ref/taxonomy/forms/amecro",
        )
        self.assertIn("TAXONOMIC AMBIGUITY", text)
        self.assertIn("amecro", text)

    async def test_silent_when_single_form(self) -> None:
        p = _make_plugin()
        text = await _run(
            p, "get_taxonomy_forms", {"speciesCode": "amecro"}, ["amecro"],
            url=f"{_BASE_URL}/ref/taxonomy/forms/amecro",
        )
        self.assertNotIn("TAXONOMIC AMBIGUITY", text)


# ---- Date formatting ------------------------------------------------------


class DateFormattingTests(unittest.TestCase):
    def test_midnight_renders_as_date_only(self) -> None:
        self.assertEqual(plugin_module._format_date("2026-05-13"), "2026-05-13")
        self.assertEqual(plugin_module._format_date("2026-05-13 00:00"), "2026-05-13")

    def test_real_time_renders_full_iso(self) -> None:
        self.assertEqual(
            plugin_module._format_date("2026-05-13 09:15"), "2026-05-13T09:15"
        )

    def test_empty_returns_unknown(self) -> None:
        self.assertEqual(plugin_module._format_date(None), "Unknown")
        self.assertEqual(plugin_module._format_date(""), "Unknown")

    def test_unknown_string_passes_through(self) -> None:
        self.assertEqual(plugin_module._format_date("not a date"), "not a date")


# ---- Best-effort robustness ----------------------------------------------


class CaveatRobustnessTests(unittest.TestCase):
    def test_caveats_silent_on_non_list_data(self) -> None:
        result = plugin_module._build_caveats(
            "get_recent_observations",
            {"garbage": True},
            {},
            _make_plugin().plugin_config,
        )
        self.assertEqual(result, [])

    def test_caveats_silent_on_unknown_tool(self) -> None:
        result = plugin_module._build_caveats(
            "no_such_tool", [1, 2, 3], {}, _make_plugin().plugin_config
        )
        self.assertEqual(result, [])

    def test_distance_silent_on_malformed_coords(self) -> None:
        # Distance rendering must not raise on bad coordinate types.
        block = plugin_module._format_obs_block(
            {
                "comName": "X",
                "subId": "S1",
                "obsValid": True,
                "obsReviewed": True,
                "lat": "bad",
                "lng": "bad",
            },
            include_observer=False,
            query_lat=40.0,
            query_lng=-73.0,
        )
        self.assertNotIn("Distance:", block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
