#!/usr/bin/env python3
"""tests/test_paper_video_scene_budget.py — the scene-budget contract.

Guards the root fix for "paper video renders a wall of unreadable text":
a scene's three jobs are carried by three SEPARATE fields, and a storyboard
whose text does not fit its frame or its time is REJECTED rather than
silently clamped onto the duration ceiling.

These are BEHAVIOUR guards (charter: assert the RESULT, not the
implementation). They deliberately do not assert on private helpers or
source-text anchors — they build a storyboard shaped like the one that
actually shipped and assert what the gate/renderer DO with it, so the
implementation can be rewritten and the guards still bite.

The historical failure (job motion_fac7615398424af4) is reproduced exactly:
8 scenes each pinned at 15.000s carrying ~1900 chars of report prose, which
``check_storyboard`` passed green while the frame overflowed ~8x.
"""

from __future__ import annotations

import pytest

from lib import motion_video as mv
from lib.motion_video._template import (MIN_FONT_PX, on_screen_capacity,
                                        render_scene_html, scene_on_screen)

pytestmark = pytest.mark.unit

WIDTH, HEIGHT = 1080, 1440
MAX_SCENE_S = 15.0


def _shipped_shape(n: int = 8, chars: int = 1900) -> list[dict]:
    """A storyboard shaped like the one that shipped: saturated + wall of text."""
    return [{'id': f'scene-{i:03d}',
             'start': (i - 1) * MAX_SCENE_S,
             'end': i * MAX_SCENE_S,
             'text': '字' * chars,
             'visual': ''}
            for i in range(1, n + 1)]


# ══════════════════════════════════════════════════════════
#  capacity model — the gate and the renderer must agree
# ══════════════════════════════════════════════════════════

def test_capacity_shrinks_as_font_grows():
    """Bigger type fits fewer glyphs — the model tracks real geometry."""
    caps = [on_screen_capacity(WIDTH, HEIGHT, px)
            for px in (120, 96, 76, 60, 46)]
    assert caps == sorted(caps), f'capacity must grow as font shrinks: {caps}'
    assert all(c > 0 for c in caps)
    # A 1080x1440 frame at the floor size cannot hold a report paragraph.
    assert on_screen_capacity(WIDTH, HEIGHT, MIN_FONT_PX) < 500


def test_renderer_never_exceeds_the_gate_capacity():
    """Whatever font the renderer picks, the gate's verdict matches reality.

    If these two ever disagree the gate becomes decorative: it would pass
    captions the renderer then clips, or reject ones that fit fine.
    """
    for n in (10, 40, 80, 140, 240):
        scene = {'id': 'scene-001', 'start': 0.0, 'end': 5.0,
                 'text': 'x', 'on_screen': '字' * n}
        errors = mv.check_scene_budget([scene], width=WIDTH, height=HEIGHT,
                                       max_scene_s=MAX_SCENE_S,
                                       narration=False)
        capacity_errors = [e for e in errors if 'on_screen caption' in e]
        fits = n <= on_screen_capacity(WIDTH, HEIGHT, MIN_FONT_PX)
        assert bool(capacity_errors) != fits, (
            f'{n} chars: gate says {"reject" if capacity_errors else "accept"} '
            f'but capacity model says {"fits" if fits else "overflows"}')


# ══════════════════════════════════════════════════════════
#  the gate bites the shape that actually shipped
# ══════════════════════════════════════════════════════════

def test_old_timeline_gate_is_blind_to_this():
    """The pre-existing gate passes the broken storyboard — that's WHY the
    budget gate exists. If this ever starts failing, the two gates have
    overlapped and this suite's premise needs re-checking."""
    scenes = _shipped_shape()
    span = (scenes[0]['start'], scenes[-1]['end'])
    assert mv.check_storyboard(scenes, span) == []


def test_budget_gate_rejects_the_shipped_shape():
    """All three failure modes are reported, per scene."""
    scenes = _shipped_shape()
    errors = mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S)
    assert errors, 'the shipped storyboard must not pass'
    blob = ' | '.join(errors)
    assert 'saturated' in blob, 'clamped duration must be reported'
    assert 'on_screen caption' in blob, 'overflowing caption must be reported'
    assert 'narration needs' in blob, 'unspeakable narration must be reported'
    # every scene is guilty of all three
    for sc in scenes:
        assert sum(1 for e in errors if sc['id'] in e) == 3, sc['id']


def test_saturation_alone_is_an_error():
    """A scene sitting exactly on the ceiling is a swallowed error even when
    its text is perfectly fine — clamping means the builder gave up."""
    scene = {'id': 'scene-001', 'start': 0.0, 'end': MAX_SCENE_S,
             'text': '短旁白。', 'on_screen': '短标题', 'visual': ''}
    errors = mv.check_scene_budget([scene], width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S)
    assert any('saturated' in e for e in errors), errors


