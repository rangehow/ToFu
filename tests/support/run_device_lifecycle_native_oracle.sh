#!/bin/sh
set -eu
: "${TOFU_REMOTE_SOURCE_COMMIT:?clean source required}"
: "${TOFU_REMOTE_BINARY_SHA256:?binary digest required}"
export TOFU_TEST_TOFUDB_BIN="$1" TOFUDB_TRANSPORT=daemon
"$TOFU_REMOTE_PYTHON" - "$1" "$TOFU_REMOTE_BINARY_SHA256" <<'PY'
import hashlib
import sys
with open(sys.argv[1], 'rb') as binary:
    if hashlib.file_digest(binary, 'sha256').hexdigest() != sys.argv[2]:
        raise SystemExit('Native test binary does not match the remote build digest')
PY
report=$(mktemp)
trap 'rm -f "$report"' EXIT
"$TOFU_REMOTE_PYTHON" -m pytest -q -rs -o 'addopts=' --junitxml="$report" \
    tests/desktop/test_desktop_node_registry.py \
    -k 'offline_confirmed_node or removal_drops or same_id_replacement or distinct_id_repair or inflight_write or remove_reports_conflict or removal_cleans or check_only_metadata'
"$TOFU_REMOTE_PYTHON" - "$report" <<'PY'
import sys
from xml.etree import ElementTree
cases = ElementTree.parse(sys.argv[1]).findall('.//testcase')
if not cases or any(len(case) for case in cases):
    raise SystemExit('Native HTTP oracle did not execute every selected case successfully')
PY
