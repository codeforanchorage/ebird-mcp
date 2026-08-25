"""Timeout-ladder invariants across config and Terraform.

API Gateway REST integrations have a hard, non-adjustable 29-second
integration timeout, and this server sits behind
``aws_api_gateway_rest_api`` (terraform/aws/api_gateway.tf). Every timeout
below it must be strictly tighter, or the failure mode is opaque:

    API Gateway   29s   hard, cannot change
      Lambda      28s   self-terminates BEFORE the gateway gives up
        plugin    20s   httpx/upstream; leaves ~8s to finish and return

A Lambda that outlives the gateway keeps burning compute nobody is waiting
for after the client already got its 504, and holds one of the reserved
concurrency slots for the remainder. A plugin timeout at or above the
Lambda timeout means a hung eBird call never produces a readable "upstream
timed out" tool error -- the Lambda is killed mid-flight and the caller
gets an opaque 502.

These values live in three places that can drift independently, so each is
pinned here rather than trusted to review:

  - ``config-example.yaml``   the committed template (config.yaml itself is
                              gitignored, so this is the source of truth in
                              version control)
  - ``terraform/aws/*.tfvars`` the fallback Terraform reads when
                              ``config.yaml:aws.lambda_timeout`` is absent
  - ``EBirdPluginConfig``      the default applied when a config omits the
                              plugin timeout entirely

Run with::

    python -m unittest tests.test_config_invariants
"""

import re
import unittest
from pathlib import Path

import yaml

from plugins.ebird.config_schema import EBirdPluginConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# API Gateway REST integration timeout. AWS does not allow raising this.
API_GATEWAY_INTEGRATION_TIMEOUT = 29

# Headroom the Lambda needs after the upstream call returns: format the
# response body, build caveats, and serialize. Small, but not zero.
MIN_PLUGIN_HEADROOM = 5

TFVARS = ("staging.tfvars", "prod.tfvars")


def _load_example_config():
    with open(PROJECT_ROOT / "config-example.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _tfvar_int(filename: str, key: str):
    """Pull a bare numeric assignment out of a .tfvars file."""
    text = (PROJECT_ROOT / "terraform" / "aws" / filename).read_text(
        encoding="utf-8"
    )
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\d+)\s*$", text, re.M)
    if match is None:
        raise AssertionError(f"{key} not found in {filename}")
    return int(match.group(1))


class TimeoutLadderTests(unittest.TestCase):
    """Each rung must sit strictly under the one above it."""

    def setUp(self):
        self.config = _load_example_config()
        self.lambda_timeout = self.config["aws"]["lambda_timeout"]
        self.plugin_timeout = self.config["plugins"]["ebird"]["timeout"]

    def test_lambda_timeout_under_api_gateway_ceiling(self):
        self.assertLess(
            self.lambda_timeout,
            API_GATEWAY_INTEGRATION_TIMEOUT,
            "lambda_timeout must be strictly under API Gateway's hard 29s "
            "integration timeout, or the gateway 504s while the Lambda keeps "
            "running and holding a concurrency slot.",
        )

    def test_plugin_timeout_under_lambda_timeout(self):
        self.assertLessEqual(
            self.plugin_timeout,
            self.lambda_timeout - MIN_PLUGIN_HEADROOM,
            "plugins.ebird.timeout must leave the Lambda at least "
            f"{MIN_PLUGIN_HEADROOM}s to format and return a readable "
            "'upstream timed out' error rather than being killed mid-flight.",
        )

    def test_plugin_schema_default_respects_the_ladder(self):
        """A config that omits the plugin timeout still lands under Lambda."""
        default = EBirdPluginConfig(api_key="x").timeout
        self.assertLessEqual(
            default,
            self.lambda_timeout - MIN_PLUGIN_HEADROOM,
            "The EBirdPluginConfig default is what a config omitting "
            "`timeout` gets; it must respect the same ladder.",
        )

    def test_plugin_schema_rejects_timeouts_above_the_lambda(self):
        """The bound is enforced, not just documented in a description."""
        with self.assertRaises(ValueError):
            EBirdPluginConfig(api_key="x", timeout=30)


class TfvarsDriftTests(unittest.TestCase):
    """Terraform's fallback must not contradict the config it falls back from.

    `terraform/aws/main.tf` reads `local.config.aws.lambda_timeout` first and
    only uses the tfvars value when the config omits it. A stale tfvars is
    therefore silent rather than loud -- it applies exactly when someone
    trims the config, which is the worst moment to discover the drift.
    """

    def setUp(self):
        self.config = _load_example_config()

    def test_tfvars_lambda_timeout_matches_config(self):
        expected = self.config["aws"]["lambda_timeout"]
        for filename in TFVARS:
            with self.subTest(tfvars=filename):
                self.assertEqual(
                    _tfvar_int(filename, "lambda_timeout"),
                    expected,
                    f"{filename} disagrees with config-example.yaml on "
                    "lambda_timeout; main.tf prefers the config, so the "
                    "tfvars value only surfaces once the config drops it.",
                )

    def test_tfvars_lambda_timeout_under_api_gateway_ceiling(self):
        for filename in TFVARS:
            with self.subTest(tfvars=filename):
                self.assertLess(
                    _tfvar_int(filename, "lambda_timeout"),
                    API_GATEWAY_INTEGRATION_TIMEOUT,
                    f"{filename} would outlive the gateway if it ever won.",
                )


if __name__ == "__main__":
    unittest.main()
