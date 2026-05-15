#!/usr/bin/env python3
"""Refresh the bundled eBird taxonomy snapshot.

Fetches the full taxonomy from eBird and writes it to
``plugins/ebird/data/taxonomy.json`` so the Lambda can serve
``get_taxonomy`` calls without burning daily eBird quota. eBird publishes
new taxonomy annually (typically October), so refreshing on each deploy
is well within freshness tolerance.

Reads the API key from ``config.yaml`` (same file the Lambda uses).
Failure is non-fatal for the deploy: the plugin transparently falls
through to the live eBird API when the bundle is missing or unreadable.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
OUTPUT_PATH = PROJECT_ROOT / "plugins" / "ebird" / "data" / "taxonomy.json"

# Without an explicit cat, eBird's default response is `species` only. Listing
# every category gets us the full taxonomy in one shot so the in-Lambda filter
# can answer any cat without a fallback HTTP call.
CATEGORIES = "species,issf,hybrid,slash,spuh,domestic,form"
URL = (
    "https://api.ebird.org/v2/ref/taxonomy/ebird"
    f"?fmt=json&locale=en&cat={CATEGORIES}"
)


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"config.yaml not found at {CONFIG_PATH}", file=sys.stderr)
        return 1

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    api_key = config.get("plugins", {}).get("ebird", {}).get("api_key", "")
    if not api_key or api_key == "REPLACE_ME":
        print(
            "plugins.ebird.api_key is missing or still REPLACE_ME in config.yaml",
            file=sys.stderr,
        )
        return 1

    req = urllib.request.Request(URL, headers={"X-eBirdApiToken": api_key})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"Failed to fetch taxonomy from eBird: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Taxonomy response was not valid JSON: {e}", file=sys.stderr)
        return 1

    if not isinstance(data, list) or not data:
        print("Unexpected taxonomy response (not a non-empty list)", file=sys.stderr)
        return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))

    size_kb = OUTPUT_PATH.stat().st_size // 1024
    print(
        f"Wrote {len(data):,} taxonomy entries to "
        f"{OUTPUT_PATH.relative_to(PROJECT_ROOT)} ({size_kb:,} KB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
