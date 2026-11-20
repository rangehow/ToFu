#!/bin/sh
set -eu
export TOFU_TEST_TOFUDB_BIN="$1"
exec "$TOFU_REMOTE_PYTHON" -m pytest -q -rs tests/storage/test_turn_resume_plan_read.py tests/storage/test_continue_plan_acceptance.py tests/storage/test_continue_selection.py -k native -o 'addopts='
