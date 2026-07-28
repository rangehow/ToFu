"""Static wiring guard: newChat() must re-resolve the Project Brain surfaces.

Reported bug (2026-07-28): on a Studio conversation the user clicked
"+ New Chat". newChat() dropped the active conv (activeConvId=null) and cleared
projectState — but NEVER re-resolved the Project Brain surfaces. The collab
bar (#presenceStrip, presence.js) kept rendering the PREVIOUS conversation's
project counts ("N need you · M open") for up to one presence tick (~15s),
and clicking it in that window opened the Brain panel with no project behind
it — every tab blank while the bar claimed work was waiting.

loadConversation has the correct seam (main.js _restoreConvToolState calls
presenceRefresh() + projectBrainRefresh() on every switch); newChat had
neither. This asserts both calls are present in the REAL newChat body — the
same static-guard idiom as test_frontend_conv_influence_bar_wiring.py — with a
negative control proving the predicate is load-bearing.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
LIFECYCLE_JS = os.path.join(ROOT, 'static', 'js', 'main', 'main_conv_lifecycle.js')


def _slice_fn(src: str, signature: str) -> str:
    start = src.index(signature)
    i = src.index('{', start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f'could not slice {signature}')


def _newchat_fn() -> str:
    with open(LIFECYCLE_JS, encoding='utf-8') as f:
        return _slice_fn(f.read(), 'function newChat() {')


def test_newchat_refreshes_brain_surfaces():
    """newChat drops the displayed project context (activeConvId=null, and
    usually projectState via _clearProjectStateLocal), so it MUST re-resolve
    the two live Brain surfaces immediately — the same seam loadConversation
    uses — or the collab bar keeps showing the previous conversation's
    project as a stale, clickable lie."""
    body = _newchat_fn()
    assert 'presenceRefresh()' in body, (
        'newChat must call presenceRefresh() — otherwise the collab bar '
        'keeps rendering the previous conversation\'s project counts for up '
        'to one presence tick after "+ New Chat" (the reported stale bar).')
    assert 'projectBrainRefresh()' in body, (
        'newChat must call projectBrainRefresh() — an open Brain panel must '
        're-resolve (close its feed) when the displayed conversation\'s '
        'project vanishes.')


def test_NC_absence_would_fail():
    """Negative control: a body WITHOUT the calls must fail the same
    predicate — proves the assertions match the real invocations, not an
    incidental comment or substring."""
    body = _newchat_fn()
    assert "if (typeof presenceRefresh === 'function') presenceRefresh();" in body, (
        'harness stale: presenceRefresh invocation not found verbatim')
    neutered = body.replace(
        "if (typeof presenceRefresh === 'function') presenceRefresh();",
        '/* removed */', 1)
    assert 'presenceRefresh()' not in neutered, (
        'NC: with the call removed the predicate must be false — if still '
        'true, the real assertion is matching something incidental.')
