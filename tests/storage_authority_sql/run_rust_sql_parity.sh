#!/bin/sh
# remote_cargo --after-build runner: Python oracle and the just-built Rust
# authority run on the SAME disposable node, never on the production pod.
set -eu
: "${1:?remote_cargo must supply the checked release binary}"
export TOFU_TEST_SQL_AUTHORITY_BIN="$1"
export PYTHONPATH="$PWD"
export TOFU_RUN_SERVER=0
PYTHON=${TOFU_REMOTE_PYTHON:-python3}
"$PYTHON" - <<'PY'
import json
import os
import lib
from tests.support.sql_authority_identity import remote_build_identity

assert os.path.realpath(lib.__file__).startswith(os.getcwd() + os.sep), lib.__file__
print(json.dumps({'receipt': 'sql-authority-build.v1', **remote_build_identity(require_clean=True)},
                 sort_keys=True), flush=True)
PY
slice=${2:-full}
case "$slice" in
    changed)
        diff=tests/storage_authority_sql/test_rust_sql_authority_differential.py
        set -- "$diff::test_port_coverage" "$diff::test_explicit_python_oracle_ignores_inherited_rust_candidate" \
          "$diff::test_identity_rejects_python_versus_python_collapse" \
          "$diff::test_identity_reads_actual_command_not_only_popen_args" \
          "$diff::test_script_matches_python_authority[record put version CAS conflict then success]" \
          "$diff::test_script_matches_python_authority[turn attempt regenerate creates and replays atomically]"
        ;;
    full)
        set -- tests/storage_authority_sql/test_rust_sql_authority_differential.py \
          tests/storage_authority_sql/test_rust_sql_authority_taxonomy.py \
          tests/storage_authority_sql/test_rust_sql_authority_launch_limits.py
        ;;
    *) echo "unknown SQL authority slice: $slice" >&2; exit 2 ;;
esac
set +e
"$PYTHON" -m pytest -q -p no:cacheprovider -n 6 \
    --junitxml="$TMPDIR/sql-authority-oracle.xml" -o junit_family=legacy \
    "$@" tests/storage_authority_sql/test_rust_sql_authority_identity.py \
    tests/storage_authority_sql/test_rust_sql_authority_wire.py \
    tests/storage_authority_sql/test_rust_sql_authority_outbox_replay.py \
    tests/storage/test_storage_sql_authority_seam.py
rc=$?
set -e
"$PYTHON" -m tests.storage_authority_sql.receipt_report "$TMPDIR/sql-authority-oracle.xml"
[ "$rc" -eq 0 ] || exit "$rc"
[ "$slice" = full ] || exit 0
sh tests/storage_authority_sql/run_rust_sql_contracts.sh "$TOFU_TEST_SQL_AUTHORITY_BIN"
set +e
"$PYTHON" -m pytest -q -p no:cacheprovider -n 8 \
    --junitxml="$TMPDIR/sql-authority-load.xml" -o junit_family=legacy \
    tests/storage_authority_sql/test_rust_sql_authority_release_load.py
rc=$?
set -e
"$PYTHON" -m tests.storage_authority_sql.receipt_report "$TMPDIR/sql-authority-load.xml"
exit "$rc"
