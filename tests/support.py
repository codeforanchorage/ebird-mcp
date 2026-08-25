"""Shared test helpers.

``server.http_handler`` keeps its PluginManager and MCPServer as module
globals so a warm Lambda container can reuse them across invocations.
That is right for production and hostile to tests: anything a test
assigns to those names outlives the test and is visible to every test
that runs afterwards, in any file. Worse, the Lambda adapter's cleanup
path does ``await _plugin_manager.shutdown()``, so a non-awaitable stub
left behind by one file makes an unrelated file fail — and whether it
does depends on alphabetical file order, which is a miserable thing to
debug.

``HTTPHandlerIsolation`` snapshots and restores both globals around every
test that touches them. ``tests/conftest.py`` applies the same protection
to every test when the suite is run under pytest.
"""

import server.http_handler as http_handler

_GLOBALS = ("_plugin_manager", "_mcp_server", "_config")


def snapshot_http_handler_globals():
    return {name: getattr(http_handler, name) for name in _GLOBALS}


def restore_http_handler_globals(snapshot):
    for name, value in snapshot.items():
        setattr(http_handler, name, value)


def reset_http_handler_globals():
    """Force the next request to re-initialize from scratch."""
    for name in _GLOBALS:
        setattr(http_handler, name, None)


class HTTPHandlerIsolation:
    """Mixin: leave server.http_handler exactly as it was found."""

    def setUp(self):
        super().setUp()
        self._http_handler_snapshot = snapshot_http_handler_globals()
        self.addCleanup(
            restore_http_handler_globals, self._http_handler_snapshot
        )
        reset_http_handler_globals()
