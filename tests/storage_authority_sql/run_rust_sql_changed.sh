#!/bin/sh
# Keep this follow-up bounded to the identity, wire, replay and launch contracts.
set -eu
exec sh tests/storage_authority_sql/run_rust_sql_parity.sh "$1" changed
