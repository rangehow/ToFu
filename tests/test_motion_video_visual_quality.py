"""Guards for the motion-video visual-quality gates.

Why this file exists (all three were REAL defects found by measurement, not
review):

1. ``_gate`` read a FLAT top-level ``findings`` key, but ``hyperframes check``
   nests its findings under ``lint`` / ``runtime`` / ``layout``. Every finding
   of the merged report was silently dropped, so a composition with a fatal
   runtime error came back ``ok=False`` with ``errors == []`` — which reads as
   "failed for no reason" to every consumer, and as SUCCESS to any consumer
   that only looks at the error list.
2. The zero-LLM template named ``PingFang SC`` / ``Noto Sans CJK SC``, neither
   of which is in the renderer's auto-resolved font list. Naming an absent face
   does not get you that face; it gets a silent fallback to whatever fontconfig
   has. On a host whose only CJK face is a SERIF, the sans stack rendered serif.
3. The engine ran only the regex gate, which is structurally blind to fonts,
   contrast, runtime errors and text overflow — i.e. to everything a viewer
   describes as "no formatting".

Discipline (charter): these assert RESULTS (what the pipeline reports for a
given composition), never constants, so re-tuning the CLI invocation or the
template's font stack cannot make them falsely green.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.motion_video._render import _collect_findings, _gate  # noqa: E402
from lib.motion_video._template import render_scene_html  # noqa: E402

pytestmark = pytest.mark.unit


# ── 1. The nested-report parser ───────────────────────────

def test_findings_are_collected_from_the_nested_check_report():
    """A ``check`` payload's per-section findings must all be collected.

    This is the exact shape the installed CLI emits; reading only the flat
    ``findings`` key returned an empty list while ``ok`` was false.
    """
    payload = {
        'ok': False,
        'lint': {'ok': True, 'findings': []},
        'runtime': {'ok': False, 'findings': [
            {'code': 'page_error', 'severity': 'error',
             'message': 'NOT_DEFINED_AT_ALL is not defined'}]},
        'layout': {'ok': False, 'findings': [
            {'severity': 'error', 'message': 'text overflows its container'}]},
    }
    findings, saw = _collect_findings(payload)
    assert saw is True
    messages = [f['message'] for f in findings]
    assert 'NOT_DEFINED_AT_ALL is not defined' in messages
    assert 'text overflows its container' in messages


def test_flat_report_shape_still_parses():
    """The legacy single-subcommand shape must keep working."""
    findings, saw = _collect_findings(
        {'findings': [{'severity': 'error', 'message': 'flat style'}]})
    assert saw is True
    assert [f['message'] for f in findings] == ['flat style']


def test_unrecognisable_payload_reports_that_it_saw_no_report():
    findings, saw = _collect_findings({'ok': False})
    assert findings == []
    assert saw is False


# ── 2. A failure never reports an EMPTY reason list ───────

def _fake_run(monkeypatch, *, rc, out, err=''):
    monkeypatch.setattr('lib.motion_video._render._cli_or_env_error',
                        lambda: '/fake/hyperframes')
    monkeypatch.setattr(
        'lib.motion_video._render._run_cli',
        lambda *a, **k: {'rc': rc, 'out': out, 'err': err,
                         'elapsed': 0.1, 'category': ''})


def test_a_failing_gate_always_names_at_least_one_reason(monkeypatch):
    """``ok=False`` with ``errors=[]`` is the shape that read as success.

    The CLI can exit non-zero while its report names no error-severity
    finding. The gate must synthesize one so no consumer sees a failure with
    an empty reason list.
    """
    _fake_run(monkeypatch, rc=1, out=json.dumps({'ok': False}),
              err='boom: the renderer exploded')
    res = _gate('check', '/tmp/nowhere', timeout=5)
    assert res['ok'] is False
    assert res['errors'], 'a failure must never carry an empty error list'
    assert 'boom: the renderer exploded' in res['errors'][0]


def test_a_failing_gate_prefers_the_real_findings_over_the_synthetic_one(monkeypatch):
    """The synthetic reason must not mask a real, specific finding."""
    _fake_run(monkeypatch, rc=1, out=json.dumps({
        'ok': False,
        'runtime': {'findings': [{'severity': 'error',
                                  'message': 'foo is not defined'}]}}))
    res = _gate('check', '/tmp/nowhere', timeout=5)
    assert res['errors'] == ['foo is not defined']


def test_a_clean_report_stays_green(monkeypatch):
    """The complement: a passing gate must not manufacture errors."""
    _fake_run(monkeypatch, rc=0, out=json.dumps({
        'ok': True, 'lint': {'findings': []}, 'runtime': {'findings': []},
        'layout': {'findings': []}}))
    res = _gate('check', '/tmp/nowhere', timeout=5)
    assert res['ok'] is True
    assert res['errors'] == []


# ── 3. The template names only resolvable fonts ───────────

#: Families the installed renderer resolves on its own. Anything else must be
#: accompanied by an @font-face rule or it silently degrades.
_AUTO_RESOLVED = {'inter', 'roboto', 'open sans', 'lato', 'montserrat',
                  'poppins', 'source sans 3', 'noto sans', 'noto sans jp',
                  'system-ui', 'sans-serif', 'serif', 'monospace',
                  'ui-monospace'}


def test_template_never_names_a_font_the_renderer_cannot_resolve():
    """Naming an absent face is worse than not naming one.

    It does not produce that face — it produces a silent fallback to whatever
    fontconfig offers (a SERIF, on the verified host) while looking deliberate
    in the source. Asserting on the RENDERED html (not on a constant) means a
    future edit that reintroduces such a family fails here.
    """
    html = render_scene_html(
        {'id': 'scene-001', 'start': 0.0, 'end': 4.0,
         'on_screen': '固态电池的能量密度', 'visual': 'x'},
        duration=4.0, scene_index=1, total_scenes=2)
    assert '@font-face' not in html, (
        'this guard assumes the template declares no faces of its own; '
        'if that changes, the named families below become legitimate')

    import re
    families: set[str] = set()
    for decl in re.findall(r'font-family:\s*([^;}]+)', html):
        for part in decl.split(','):
            families.add(part.strip().strip('"\'').lower())
    unresolvable = {f for f in families if f and f not in _AUTO_RESOLVED}
    assert not unresolvable, (
        f'template names font families the renderer cannot resolve: '
        f'{sorted(unresolvable)} — they degrade silently to a fontconfig '
        f'fallback. Either drop them or ship an @font-face.')


# ── 4. The engine actually CONSULTS the real gates ────────

def test_engine_records_gate_findings_on_the_quality_axis():
    """A scene that fails the real gates must not ship as a clean success.

    Asserts the wiring exists at the call site (charter: testing the helper is
    not testing the wiring) — the compose stage must call the real-gate helper
    and feed its findings into both the emitted event and the degraded axis.
    """
    import inspect

    from lib.motion_video import engine

    src = inspect.getsource(engine.run_motion_task)
    assert '_scene_gate_findings(' in src, (
        'the compose stage no longer runs the real gates — the regex gate '
        'alone cannot see fonts, contrast, runtime errors or overflow')
    assert 'scene_gate_issues' in src
    # The findings must reach the QUALITY axis, not just a log line.
    assert 'degraded=bool(degraded_narration or scene_gate_issues)' in src \
        or 'scene_gate_issues' in src.split('degraded=')[1], (
        'gate findings are collected but never reported as degraded')


def test_scene_gate_helper_treats_a_missing_toolchain_as_not_a_defect():
    """No CLI installed is an ENV problem, not an ugly frame.

    Without this, every scene on a host with no hyperframes CLI would be
    marked degraded and the film would look broken for the wrong reason.
    """
    from lib.motion_video.engine import _scene_gate_findings

    class _MV:
        @staticmethod
        def check_project(_d, **_k):
            return {'ok': False, 'category': 'env_missing', 'errors': []}

    assert _scene_gate_findings(_MV(), '/tmp/x', 'scene-001') == []


def test_scene_gate_helper_surfaces_real_findings():
    """The complement — a genuine defect must come back."""
    from lib.motion_video.engine import _scene_gate_findings

    class _MV:
        @staticmethod
        def check_project(_d, **_k):
            return {'ok': False, 'category': 'unknown',
                    'errors': ['text overflows its container']}

    assert _scene_gate_findings(_MV(), '/tmp/x', 'scene-001') == [
        'text overflows its container']


def test_scene_gate_helper_never_raises():
    """A gate crash must not take down a job."""
    from lib.motion_video.engine import _scene_gate_findings

    class _MV:
        @staticmethod
        def check_project(_d, **_k):
            raise RuntimeError('chrome died')

    assert _scene_gate_findings(_MV(), '/tmp/x', 'scene-001') == []


# ── 5. The author is briefed with real craft, not just rules ──

def test_author_prompt_carries_the_vendored_craft_guide():
    """The craft knowledge must be IN-TREE, not an optional user install.

    A default-path quality feature that depends on a user having installed a
    skill package degrades silently to today's output when they have not.
    """
    from lib.motion_video._scene_author import _build_prompt

    prompt = _build_prompt(
        {'id': 'scene-001', 'text': '固态电池能量密度翻倍', 'visual': 'stat card'},
        width=1080, height=1440, duration=4.0, scene_index=1, total_scenes=3)
    # Archetype vocabulary + hierarchy + stagger are what separate an authored
    # scene from the one-centred-line fallback.
    for token in ('archetype', 'hierarchy', 'stagger'):
        assert token in prompt.lower(), f'craft guide missing {token!r}'
    # The host font constraint must be stated, or the author reintroduces the
    # silent serif fallback this batch just removed.
    assert 'auto-resolve' in prompt.lower()
    # Inline SVG must be presented as the legitimate asset route.
    assert 'INLINE' in prompt


def test_craft_guide_is_vendored_in_tree():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'lib', 'motion_video', 'guide', 'MOTION_CRAFT.md')
    assert os.path.isfile(path), (
        'the craft guide must live in-tree beside the composition contract')
    body = open(path, encoding='utf-8').read()
    assert len(body) > 3000
