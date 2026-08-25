"""Structured output: the declared schema is binding, so validate it.

The MCP spec says a server that declares an ``outputSchema`` MUST return
conforming structured results and clients SHOULD validate them. Declaring
one is therefore a commitment, and these tests hold the server to it by
running real tool output through ``jsonschema`` on every awkward branch --
not just the happy path, which is where the sibling fork's zero-result bug
hid.

The branches exercised here, each of which eBird produces constantly:

  * zero rows (out-of-season region, short `back`, rare species)
  * howMany null -- eBird's "X" for present-but-uncounted
  * the maxResults cap being hit, so the true total is unknown
  * sparse rows where eBird simply omits fields
  * an unparseable obsDt
  * the text byte-backstop clipping the rendering but not the rows

Run with::

    python -m unittest tests.test_structured_output
"""

import datetime as dt
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from jsonschema import Draft202012Validator

from plugins.ebird import plugin as plugin_module
from plugins.ebird.plugin import TOOL_OUTPUT_SCHEMAS, EBirdPlugin
from plugins.ebird.schemas import (
    ALL_CAVEAT_CODES,
    CAVEAT_ABSENCE_OF_EVIDENCE,
    CAVEAT_COUNT_NOT_REPORTED,
    CAVEAT_NON_SPECIES_TAXA,
    CAVEAT_POSSIBLY_TRUNCATED,
    CAVEAT_RESPONSE_SIZE_CEILING,
    CAVEAT_ROWS_TRUNCATED,
    CAVEAT_UNCOMPARABLE_SPECIES_TOTALS,
    HOTSPOTS_SCHEMA,
    OBSERVATIONS_SCHEMA,
    TAXONOMY_FORMS_SCHEMA,
    TAXONOMY_SCHEMA,
)

_FIXED_NOW = dt.datetime(2026, 5, 13, 12, 0, 0)
_BASE = "https://api.ebird.org/v2"


def _obs(**overrides: Any) -> Dict[str, Any]:
    row = {
        "speciesCode": "amerob",
        "comName": "American Robin",
        "sciName": "Turdus migratorius",
        "obsDt": "2026-05-12 09:15",
        "howMany": 3,
        "lat": 61.2,
        "lng": -149.9,
        "locId": "L99381",
        "locName": "Westchester Lagoon",
        "obsValid": True,
        "obsReviewed": False,
        "subId": "S12345678",
    }
    row.update(overrides)
    return row


def _hotspot(**overrides: Any) -> Dict[str, Any]:
    row = {
        "locId": "L99381",
        "locName": "Westchester Lagoon",
        "lat": 61.2,
        "lng": -149.9,
        "numSpeciesAllTime": 210,
        "latestObsDt": "2026-05-12 09:15",
    }
    row.update(overrides)
    return row


