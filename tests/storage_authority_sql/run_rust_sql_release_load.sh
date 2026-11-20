#!/bin/sh
set -eu
: "${1:?remote_cargo must supply the checked release binary}"
: "${TOFU_REMOTE_PYTHON:?remote_cargo must supply the shared Python runtime}"
export TOFU_TEST_SQL_AUTHORITY_BIN="$1"
export PYTHONPATH="$PWD"
export TOFU_RUN_SERVER=0
"$TOFU_REMOTE_PYTHON" -c 'import lib, os; assert os.path.realpath(lib.__file__).startswith(os.getcwd() + os.sep), lib.__file__'
"$TOFU_REMOTE_PYTHON" -m pytest -q -p no:cacheprovider \
    'tests/storage/test_turn_projection_reader.py::test_every_authority_reassembles_turn_get_from_its_pages[sqlite]'
"$TOFU_REMOTE_PYTHON" -m pytest -q -p no:cacheprovider -n 8 \
    tests/storage_authority_sql/test_rust_sql_authority_release_load.py
