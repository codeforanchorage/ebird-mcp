"""Caller mistakes must not be logged as server faults.

A traceback is a claim that the server broke. Spending one on "you forgot
speciesCode" or "back must be an integer" is how real faults become
unfindable: they sit in CloudWatch looking identical to argument
validation.

The split enforced here:

    caller mistake   ToolInputError   WARNING, no traceback
    server fault     anything else    ERROR, full traceback

Deliberately NOT inferred from ValueError -- ``json.JSONDecodeError``
subclasses it, and ``_parse_hotspot_text`` coerces eBird's CSV fallback
with a bare ``float()`` that raises ValueError on a malformed upstream
row. Both are genuine faults whose tracebacks we want, so the marker type
is explicit and the residue is pinned by count below.

Run with::

    python -m unittest tests.test_caller_error_logging
"""

import ast
import logging
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set

from core.interfaces import ToolInputError
from plugins.ebird.plugin import EBirdPlugin

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "ebird"
PLUGIN_PY = PLUGIN_DIR / "plugin.py"
CLIENT_PY = PLUGIN_DIR / "ebird_client.py"

# The only two functions allowed to coerce a caller-supplied argument to a
# number. Every other int()/float() in the plugin is either render-time
# defensive casting of already-validated values or upstream CSV parsing.
SANCTIONED_COERCION_HELPERS = {"_clamp_int", "_coerce_float"}

# _parse_hotspot_text coerces eBird's CSV fallback rows with bare float().
# A malformed upstream row is a genuine upstream fault, so those stay plain
# ValueError and keep their traceback. Pinned by count so a NEW unguarded
# upstream coercion has to be classified deliberately rather than drift in.
EXPECTED_UPSTREAM_COERCIONS = 3


def _make_plugin() -> EBirdPlugin:
    """Plugin wired to a client that answers every endpoint with no rows.

    The stub must expose the endpoint methods even for tests that never
    reach them: ``_dispatch`` resolves ``client.<method>`` BEFORE it
    evaluates ``_require(...)`` in the argument list, so a bare stub fails
    with AttributeError and masks the rejection under test.
    """
    p = EBirdPlugin({"enabled": True, "api_key": "test-key"})

    class _StubClient:
        base_url = "https://api.ebird.org/v2"

        def __getattr__(self, name: str):
            async def _call(**kwargs):
                return [], f"{self.base_url}/{name}", {}

            return _call

        async def aclose(self) -> None:  # pragma: no cover
            pass

    p.client = _StubClient()  # type: ignore[assignment]
    p._initialized = True
    return p


