#!/usr/bin/env python3
"""The peer-message inbox row must (a) attribute the sender by conversation
TITLE, not a raw id, and (b) place the verbatim "模型原文" entry on the FAR
RIGHT of the header — the same convention every other tool row follows.

WHY THIS TEST EXISTS
--------------------
The reported bug (screenshot): a round-boundary peer injection ("收到 N 条对话
消息") rendered the sender as a bare truncated conv id (``mrnaj25i``) and buried
the verbatim "model view" toggle INSIDE each body card. Every other tool row
carries its "模型原文" affordance on the far right of the header, and a human
cares WHO sent the message — a title, not an opaque id.

FIX (static/js/ui/tool_rounds.js ``_renderPeerInjectRow``)
  1. The sender is resolved through the shared ``convTitleById`` seam into a
     ``.sw-peer-from-bubble`` TITLE bubble (raw id only in the tooltip).
  2. The verbatim model text moves to a header ``[data-tc-preview-text]`` button
     (``_tcModelViewBtnForText`` + ``_injectVerbatimText``) rendered AFTER the
     info badge, so CSS ``margin-left:auto`` floats it to the far right. The
     per-card ``.sw-card-raw`` toggle and the explicit ``.sw-inbox-row-chev``
     span are removed (a CSS ``::after`` caret handles the collapse affordance).

This test EXTRACTS the real shipped helpers + ``_renderPeerInjectRow`` and evals
them in node, asserting the title bubble, the far-right model-view button, and
the absence of the raw id from the visible label. NC neuters ``convTitleById``
so the bubble falls back — proving the title lookup is load-bearing.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
TR_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')


def _pull_fn(src: str, name: str) -> str:
    """Brace-balanced extraction of a top-level `function <name>(...) {...}`."""
    i = src.index('function ' + name + '(')
    depth = 0
    j = src.index('{', i)
    for k in range(j, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f'could not extract {name}')


_HARNESS = r"""
const fs = require('fs');
let src = fs.readFileSync(process.env.TR_JS, 'utf8');
const NEUTER = process.env.NEUTER === '1';

global.escapeHtml = s => String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
global.Icon = () => '<svg></svg>';
global.renderMarkdown = s => '<p>' + global.escapeHtml(s) + '</p>';
global.t = (k, d) => (d !== undefined ? d : k);
global.convTitleById = NEUTER
  ? (cid => 'Untitled chat')                       // NC: lookup always falls back
  : (cid => cid === 'mrnaj25i' ? '修复显示层 Bug' : '');
global.window = {};
const _tcModelTextRegistry = new Map();
let _tcModelTextSeq = 0;
global._tcModelTextRegistry = _tcModelTextRegistry;

function pull(name){const i=src.indexOf('function '+name+'(');let d=0,j=src.indexOf('{',i);
  for(let k=j;k<src.length;k++){if(src[k]==='{')d++;else if(src[k]==='}'){d--;if(d===0)return src.slice(i,k+1);}}}
eval(pull('_injectVerbatimText'));
eval(pull('_peerFromBubble'));
eval(pull('_tcModelViewBtnForText'));
eval(pull('_renderPeerInjectRow'));

const out = [];
function check(name, cond){ out.push((cond?'PASS ':'FAIL ')+name); }

const html = _renderPeerInjectRow({
  roundNum: 9000002, peerCount: 1,
  peerPreviews: [{ fromConv: 'mrnaj25i', text: 'Heads up: fixing the bug' }],
});

if (!NEUTER) {
  check('title_bubble_present', html.includes('修复显示层 Bug'));
  check('title_bubble_class', html.includes('sw-peer-from-bubble'));
  // The raw id must NOT be a visible label — only inside the title= tooltip.
  check('raw_id_not_a_label', !html.includes('>mrnaj25i<'));
  check('raw_id_in_tooltip', html.includes('title="conv mrnaj25i"'));
  // Verbatim model-view button present AND after the info badge (far-right).
  check('modelview_btn_present', html.includes('data-tc-preview-text'));
  check('modelview_after_badge',
    html.indexOf('data-tc-preview-text') > html.indexOf('ptool-badge-info'));
  // The registry got the verbatim text (what the model actually saw).
  const entry = [..._tcModelTextRegistry.values()].pop();
  check('verbatim_text_registered',
    entry && entry.text.includes('Heads up: fixing the bug'));
  // The dead chevron span is gone (CSS ::after handles collapse now).
  check('no_chevron_span', !html.includes('sw-inbox-row-chev'));
  // No per-card raw toggle inside the body anymore.
  check('no_percard_raw_toggle', !html.includes('sw-card-raw'));
} else {
  // NEUTER: convTitleById always falls back → the specific title is gone,
  // proving the title lookup (not a hardcoded string) produced the bubble.
  check('nc_title_falls_back',
    !html.includes('修复显示层 Bug') && html.includes('Untitled chat'));
}

console.log(out.join('\n'));
"""


def _run(neuter: bool) -> str:
    env = dict(os.environ, TR_JS=TR_JS, NEUTER='1' if neuter else '0')
    proc = subprocess.run(['node', '-e', _HARNESS], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_peer_inject_row_title_bubble_and_far_right_modelview():
    out = _run(neuter=False)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'peer-inject row layout failures:\n' + out
    assert out.count('PASS') >= 9, f'expected >=9 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_neutered_convtitle_loses_the_title_bubble():
    out = _run(neuter=True)
    assert 'PASS nc_title_falls_back' in out, (
        'NC control failed — the title bubble is not sourced from convTitleById:\n' + out)


def test_source_uses_title_bubble_and_header_modelview():
    """Cheap source guard (runs without node): the peer row builds a sender
    bubble + a header model-view button, and no longer emits a chevron span."""
    src = open(TR_JS, encoding='utf-8').read()
    peer = _pull_fn(src, '_renderPeerInjectRow')
    assert '_peerFromBubble' in peer, 'peer row no longer builds a sender title bubble'
    assert '_tcModelViewBtnForText' in peer, 'peer row no longer emits a header model-view button'
    assert 'sw-inbox-row-chev' not in peer, 'the dead chevron span was re-introduced'


if __name__ == '__main__':
    if not shutil.which('node'):
        print('SKIP — node not available')
    else:
        print(_run(neuter=False))
        print(_run(neuter=True))
    test_source_uses_title_bubble_and_header_modelview()
    print('PASS source guard')
