#!/usr/bin/env python3
"""Interleaved per-tool timeline render (epic pt_8b406df8fbe24ae5, step 5).

THE user-visible objective: a finished multi-tool assistant turn must render
each tool's PRECEDING thinking + narration ADJACENT to that tool (inline),
instead of the legacy three grouped blocks (all tools / all thinking / all
content). This test extracts the REAL shipped `renderSegmentTimelineHTML`
(+ its helpers `_renderTimelineBatch` / `_roundsByToolCallId` /
`_segTimelineEnabled`) from tool_rounds.js and evals it in node with the
tool/markdown renderers stubbed to emit identifiable ORDER MARKERS, so we
assert the interleaving ORDER — not styling.

Asserted:
  1. INTERLEAVE: for a 2-batch turn, the output order is
     thinking0 → narration0 → tool(s of batch0) → narration1 → tool(batch1),
     i.e. each batch's prose sits immediately before its tool rows — NOT all
     thinking then all tools.
  2. DELIVERABLE EXCLUDED: the terminal deliverable text is NOT in the timeline
     (the caller renders the answer separately, after the panel).
  3. FALLBACK: renderSegmentTimelineHTML returns "" when segments carry tools
     that can't be matched to toolRounds (caller uses the legacy grouped path).
  4. NC: neuter the batch-flush so ALL segments collapse into one batch →
     the per-batch interleaving assertion fails → proves batching is
     load-bearing for the adjacency.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
TR_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')


def _extract_timeline_fns() -> str:
    """Extract the contiguous timeline helper block from tool_rounds.js:
    from `function _segTimelineEnabled(` through the end of
    `renderSegmentTimelineHTML` (just before `function _renderUnifiedGroup(`)."""
    src = open(TR_JS, encoding='utf-8').read()
    start = src.index('function _segTimelineEnabled(')
    end = src.index('\nfunction _renderUnifiedGroup(')
    chunk = src[start:end]
    assert 'renderSegmentTimelineHTML' in chunk, 'extraction missed the timeline fns'
    assert '_renderTimelineBatch' in chunk, 'extraction missed _renderTimelineBatch'
    return chunk


# Stubs for the collaborators the timeline helper calls. Each emits an
# identifiable marker so we can assert ORDER. _renderToolGroupsHTML emits one
# TOOL[name] marker per round it's given (preserving batch grouping).
_STUBS = r"""
function escapeHtml(s){ return String(s == null ? '' : s); }
function renderMarkdown(s){ return '<md>' + String(s) + '</md>'; }
function t(k){ return k; }
function getToolRoundsFromMsg(m){ return (m && m.toolRounds) || []; }
function _toolPanelHeaderLabel(rounds, active){ return 'HDR'; }
function _renderToolGroupsHTML(rounds, allRounds){
  return (rounds || []).map(function(r){ return '<TOOL name=' + (r.toolName||'') + '>'; }).join('');
}
var localStorage = { _v: {}, getItem: function(k){ return this._v[k] == null ? null : this._v[k]; },
                     setItem: function(k,v){ this._v[k] = String(v); } };
"""

_HARNESS = _STUBS + r"""
const src = process.env.TL_SRC;
eval(src);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A 2-batch finished turn: batch0 = thinking + narration + 2 tools; batch1 =
// narration + 1 tool; then the terminal deliverable answer.
const segments = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'narrate0', deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', input: '{}', llmRound: 0, result: { content: 'x', status: 'done' } },
  { type: 'tool_use', id: 'a2', name: 'read_files', input: '{}', llmRound: 0, result: { content: 'y', status: 'done' } },
  { type: 'text', text: 'narrate1', deliverable: false, llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'apply_diff', input: '{}', llmRound: 1, result: { content: 'z', status: 'done' } },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const msg = {
  role: 'assistant', content: 'THE ANSWER', thinking: 'reason0',
  toolRounds: [
    { toolCallId: 'a1', toolName: 'grep_search', status: 'done', llmRound: 0 },
    { toolCallId: 'a2', toolName: 'read_files', status: 'done', llmRound: 0 },
    { toolCallId: 'b1', toolName: 'apply_diff', status: 'done', llmRound: 1 },
  ],
};

const html = renderSegmentTimelineHTML(segments, msg, 0);

check('nonempty', !!html && html.indexOf('ptool-panel') !== -1);

// Extract the ORDER of markers: thinking(reason0), narration(<md>narrateN),
// and TOOL[name].
function idx(needle){ return html.indexOf(needle); }
const iReason0   = idx('reason0');
const iNarr0     = idx('<md>narrate0</md>');
const iToolGrep  = idx('<TOOL name=grep_search>');
const iToolRead  = idx('<TOOL name=read_files>');
const iNarr1     = idx('<md>narrate1</md>');
const iToolDiff  = idx('<TOOL name=apply_diff>');