def _coercion_sites(path: Path) -> List[tuple]:
    """Every int()/float() call site with its enclosing function name.

    Walks the AST rather than grepping lines: inline forms such as
    ``min(int(arguments.get("limit", 20)), 100)`` are invisible to a
    line-oriented pass, and those are exactly the ones that leak Python's
    own coercion message to the caller.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    sites = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"int", "float"}
        ):
            continue
        enclosing, cur = "<module>", node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing = cur.name
                break
        sites.append((node.lineno, enclosing, node.func.id))
    return sites


class CoercionSweepTests(unittest.TestCase):
    """Numeric coercion of caller arguments happens in exactly two places."""

    def test_caller_argument_coercion_is_centralised(self):
        """No bare int()/float() over caller args outside the helpers.

        The failure mode this prevents: a caller passes maxResults="abc"
        and gets back "invalid literal for int() with base 10: 'abc'" plus
        a stack trace -- Python internals presented as a server fault.
        """
        offenders = [
            (lineno, fn, kind)
            for lineno, fn, kind in _coercion_sites(PLUGIN_PY)
            if fn == "_clamp_and_validate"
        ]
        self.assertEqual(
            offenders,
            [],
            "_clamp_and_validate must delegate every coercion to "
            f"{sorted(SANCTIONED_COERCION_HELPERS)}; found {offenders}",
        )

    def test_the_sanctioned_helpers_still_exist_and_are_used(self):
        functions: Set[str] = {
            fn for _, fn, _ in _coercion_sites(PLUGIN_PY)
        }
        for helper in SANCTIONED_COERCION_HELPERS:
            self.assertIn(
                helper,
                functions,
                f"{helper} no longer performs the coercion it is meant to "
                "own; the sweep above would then be vacuously true.",
            )

    def test_upstream_coercion_residue_is_pinned(self):
        """Upstream parsing keeps plain ValueError -- but not silently.

        These are genuine upstream faults, so they must NOT become
        ToolInputError. Pinning the count means adding a new one is a
        deliberate act with a test to update, not a drift.
        """
        upstream = [
            site
            for site in _coercion_sites(CLIENT_PY)
            if site[1] == "_parse_hotspot_text"
        ]
        self.assertEqual(
            len(upstream),
            EXPECTED_UPSTREAM_COERCIONS,
            "The number of upstream CSV coercions changed. Classify the new "
            "site: caller input -> ToolInputError, upstream data -> plain "
            "ValueError (keeps its traceback), then update this count.",
        )


class RaiseSiteClassificationTests(unittest.TestCase):
    """Every raise in the plugin is classified, none left as ValueError."""

    def test_no_bare_value_error_raises_remain(self):
        raises = []
        for path in (PLUGIN_PY, CLIENT_PY):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id == "ValueError"
                ):
                    raises.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            raises,
            [],
            "A bare `raise ValueError` is unclassified: it will log as a "
            "server fault with a traceback. Use ToolInputError for caller "
            f"input, or add a comment saying why it is upstream. Found: "
            f"{raises}",
        )


class CallerErrorLoggingTests(unittest.IsolatedAsyncioTestCase):
    """Rejections log at WARNING with no traceback."""

    async def _run(self, tool: str, arguments: Dict[str, Any]):
        plugin = _make_plugin()
        with self.assertLogs("plugins.ebird.plugin", level="DEBUG") as cap:
            result = await plugin.execute_tool(tool, arguments)
        return result, cap.records

    async def test_invalid_region_logs_warning_without_traceback(self):
        result, records = await self._run(
            "get_recent_observations", {"regionCode": "../../etc/passwd"}
        )
        self.assertFalse(result.success)
        self.assertTrue(records, "the rejection should be visible in logs")
        self.assertTrue(
            all(r.levelno == logging.WARNING for r in records),
            "a caller mistake must not log at ERROR",
        )
        self.assertTrue(
            all(r.exc_info is None for r in records),
            "a caller mistake must not carry a traceback",
        )

    async def test_non_numeric_max_results_names_the_argument(self):
        """Not Python's own coercion message."""
        result, _ = await self._run(
            "get_recent_observations",
            {"regionCode": "US-AK", "maxResults": "abc"},
        )
        self.assertFalse(result.success)
        self.assertIn("maxResults", result.error_message)
        self.assertNotIn("invalid literal for int()", result.error_message)

    async def test_non_numeric_lat_names_the_argument(self):
        result, _ = await self._run(
            "get_nearby_observations", {"lat": "north", "lng": -149.9}
        )
        self.assertFalse(result.success)
        self.assertIn("lat", result.error_message)
        self.assertNotIn("could not convert string to float", result.error_message)

    async def test_missing_required_argument_logs_warning(self):
        result, records = await self._run("get_recent_observations", {})
        self.assertFalse(result.success)
        self.assertIn("Missing required argument", result.error_message)
        self.assertIn("regionCode", result.error_message)
        self.assertTrue(
            all(r.levelno == logging.WARNING for r in records),
            "a missing argument is a caller mistake, not a server fault",
        )
        self.assertTrue(all(r.exc_info is None for r in records))

    async def test_missing_argument_raises_the_marker_type(self):
        """Not KeyError -- so a real KeyError still reads as a bug."""
        from plugins.ebird.plugin import _require

        with self.assertRaises(ToolInputError):
            _require({}, "regionCode")


class GenuineFaultLoggingTests(unittest.IsolatedAsyncioTestCase):
    """Real faults keep ERROR and the full traceback."""

    async def test_upstream_fault_logs_error_with_traceback(self):
        plugin = _make_plugin()

        class _Boom:
            base_url = "https://api.ebird.org/v2"

            async def get_hotspots(self, **kwargs):
                raise RuntimeError("upstream exploded")

            async def aclose(self):  # pragma: no cover
                pass

        plugin.client = _Boom()  # type: ignore[assignment]

        with self.assertLogs("plugins.ebird.plugin", level="DEBUG") as cap:
            result = await plugin.execute_tool(
                "get_hotspots", {"regionCode": "US-AK"}
            )

        self.assertFalse(result.success)
        errors = [r for r in cap.records if r.levelno == logging.ERROR]
        self.assertTrue(errors, "a genuine fault must log at ERROR")
        self.assertTrue(
            any(r.exc_info for r in errors),
            "a genuine fault must keep its traceback",
        )


class MalformedRequestJsonTests(unittest.IsolatedAsyncioTestCase):
    """B4: -32700 already tells the client; the traceback adds nothing."""

    async def test_parse_error_logs_warning_not_error(self):
        from core.mcp_server import MCPServer

        server = MCPServer(plugin_manager=None)  # never reached
        with self.assertLogs("core.mcp_server", level="DEBUG") as cap:
            response = await server.handle_http_request("{not json")

        self.assertEqual(response["statusCode"], 400)
        errors = [r for r in cap.records if r.levelno >= logging.ERROR]
        self.assertEqual(
            errors, [], "malformed request JSON is a caller mistake"
        )
        self.assertTrue(
            all(r.exc_info is None for r in cap.records),
            "no traceback for a parse error",
        )


if __name__ == "__main__":
    unittest.main()
