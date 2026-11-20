#!/bin/sh
set -eu
actual=$(sha256sum < "$1"); test "${actual%% *}" = "$TOFU_REMOTE_BINARY_SHA256"
printf 'account HTTP source=%s binary=%s\n' "$TOFU_REMOTE_SOURCE_SHA256" "$TOFU_REMOTE_BINARY_SHA256"
exec "$TOFU_REMOTE_PYTHON" -m tests.support.native_account_http "$1"
