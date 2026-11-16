"""scripts/local_server.py must serve from the config it was pointed at.

``EBIRD_MCP_CONFIG_FILE`` used to set the local server's log level and
nothing else: the request handler loaded ``./config.yaml`` on its own, so
an override appeared to work while every request ran on the other file.

Each case runs in a subprocess because the script resolves its config at
import time and the handler caches it in module globals.

Run with::

    python -m unittest tests.test_local_server_config
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

# Import the script (which resolves the config) and report what the
# REQUEST HANDLER would serve with -- not what the script itself read.
_PROBE = """
import json, sys
sys.path.insert(0, r"SCRIPTS")
import local_server
from server import http_handler
config = http_handler._load_config()
print(json.dumps({
    "server_name": config["server_name"],
    "base_url": config["plugins"]["ebird"]["base_url"],
    "log_config_is_handler_config": local_server.config is config,
    "plugins_found": sorted(
        name for name, _ in
        __import__("core.plugin_manager", fromlist=["PluginManager"])
        .PluginManager({"plugins": {}}).discover_plugins()
    ),
}))
"""


def _write_config(directory: str, name: str, server_name: str) -> str:
    config = yaml.safe_load(
        (REPO_ROOT / "config-example.yaml").read_text(encoding="utf-8")
    )
    config["server_name"] = server_name
    config["plugins"]["ebird"]["api_key"] = "test-key"
    config["plugins"]["ebird"]["base_url"] = f"https://{name}.invalid/v2"
    path = os.path.join(directory, f"{name}.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f)
    return path


def _probe(
    cwd: Path = REPO_ROOT, **env_overrides: str
) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("EBIRD_MCP_CONFIG_FILE", "OPENCONTEXT_CONFIG")
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", _PROBE.replace("SCRIPTS", str(SCRIPTS))],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class LocalServerConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def _served(self, **env: str) -> dict:
        result = _probe(**env)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_config_file_override_reaches_the_request_handler(self) -> None:
        path = _write_config(self.tmp, "override", "Override Server")
        served = self._served(EBIRD_MCP_CONFIG_FILE=path)

        self.assertEqual(served["server_name"], "Override Server")
        self.assertEqual(served["base_url"], "https://override.invalid/v2")
        self.assertTrue(served["log_config_is_handler_config"])

    def test_opencontext_config_wins_as_it_does_in_prod(self) -> None:
        loser = _write_config(self.tmp, "loser", "Loser")
        winner = _write_config(self.tmp, "winner", "Winner")
        served = self._served(
            EBIRD_MCP_CONFIG_FILE=loser, OPENCONTEXT_CONFIG=winner
        )

        self.assertEqual(served["server_name"], "Winner")

    def test_relative_override_resolves_against_the_launch_directory(
        self,
    ) -> None:
        """...and plugins are still found, though the CWD is not the repo."""
        _write_config(self.tmp, "relative", "Relative Server")
        result = _probe(
            cwd=Path(self.tmp), EBIRD_MCP_CONFIG_FILE="relative.yaml"
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        served = json.loads(result.stdout.strip().splitlines()[-1])

        self.assertEqual(served["server_name"], "Relative Server")
        self.assertEqual(served["plugins_found"], ["ebird"])

    def test_missing_config_file_fails_at_startup_with_the_path(self) -> None:
        missing = os.path.join(self.tmp, "nope.yaml")
        result = _probe(EBIRD_MCP_CONFIG_FILE=missing)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Config file not found", result.stderr)
        self.assertIn("nope.yaml", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
