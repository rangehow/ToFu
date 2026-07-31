#!/usr/bin/env bash
# scripts/verify_epic_e_deferrals.sh — Epic-E deferral acceptance runbook.
#
# ONE-COMMAND post-restart verification (owner directive 2026-08-01). Run
# AFTER the server restarts with a manifest that defers cross_tab_sync.js
# (sub-3A) + health_stream_timer.js (sub-3B):
#
#     bash scripts/verify_epic_e_deferrals.sh [base_url]
#
# base_url defaults to http://localhost:15000.
#
# Checks (all against the SERVED artifacts, never the source tree):
#   sub-3A  1. served core EXCLUDES _handleCrossTabMsg (cross_tab_sync gone)
#         2. served feature INCLUDES _handleCrossTabMsg
#         3. served core's feature-loader carries the _wireConvSyncPush stub
#   sub-3B  4. served core EXCLUDES the twStart DEFINITION + _streamTimers
#         5. served feature INCLUDES the twStart DEFINITION + streamHealthSubscribe
#         6. served core KEEPS the typeof-guarded call sites (gates intact)
#   packs   7. advertised i18n pack serves 200
#   ledger  8. prints measured byte counts for docs/EPIC_E_SIZE_LEDGER.md
#
# Exit 0 = all green; exit 1 = at least one failure (details on stdout).

set -u
BASE="${1:-http://localhost:15000}"
FAILS=0

say()  { printf '%s\n' "$*"; }
pass() { say "PASS  $1"; }
fail() { say "FAIL  $1"; FAILS=$((FAILS+1)); }

fetch() { curl -sL --max-time 60 "$1"; }
code()  { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$1"; }

say "== Epic-E deferral verification against $BASE =="

INDEX=$(fetch "$BASE/")
CORE=$(printf '%s' "$INDEX" | grep -o 'bundle-[0-9a-f]*\.js' | head -1)
FEAT=$(printf '%s' "$INDEX" | grep -o 'feature-[0-9a-f]*\.js' | head -1)
PACK=$(printf '%s' "$INDEX" | grep -o 'i18n-zh-[0-9a-f]*\.js' | head -1)
PACK_URLS=$(printf '%s' "$INDEX" | grep -o '__I18N_PACK_URLS__' | head -1)

[ -n "$CORE" ] && pass "index advertises core: $CORE" || { fail "index advertises NO core bundle"; exit 1; }
[ -n "$FEAT" ] && pass "index advertises feature: $FEAT" || fail "index advertises NO feature bundle"

CORE_BODY=$(fetch "$BASE/static/js/$CORE")
FEAT_BODY=$(fetch "$BASE/static/js/$FEAT")

# ── sub-3A ──
printf '%s' "$CORE_BODY" | grep -q '_handleCrossTabMsg' \
  && fail "sub-3A.1 core still contains _handleCrossTabMsg" \
  || pass "sub-3A.1 core excludes _handleCrossTabMsg"
printf '%s' "$FEAT_BODY" | grep -q '_handleCrossTabMsg' \
  && pass "sub-3A.2 feature includes _handleCrossTabMsg" \
  || fail "sub-3A.2 feature MISSING _handleCrossTabMsg"
printf '%s' "$CORE_BODY" | grep -q '_wireConvSyncPush' \
  && pass "sub-3A.3 core feature-loader carries _wireConvSyncPush (stub entry)" \
  || fail "sub-3A.3 core feature-loader MISSING _wireConvSyncPush"

# ── sub-3B ──
printf '%s' "$CORE_BODY" | grep -q 'function twStart(' \
  && fail "sub-3B.4 core still contains the twStart definition" \
  || pass "sub-3B.4 core excludes the twStart definition"
printf '%s' "$CORE_BODY" | grep -q '_streamTimers' \
  && fail "sub-3B.4b core still contains _streamTimers (module state)" \
  || pass "sub-3B.4b core excludes _streamTimers"
printf '%s' "$FEAT_BODY" | grep -q 'function twStart(' \
  && pass "sub-3B.5 feature includes the twStart definition" \
  || fail "sub-3B.5 feature MISSING the twStart definition"
printf '%s' "$FEAT_BODY" | grep -q 'streamHealthSubscribe' \
  && pass "sub-3B.5b feature includes streamHealthSubscribe" \
  || fail "sub-3B.5b feature MISSING streamHealthSubscribe"
printf '%s' "$CORE_BODY" | grep -q 'typeof twStart' \
  && pass "sub-3B.6 core keeps the typeof-guarded call sites" \
  || fail "sub-3B.6 core lost the typeof-guarded call sites"

# ── packs ──
if [ -n "$PACK_URLS" ] && [ -n "$PACK" ]; then
  RC=$(code "$BASE/static/js/$PACK")
  [ "$RC" = "200" ] && pass "packs.7 zh pack serves 200 ($PACK)" \
                    || fail "packs.7 zh pack HTTP $RC ($PACK)"
else
  say "SKIP  packs.7 (dual-language bundle mode — no pack advertised)"
fi

# ── ledger measurements ──
CORE_BYTES=$(printf '%s' "$CORE_BODY" | wc -c)
FEAT_BYTES=$(printf '%s' "$FEAT_BODY" | wc -c)
say ""
say "== ledger measurements (paste into docs/EPIC_E_SIZE_LEDGER.md) =="
say "core    $CORE  $CORE_BYTES bytes"
say "feature $FEAT  $FEAT_BYTES bytes"

say ""
if [ "$FAILS" -eq 0 ]; then
  say "ALL GREEN — Epic-E deferrals sub-3A + sub-3B verified live."
  exit 0
else
  say "$FAILS FAILURE(S) — deferrals NOT verified; do not mark shipped."
  exit 1
fi
