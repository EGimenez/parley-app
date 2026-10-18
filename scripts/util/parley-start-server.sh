#!/usr/bin/env sh
# Start the Parley client-server server from the repo root with src on PYTHONPATH.
# Pass through args, e.g.:  ./scripts/util/parley-start-server.sh --config-file config/cfg_gmail.yaml
#                           ./scripts/util/parley-start-server.sh memory        # credential-free demo
cd "$(dirname "$0")/../.." || exit 1
PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" exec python -m parley --serve "$@"
