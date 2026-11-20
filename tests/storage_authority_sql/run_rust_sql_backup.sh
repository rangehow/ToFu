#!/bin/sh
# Verify Rust-produced backup artifacts through the retained Python restore
# and hash-verification contracts on the remote build node.
set -eu
: "${1:?remote_cargo must supply the checked release binary}"
export TOFU_TEST_SQL_AUTHORITY_BIN="$1" PYTHONPATH="$PWD"
exec "$TOFU_REMOTE_PYTHON" -m pytest -q -p no:cacheprovider \
    tests/storage/test_storage_maintenance_cli.py \
    'tests/storage/test_storage_authority_sql_contract.py::test_backup_is_verified_and_kept_inside_project[sqlite]'
