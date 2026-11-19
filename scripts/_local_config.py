"""Which config a locally run server uses. Shared by the local entry points.

``scripts/local_server.py`` (HTTP) and ``scripts/stdio_server.py`` (stdio)
both serve through the core's ``UniversalHTTPHandler``, which loads its
config from ``OPENCONTEXT_CONFIG``. This resolves the choice once and hands
it to the handler through that variable (which accepts a YAML path as well
as the JSON Terraform injects), so there is exactly one answer and the two
entry points cannot disagree about it.

It used to be read in local_server.py for the logging level only, while the
handler went on loading ./config.yaml by itself: EBIRD_MCP_CONFIG_FILE
looked honoured -- the server started, the log level changed -- and every
request was still served from the other file, API key and all.

Precedence: OPENCONTEXT_CONFIG if already set (same as prod), then
EBIRD_MCP_CONFIG_FILE, then config.yaml at the repo root -- NOT the process
CWD, so the scripts work from any directory.
"""

import os
from pathlib import Path


def use_local_config(project_root: Path) -> None:
    """Point the handler at the local config and run from the repo root.

    Call BEFORE importing ``server.http_handler``. Exits with a readable
    message, not a traceback, when the chosen file does not exist.
    """
    config_path = os.path.abspath(
        os.environ.get(
            "EBIRD_MCP_CONFIG_FILE", str(project_root / "config.yaml")
        )
    )
    # The core discovers plugins relative to the CWD (plugins/,
    # custom_plugins/), so run from the repo root wherever the script was
    # launched from. Done AFTER resolving the config path above, so a
    # relative EBIRD_MCP_CONFIG_FILE still means relative to where the user
    # typed it.
    os.chdir(project_root)
    if not os.environ.get("OPENCONTEXT_CONFIG"):
        if not os.path.isfile(config_path):
            raise SystemExit(
                f"Config file not found: {config_path}\n"
                f"Copy config-example.yaml to config.yaml, or point "
                f"EBIRD_MCP_CONFIG_FILE at one."
            )
        os.environ["OPENCONTEXT_CONFIG"] = config_path
