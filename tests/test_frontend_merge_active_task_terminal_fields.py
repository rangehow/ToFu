"""Regression: MERGE_ACTIVE_TASK merges terminal cost-accounting fields, and
``_msgFingerprint`` repaints when they land.

WHY (bug, conv mrzutwddkeuw0n, 2026-07-25)
------------------------------------------
The first turn's cost popover showed only the aggregate rows (Input / Cache
read / Output / Cache 节省 / Total) — NO per-round breakdown, NO Task ID row,
NO key-tail route tag — even though the DB / done event / full+windowed serve
ALL carried ``apiRounds`` (39) + ``_taskId`` (verified end to end).

Chain:
  1. The turn ran 26 min; the tab's IndexedDB cache held a MID-STREAM copy of
     the assistant message (model/content/thinking/toolRounds — none of the
     terminal fields the sync stamps at settle).
  2. A second, queued turn kept the conv CONTINUOUSLY busy (activeTaskId set
     12:13→14:03), so ``loadConversationMessages`` always took the
     MERGE_ACTIVE_TASK keep-local branch — never the OVERWRITE adopt.
  3. That branch merged ONLY ``finishReason`` / ``usage`` / ``model`` from the
     server into kept messages — never ``apiRounds`` / ``_taskId`` / ``cost`` /
     ``provider_id`` / … — so the finish bar half-upgraded: usage showed, cost
     lazily computed, but the per-round table (needs apiRounds), the Task ID
     row (needs _taskId) and the key tail (needs apiRounds[-1]._dispatch)
     never rendered.
  4. ``_msgFingerprint`` folded none of those fields either, so even when they
     later arrived no surgical repaint re-baked the bar.

FIX (static/js/core/conversations.js + static/js/ui/chat_render.js)
-------------------------------------------------------------------
  1. The MERGE_ACTIVE_TASK merge loop now also fills the terminal
     cost-accounting fields (apiRounds upgrade-if-longer, _taskId / cost /
     provider_id / preset / thinkingDepth / modified* / fallback*
     fill-if-missing).
  2. ``_msgFingerprint`` folds a cheap O(1) token (apiRounds count, _taskId /
     usage presence) so the surgical renderChat diff repaints a row the moment
     those fields land.

HARNESS
-------
  A. bare-node: eval the REAL shipped core/conversations.js, seed an
     active-task conv whose local assistant is in the exact half-upgraded
     state (finishReason + usage + cost present; apiRounds/_taskId missing),
     drive the REAL ``loadConversationMessages`` against a stubbed server GET,
     and assert every terminal field lands (plus apiRounds never DOWNGRADES).
     NEUTER: strip the apiRounds merge line on a COPY → the field never
     arrives → the round-table assertion fails.
  B. jsdom: drive the REAL ``_msgFingerprint`` (ui/chat_render.js) and assert
     it moves when apiRounds / _taskId / usage arrive — the repaint trigger.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ═══════════════════════════════════════════════════════════════════════
# Harness A — REAL loadConversationMessages, MERGE_ACTIVE_TASK branch
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_A = r"""
const fs = require('fs');
global.window = global;
global.activeConvId = 'c1';
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.renderConversationList = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);
global.apiUrl = (p) => p;
global.AbortSignal = { timeout: () => undefined };
// External helpers living in OTHER bundle files (not eval'd here).
global.convHasPendingSync = () => false;             // core/pending_sync.js
global._serverHasTranslationLocalLacks = () => false; // core/conv_persist_helpers.js
global.ConvCache = {
  isAvailable: () => true,
  get: async () => null,          // cache MISS → Phase-2 runs with cacheHit=false
  getMeta: async () => null,
  getAllMeta: async () => [],
  put: async () => {},
  remove: async () => {},
};
global.ConvView = { replaceAll: () => {} };

// The server conv body: same length as local (no append, no orphan branch).
const _FULL_ASSISTANT = {
  role: 'assistant', _msgId: 'm1', content: 'short',
  thinking: 't', toolRounds: [{ roundNum: 0, toolName: 'grep_search' }],
  model: 'kimi-k3', provider_id: 'sankuai', preset: 'p', thinkingDepth: 'high',
  finishReason: 'stop',
  usage: { prompt_tokens: 5854882, completion_tokens: 34741,
           cache_read_tokens: 5669120, cache_write_tokens: 0 },
  apiRounds: Array.from({ length: 39 }, (_, i) => ({
    round: i + 1, model: 'kimi-k3', tag: 'R' + (i + 1),
    usage: { prompt_tokens: 150000, completion_tokens: 800 },
  })),
  _taskId: 'task-settled-1',
  cost: { costCny: 18.5138, costUsd: 2.5 },
  modifiedFiles: 2, modifiedFileList: [{ path: 'a.js' }],
  fallbackModel: 'aws.claude-opus-4.8', fallbackFrom: 'kimi-k3',
  fallbackReason: 'ratelimit: 429', fallbackKind: 'ratelimit',
};
let SERVER_BODY = null;
global.Api = {
  conversations: {
    getResponse: async () => ({
      status: 200, ok: true,
      headers: { get: () => null },
      json: async () => SERVER_BODY,
    }),
    get: async () => SERVER_BODY,
  },
};

