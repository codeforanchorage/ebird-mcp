"""Shared pytest fixtures.

The suite's documented runner is ``python -m unittest discover tests``,
where the ``HTTPHandlerIsolation`` mixin in ``tests/support.py`` does this
job for the tests that need it. Under pytest — which this repo also
supports, via the ``[tool.pytest.ini_options]`` block in pyproject.toml —
the same protection is applied to EVERY test automatically, so a new test
cannot leak the globals by forgetting the mixin.

See tests/support.py for why the globals exist and why leaking them is
worse than it looks.
"""

import pytest

from tests.support import (
    restore_http_handler_globals,
    snapshot_http_handler_globals,
)


@pytest.fixture(autouse=True)
def _isolate_http_handler_globals():
    snapshot = snapshot_http_handler_globals()
    yield
    restore_http_handler_globals(snapshot)
