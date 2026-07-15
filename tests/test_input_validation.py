"""Hardening tests for argument validation in the eBird plugin.

``regionCode`` and ``speciesCode`` are interpolated directly into the
upstream URL path (e.g. ``/data/obs/{regionCode}/recent``), so the format
validators in ``_clamp_and_validate`` are the path-traversal defense. These
tests pin that defense down explicitly — a live probe could not confirm it
from the outside because the API Gateway mangles slash-bearing values before
they reach the Lambda, and a gateway quirk is not a security control.

Two properties are asserted:

1. The validator itself rejects traversal-shaped values, with its own
   error message (not a gateway artifact).
2. The rejection happens in ``execute_tool`` BEFORE any URL construction —
   ``_dispatch`` (the only route to the HTTP client) is never reached.

Run with::

    python -m unittest tests.test_input_validation
"""

import unittest
from typing import Any, Dict, List
from unittest.mock import patch

from plugins.ebird.plugin import EBirdPlugin, _clamp_and_validate


def _make_plugin() -> EBirdPlugin:
    """Build a plugin without touching the real eBird API."""
    p = EBirdPlugin({"enabled": True, "api_key": "test-key"})

    class _StubClient:
        base_url = "https://api.ebird.org/v2"

        async def aclose(self) -> None:  # pragma: no cover
            pass

    p.client = _StubClient()  # type: ignore[assignment]
    p._initialized = True
    return p


# ---- regionCode format validator --------------------------------------------


class RegionCodeValidatorTests(unittest.TestCase):
    """Direct tests of _clamp_and_validate's regionCode gate."""

    VALID = [
        "US",              # country
        "US-AK",           # subnational1
        "US-AK-020",       # subnational2
        "L64746143",       # hotspot location ID
        "MX-ROO",          # non-US subnational1
    ]

    REJECTED = [
        "NOT-A-REGION",                                        # wrong shape
        "US-AK-020/../../US-NY",                               # path traversal
        "US-AK-020/../../../ref/region/list/subnational1/US",  # endpoint escape
        "US-AK-020%2F..%2F..%2FUS-NY",                         # URL-encoded traversal
        "",                                                    # empty
        "../",                                                 # bare traversal
        ".",                                                   # current dir
        "US/recent",                                           # slash smuggling
        "us-ak",                                               # lowercase (would 404 upstream)
    ]

    def test_valid_region_codes_pass(self) -> None:
        for code in self.VALID:
            with self.subTest(regionCode=code):
                out = _clamp_and_validate({"regionCode": code})
                self.assertEqual(out["regionCode"], code)

    def test_traversal_and_malformed_codes_rejected(self) -> None:
        for code in self.REJECTED:
            with self.subTest(regionCode=code):
                with self.assertRaises(ValueError) as ctx:
                    _clamp_and_validate({"regionCode": code})
                # Assert on the validator's OWN message, so we know rejection
                # comes from this code path and not some upstream layer.
                self.assertIn("Invalid regionCode", str(ctx.exception))
                self.assertIn(repr(code), str(ctx.exception))


class SpeciesCodeValidatorTests(unittest.TestCase):
    """speciesCode also flows into URL paths (/data/obs/.../recent/{code})."""

    def test_valid_species_codes_pass(self) -> None:
        for code in ("amecro", "yerwar1", "x00023"):
            with self.subTest(speciesCode=code):
                out = _clamp_and_validate({"speciesCode": code})
                self.assertEqual(out["speciesCode"], code)

    def test_traversal_and_malformed_codes_rejected(self) -> None:
        for code in (
            "amecro/../../ref/taxonomy/ebird",
            "amecro%2F..%2F..",
            "../",
            "AMRO",  # uppercase banding code — also invalid
        ):
            with self.subTest(speciesCode=code):
                with self.assertRaises(ValueError) as ctx:
                    _clamp_and_validate({"speciesCode": code})
                self.assertIn("Invalid speciesCode", str(ctx.exception))


# ---- Rejection happens before URL construction -------------------------------


class ValidationPrecedesDispatchTests(unittest.IsolatedAsyncioTestCase):
    """execute_tool must reject bad path args before _dispatch (and therefore
    before any URL is built or any HTTP client is touched)."""

    async def _run_expecting_rejection(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> str:
        plugin = _make_plugin()
        dispatch_calls: List[str] = []

        async def _tracking_dispatch(self, name, args, detail):  # noqa: ARG001
            dispatch_calls.append(name)
            return [], "http://unused", {}

        with patch.object(EBirdPlugin, "_dispatch", _tracking_dispatch):
            result = await plugin.execute_tool(tool_name, arguments)

        self.assertFalse(result.success)
        self.assertEqual(
            dispatch_calls, [],
            "_dispatch was reached — validation must run before URL construction",
        )
        return result.error_message or ""

    async def test_region_traversal_rejected_before_dispatch(self) -> None:
        msg = await self._run_expecting_rejection(
            "get_recent_observations",
            {"regionCode": "US-AK-020/../../US-NY"},
        )
        self.assertIn("Invalid regionCode", msg)

    async def test_region_endpoint_escape_rejected_before_dispatch(self) -> None:
        msg = await self._run_expecting_rejection(
            "get_recent_observations",
            {"regionCode": "US-AK-020/../../../ref/region/list/subnational1/US"},
        )
        self.assertIn("Invalid regionCode", msg)

    async def test_url_encoded_traversal_rejected_before_dispatch(self) -> None:
        # No percent-decoding happens anywhere before validation, so the
        # encoded form must fail the same shape check.
        msg = await self._run_expecting_rejection(
            "get_notable_observations",
            {"regionCode": "US-AK-020%2F..%2F..%2FUS-NY"},
        )
        self.assertIn("Invalid regionCode", msg)

    async def test_species_traversal_rejected_before_dispatch(self) -> None:
        msg = await self._run_expecting_rejection(
            "get_recent_observations_for_species",
            {"regionCode": "US-AK", "speciesCode": "amecro/../.."},
        )
        self.assertIn("Invalid speciesCode", msg)


# ---- Numeric clamps ----------------------------------------------------------


class NumericClampTests(unittest.TestCase):
    """Clamps are silent by design; the effective value is echoed in the
    response's Query: line (built from the params the client actually sent)."""

    def test_max_results_clamped_to_ceiling(self) -> None:
        self.assertEqual(
            _clamp_and_validate({"maxResults": 10000})["maxResults"], 1000
        )

    def test_max_results_clamped_to_floor(self) -> None:
        self.assertEqual(_clamp_and_validate({"maxResults": 0})["maxResults"], 1)

    def test_max_results_in_range_untouched(self) -> None:
        self.assertEqual(
            _clamp_and_validate({"maxResults": 500})["maxResults"], 500
        )

    def test_dist_clamped(self) -> None:
        self.assertEqual(_clamp_and_validate({"dist": 5000})["dist"], 50)

    def test_back_clamped(self) -> None:
        self.assertEqual(_clamp_and_validate({"back": 365})["back"], 30)


class ToolSchemaCeilingTests(unittest.TestCase):
    def test_max_results_schema_maximum_is_1000(self) -> None:
        """Every tool that accepts maxResults must advertise the 1000 ceiling
        (the clamp enforces it; the schema should not promise more)."""
        plugin = _make_plugin()
        seen = 0
        for tool in plugin.get_tools():
            prop = tool.input_schema.get("properties", {}).get("maxResults")
            if prop is None:
                continue
            seen += 1
            self.assertEqual(prop["maximum"], 1000, tool.name)
            self.assertIn("1-1000", prop["description"], tool.name)
        self.assertGreaterEqual(seen, 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