def test_narration_overflow_is_caught():
    """Spoken text that cannot be uttered in the slot stretches the film."""
    scene = {'id': 'scene-001', 'start': 0.0, 'end': 5.0,
             'text': '字' * 2000, 'on_screen': '标题', 'visual': ''}
    errors = mv.check_scene_budget([scene], width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S)
    assert any('narration needs' in e for e in errors), errors


def test_a_healthy_storyboard_passes_both_gates():
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 6.0,
               'text': '这是一段可以在六秒内说完的旁白。', 'on_screen': '第一个要点',
               'visual': '俯拍城市夜景'},
              {'id': 'scene-002', 'start': 6.0, 'end': 12.0,
               'text': '第二段旁白同样简短。', 'on_screen': '第二个要点',
               'visual': '特写手部动作'}]
    span = (scenes[0]['start'], scenes[-1]['end'])
    assert mv.check_storyboard(scenes, span) == []
    assert mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                 max_scene_s=MAX_SCENE_S) == []


# ══════════════════════════════════════════════════════════
#  the three fields stay three fields
# ══════════════════════════════════════════════════════════

def test_visual_is_never_drawn_as_the_headline():
    """``visual`` is art direction. The end card proves it: the topic recipe
    marks it ``visual='sources'`` while its human-readable credit line lives
    in ``text`` — rendering ``visual`` would put the literal word 'sources'
    on screen."""
    end_card = {'id': 'scene-003', 'start': 0.0, 'end': 3.5,
                'text': '资料来源:arxiv.org · nature.com',
                'visual': 'sources', 'spoken': False}
    assert scene_on_screen(end_card) == '资料来源:arxiv.org · nature.com'
    html = render_scene_html(end_card, width=WIDTH, height=HEIGHT,
                             scene_index=3, total_scenes=3)
    assert '资料来源:arxiv.org · nature.com' in html
    assert '>sources<' not in html


def test_sources_end_card_survives_the_real_recipe_path():
    """Same guarantee, but through the recipe that actually builds it, so a
    change to the end-card shape cannot slip past the hand-made fixture."""
    from lib.motion_video._recipe import _provisional_scenes

    scenes = _provisional_scenes(['第一段口播。', '第二段口播。'],
                                 '资料来源:arxiv.org · nature.com')
    tail = scenes[-1]
    assert tail['visual'] == 'sources' and tail['spoken'] is False
    html = render_scene_html(tail, width=WIDTH, height=HEIGHT,
                             scene_index=len(scenes), total_scenes=len(scenes))
    assert '资料来源' in html
    assert '>sources<' not in html


def test_on_screen_wins_over_text_but_text_is_the_fallback():
    """Legacy storyboards (no on_screen) must keep rendering."""
    assert scene_on_screen({'text': '旁白', 'on_screen': '标题'}) == '标题'
    assert scene_on_screen({'text': '旁白'}) == '旁白'
    assert scene_on_screen({'text': '旁白', 'on_screen': '   '}) == '旁白'


def test_scene_author_still_receives_visual_as_art_direction():
    """The per-scene author's art-direction channel must not be repurposed."""
    from lib.motion_video._scene_author import _build_prompt

    prompt = _build_prompt({'id': 'scene-001', 'text': '旁白内容',
                            'visual': '俯拍城市夜景,霓虹渐显'},
                           width=WIDTH, height=HEIGHT, duration=4.0,
                           scene_index=1, total_scenes=3)
    assert 'visual direction: 俯拍城市夜景,霓虹渐显' in prompt


# ══════════════════════════════════════════════════════════
#  NEUTER — prove each check is load-bearing
# ══════════════════════════════════════════════════════════

def test_NEUTER_without_saturation_check_the_shipped_shape_slips_through():
    """Drop the saturation + capacity + narration findings and the broken
    storyboard is accepted — i.e. these findings ARE the defence, not
    decoration."""
    scenes = _shipped_shape()
    errors = mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S)
    neutered = [e for e in errors
                if 'saturated' not in e
                and 'on_screen caption' not in e
                and 'narration needs' not in e]
    assert neutered == [], (
        'something ELSE is rejecting this storyboard — the three findings '
        'under test are no longer the thing that catches it')


def test_NEUTER_raising_the_ceiling_hides_the_clamp():
    """Saturation is judged against the ceiling in force. If a future change
    quietly raises _MAX_SCENE_S, the same 15s scenes stop reading as clamped
    — this pins that the check tracks the ceiling rather than the number 15."""
    scenes = _shipped_shape(n=1)
    at_ceiling = mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                       max_scene_s=MAX_SCENE_S)
    raised = mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                   max_scene_s=60.0)
    assert any('saturated' in e for e in at_ceiling)
    assert not any('saturated' in e for e in raised)