global.conversations = [];
eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conversations.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function seedConv(localAssistant) {
  conversations.length = 0;
  conversations.push({
    id: 'c1', title: 'c1', _needsLoad: true,
    messages: [
      { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
      localAssistant,
    ],
    createdAt: 1, updatedAt: 1,
    activeTaskId: 'task-live-now',   // ← the MERGE_ACTIVE_TASK gate
  });
}

(async () => {
  // ══ A. Half-upgraded local (finishReason + usage + cost; NO apiRounds /
  //    _taskId — the exact 14:00 screenshot state) + full server copy. ══
  {
    const localAssistant = {
      role: 'assistant', _msgId: 'm1',
      content: 'locally-longer-content-than-server',  // keep: upgrade is longer-wins
      thinking: 't', toolRounds: [{ roundNum: 0, toolName: 'grep_search' }],
      model: 'kimi-k3', finishReason: 'stop',
      usage: { prompt_tokens: 5854882, completion_tokens: 34741,
               cache_read_tokens: 5669120, cache_write_tokens: 0 },
      cost: { costCny: 18.5138, costUsd: 2.5 },   // lazy write-back already landed
      // apiRounds / _taskId / provider_id / preset / thinkingDepth /
      // modified* / fallback* — ALL missing (the degraded cache shape).
    };
    seedConv(localAssistant);
    SERVER_BODY = {
      id: 'c1', title: 'c1', rev: 7,
      updatedAt: 2, messages: [
        { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
        JSON.parse(JSON.stringify(_FULL_ASSISTANT)),
      ],
    };
    await loadConversationMessages('c1');
    const lm = conversations[0].messages[1];
    check('A_branch_kept_local_content', lm.content === 'locally-longer-content-than-server');
    check('A_apiRounds_merged', Array.isArray(lm.apiRounds) && lm.apiRounds.length === 39);
    check('A_apiRounds_is_server_copy', lm.apiRounds
      && lm.apiRounds[38] && lm.apiRounds[38].round === 39);
    check('A_taskId_merged', lm._taskId === 'task-settled-1');
    check('A_cost_preserved', !!(lm.cost && lm.cost.costCny === 18.5138));
    check('A_provider_merged', lm.provider_id === 'sankuai');
    check('A_preset_merged', lm.preset === 'p');
    check('A_thinkingDepth_merged', lm.thinkingDepth === 'high');
    check('A_modifiedFiles_merged', lm.modifiedFiles === 2);
    check('A_modifiedFileList_merged',
      Array.isArray(lm.modifiedFileList) && lm.modifiedFileList.length === 1);
    check('A_fallbackModel_merged', lm.fallbackModel === 'aws.claude-opus-4.8');
    check('A_fallbackFrom_merged', lm.fallbackFrom === 'kimi-k3');
    check('A_fallbackReason_merged', lm.fallbackReason === 'ratelimit: 429');
    check('A_fallbackKind_merged', lm.fallbackKind === 'ratelimit');
    // Pre-existing three-field behaviour stays intact (control).
    check('A_control_finishReason', lm.finishReason === 'stop');
    check('A_control_usage', !!(lm.usage && lm.usage.prompt_tokens === 5854882));
    check('A_control_model', lm.model === 'kimi-k3');
  }

  // ══ B. apiRounds NEVER downgrades: local 39 rounds, server 10 → keep 39. ══
  {
    const localAssistant = {
      role: 'assistant', _msgId: 'm1', content: 'x', thinking: 't',
      toolRounds: [{ roundNum: 0 }],
      apiRounds: Array.from({ length: 39 }, (_, i) => ({ round: i + 1 })),
      finishReason: 'stop', usage: { prompt_tokens: 10 },
    };
    seedConv(localAssistant);
    const serverAssistant = JSON.parse(JSON.stringify(_FULL_ASSISTANT));
    serverAssistant.apiRounds = serverAssistant.apiRounds.slice(0, 10);
    serverAssistant.content = 'x';
    SERVER_BODY = {
      id: 'c1', title: 'c1', rev: 8,
      updatedAt: 3, messages: [
        { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
        serverAssistant,
      ],
    };
    await loadConversationMessages('c1');
    const lm = conversations[0].messages[1];
    check('B_apiRounds_no_downgrade',
      Array.isArray(lm.apiRounds) && lm.apiRounds.length === 39);
  }

  // ══ C. Mid-stream partial local (10 rounds) + server 39 → upgrade to 39. ══
  {
    const localAssistant = {
      role: 'assistant', _msgId: 'm1', content: 'x', thinking: 't',
      toolRounds: [{ roundNum: 0 }],
      apiRounds: Array.from({ length: 10 }, (_, i) => ({ round: i + 1 })),
      finishReason: 'stop', usage: { prompt_tokens: 10 },
    };
    seedConv(localAssistant);
    SERVER_BODY = {
      id: 'c1', title: 'c1', rev: 9,
      updatedAt: 4, messages: [
        { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
        JSON.parse(JSON.stringify(_FULL_ASSISTANT)),
      ],
    };
    await loadConversationMessages('c1');
    const lm = conversations[0].messages[1];
    check('C_apiRounds_upgrade_to_longer',
      Array.isArray(lm.apiRounds) && lm.apiRounds.length === 39);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run_a(js_path: str) -> str:
    harness = os.path.join(HERE, '_merge_active_task_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS_A)
    try:
        proc = subprocess.run(
            ['node', harness, js_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_merge_active_task_fills_terminal_fields():
    output = _run_a(os.path.join(JS_DIR, 'core', 'conversations.js'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'MERGE_ACTIVE_TASK terminal-field merge failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_apiRounds_merge_line_is_load_bearing(tmp_path):
    """NEUTER: strip the apiRounds merge on a COPY → A_apiRounds_merged must
    FAIL (the local message keeps its missing round list → the popover's
    per-round table can never render). Proves the merge line is what delivers
    apiRounds into a kept-local message. Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = """        if (Array.isArray(sm.apiRounds)
            && (!Array.isArray(lm.apiRounds) || sm.apiRounds.length > lm.apiRounds.length)) {
          lm.apiRounds = sm.apiRounds;
        }"""
    assert src.count(needle) == 1, (
        'apiRounds merge fragment drifted — update the neuter target')
    neutered = src.replace(needle, '', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_apirounds.js'
    copy.write_text(neutered, encoding='utf-8')

    output = _run_a(str(copy))
    assert 'FAIL A_apiRounds_merged' in output, (
        'NEUTER did not bite: apiRounds still merged without the merge line.\n'
        + output)
    # The rest of the merge list must keep working without the apiRounds line.
    assert 'PASS A_taskId_merged' in output, output

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'


# ═══════════════════════════════════════════════════════════════════════
# Harness B — REAL _msgFingerprint moves when terminal fields land
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_B = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  // argv[4]=core/translation_model.js (ships translationFingerprint, which
  // _msgFingerprint delegates to) — loaded BEFORE the fn under test.
  targets: [process.argv[4], process.argv[2]],   // then ui/chat_render.js
  globals: {},
});

if (typeof _msgFingerprint !== 'function') {
  check('fingerprint_exposed', false);
  report();
  return;
}
check('fingerprint_exposed', true);

// The exact pre-merge steady state: settled-looking message (finishReason +
// usage + cost all present) but NO apiRounds / _taskId — the screenshot shape.
function degradedMsg() {
  return {
    role: 'assistant',
    content: 'The final answer.',
    thinking: '',
    finishReason: 'stop',
    usage: { prompt_tokens: 5854882, completion_tokens: 34741,
             cache_read_tokens: 5669120, cache_write_tokens: 0 },
    cost: { costCny: 18.5138, costUsd: 2.5 },
    model: 'kimi-k3',
    toolRounds: [{ roundNum: 0 }],
  };
}

const before = _msgFingerprint(degradedMsg());

// apiRounds land (the merge fills 39 rounds) → fingerprint MUST move or the
// surgical diff never repaints the rounds-less finish bar.
const m1 = degradedMsg();
m1.apiRounds = Array.from({ length: 39 }, (_, i) => ({ round: i + 1 }));
const withRounds = _msgFingerprint(m1);
check('fp_moves_when_apiRounds_land', before !== withRounds);

// The fold is count-sensitive: a partial (mid-stream) 10-round list upgrading
// to the settled 39 must ALSO move it (upgrade-if-longer repaint).
const m2 = degradedMsg();
m2.apiRounds = Array.from({ length: 10 }, (_, i) => ({ round: i + 1 }));
const with10 = _msgFingerprint(m2);
check('fp_moves_when_round_count_grows', with10 !== withRounds);

// _taskId lands → moves (the popover Task ID row repaint).
const m3 = degradedMsg();
m3.apiRounds = m1.apiRounds;
m3._taskId = 'task-settled-1';
const withTaskId = _msgFingerprint(m3);
check('fp_moves_when_taskId_lands', withRounds !== withTaskId);

// usage landing on a bare message moves it too (the token-tag repaint) —
// guards the same class for a message painted before any usage existed.
const bare = { role: 'assistant', content: 'x', thinking: '' };
const bareFp = _msgFingerprint(bare);
const withUsage = _msgFingerprint({ ...bare, usage: { prompt_tokens: 5 } });
check('fp_moves_when_usage_lands', bareFp !== withUsage);

// Stability control: two equally-degraded messages fingerprint identically
// (the fold adds no noise when the fields are absent).
check('NC_degraded_pair_stable',
  _msgFingerprint(degradedMsg()) === _msgFingerprint(degradedMsg()));

report();
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_fingerprint_moves_when_terminal_fields_land():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'chat_render.js'),
        body_js=_HARNESS_B,
        extra_targets=[os.path.join(JS_DIR, 'core', 'translation_model.js')],
        min_pass=6,
        label='terminal-fields-fingerprint',
    )
