"""Domain-layer correctness: false zeros, grain, and empty grids.

Three failure shapes, all of which reach the caller as a confident wrong
answer rather than an error:

1. A FALSE ZERO -- an empty result that the caller's own filters produced,
   reported as though it were an absence in the world. The request already
   carries the diagnostic; not reporting it is what makes the zero read as
   absolute.

2. THE WRONG GRAIN -- rows counted when the question was species, or
   checklists, or places. eBird's region endpoints return one row per
   species, the nearby ones return one per observation, and the two look
   identical once rendered.

3. A PAGE OF EMPTY CELLS -- a table whose every non-identifier column is
   blank, which reads as "all of these are dead" when it may only mean
   "this response never carried that column".

Run with::

    python -m unittest tests.test_domain_correctness
"""

import datetime as dt
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from plugins.ebird import plugin as plugin_module
from plugins.ebird.plugin import EBirdPlugin

_FIXED_NOW = dt.datetime(2026, 5, 13, 12, 0, 0)
_BASE = "https://api.ebird.org/v2"


def _obs(**overrides: Any) -> Dict[str, Any]:
    row = {
        "speciesCode": "amerob",
        "comName": "American Robin",
        "sciName": "Turdus migratorius",
        "obsDt": "2026-05-12 09:15",
        "howMany": 2,
        "lat": 61.2,
        "lng": -149.9,
        "locId": "L1",
        "locName": "Somewhere",
        "subId": "S1",
    }
    row.update(overrides)
    return row


async def _run(tool: str, payload: Any, arguments: Optional[Dict] = None) -> str:
    class _Client:
        base_url = _BASE

        def __getattr__(self, name):
            async def _call(**kwargs):
                return payload, f"{_BASE}/{name}", {}

            return _call

        async def aclose(self):  # pragma: no cover
            pass

    plugin = EBirdPlugin({"enabled": True, "api_key": "k"})
    plugin.client = _Client()  # type: ignore[assignment]
    plugin._initialized = True
    with patch.object(plugin_module, "_now_naive_utc", lambda: _FIXED_NOW):
        result = await plugin.execute_tool(tool, arguments or {})
    return result.content[0]["text"]


class FalseZeroTests(unittest.IsolatedAsyncioTestCase):
    """A filtered zero must not read as an absolute one."""

    async def test_hotspot_filter_is_named_on_an_empty_result(self):
        text = await _run(
            "get_recent_observations",
            [],
            {"regionCode": "US-AK", "hotspot": True},
        )
        self.assertIn("NARROWED", text)
        self.assertIn("hotspot=true", text)

    async def test_include_provisional_false_is_named(self):
        """The strongest of these filters and the least obvious.

        Most eBird records are never explicitly reviewed, so excluding
        unreviewed reports can empty an otherwise-populated result.
        """
        text = await _run(
            "get_recent_observations",
            [],
            {"regionCode": "US-AK", "includeProvisional": False},
        )
        self.assertIn("includeProvisional=false", text)
        self.assertIn("MOST eBird records", text)

    async def test_tight_radius_is_named(self):
        text = await _run(
            "get_nearby_observations",
            [],
            {"lat": 61.0, "lng": -150.0, "dist": 2},
        )
        self.assertIn("dist=2", text)

    async def test_unfiltered_empty_result_has_no_narrowing_note(self):
        """No false alarm when the caller narrowed nothing."""
        text = await _run(
            "get_recent_observations", [], {"regionCode": "US-AK"}
        )
        self.assertNotIn("NARROWED", text)

    async def test_default_true_include_provisional_is_not_flagged(self):
        """Only an explicit false narrows anything."""
        text = await _run(
            "get_recent_observations",
            [],
            {"regionCode": "US-AK", "includeProvisional": True},
        )
        self.assertNotIn("NARROWED", text)

    async def test_narrowing_note_reaches_structured_content_too(self):
        """It is part of the ABSENCE_OF_EVIDENCE message, not a text extra."""

        class _Client:
            base_url = _BASE

            def __getattr__(self, name):
                async def _call(**kwargs):
                    return [], f"{_BASE}/{name}", {}

                return _call

            async def aclose(self):  # pragma: no cover
                pass

        plugin = EBirdPlugin({"enabled": True, "api_key": "k"})
        plugin.client = _Client()  # type: ignore[assignment]
        plugin._initialized = True
        result = await plugin.execute_tool(
            "get_recent_observations",
            {"regionCode": "US-AK", "hotspot": True},
        )
        absence = [
            c
            for c in result.structured_content["caveats"]
            if c["code"] == "ABSENCE_OF_EVIDENCE"
        ]
        self.assertTrue(absence)
        self.assertIn("hotspot=true", absence[0]["message"])

    async def test_filters_are_not_announced_when_rows_came_back(self):
        """The note is about explaining a zero, not narrating the query."""
        text = await _run(
            "get_recent_observations",
            [_obs()],
            {"regionCode": "US-AK", "hotspot": True},
        )
        self.assertNotIn("NARROWED", text)


