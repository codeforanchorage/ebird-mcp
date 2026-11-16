"""Upstream failures must reach the caller readable, and inside the ladder.

Three things are pinned here, each of which was wrong at once:

  - The final failure after a retry surfaced as tenacity's own
    ``RetryError[<Future at 0x... raised ReadTimeout>]`` -- an object repr
    handed to the caller as the tool error.
  - A timeout was retried. One attempt can spend the whole 20s plugin
    budget, so a second cannot fit under the 28s Lambda timeout: the
    Lambda is killed mid-flight and the caller gets an opaque 502 instead
    of the readable error the timeout ladder exists to produce.
  - ``get_nearby_hotspots`` re-requested on ANY exception, not just the
    CSV-instead-of-JSON case it was written for, doubling upstream calls
    (and quota) on timeouts, HTTP errors and the quota gate itself.

Run with::

    python -m unittest tests.test_upstream_failures
"""

import json
import unittest
from typing import Any, List

import httpx

from plugins.ebird import ebird_client
from plugins.ebird.plugin import EBirdPlugin

_REGION_CALL = ("get_recent_observations", {"regionCode": "US-AK"})
_NEARBY_HOTSPOTS_CALL = ("get_nearby_hotspots", {"lat": 61.2, "lng": -149.9})


class _Response:
    """The slice of httpx.Response that EBirdClient._get touches."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://api.ebird.org/v2/x"),
                response=self,  # type: ignore[arg-type]
            )

    def json(self) -> Any:
        return json.loads(self.text)


class _UpstreamTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._quota = dict(ebird_client._quota_state)
        self.addCleanup(ebird_client._quota_state.update, self._quota)
        ebird_client._quota_state.update({"date": None, "count": 0})

        self.plugin = EBirdPlugin({"enabled": True, "api_key": "test-key"})
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.shutdown)
        self.calls: List[dict] = []

    def _script(self, *outcomes: Any) -> None:
        """Answer successive upstream GETs with these outcomes, in order.

        An exception instance is raised; anything else is returned. The
        last outcome repeats, so an unexpected extra call shows up as a
        wrong attempt count rather than an IndexError.
        """
        remaining = list(outcomes)

        async def _get(endpoint: str, **kwargs: Any) -> Any:
            self.calls.append({"endpoint": endpoint, **kwargs})
            outcome = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        assert self.plugin.client is not None
        self.plugin.client._client.get = _get  # type: ignore[method-assign]


class TimeoutTests(_UpstreamTestCase):
    async def test_timeout_is_not_retried(self) -> None:
        for exc in (
            httpx.ReadTimeout(""),
            httpx.ConnectTimeout(""),
            httpx.PoolTimeout(""),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.calls.clear()
                self._script(exc)
                with self.assertLogs("plugins.ebird.plugin", "WARNING"):
                    await self.plugin.execute_tool(*_REGION_CALL)
                self.assertEqual(
                    len(self.calls),
                    1,
                    "A timeout already spent the plugin budget; a second "
                    "attempt cannot finish inside the Lambda timeout.",
                )

    async def test_timeout_error_is_readable(self) -> None:
        self._script(httpx.ReadTimeout(""))
        with self.assertLogs("plugins.ebird.plugin", "WARNING") as logs:
            result = await self.plugin.execute_tool(*_REGION_CALL)

        self.assertFalse(result.success)
        self.assertIn("timed out", result.error_message)
        self.assertIn(
            f"{self.plugin.plugin_config.timeout}s", result.error_message
        )
        self.assertNotIn("RetryError", result.error_message)
        self.assertNotIn("Future at", result.error_message)
        # Upstream slowness is not a server fault: no traceback.
        self.assertTrue(all(r.exc_info is None for r in logs.records))


class TransportErrorTests(_UpstreamTestCase):
    async def test_fast_transport_failure_gets_one_retry(self) -> None:
        self._script(httpx.ConnectError("connection reset"), _Response("[]"))
        result = await self.plugin.execute_tool(*_REGION_CALL)

        self.assertEqual(len(self.calls), 2)
        self.assertTrue(result.success)

    async def test_exhausted_retry_reports_the_real_error(self) -> None:
        self._script(httpx.ConnectError("connection reset"))
        with self.assertLogs("plugins.ebird.plugin", "WARNING") as logs:
            result = await self.plugin.execute_tool(*_REGION_CALL)

        self.assertEqual(len(self.calls), 2)
        self.assertFalse(result.success)
        self.assertIn("Could not reach the eBird API", result.error_message)
        self.assertIn("ConnectError", result.error_message)
        self.assertNotIn("RetryError", result.error_message)
        self.assertNotIn("Future at", result.error_message)
        self.assertTrue(all(r.exc_info is None for r in logs.records))

    async def test_http_errors_are_not_retried(self) -> None:
        self._script(_Response("upstream broke", status_code=500))
        with self.assertLogs("plugins.ebird.plugin", "WARNING"):
            result = await self.plugin.execute_tool(*_REGION_CALL)

        self.assertEqual(len(self.calls), 1)
        self.assertIn("eBird API HTTP 500", result.error_message)


class NearbyHotspotsFallbackTests(_UpstreamTestCase):
    """The CSV fallback is for a format problem and nothing else."""

    async def test_csv_body_still_falls_back(self) -> None:
        csv = "L123,US,US-AK,US-AK-020,61.2,-149.9,Westchester Lagoon"
        self._script(_Response(csv))
        with self.assertLogs("plugins.ebird.ebird_client", "WARNING"):
            result = await self.plugin.execute_tool(*_NEARBY_HOTSPOTS_CALL)

        self.assertTrue(result.success)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("fmt", self.calls[1]["params"])
        rows = result.structured_content["rows"]
        self.assertEqual(rows[0]["locId"], "L123")
        self.assertEqual(rows[0]["locName"], "Westchester Lagoon")

    async def test_timeout_does_not_trigger_the_fallback(self) -> None:
        self._script(httpx.ReadTimeout(""))
        with self.assertLogs("plugins.ebird.plugin", "WARNING"):
            result = await self.plugin.execute_tool(*_NEARBY_HOTSPOTS_CALL)

        self.assertEqual(len(self.calls), 1)
        self.assertIn("timed out", result.error_message)

    async def test_http_error_does_not_trigger_the_fallback(self) -> None:
        self._script(_Response("bad request", status_code=400))
        with self.assertLogs("plugins.ebird.plugin", "WARNING"):
            result = await self.plugin.execute_tool(*_NEARBY_HOTSPOTS_CALL)

        self.assertEqual(len(self.calls), 1)
        self.assertIn("eBird API HTTP 400", result.error_message)

    async def test_failed_call_spends_one_quota_unit(self) -> None:
        self._script(_Response("bad request", status_code=400))
        with self.assertLogs("plugins.ebird.plugin", "WARNING"):
            await self.plugin.execute_tool(*_NEARBY_HOTSPOTS_CALL)

        self.assertEqual(ebird_client._quota_state["count"], 1)


if __name__ == "__main__":
    unittest.main()
