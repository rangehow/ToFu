#!/usr/bin/env bash
# scripts/verify_epic_e_deferrals.sh — Epic-E deferral acceptance runbook.
#
# ONE-COMMAND post-restart verification (owner directive 2026-08-01). Run
# AFTER the server restarts with a manifest that defers cross_tab_sync.js
# (sub-3A) + health_stream_timer.js (sub-3B) + tofu-pet.js/tofu-scene.js
# (sub-3C) + ui/tool_rounds_rich.js (sub-4, rich conv-meta + timer-watcher
# renderers split out of tool_rounds.js) + access_matrix.js (sub-5A) +
# streaming_swarm_panel.js (sub-5B):
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
#   sub-3C  7. served core EXCLUDES window.TofuPet=/window.TofuScene= assigns
#         8. served feature INCLUDES both assigns
#         (needles are space-free: esbuild collapses 'window.X = {' to 'window.X={')
#   packs   9. advertised i18n pack serves 200
#   sub-4  10. served core EXCLUDES the rich renderer defs but KEEPS the
#               typeof guards + _localizeInspectOps (cross-boundary stay)
#         11. served feature INCLUDES the rich renderer defs
#   sub-5  12. served core EXCLUDES _renderAccessMatrix + _buildSwarmPanelHTML defs
#         13. served feature INCLUDES both; core KEEPS both typeof guards
#   ledger 14. prints measured byte counts for docs/EPIC_E_SIZE_LEDGER.md
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

# ── sub-3C ──
printf '%s' "$CORE_BODY" | grep -q 'window.TofuPet=' \
  && fail "sub-3C.7 core still contains the window.TofuPet assign" \
  || pass "sub-3C.7 core excludes the window.TofuPet assign"
printf '%s' "$CORE_BODY" | grep -q 'window.TofuScene=' \
  && fail "sub-3C.7b core still contains the window.TofuScene assign" \
  || pass "sub-3C.7b core excludes the window.TofuScene assign"
printf '%s' "$FEAT_BODY" | grep -q 'window.TofuPet=' \
  && pass "sub-3C.8 feature includes the window.TofuPet assign" \
  || fail "sub-3C.8 feature MISSING the window.TofuPet assign"
printf '%s' "$FEAT_BODY" | grep -q 'window.TofuScene=' \
  && pass "sub-3C.8b feature includes the window.TofuScene assign" \
  || fail "sub-3C.8b feature MISSING the window.TofuScene assign"

# ── packs ──
if [ -n "$PACK_URLS" ] && [ -n "$PACK" ]; then
  RC=$(code "$BASE/static/js/$PACK")
  [ "$RC" = "200" ] && pass "packs.9 zh pack serves 200 ($PACK)" \
                    || fail "packs.9 zh pack HTTP $RC ($PACK)"
else
  say "SKIP  packs.9 (dual-language bundle mode — no pack advertised)"
fi

# ── sub-4 ──
printf '%s' "$CORE_BODY" | grep -q 'function _renderConvMetaBlock(' \
  && fail "sub-4.10 core still contains the _renderConvMetaBlock def" \
  || pass "sub-4.10 core excludes the _renderConvMetaBlock def"
printf '%s' "$CORE_BODY" | grep -q 'function _renderTimerWatcherBlock(' \
  && fail "sub-4.10b core still contains the _renderTimerWatcherBlock def" \
  || pass "sub-4.10b core excludes the _renderTimerWatcherBlock def"
printf '%s' "$CORE_BODY" | grep -q 'typeof _renderConvMetaBlock' \
  && pass "sub-4.10c core keeps the conv-meta typeof guard" \
  || fail "sub-4.10c core lost the conv-meta typeof guard"
printf '%s' "$CORE_BODY" | grep -q 'function _localizeInspectOps(' \
  && pass "sub-4.10d core keeps _localizeInspectOps (cross-boundary stay)" \
  || fail "sub-4.10d core lost _localizeInspectOps (image tiles break)"
printf '%s' "$FEAT_BODY" | grep -q 'function _renderConvMetaBlock(' \
  && pass "sub-4.11 feature includes the _renderConvMetaBlock def" \
  || fail "sub-4.11 feature MISSING the _renderConvMetaBlock def"
printf '%s' "$FEAT_BODY" | grep -q 'function _renderTimerWatcherBlock(' \
  && pass "sub-4.11b feature includes the _renderTimerWatcherBlock def" \
  || fail "sub-4.11b feature MISSING the _renderTimerWatcherBlock def"

# ── sub-5 (access_matrix + streaming_swarm_panel) ──
printf '%s' "$CORE_BODY" | grep -q 'function _renderAccessMatrix(' \
  && fail "sub-5A.12 core still contains the _renderAccessMatrix def" \
  || pass "sub-5A.12 core excludes the _renderAccessMatrix def"
printf '%s' "$FEAT_BODY" | grep -q 'function _renderAccessMatrix(' \
  && pass "sub-5A.13 feature includes the _renderAccessMatrix def" \
  || fail "sub-5A.13 feature MISSING the _renderAccessMatrix def"
printf '%s' "$CORE_BODY" | grep -q 'function _buildSwarmPanelHTML(' \
  && fail "sub-5B.12b core still contains the _buildSwarmPanelHTML def" \
  || pass "sub-5B.12b core excludes the _buildSwarmPanelHTML def"
printf '%s' "$FEAT_BODY" | grep -q 'function _buildSwarmPanelHTML(' \
  && pass "sub-5B.13b feature includes the _buildSwarmPanelHTML def" \
  || fail "sub-5B.13b feature MISSING the _buildSwarmPanelHTML def"
printf '%s' "$CORE_BODY" | grep -q 'typeof _buildSwarmPanelHTML' \
  && pass "sub-5B.13c core keeps the swarm-panel typeof guards" \
  || fail "sub-5B.13c core lost the swarm-panel typeof guards"

# ── ledger measurements ──
CORE_BYTES=$(printf '%s' "$CORE_BODY" | wc -c)
FEAT_BYTES=$(printf '%s' "$FEAT_BODY" | wc -c)
say ""
say "== ledger measurements (paste into docs/EPIC_E_SIZE_LEDGER.md) =="
say "core    $CORE  $CORE_BYTES bytes"
say "feature $FEAT  $FEAT_BYTES bytes"

say ""
if [ "$FAILS" -eq 0 ]; then
  say "ALL GREEN — Epic-E deferrals sub-3A + sub-3B + sub-3C + sub-4 + sub-5A/5B verified live."
  exit 0
else
  say "$FAILS FAILURE(S) — deferrals NOT verified; do not mark shipped."
  exit 1
fi