class GrainTests(unittest.IsolatedAsyncioTestCase):
    """Rows, species, checklists and locations are four different answers."""

    async def test_summary_line_leads_the_body(self):
        text = await _run(
            "get_recent_observations",
            [_obs(), _obs(speciesCode="bkcchi", subId="S2", locId="L2")],
            {"regionCode": "US-AK"},
        )
        self.assertIn("2 observations · 2 species · 2 checklists", text)

    async def test_repeated_species_is_called_out_explicitly(self):
        """The overcount this prevents is the whole point."""
        text = await _run(
            "get_recent_observations",
            [_obs(), _obs(subId="S2"), _obs(subId="S3")],
            {"regionCode": "US-AK"},
        )
        self.assertIn("3 observations · 1 species", text)
        self.assertIn("overstate the species total", text)

    async def test_no_discrepancy_note_when_counts_agree(self):
        text = await _run(
            "get_recent_observations",
            [_obs(), _obs(speciesCode="bkcchi", subId="S2")],
            {"regionCode": "US-AK"},
        )
        self.assertNotIn("overstate the species total", text)

    async def test_singulars_are_not_mangled(self):
        text = await _run(
            "get_recent_observations", [_obs()], {"regionCode": "US-AK"}
        )
        self.assertIn("1 observation · 1 species · 1 checklist · 1 location", text)

    async def test_grain_line_also_precedes_the_compact_table(self):
        rows = [_obs(subId=f"S{i}") for i in range(25)]
        text = await _run(
            "get_recent_observations", rows, {"regionCode": "US-AK"}
        )
        self.assertIn("25 observations · 1 species", text)
        self.assertIn("compact table", text)
        self.assertLess(
            text.index("25 observations"),
            text.index("compact table"),
            "the counts must lead, not trail the data they qualify",
        )

    async def test_text_grain_matches_structured_summary(self):
        """One truth, two renderings."""

        class _Client:
            base_url = _BASE

            def __getattr__(self, name):
                async def _call(**kwargs):
                    return (
                        [_obs(), _obs(subId="S2"), _obs(speciesCode="bkcchi")],
                        f"{_BASE}/{name}",
                        {},
                    )

                return _call

            async def aclose(self):  # pragma: no cover
                pass

        plugin = EBirdPlugin({"enabled": True, "api_key": "k"})
        plugin.client = _Client()  # type: ignore[assignment]
        plugin._initialized = True
        result = await plugin.execute_tool(
            "get_recent_observations", {"regionCode": "US-AK"}
        )
        summary = result.structured_content["summary"]
        text = result.content[0]["text"]
        self.assertIn(
            f"{summary['returned']} observations · "
            f"{summary['distinct_species']} species · "
            f"{summary['distinct_checklists']} checklists",
            text,
        )


class EmptyGridTests(unittest.IsolatedAsyncioTestCase):
    """A page of blank cells gets summarised, without inventing a cause."""

    def _bare(self, n: int) -> List[Dict[str, Any]]:
        # What _parse_hotspot_text recovers from eBird's CSV fallback:
        # identifiers and coordinates, nothing else.
        return [
            {"locId": f"L{i}", "locName": f"Spot {i}", "lat": 61.0, "lng": -149.0}
            for i in range(n)
        ]

    async def test_page_of_blank_rows_is_summarised(self):
        text = await _run(
            "get_hotspots", self._bare(50), {"regionCode": "US-AK"}
        )
        self.assertIn("NO ACTIVITY DATA", text)
        self.assertNotIn("none (inactive)", text)

    async def test_summary_does_not_assert_the_hotspots_are_dormant(self):
        """The false finding this replaces would be its own false finding."""
        text = await _run(
            "get_hotspots", self._bare(50), {"regionCode": "US-AK"}
        )
        self.assertIn("cannot tell them apart", text)
        self.assertIn("Do NOT report them as dormant", text)
        self.assertIn("get_recent_observations", text)

    async def test_a_few_blank_rows_still_render_normally(self):
        """One or two empty rows is not a page of them."""
        text = await _run(
            "get_hotspots", self._bare(3), {"regionCode": "US-AK"}
        )
        self.assertNotIn("NO ACTIVITY DATA", text)
        self.assertIn("inactive hotspot", text)

    async def test_any_real_data_disables_the_summary(self):
        rows = self._bare(50)
        rows[7]["latestObsDt"] = "2026-05-12 09:15"
        text = await _run("get_hotspots", rows, {"regionCode": "US-AK"})
        self.assertNotIn("NO ACTIVITY DATA", text)

    async def test_structured_rows_are_unaffected_by_the_summary(self):
        """The text summarises; the machine channel still carries all rows."""

        rows = self._bare(50)

        class _Client:
            base_url = _BASE

            def __getattr__(self, name):
                async def _call(**kwargs):
                    return rows, f"{_BASE}/{name}", {}

                return _call

            async def aclose(self):  # pragma: no cover
                pass

        plugin = EBirdPlugin({"enabled": True, "api_key": "k"})
        plugin.client = _Client()  # type: ignore[assignment]
        plugin._initialized = True
        result = await plugin.execute_tool(
            "get_hotspots", {"regionCode": "US-AK"}
        )
        self.assertEqual(len(result.structured_content["rows"]), 50)
        self.assertEqual(
            result.structured_content["summary"]["active_hotspots"], 0
        )


class DaysAgoPrecisionTests(unittest.TestCase):
    """The +/- 1 day is named, not silently presented as exact."""

    def test_days_ago_is_documented_as_approximate(self):
        doc = plugin_module._days_ago.__doc__ or ""
        self.assertIn("+/- 1 day", doc)
        self.assertIn("LOCAL", doc)

    def test_days_ago_still_computes(self):
        with patch.object(plugin_module, "_now_naive_utc", lambda: _FIXED_NOW):
            self.assertEqual(plugin_module._days_ago("2026-05-12 09:15"), 1)
            self.assertEqual(plugin_module._days_ago("2026-05-13 09:15"), 0)
            self.assertIsNone(plugin_module._days_ago("not a date"))


if __name__ == "__main__":
    unittest.main()
