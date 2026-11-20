#!/bin/sh
# Remote Cargo oracle: exact fresh daemon, disposable authority, no provider I/O.
set -eu
: "${TOFU_REMOTE_PYTHON:?remote test Python is required}"
: "${TOFU_REMOTE_SOURCE_COMMIT:?clean source is required}"
: "${TOFU_REMOTE_BINARY_SHA256:?fresh binary digest is required}"
export TOFU_TEST_TOFUDB_BIN="$1"
export TOFUDB_TRANSPORT=daemon
export TOFU_PERSONAL_STORAGE_BACKEND=tofudb
export PYTHONDONTWRITEBYTECODE=1
export TOFU_REMOTE_BINARY_SHA256
exec "$TOFU_REMOTE_PYTHON" -m pytest -q -o addopts='' -p no:cacheprovider \
  tests/scheduler/test_scheduled_detect_principal.py -k native
