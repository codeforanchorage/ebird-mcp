"""scripts/stdio_server.py: the server itself, spoken to over stdio.

Run as a real subprocess, because the properties that matter are
process-level: stdout carries protocol messages and nothing else, the
process ends when stdin does, and it starts from any directory.

No network: nothing here reaches an upstream call (the one tools/call is
rejected by argument validation first).

Run with::

    python -m unittest tests.test_stdio_server
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, List

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "stdio_server.py"


def _write_config(directory: str) -> str:
    config = yaml.safe_load(
        (REPO_ROOT / "config-example.yaml").read_text(encoding="utf-8")
    )
    config["plugins"]["ebird"]["api_key"] = "test-key"
    config["plugins"]["ebird"]["base_url"] = "https://stdio.invalid/v2"
    path = os.path.join(directory, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f)
    return path


def _run(lines: List[str], cwd: str, **env_overrides: str):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("EBIRD_MCP_CONFIG_FILE", "OPENCONTEXT_CONFIG")
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=("\n".join(lines) + "\n").encode("utf-8"),
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=60,
    )


def _msg(**fields: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", **fields})


class StdioServerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config = _write_config(self.tmp)

    def _session(self, lines: List[str]) -> List[Any]:
        # cwd is the temp dir, not the repo: the script must find its
        # plugins anyway.
        result = _run(lines, self.tmp, EBIRD_MCP_CONFIG_FILE=self.config)
        self.assertEqual(
            result.returncode, 0, result.stderr.decode("utf-8")[-2000:]
        )
        # Bytes, so a "\r\n" or a stray non-protocol line cannot hide.
        self.assertNotIn(b"\r", result.stdout)
        return [
            json.loads(line) for line in result.stdout.split(b"\n") if line
        ]

    def test_handshake_list_and_ping(self) -> None:
        out = self._session(
            [
                _msg(
                    id=1,
                    method="initialize",
                    params={
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                ),
                _msg(method="notifications/initialized"),
                _msg(id=2, method="tools/list"),
                "",
                _msg(id=3, method="ping"),
            ]
        )
        # Three requests answered, in order; the notification and the
        # blank line produced nothing.
        self.assertEqual([m["id"] for m in out], [1, 2, 3])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertIn("instructions", out[0]["result"])
        names = [t["name"] for t in out[1]["result"]["tools"]]
        self.assertEqual(len(names), 10)
        self.assertIn("ebird__get_recent_observations", names)
        self.assertEqual(out[2]["result"], {})

    def test_errors_are_the_servers_own_and_do_not_end_the_session(
        self,
    ) -> None:
        out = self._session(
            [
                "this is not json",
                _msg(
                    id=5,
                    method="tools/call",
                    params={"name": "ebird__nope", "arguments": {}},
                ),
                _msg(
                    id=6,
                    method="tools/call",
                    params={
                        "name": "ebird__get_hotspots",
                        "arguments": {"regionCode": "../etc"},
                    },
                ),
                _msg(id=7, method="ping"),
            ]
        )
        self.assertEqual([m.get("id") for m in out], [None, 5, 6, 7])
        self.assertEqual(out[0]["error"]["code"], -32700)
        self.assertEqual(out[1]["error"]["code"], -32602)
        self.assertIn("ebird__nope", out[1]["error"]["message"])
        # A rejected argument is a tool error, not a protocol error.
        self.assertTrue(out[2]["result"]["isError"])
        self.assertIn(
            "Invalid regionCode", out[2]["result"]["content"][0]["text"]
        )
        self.assertEqual(out[3]["result"], {})

    def test_a_notification_is_never_answered(self) -> None:
        out = self._session(
            [
                _msg(method="notifications/initialized"),
                _msg(method="no/such/notification"),
            ]
        )
        self.assertEqual(out, [])

    def test_missing_config_fails_at_startup_with_the_path(self) -> None:
        missing = os.path.join(self.tmp, "nope.yaml")
        result = _run(
            [_msg(id=1, method="ping")],
            self.tmp,
            EBIRD_MCP_CONFIG_FILE=missing,
        )
        stderr = result.stderr.decode("utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn("Config file not found", stderr)
        self.assertIn("nope.yaml", stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
