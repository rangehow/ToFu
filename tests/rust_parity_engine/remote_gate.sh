#!/bin/sh
# Build-and-oracle consumer for scripts/remote_cargo.py --after-build.
set -eu
: "${TOFU_REMOTE_PYTHON:?remote_cargo must supply the existing project interpreter}"
export PYTHONPATH="$PWD"
export PYTHONDONTWRITEBYTECODE=1
report="$TMPDIR/engine-parity.json"
status=0
"$TOFU_REMOTE_PYTHON" -m tests.rust_parity_engine.shared_harness --rust "$1" --report "$report" || status=$?
if [ -f "$report" ]; then
    "$TOFU_REMOTE_PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d,sort_keys=True,ensure_ascii=False))' "$report"
fi
exit "$status"
