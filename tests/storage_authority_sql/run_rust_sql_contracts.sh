#!/bin/sh
# Retained semantic, maintenance, recovery and publication contracts.
set -eu
: "${1:?remote_cargo must supply the checked release binary}"
export TOFU_TEST_SQL_AUTHORITY_BIN="$1" PYTHONPATH="$PWD"
set +e
"$TOFU_REMOTE_PYTHON" -m pytest -q -p no:cacheprovider -n 8 \
    --junitxml="$TMPDIR/sql-authority-contracts.xml" -o junit_family=legacy \
    tests/storage/test_storage_authority_sql_contract.py \
    tests/storage/test_storage_application_contract.py \
    tests/storage/test_storage_fastpath.py \
    tests/storage/test_storage_logical_outbox.py \
    tests/storage/test_storage_maintenance_cli.py \
    tests/storage/test_turn_projection_reader.py \
    tests/storage/test_turn_projection_pages.py \
    tests/storage/test_turn_resume_plan_read.py \
    tests/storage/test_continue_plan_acceptance.py \
    tests/storage/test_project_dispatch_segments.py
rc=$?
set -e
"$TOFU_REMOTE_PYTHON" -m tests.storage_authority_sql.receipt_report "$TMPDIR/sql-authority-contracts.xml"
exit "$rc"