class _Harness:
    """Plugin wired to a client that returns whatever the test supplies."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def plugin(self) -> EBirdPlugin:
        payload = self.payload

        class _Client:
            base_url = _BASE

            def __getattr__(self, name: str):
                async def _call(**kwargs):
                    return payload, f"{_BASE}/{name}", {"echo": "params"}

                return _call

            async def aclose(self):  # pragma: no cover
                pass

        p = EBirdPlugin({"enabled": True, "api_key": "test-key"})
        p.client = _Client()  # type: ignore[assignment]
        p._initialized = True
        return p


async def _run(
    tool: str, payload: Any, arguments: Optional[Dict[str, Any]] = None
):
    plugin = _Harness(payload).plugin()
    with patch.object(plugin_module, "_now_naive_utc", lambda: _FIXED_NOW):
        return await plugin.execute_tool(tool, arguments or {})


def _validate(schema: Dict[str, Any], payload: Any) -> None:
    """Raise with every violation, not just the first."""
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda e: list(e.path),
    )
    if errors:
        detail = "\n".join(
            f"  {list(e.path)}: {e.message}" for e in errors
        )
        raise AssertionError(f"structuredContent violates its schema:\n{detail}")


class SchemaDeclarationTests(unittest.TestCase):
    """The declared schemas must themselves be valid, and consistent."""

    def test_declared_schemas_are_valid_json_schema(self):
        for tool, schema in TOOL_OUTPUT_SCHEMAS.items():
            with self.subTest(tool=tool):
                Draft202012Validator.check_schema(schema)

    def test_no_maximum_constraints_anywhere(self):
        """A `maximum` real data can exceed makes the server violate itself.

        eBird counts, species totals and distances have no principled upper
        bound we can assert, so none is declared.
        """
        def walk(node, path=""):
            found = []
            if isinstance(node, dict):
                if "maximum" in node:
                    found.append(path or "<root>")
                for k, v in node.items():
                    found += walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    found += walk(v, f"{path}[{i}]")
            return found

        for tool, schema in TOOL_OUTPUT_SCHEMAS.items():
            with self.subTest(tool=tool):
                self.assertEqual(walk(schema), [])

    def test_every_tool_with_a_schema_is_dispatchable(self):
        """A schema with no builder would advertise a promise nothing keeps."""
        tools = {
            t.name
            for t in EBirdPlugin(
                {"enabled": True, "api_key": "k"}
            ).get_tools()
        }
        self.assertTrue(set(TOOL_OUTPUT_SCHEMAS) <= tools)

    def test_caveat_enum_matches_the_code_constants(self):
        enum = set(
            OBSERVATIONS_SCHEMA["properties"]["caveats"]["items"][
                "properties"
            ]["code"]["enum"]
        )
        self.assertEqual(enum, set(ALL_CAVEAT_CODES))


class ObservationStructureTests(unittest.IsolatedAsyncioTestCase):
    async def test_populated_result_conforms(self):
        result = await _run(
            "get_recent_observations", [_obs(), _obs(speciesCode="bkcchi")],
            {"regionCode": "US-AK"},
        )
        _validate(OBSERVATIONS_SCHEMA, result.structured_content)
        self.assertEqual(result.structured_content["summary"]["returned"], 2)

    async def test_rows_carry_raw_ebird_fields(self):
        result = await _run(
            "get_recent_observations", [_obs()], {"regionCode": "US-AK"}
        )
        row = result.structured_content["rows"][0]
        for field in (
            "speciesCode", "sciName", "comName", "obsDt", "howMany",
            "lat", "lng", "locId", "locName", "obsValid", "obsReviewed",
            "subId",
        ):
            self.assertIn(field, row, f"{field} missing from structured row")
        self.assertEqual(row["speciesCode"], "amerob")
        self.assertEqual(row["subId"], "S12345678")

    async def test_obsDt_keeps_ebirds_own_form_and_ships_ours_alongside(self):
        result = await _run(
            "get_recent_observations", [_obs()], {"regionCode": "US-AK"}
        )
        row = result.structured_content["rows"][0]
        self.assertEqual(
            row["obsDt"],
            "2026-05-12 09:15",
            "obsDt must be eBird's own string, not normalized in place",
        )
        self.assertEqual(row["obsDtIso"], "2026-05-12T09:15:00")

    async def test_unparseable_obsDt_yields_null_iso_not_an_error(self):
        result = await _run(
            "get_recent_observations",
            [_obs(obsDt="sometime last spring")],
            {"regionCode": "US-AK"},
        )
        _validate(OBSERVATIONS_SCHEMA, result.structured_content)
        row = result.structured_content["rows"][0]
        self.assertEqual(row["obsDt"], "sometime last spring")
        self.assertIsNone(row["obsDtIso"])

    async def test_sparse_upstream_row_still_conforms(self):
        """eBird omits fields across endpoints and detail levels."""
        result = await _run(
            "get_recent_observations",
            [{"speciesCode": "amerob", "obsDt": "2026-05-12 09:15"}],
            {"regionCode": "US-AK"},
        )
        _validate(OBSERVATIONS_SCHEMA, result.structured_content)

    async def test_distance_present_on_nearby_null_on_region(self):
        nearby = await _run(
            "get_nearby_observations", [_obs()], {"lat": 61.0, "lng": -150.0}
        )
        _validate(OBSERVATIONS_SCHEMA, nearby.structured_content)
        self.assertIsNotNone(nearby.structured_content["rows"][0]["distance_km"])

        region = await _run(
            "get_recent_observations", [_obs()], {"regionCode": "US-AK"}
        )
        self.assertIsNone(region.structured_content["rows"][0]["distance_km"])


class NullCountTests(unittest.IsolatedAsyncioTestCase):
    """howMany null means PRESENT, not zero — the most misread field here."""

    async def test_null_how_many_survives_as_null(self):
        result = await _run(
            "get_recent_observations",
            [_obs(howMany=None)],
            {"regionCode": "US-AK"},
        )
        _validate(OBSERVATIONS_SCHEMA, result.structured_content)
        self.assertIsNone(result.structured_content["rows"][0]["howMany"])

    async def test_null_how_many_is_not_coerced_to_zero(self):
        result = await _run(
            "get_recent_observations",
            [_obs(howMany=None)],
            {"regionCode": "US-AK"},
        )
        self.assertNotEqual(result.structured_content["rows"][0]["howMany"], 0)

    async def test_null_counts_are_counted_and_caveated(self):
        result = await _run(
            "get_recent_observations",
            [_obs(howMany=None), _obs(howMany=2), _obs(howMany=None)],
            {"regionCode": "US-AK"},
        )
        summary = result.structured_content["summary"]
        self.assertEqual(summary["counts_not_reported"], 2)
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertIn(CAVEAT_COUNT_NOT_REPORTED, codes)

    async def test_no_caveat_when_every_count_is_reported(self):
        result = await _run(
            "get_recent_observations",
            [_obs(howMany=1), _obs(howMany=2)],
            {"regionCode": "US-AK"},
        )
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertNotIn(CAVEAT_COUNT_NOT_REPORTED, codes)


class TotalCountHonestyTests(unittest.IsolatedAsyncioTestCase):
    """null and 0 are different claims and must not be conflated."""

    async def test_zero_rows_reports_zero_not_null(self):
        result = await _run(
            "get_recent_observations", [], {"regionCode": "US-AK"}
        )
        summary = result.structured_content["summary"]
        self.assertEqual(
            summary["total_count"],
            0,
            "Zero matches is a known, complete count. null would say "
            "'unmeasured', which is a weaker and different claim.",
        )
        self.assertEqual(summary["returned"], 0)
        self.assertIs(summary["truncated"], False)

    async def test_short_result_reports_a_real_total(self):
        result = await _run(
            "get_recent_observations",
            [_obs(), _obs()],
            {"regionCode": "US-AK", "maxResults": 100},
        )
        self.assertEqual(result.structured_content["summary"]["total_count"], 2)

    async def test_cap_hit_reports_null_total(self):
        rows = [_obs(subId=f"S{i}") for i in range(5)]
        result = await _run(
            "get_recent_observations",
            rows,
            {"regionCode": "US-AK", "maxResults": 5},
        )
        _validate(OBSERVATIONS_SCHEMA, result.structured_content)
        self.assertIsNone(
            result.structured_content["summary"]["total_count"],
            "At the cap the true total is unknown; reporting `returned` "
            "would dress a capped sample up as a complete census.",
        )
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertIn(CAVEAT_POSSIBLY_TRUNCATED, codes)


class GrainTests(unittest.IsolatedAsyncioTestCase):
    """Rows, species, checklists and locations are different questions."""

    async def test_distinct_counts_differ_from_row_count(self):
        rows = [
            _obs(speciesCode="amerob", subId="S1", locId="L1"),
            _obs(speciesCode="amerob", subId="S1", locId="L1"),
            _obs(speciesCode="bkcchi", subId="S2", locId="L2"),
        ]
        result = await _run(
            "get_recent_observations", rows, {"regionCode": "US-AK"}
        )
        summary = result.structured_content["summary"]
        self.assertEqual(summary["returned"], 3)
        self.assertEqual(summary["distinct_species"], 2)
        self.assertEqual(summary["distinct_checklists"], 2)
        self.assertEqual(summary["distinct_locations"], 2)


class EmptyResultTests(unittest.IsolatedAsyncioTestCase):
    """C7: the zero-result path is where advertised schemas get broken."""

    OBSERVATION_TOOLS = [
        ("get_recent_observations", {"regionCode": "US-AK"}),
        ("get_recent_observations_for_species",
         {"regionCode": "US-AK", "speciesCode": "amerob"}),
        ("get_notable_observations", {"regionCode": "US-AK"}),
        ("get_nearby_observations", {"lat": 61.0, "lng": -150.0}),
        ("get_nearby_notable_observations", {"lat": 61.0, "lng": -150.0}),
        ("get_nearby_observations_for_species",
         {"lat": 61.0, "lng": -150.0, "speciesCode": "amerob"}),
    ]

    async def test_every_observation_tool_emits_structured_on_empty(self):
        for tool, args in self.OBSERVATION_TOOLS:
            with self.subTest(tool=tool):
                result = await _run(tool, [], args)
                self.assertIsNotNone(
                    result.structured_content,
                    f"{tool} advertises an outputSchema but returned no "
                    "structuredContent on a zero-result query",
                )
                _validate(OBSERVATIONS_SCHEMA, result.structured_content)
                self.assertEqual(result.structured_content["rows"], [])

    async def test_hotspot_tools_emit_structured_on_empty(self):
        for tool, args in (
            ("get_hotspots", {"regionCode": "US-AK"}),
            ("get_nearby_hotspots", {"lat": 61.0, "lng": -150.0}),
        ):
            with self.subTest(tool=tool):
                result = await _run(tool, [], args)
                self.assertIsNotNone(result.structured_content)
                _validate(HOTSPOTS_SCHEMA, result.structured_content)

    async def test_absence_of_evidence_is_coded_not_only_prose(self):
        result = await _run(
            "get_recent_observations_for_species",
            [],
            {"regionCode": "US-AK", "speciesCode": "amerob"},
        )
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertIn(CAVEAT_ABSENCE_OF_EVIDENCE, codes)

    async def test_absence_prose_still_appears_once_in_the_text(self):
        """It lives in the body; it must not be rendered twice."""
        result = await _run(
            "get_recent_observations_for_species",
            [],
            {"regionCode": "US-AK", "speciesCode": "amerob"},
        )
        text = result.content[0]["text"]
        self.assertEqual(text.count("⚠️ ABSENCE-OF-EVIDENCE"), 1)


class HotspotStructureTests(unittest.IsolatedAsyncioTestCase):
    async def test_populated_hotspots_conform(self):
        result = await _run(
            "get_hotspots", [_hotspot(), _hotspot(locId="L2")],
            {"regionCode": "US-AK"},
        )
        _validate(HOTSPOTS_SCHEMA, result.structured_content)

    async def test_inactive_hotspot_reports_null_not_a_fake_date(self):
        result = await _run(
            "get_hotspots",
            [_hotspot(latestObsDt=None), _hotspot()],
            {"regionCode": "US-AK"},
        )
        _validate(HOTSPOTS_SCHEMA, result.structured_content)
        rows = result.structured_content["rows"]
        self.assertIsNone(rows[0]["latestObsDt"])
        self.assertIsNone(rows[0]["days_since_last_obs"])
        self.assertEqual(result.structured_content["summary"]["active_hotspots"], 1)

    async def test_days_since_last_obs_is_computed(self):
        result = await _run(
            "get_hotspots", [_hotspot()], {"regionCode": "US-AK"}
        )
        self.assertEqual(
            result.structured_content["rows"][0]["days_since_last_obs"], 1
        )

    async def test_uncomparable_totals_caveat_fires(self):
        result = await _run(
            "get_hotspots", [_hotspot()], {"regionCode": "US-AK"}
        )
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertIn(CAVEAT_UNCOMPARABLE_SPECIES_TOTALS, codes)

    async def test_hotspot_total_count_is_never_null(self):
        """These endpoints take no cap, so the set is always complete."""
        result = await _run(
            "get_hotspots", [_hotspot()], {"regionCode": "US-AK"}
        )
        self.assertEqual(result.structured_content["summary"]["total_count"], 1)


class CaveatChannelParityTests(unittest.IsolatedAsyncioTestCase):
    """The two channels come from one list and must not drift."""

    async def test_every_structured_message_appears_in_the_text(self):
        rows = [_obs(howMany=None, subId="S1")]
        result = await _run(
            "get_recent_observations", rows, {"regionCode": "US-AK"}
        )
        text = result.content[0]["text"]
        for caveat in result.structured_content["caveats"]:
            with self.subTest(code=caveat["code"]):
                self.assertIn(
                    caveat["message"],
                    text,
                    f"{caveat['code']} is in structuredContent but its "
                    "wording is absent from the text block — the channels "
                    "have drifted.",
                )

    async def test_internal_render_flag_never_leaks_to_the_wire(self):
        result = await _run(
            "get_recent_observations", [], {"regionCode": "US-AK"}
        )
        for caveat in result.structured_content["caveats"]:
            self.assertEqual(set(caveat), {"code", "message"})

    async def test_all_emitted_codes_are_declared(self):
        rows = [_obs(howMany=None)]
        for tool, args in (
            ("get_recent_observations", {"regionCode": "US-AK"}),
            ("get_notable_observations", {"regionCode": "US-AK"}),
            ("get_nearby_observations", {"lat": 61.0, "lng": -150.0}),
            ("get_hotspots", {"regionCode": "US-AK"}),
        ):
            payload = [_hotspot()] if "hotspot" in tool else rows
            result = await _run(tool, payload, args)
            for caveat in result.structured_content["caveats"]:
                with self.subTest(tool=tool, code=caveat["code"]):
                    self.assertIn(caveat["code"], ALL_CAVEAT_CODES)


class TextClippingTests(unittest.IsolatedAsyncioTestCase):
    """The text may be clipped; the machine-readable channel never is."""

    def _bulky_rows(self, n: int) -> List[Dict[str, Any]]:
        # Long location names push the rendering past the byte backstop
        # without needing an implausible record count.
        return [
            _obs(subId=f"S{i}", locName="Very Long Location Name " * 40)
            for i in range(n)
        ]

    async def test_structured_rows_are_complete_when_text_is_clipped(self):
        rows = self._bulky_rows(500)
        result = await _run(
            "get_recent_observations",
            rows,
            {"regionCode": "US-AK", "maxResults": 1000},
        )
        text = result.content[0]["text"]
        self.assertIn(
            "RESPONSE SIZE CEILING",
            text,
            "fixture did not actually trip the text backstop",
        )
        self.assertEqual(
            len(result.structured_content["rows"]),
            500,
            "structuredContent must carry every row even when the text "
            "rendering was clipped for readability",
        )
        self.assertIs(result.structured_content["summary"]["truncated"], False)

    async def test_clipping_the_text_raises_a_coded_caveat(self):
        result = await _run(
            "get_recent_observations",
            self._bulky_rows(500),
            {"regionCode": "US-AK", "maxResults": 1000},
        )
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertIn(CAVEAT_RESPONSE_SIZE_CEILING, codes)

    async def test_compact_rendering_does_not_reduce_structured_rows(self):
        """Above the compact threshold the text is a table; rows are not."""
        rows = [_obs(subId=f"S{i}") for i in range(25)]
        result = await _run(
            "get_recent_observations", rows, {"regionCode": "US-AK"}
        )
        self.assertIn("compact table", result.content[0]["text"])
        self.assertEqual(len(result.structured_content["rows"]), 25)


def _taxon(**overrides: Any) -> Dict[str, Any]:
    entry = {
        "speciesCode": "amerob",
        "comName": "American Robin",
        "sciName": "Turdus migratorius",
        "category": "species",
        "order": "Passeriformes",
        "familyComName": "Thrushes and Allies",
        "familySciName": "Turdidae",
        "bandingCodes": ["AMRO"],
        # Internal fields that must NOT be re-exported.
        "taxonOrder": 28150.0,
        "familyCode": "turdid1",
        "comNameCodes": ["AMRO"],
        "sciNameCodes": ["TUMI"],
    }
    entry.update(overrides)
    return entry


class TaxonomyStructureTests(unittest.IsolatedAsyncioTestCase):
    async def test_populated_taxonomy_conforms(self):
        result = await _run("get_taxonomy", [_taxon(), _taxon(speciesCode="bkcchi")])
        _validate(TAXONOMY_SCHEMA, result.structured_content)
        self.assertEqual(result.structured_content["summary"]["returned"], 2)

    async def test_empty_taxonomy_conforms(self):
        result = await _run("get_taxonomy", [])
        self.assertIsNotNone(result.structured_content)
        _validate(TAXONOMY_SCHEMA, result.structured_content)
        self.assertEqual(result.structured_content["summary"]["total_count"], 0)

    async def test_internal_ordering_keys_are_not_re_exported(self):
        """A field invites use; taxonOrder is renumbered every year."""
        result = await _run("get_taxonomy", [_taxon()])
        row = result.structured_content["rows"][0]
        for internal in ("taxonOrder", "familyCode", "comNameCodes", "sciNameCodes"):
            self.assertNotIn(internal, row)

    async def test_banding_codes_are_exposed_as_a_distinct_field(self):
        """Kept precisely because they are NOT speciesCodes."""
        result = await _run("get_taxonomy", [_taxon()])
        row = result.structured_content["rows"][0]
        self.assertEqual(row["bandingCodes"], ["AMRO"])
        self.assertEqual(row["speciesCode"], "amerob")

    async def test_category_breakdown_is_reported(self):
        result = await _run(
            "get_taxonomy",
            [_taxon(), _taxon(category="hybrid"), _taxon(category="hybrid")],
        )
        self.assertEqual(
            result.structured_content["summary"]["categories"],
            {"species": 1, "hybrid": 2},
        )

    async def test_non_species_taxa_raise_a_coded_caveat(self):
        result = await _run(
            "get_taxonomy", [_taxon(), _taxon(category="spuh")]
        )
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertIn(CAVEAT_NON_SPECIES_TAXA, codes)

    async def test_pure_species_result_raises_no_such_caveat(self):
        result = await _run("get_taxonomy", [_taxon(), _taxon()])
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertNotIn(CAVEAT_NON_SPECIES_TAXA, codes)


class TaxonomyTruncationTests(unittest.IsolatedAsyncioTestCase):
    """The one deliberately-incomplete structured channel must say so."""

    def _many(self, n: int) -> List[Dict[str, Any]]:
        return [_taxon(speciesCode=f"sp{i:05d}") for i in range(n)]

    async def test_rows_are_capped(self):
        result = await _run("get_taxonomy", self._many(2500))
        _validate(TAXONOMY_SCHEMA, result.structured_content)
        self.assertEqual(
            len(result.structured_content["rows"]),
            plugin_module._MAX_STRUCTURED_TAXONOMY_ROWS,
        )

    async def test_truncation_reports_the_true_total_not_the_capped_one(self):
        result = await _run("get_taxonomy", self._many(2500))
        summary = result.structured_content["summary"]
        self.assertEqual(
            summary["total_count"],
            2500,
            "The bundle is local, so the real total is knowable. "
            "Truncating rows must not make the count a lie.",
        )
        self.assertIs(summary["truncated"], True)
        self.assertEqual(
            summary["returned"],
            plugin_module._MAX_STRUCTURED_TAXONOMY_ROWS,
        )

    async def test_truncation_raises_a_coded_caveat(self):
        result = await _run("get_taxonomy", self._many(2500))
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertIn(
            CAVEAT_ROWS_TRUNCATED,
            codes,
            "Rows were dropped from the machine-readable channel; that "
            "must never be silent.",
        )

    async def test_under_the_cap_is_not_marked_truncated(self):
        result = await _run("get_taxonomy", self._many(50))
        summary = result.structured_content["summary"]
        self.assertIs(summary["truncated"], False)
        self.assertEqual(summary["total_count"], 50)
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertNotIn(CAVEAT_ROWS_TRUNCATED, codes)

    async def test_real_bundle_scale_stays_within_the_payload_budget(self):
        """The reason the cap exists, checked against real data.

        Skipped when the deploy-time bundle is absent (it is gitignored).
        """
        import json
        from pathlib import Path

        bundle = (
            Path(plugin_module.__file__).parent / "data" / "taxonomy.json"
        )
        if not bundle.exists():
            self.skipTest("taxonomy bundle not built locally")
        entries = json.loads(bundle.read_text(encoding="utf-8"))
        species = [e for e in entries if e.get("category") == "species"]
        result = await _run("get_taxonomy", species)
        payload = result.structured_content
        _validate(TAXONOMY_SCHEMA, payload)
        self.assertEqual(payload["summary"]["total_count"], len(species))
        self.assertIs(payload["summary"]["truncated"], True)
        size_mb = len(json.dumps(payload)) / 1024 / 1024
        self.assertLess(
            size_mb,
            1.0,
            f"structured taxonomy payload is {size_mb:.2f} MB; the cap "
            "exists to keep this well under Lambda's 6 MB response limit",
        )


class TaxonomyFormsTests(unittest.IsolatedAsyncioTestCase):
    async def test_forms_conform_and_are_complete(self):
        result = await _run(
            "get_taxonomy_forms", ["yerwar", "yerwar1", "yerwar2"],
            {"speciesCode": "yerwar"},
        )
        _validate(TAXONOMY_FORMS_SCHEMA, result.structured_content)
        summary = result.structured_content["summary"]
        self.assertEqual(summary["returned"], 3)
        self.assertEqual(summary["total_count"], 3)
        self.assertIs(summary["truncated"], False)

    async def test_form_codes_become_rows(self):
        result = await _run(
            "get_taxonomy_forms", ["yerwar", "yerwar1"],
            {"speciesCode": "yerwar"},
        )
        self.assertEqual(
            [r["speciesCode"] for r in result.structured_content["rows"]],
            ["yerwar", "yerwar1"],
        )

    async def test_multiple_forms_raise_the_ambiguity_caveat(self):
        result = await _run(
            "get_taxonomy_forms", ["yerwar", "yerwar1"],
            {"speciesCode": "yerwar"},
        )
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertIn("TAXONOMIC_AMBIGUITY", codes)

    async def test_single_form_conforms_with_no_ambiguity_caveat(self):
        result = await _run(
            "get_taxonomy_forms", ["amerob"], {"speciesCode": "amerob"}
        )
        _validate(TAXONOMY_FORMS_SCHEMA, result.structured_content)
        codes = {c["code"] for c in result.structured_content["caveats"]}
        self.assertNotIn("TAXONOMIC_AMBIGUITY", codes)

    async def test_no_forms_still_emits_structured_content(self):
        result = await _run(
            "get_taxonomy_forms", [], {"speciesCode": "amerob"}
        )
        self.assertIsNotNone(result.structured_content)
        _validate(TAXONOMY_FORMS_SCHEMA, result.structured_content)
        self.assertEqual(result.structured_content["rows"], [])


class ToolsListWiringTests(unittest.TestCase):
    def test_output_schema_emitted_in_tools_list(self):
        from core.plugin_manager import PluginManager

        manager = PluginManager({})
        manager.plugins = {
            "ebird": EBirdPlugin({"enabled": True, "api_key": "k"})
        }
        emitted = {t["name"]: t for t in manager.get_all_tools()}
        self.assertIn(
            "outputSchema", emitted["ebird__get_recent_observations"]
        )

    def test_every_ebird_tool_declares_a_schema(self):
        """All ten are converted; none is left returning bare prose."""
        tools = EBirdPlugin({"enabled": True, "api_key": "k"}).get_tools()
        missing = [t.name for t in tools if not t.output_schema]
        self.assertEqual(missing, [], f"no outputSchema on: {missing}")

    def test_tools_without_a_schema_omit_the_field(self):
        """The plumbing must stay opt-in for other plugins.

        No eBird tool exercises this any more — all ten declare a schema —
        so it is checked against a synthetic plugin rather than deleted.
        A tool that declares nothing must emit no `outputSchema` key at
        all, not `outputSchema: null`.
        """
        from core.interfaces import MCPPlugin, PluginType, ToolDefinition
        from core.plugin_manager import PluginManager

        class _Bare(MCPPlugin):
            plugin_name = "bare"
            plugin_type = PluginType.CUSTOM_API

            async def initialize(self):
                return True

            async def shutdown(self):
                return None

            async def health_check(self):
                return True

            def get_tools(self):
                return [
                    ToolDefinition(
                        name="plain",
                        description="",
                        input_schema={"type": "object"},
                    )
                ]

            async def execute_tool(self, tool_name, arguments):
                return None

        manager = PluginManager({})
        manager.plugins = {"bare": _Bare({})}
        emitted = {t["name"]: t for t in manager.get_all_tools()}
        self.assertNotIn("outputSchema", emitted["bare__plain"])


if __name__ == "__main__":
    unittest.main()
