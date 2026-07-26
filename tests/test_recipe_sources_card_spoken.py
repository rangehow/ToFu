"""tests/test_recipe_sources_card_spoken.py — sources card must be VISUAL, not SPOKEN.

Owner directive 2026-07-26: the 片尾来源卡 was appended as a narration
segment, so the TTS pass synthesized it and the final video ended with a
robotic voice reading out domain names — a hard product defect for the
"zero-perception high-quality explainer" goal. The card must be a SILENT
visual end card: present in scenes.json and in the rendered video, absent
from the narration manifest and the muxed audio track.

Second directive in the same batch: news topics need freshness — the primary
research query must carry ``freshness='week'`` and the script prompt must
carry the current date, otherwise research on "某新闻话题" returns
encyclopedia background instead of news.

All seams faked; no network / LLM / TTS.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit

from lib.motion_video import _recipe as rec

_CARDS = [
    {'title': 'Fusion milestone', 'url': 'https://example.org/fusion',
     'snippet': 'Net energy gain was reproduced in three consecutive shots.'},
    {'title': 'Tokamak basics', 'url': 'https://sci.example.com/tokamak',
     'snippet': 'Magnetic confinement holds the plasma away from the wall.'},
]


def _wire(monkeypatch, segments=('净能量增益被重复实现。', '磁约束是主流路线。')):
    monkeypatch.setattr(rec, '_web_search',
                        lambda q, user_question='', freshness='': list(_CARDS))
    monkeypatch.setattr(rec, '_llm_chat', lambda m, **k: (json.dumps(
        {'title': '核聚变', 'segments': list(segments)}, ensure_ascii=False),
        {'total_tokens': 30}))


# ══════════════════════════════════════════════════════════
#  Sources card: visual, not spoken
# ══════════════════════════════════════════════════════════

def test_sources_card_is_not_a_narration_segment(monkeypatch, tmp_path):
    """The script stage's narration segments must NOT contain the sources
    line — it belongs to the timeline's silent end card."""
    _wire(monkeypatch)
    ctx = {'topic': 't', 'lang': 'zh', 'max_scenes': 8,
           'artifacts': {'research': {'cards': rec._cards_from_results(_CARDS)}}}
    art = rec._run_script(ctx)
    assert not any('资料来源' in s for s in art['segments']), (
        'the sources line is still inside the narration segments — TTS will '
        'read domain names aloud')


def test_sources_card_scene_is_silent_and_tts_skips_it(monkeypatch, tmp_path):
    """Timeline: the sources card exists in scenes.json with spoken=False and
    a fixed duration, and the TTS pass is only called for SPOKEN scenes."""
    _wire(monkeypatch)
    seen_by_tts = []

    def fake_tts(scenes, out_dir, **kw):
        seen_by_tts.extend(s['id'] for s in scenes)
        return {'ok': False, 'degraded': True}  # degrade to keep it offline

    monkeypatch.setattr(rec, '_tts_durations', fake_tts)
    ctx = {'topic': 't', 'lang': 'zh', 'workdir': str(tmp_path),
           'narration': True,
           'artifacts': {'script': {'segments': ['第一段口播', '第二段口播'],
                                    'sources_line': '资料来源:example.org'}}}
    art = rec._run_timeline(ctx)
    scenes = json.load(open(art['scenes_path'], encoding='utf-8'))

    card = scenes[-1]
    assert card.get('spoken') is False, 'the end card is not marked silent'
    assert '资料来源' in card['text']
    assert 2.0 <= (card['end'] - card['start']) <= 6.0, (
        'the silent card should hold a fixed, comfortable duration')
    # Every earlier scene is spoken; TTS never saw the card.
    assert all(s.get('spoken', True) for s in scenes[:-1])
    assert card['id'] not in seen_by_tts, (
        f'TTS was asked to voice the silent card ({card["id"]})')
    assert set(seen_by_tts) == {s['id'] for s in scenes[:-1]}


def test_engine_manifest_reuse_ignores_silent_scenes():
    """_reusable_manifest must not demand a wav for a spoken=False scene —
    otherwise a correctly-silent card makes the whole manifest unreusable and
    the engine re-synthesizes everything (cost + latency regression)."""
    from lib.motion_video import engine as eng
    from lib.json_store import write_json_atomic
    import tempfile
    td = tempfile.mkdtemp()
    audio = os.path.join(td, 'audio')
    os.makedirs(audio)
    wav = os.path.join(audio, 'scene-001.wav')
    open(wav, 'wb').write(b'RIFF')
    write_json_atomic(os.path.join(audio, 'manifest.json'),
                      {'ok': True, 'scenes': [
                          {'scene_id': 'scene-001', 'wav': wav,
                           'audio_duration': 3.0, 'target_duration': 3.0,
                           'overflow': 0.0}]})
    scenes = [{'id': 'scene-001'},
              {'id': 'scene-002', 'spoken': False}]
    assert eng._reusable_manifest(audio, scenes) is not None, (
        'a manifest covering only spoken scenes was rejected')


# ══════════════════════════════════════════════════════════
#  News freshness
# ══════════════════════════════════════════════════════════

def test_primary_research_query_carries_week_freshness(monkeypatch):
    """A news topic must research the NEWS, not the encyclopedia: the primary
    query goes out with freshness='week'. The background query keeps none."""
    calls = []
    monkeypatch.setattr(rec, '_web_search', lambda q, user_question='',
                        freshness='': (calls.append((q, freshness)),
                                       list(_CARDS))[1])
    rec._run_research({'topic': '核聚变净能量增益', 'lang': 'zh'})
    assert calls and calls[0][1] == 'week', (
        f'primary query freshness is {calls[0][1]!r}, not week')
    # The background angle stays un-freshness-gated (evergreen grounding).
    assert len(calls) >= 2 and calls[1][1] == ''


def test_script_prompt_carries_current_date(monkeypatch):
    """The writer must know what "recent" means — the prompt includes the
    current date so time-sensitive claims use the latest cards' framing."""
    prompt = rec._build_script_prompt('核聚变净能量增益', rec._cards_from_results(_CARDS),
                                      lang='zh', max_scenes=8)
    import datetime
    today = datetime.date.today().isoformat()
    assert today in prompt, 'script prompt has no current date'
