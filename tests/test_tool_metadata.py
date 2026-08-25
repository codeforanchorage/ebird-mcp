"""Tool metadata advertised through tools/list.

``name`` is a stable programmatic identifier and stays the prefixed wire
name (``ebird__get_nearby_notable_observations``). It reads badly wherever
a client shows tools to a person, which is what the MCP schema's top-level
``title`` is for — clients resolve a display name as
title -> annotations.title -> name.

The TOOL_TITLES map is checked in BOTH directions. A one-directional check
rots the first time someone adds a tool (no title, silently falls back to
the wire name) or removes one (a stale entry nobody notices).

Run with::

    python -m unittest tests.test_tool_metadata
"""

import unittest
from typing import Any, Dict, List

from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult
from core.plugin_manager import PluginManager
from plugins.ebird.plugin import TOOL_TITLES, EBirdPlugin


def _tools() -> List[ToolDefinition]:
    return EBirdPlugin({"enabled": True, "api_key": "test-key"}).get_tools()


class ToolTitleMapTests(unittest.TestCase):
    """The map and the tool catalog must agree exactly."""

    def test_every_tool_has_a_title(self):
        missing = [t.name for t in _tools() if not t.title]
        self.assertEqual(
            missing,
            [],
            "These tools would fall back to the prefixed wire name in a "
            f"client's tool picker; add them to TOOL_TITLES: {missing}",
        )

    def test_no_stale_titles_for_removed_tools(self):
        actual = {t.name for t in _tools()}
        stale = sorted(set(TOOL_TITLES) - actual)
        self.assertEqual(
            stale,
            [],
            f"TOOL_TITLES names tools that no longer exist: {stale}",
        )

    def test_titles_are_human_readable_not_wire_names(self):
        """A title that is just the identifier defeats the point."""
        for tool in _tools():
            with self.subTest(tool=tool.name):
                self.assertNotEqual(tool.title, tool.name)
                self.assertNotIn("_", tool.title)

    def test_titles_are_unique(self):
        titles = [t.title for t in _tools()]
        duplicates = {t for t in titles if titles.count(t) > 1}
        self.assertEqual(
            duplicates, set(), f"ambiguous in a picker: {duplicates}"
        )

    def test_names_are_unchanged_by_titling(self):
        """Nothing that dispatches on `name` may be affected."""
        self.assertEqual(
            sorted(t.name for t in _tools()),
            sorted(TOOL_TITLES),
        )


class _TitledPlugin(MCPPlugin):
    """Two tools: one titled, one not."""

    plugin_name = "demo"
    plugin_type = PluginType.CUSTOM_API

    async def initialize(self) -> bool:
        return True

    async def shutdown(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="titled",
                title="Nicely Titled",
                description="",
                input_schema={"type": "object"},
            ),
            ToolDefinition(
                name="untitled", description="", input_schema={"type": "object"}
            ),
        ]

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:  # pragma: no cover
        return ToolResult(content=[], success=True)


class ToolsListWireFormatTests(unittest.TestCase):
    """`title` is a TOP-LEVEL Tool field, not an annotation."""

    def setUp(self):
        manager = PluginManager({})
        manager.plugins = {"demo": _TitledPlugin({})}
        self.emitted = {t["name"]: t for t in manager.get_all_tools()}

    def test_title_emitted_at_top_level(self):
        tool = self.emitted["demo__titled"]
        self.assertEqual(tool["title"], "Nicely Titled")
        self.assertNotIn(
            "annotations",
            tool,
            "title must not be buried in annotations; clients prefer the "
            "top-level field and only fall back to annotations.title",
        )

    def test_absent_title_omits_the_field_entirely(self):
        """Not `title: null` — an absent field is the correct wire shape."""
        self.assertNotIn("title", self.emitted["demo__untitled"])

    def test_prefixed_name_is_still_the_identifier(self):
        self.assertEqual(self.emitted["demo__titled"]["name"], "demo__titled")


class AnnotationHintTests(unittest.TestCase):
    """idempotentHint is deliberately absent.

    The schema documents it as meaningful only when readOnlyHint == false,
    and every eBird tool is a read-only lookup. This fork carries no
    annotations pass at all today; the assertion is a guard for the day one
    is added, so the hint cannot arrive as copied boilerplate.
    """

    def test_no_tool_advertises_idempotent_hint(self):
        manager = PluginManager({})
        manager.plugins = {
            "ebird": EBirdPlugin({"enabled": True, "api_key": "test-key"})
        }
        for tool in manager.get_all_tools():
            with self.subTest(tool=tool["name"]):
                self.assertNotIn(
                    "idempotentHint",
                    tool.get("annotations", {}),
                    "idempotentHint is meaningful only when readOnlyHint is "
                    "false; every tool here is read-only, so it is noise.",
                )


if __name__ == "__main__":
    unittest.main()
