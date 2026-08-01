#!/usr/bin/env bash
# scripts/verify_epic_e_deferrals.sh — Epic-E deferral acceptance runbook.
#
# ONE-COMMAND post-restart verification (owner directive 2026-08-01). Run
# AFTER the server restarts with a manifest that defers cross_tab_sync.js
# (sub-3A) + health_stream_timer.js (sub-3B) + tofu-pet.js/tofu-scene.js
# (sub-3C) + ui/tool_rounds_rich.js (sub-4, rich conv-meta + timer-watcher
# renderers split out of tool_rounds.js) + access_matrix.js (sub-5A) +
# streaming_swarm_panel.js (sub-5B) + myday.js/myday_tasks.js (sub-6) +
# project.js split (sub-7, state core + panel deferred) +
# ui/finish_info_rich.js (sub-8, lazy cost popover) +
# settings-panel six-pack (sub-9, update/skills/memory/optimizer/timer/preferences) +
# settings/ family (sub-10, the line-closer — branding.js STAYS core, boot
# caller main.js:88/349):
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
#   sub-6  14. served core EXCLUDES the myday defs but KEEPS the stub entries
#         15. served feature INCLUDES the myday defs
#   sub-7  16. served core KEEPS the state defs (loadProjectStatus /
#               _updateProjectUI / _applyProjectData) + reverse guard
#         17. served feature INCLUDES the panel defs (openProjectModal /
#               browseDirectory); zero state defs in feature
#   sub-8  18. served core EXCLUDES the popover defs but KEEPS
#               _cacheBreakReason + the ctx stash + the stub entry
#         19. served feature INCLUDES _buildCostPopover; zero phrase-family dup
#   sub-9  20. served core EXCLUDES the six-pack defs but KEEPS the stub
#               entries + the tofu:feature-bundle-loaded dispatch
#         21. served feature INCLUDES the six-pack defs
#   sub-10 22. served core EXCLUDES the settings-family defs (openSettings /
#               _renderProvidersTab) but KEEPS branding's _modelShortName
#               (boot caller main.js:88/349) + the entry stubs
#         23. served feature INCLUDES the settings-family defs
#   ledger 24. prints measured byte counts for docs/EPIC_E_SIZE_LEDGER.md
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

# ── sub-6 (myday + myday_tasks) ──
printf '%s' "$CORE_BODY" | grep -q 'function openDailyReport(' \
  && fail "sub-6.14 core still contains the openDailyReport def" \
  || pass "sub-6.14 core excludes the openDailyReport def"
printf '%s' "$CORE_BODY" | grep -q 'function _mydayScheduleReminder(' \
  && fail "sub-6.14b core still contains the _mydayScheduleReminder def" \
  || pass "sub-6.14b core excludes the _mydayScheduleReminder def"
printf '%s' "$CORE_BODY" | grep -q '"openDailyReport"' \
  && pass "sub-6.14c core keeps the openDailyReport stub entry" \
  || fail "sub-6.14c core lost the openDailyReport stub entry"
printf '%s' "$CORE_BODY" | grep -q '"_mydayTriggerGenerate"' \
  && pass "sub-6.14d core keeps the _mydayTriggerGenerate stub entry" \
  || fail "sub-6.14d core lost the _mydayTriggerGenerate stub entry"
printf '%s' "$FEAT_BODY" | grep -q 'function openDailyReport(' \
  && pass "sub-6.15 feature includes the openDailyReport def" \
  || fail "sub-6.15 feature MISSING the openDailyReport def"
printf '%s' "$FEAT_BODY" | grep -q 'function _mydayToggleTodo(' \
  && pass "sub-6.15b feature includes the myday_tasks def" \
  || fail "sub-6.15b feature MISSING the myday_tasks def"

# ── sub-7 (project split: state core + panel deferred) ──
printf '%s' "$CORE_BODY" | grep -q 'function loadProjectStatus(' \
  && pass "sub-7.16 core keeps the loadProjectStatus def (state)" \
  || fail "sub-7.16 core LOST the loadProjectStatus def — boot bar restore breaks"
printf '%s' "$CORE_BODY" | grep -q 'function _updateProjectUI(' \
  && pass "sub-7.16b core keeps the _updateProjectUI def (state)" \
  || fail "sub-7.16b core LOST the _updateProjectUI def"
printf '%s' "$CORE_BODY" | grep -q 'function _applyProjectData(' \
  && pass "sub-7.16c core keeps the _applyProjectData def (SSE entry)" \
  || fail "sub-7.16c core LOST the _applyProjectData def"
