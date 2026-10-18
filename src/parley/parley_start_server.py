"""Parley server entry — start the persistent client-server server.

Thin wrapper kept for the `parley-server` console script (and any script/`.bat`/
`.sh` that starts a server directly). The composition lives in `agent/compose.py`,
the serving in `client_server/server.py`, and the `python -m parley` umbrella in
`__main__.py`. This module just forwards to `server.main`.

    python -m parley.parley_start_server --config-file config/cfg_gmail.yaml
    python -m parley.parley_start_server memory        # credential-free demo
"""

import sys

# Force UTF-8 so arrow/emoji status output doesn't crash a cp1252 Windows console.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from parley.client_server.server import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