// 1. INTERLEAVE: batch0 prose precedes batch0 tools, which precede batch1
//    prose, which precedes batch1 tool. This is the whole point.
check('thinking0_before_narr0', iReason0 >= 0 && iReason0 < iNarr0);
check('narr0_before_tools0',    iNarr0 >= 0 && iNarr0 < iToolGrep);
check('tools0_before_narr1',    iToolGrep < iNarr1 && iToolRead < iNarr1);
check('narr1_before_tool1',     iNarr1 >= 0 && iNarr1 < iToolDiff);
check('narr1_after_tools0',     iNarr1 > iToolRead);

// 2. DELIVERABLE EXCLUDED from the timeline (rendered separately by caller).
check('deliverable_excluded', html.indexOf('THE ANSWER') === -1);

// 3. All three tools present, in order.
check('all_tools_present', iToolGrep >= 0 && iToolRead >= 0 && iToolDiff >= 0);
check('tools_in_order', iToolGrep < iToolRead && iToolRead < iToolDiff);

console.log(out.join('\n'));
"""

_FALLBACK_HARNESS = _STUBS + r"""
const src = process.env.TL_SRC;
eval(src);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Segments reference tools, but msg.toolRounds can't match ANY of them
// (unmatchable ids + no positional fallback because there ARE rounds but with
// different ids). Expect "" → caller falls back to the legacy grouped render.
const segments = [
  { type: 'tool_use', id: 'zzz', name: 'grep_search', input: '{}', llmRound: 0, result: {} },
];
const msg = { role: 'assistant', content: 'a',
              toolRounds: [{ toolCallId: 'DIFFERENT', toolName: 'x', llmRound: 0 }] };
const html = renderSegmentTimelineHTML(segments, msg, 0);
check('fallback_empty_when_unmatchable', html === '');

// Empty segments → "".
check('empty_segments_empty', renderSegmentTimelineHTML([], msg, 0) === '');

// Flag helper: off by default, on when set.
check('flag_off_default', _segTimelineEnabled() === false);
localStorage.setItem('tofu_segment_timeline', '1');
check('flag_on_when_set', _segTimelineEnabled() === true);
console.log(out.join('\n'));
"""

_NC_HARNESS = _STUBS + r"""
// NEUTER: collapse the llmRound batch key so EVERY segment lands in one batch.
// Then the batch1 narration is emitted with batch0's prose (all prose before
// all tools) — the per-batch adjacency ('narr1 after tools0') breaks.
let src = process.env.TL_SRC;
// Force the batch key constant → single batch.
const neutered = src.replace(
  /const key = \(s\.llmRound != null\) \? \("L" \+ s\.llmRound\) : "S";/,
  'const key = "L0";');
if (neutered === src) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const segments = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', input: '{}', llmRound: 0, result: {} },
  { type: 'text', text: 'narrate1', deliverable: false, llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'apply_diff', input: '{}', llmRound: 1, result: {} },
];
const msg = { role: 'assistant', content: 'a', toolRounds: [
  { toolCallId: 'a1', toolName: 'grep_search', llmRound: 0 },
  { toolCallId: 'b1', toolName: 'apply_diff', llmRound: 1 },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);
const iNarr1 = html.indexOf('<md>narrate1</md>');
const iToolGrep = html.indexOf('<TOOL name=grep_search>');
// With one collapsed batch, ALL prose (incl. narrate1) is emitted BEFORE all
// tools → narrate1 comes BEFORE grep_search, breaking the interleave.
check('nc_collapsed_breaks_interleave', iNarr1 >= 0 && iToolGrep >= 0 && iNarr1 < iToolGrep);
console.log(out.join('\n'));
"""


def _run(harness: str) -> str:
    env = dict(os.environ, TL_SRC=_extract_timeline_fns())
    proc = subprocess.run(['node', '-e', harness], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_timeline_interleaves_prose_adjacent_to_tools():
    out = _run(_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'interleave failures:\n' + out
    assert out.count('PASS') >= 9, f'expected >=9 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_timeline_fallback_and_flag():
    out = _run(_FALLBACK_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'fallback/flag failures:\n' + out
    assert out.count('PASS') >= 4, f'expected >=4 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_collapsed_batch_breaks_interleave():
    out = _run(_NC_HARNESS)
    assert 'PASS nc_collapsed_breaks_interleave' in out, (
        'NC control failed — collapsing batches did not break the interleave '
        '(so the per-batch flush is not what produces adjacency):\n' + out)


def test_source_has_timeline_helper():
    src = open(TR_JS, encoding='utf-8').read()
    assert 'function renderSegmentTimelineHTML(' in src
    assert 'function _segTimelineEnabled(' in src


if __name__ == '__main__':
    if not shutil.which('node'):
        print('SKIP — node not available')
    else:
        print(_run(_HARNESS))
        print(_run(_FALLBACK_HARNESS))
        print(_run(_NC_HARNESS))