printf '%s' "$CORE_BODY" | grep -q 'typeof saveRecentProject' \
  && pass "sub-7.16d core keeps the saveRecentProject reverse guard" \
  || fail "sub-7.16d core lost the saveRecentProject reverse guard"
printf '%s' "$FEAT_BODY" | grep -q 'function openProjectModal(' \
  && pass "sub-7.17 feature includes the openProjectModal def (panel)" \
  || fail "sub-7.17 feature MISSING the openProjectModal def"
printf '%s' "$FEAT_BODY" | grep -q 'function browseDirectory(' \
  && pass "sub-7.17b feature includes the browseDirectory def (panel)" \
  || fail "sub-7.17b feature MISSING the browseDirectory def"
printf '%s' "$FEAT_BODY" | grep -q 'function loadProjectStatus(' \
  && fail "sub-7.17c feature DUPLICATES loadProjectStatus (double state)" \
  || pass "sub-7.17c feature has no loadProjectStatus duplication"
printf '%s' "$CORE_BODY" | grep -q '"openProjectModal"' \
  && pass "sub-7.17d core feature-loader carries the openProjectModal stub" \
  || fail "sub-7.17d core feature-loader MISSING the openProjectModal stub"

# ── sub-8 (finish_info cost-popover lazy split) ──
printf '%s' "$CORE_BODY" | grep -q 'function _buildCostPopover(' \
  && fail "sub-8.18 core still contains the _buildCostPopover def" \
  || pass "sub-8.18 core excludes the _buildCostPopover def"
printf '%s' "$CORE_BODY" | grep -q 'function _toggleCostPopover(' \
  && fail "sub-8.18b core still contains the _toggleCostPopover def" \
  || pass "sub-8.18b core excludes the _toggleCostPopover def"
printf '%s' "$CORE_BODY" | grep -q 'function _cacheBreakReason(' \
  && pass "sub-8.18c core keeps _cacheBreakReason (cold phrase family)" \
  || fail "sub-8.18c core LOST _cacheBreakReason (warn tooltip breaks)"
printf '%s' "$CORE_BODY" | grep -q '_costCtxByMsg.set(' \
  && pass "sub-8.18d core keeps the ctx stash" \
  || fail "sub-8.18d core lost the _costCtxByMsg stash"
printf '%s' "$CORE_BODY" | grep -q '"_toggleCostPopover"' \
  && pass "sub-8.18e core keeps the _toggleCostPopover stub entry" \
  || fail "sub-8.18e core lost the _toggleCostPopover stub entry"
printf '%s' "$FEAT_BODY" | grep -q 'function _buildCostPopover(' \
  && pass "sub-8.19 feature includes the _buildCostPopover def" \
  || fail "sub-8.19 feature MISSING the _buildCostPopover def"
printf '%s' "$FEAT_BODY" | grep -q 'function _cacheBreakReason(' \
  && fail "sub-8.19b feature DUPLICATES _cacheBreakReason" \
  || pass "sub-8.19b feature has no _cacheBreakReason duplication"

# ── sub-9 (settings-panel six-pack deferral) ──
printf '%s' "$CORE_BODY" | grep -q 'function openUpdateDialog(' \
  && fail "sub-9.20 core still contains the openUpdateDialog def" \
  || pass "sub-9.20 core excludes the openUpdateDialog def"
printf '%s' "$CORE_BODY" | grep -q 'function toggleTimerPanel(' \
  && fail "sub-9.20b core still contains the toggleTimerPanel def" \
  || pass "sub-9.20b core excludes the toggleTimerPanel def"
printf '%s' "$CORE_BODY" | grep -q 'function toggleOptimizerPanel(' \
  && fail "sub-9.20c core still contains the toggleOptimizerPanel def" \
  || pass "sub-9.20c core excludes the toggleOptimizerPanel def"
printf '%s' "$CORE_BODY" | grep -q 'function openMemoryModal(' \
  && fail "sub-9.20d core still contains the openMemoryModal def" \
  || pass "sub-9.20d core excludes the openMemoryModal def"
printf '%s' "$CORE_BODY" | grep -q 'function _populateSkillsTab(' \
  && fail "sub-9.20e core still contains the _populateSkillsTab def" \
  || pass "sub-9.20e core excludes the _populateSkillsTab def"
