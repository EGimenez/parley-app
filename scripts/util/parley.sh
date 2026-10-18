#!/usr/bin/env sh
# parley — zero-install shim for the unified CLI (iteration 22).
# Runs from your current directory (so a literal -cfg path resolves where you are);
# only puts the repo's src on PYTHONPATH (the script lives in scripts/util/). E.g.:
#    ./scripts/util/parley.sh -cfg gmail --default
#    ./scripts/util/parley.sh "set me a meeting with x@y.com before friday"
PYTHONPATH="$(dirname "$0")/../../src${PYTHONPATH:+:$PYTHONPATH}" exec python -m parley "$@"
