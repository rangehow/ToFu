#!/bin/sh
# Diagnose the retained Python shipper fixture's parallel timing failure
# serially, then complete load verification of the reconciled release binary.
set -eu
: "${1:?remote_cargo must supply the checked release binary}"
export TOFU_TEST_SQL_AUTHORITY_BIN="$1" PYTHONPATH="$PWD"
status=0
"$TOFU_REMOTE_PYTHON" -m pytest -q -p no:cacheprovider \
    tests/storage/test_storage_fastpath.py::test_torn_shadow_tail_is_overwritten_not_trusted || status=1
"$TOFU_REMOTE_PYTHON" -m pytest -q -p no:cacheprovider -n 8 \
    tests/storage_authority_sql/test_rust_sql_authority_release_load.py || status=1
exit "$status"