printf '%s' "$CORE_BODY" | grep -q '"toggleTimerPanel"' \
  && pass "sub-9.20f core keeps the toggleTimerPanel stub entry" \
  || fail "sub-9.20f core lost the toggleTimerPanel stub entry"
printf '%s' "$CORE_BODY" | grep -q 'tofu:feature-bundle-loaded' \
  && pass "sub-9.20g core keeps the land-event dispatch + mobile re-wrap" \
  || fail "sub-9.20g core lost the tofu:feature-bundle-loaded mechanics"
printf '%s' "$FEAT_BODY" | grep -q 'function openUpdateDialog(' \
  && pass "sub-9.21 feature includes the openUpdateDialog def" \
  || fail "sub-9.21 feature MISSING the openUpdateDialog def"
printf '%s' "$FEAT_BODY" | grep -q 'function toggleTimerPanel(' \
  && pass "sub-9.21b feature includes the toggleTimerPanel def" \
  || fail "sub-9.21b feature MISSING the toggleTimerPanel def"
printf '%s' "$FEAT_BODY" | grep -q 'function openMemoryModal(' \
  && pass "sub-9.21c feature includes the openMemoryModal def" \
  || fail "sub-9.21c feature MISSING the openMemoryModal def"

# ── sub-10 (settings family deferral; branding boundary stays core) ──
printf '%s' "$CORE_BODY" | grep -q 'function openSettings(' \
  && fail "sub-10.22 core still contains the openSettings def" \
  || pass "sub-10.22 core excludes the openSettings def"
printf '%s' "$CORE_BODY" | grep -q 'function _renderProvidersTab(' \
  && fail "sub-10.22b core still contains the _renderProvidersTab def" \
  || pass "sub-10.22b core excludes the _renderProvidersTab def"
printf '%s' "$CORE_BODY" | grep -q 'function _populateMcpTab(' \
  && fail "sub-10.22c core still contains the _populateMcpTab def" \
  || pass "sub-10.22c core excludes the _populateMcpTab def"
printf '%s' "$CORE_BODY" | grep -q 'function _modelShortName(' \
  && pass "sub-10.22d core keeps _modelShortName (branding boundary — boot caller main.js:88/349)" \
  || fail "sub-10.22d core LOST _modelShortName — branding wrongly deferred, boot model paint ReferenceError"
printf '%s' "$CORE_BODY" | grep -q '"openSettings"' \
  && pass "sub-10.22e core keeps the openSettings stub entry" \
  || fail "sub-10.22e core lost the openSettings stub entry"
printf '%s' "$CORE_BODY" | grep -q '"switchSettingsTab"' \
  && pass "sub-10.22f core keeps the switchSettingsTab stub entry" \
  || fail "sub-10.22f core lost the switchSettingsTab stub entry"
printf '%s' "$FEAT_BODY" | grep -q 'function openSettings(' \
  && pass "sub-10.23 feature includes the openSettings def" \
  || fail "sub-10.23 feature MISSING the openSettings def"
printf '%s' "$FEAT_BODY" | grep -q 'function _renderProvidersTab(' \
  && pass "sub-10.23b feature includes the _renderProvidersTab def" \
  || fail "sub-10.23b feature MISSING the _renderProvidersTab def"
printf '%s' "$FEAT_BODY" | grep -q 'function _populateMcpTab(' \
  && pass "sub-10.23c feature includes the _populateMcpTab def" \
  || fail "sub-10.23c feature MISSING the _populateMcpTab def"
printf '%s' "$FEAT_BODY" | grep -q 'function _modelShortName(' \
  && fail "sub-10.23d feature DUPLICATES _modelShortName (double branding)" \
  || pass "sub-10.23d feature has no _modelShortName duplication"

# ── ledger measurements ──
CORE_BYTES=$(printf '%s' "$CORE_BODY" | wc -c)
FEAT_BYTES=$(printf '%s' "$FEAT_BODY" | wc -c)
say ""
say "== ledger measurements (paste into docs/EPIC_E_SIZE_LEDGER.md) =="
say "core    $CORE  $CORE_BYTES bytes"
say "feature $FEAT  $FEAT_BYTES bytes"

say ""
if [ "$FAILS" -eq 0 ]; then
  say "ALL GREEN — Epic-E deferrals sub-3A/3B/3C/4/5A/5B/6/7/8/9/10 verified live."
  exit 0
else
  say "$FAILS FAILURE(S) — deferrals NOT verified; do not mark shipped."
  exit 1
fi
