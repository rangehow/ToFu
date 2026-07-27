"""lib/paper/video_abstract.py — Paper → narrated video abstract (P3).

The motion-video pipeline's paper entry point, sibling to the podcast
chain (report → spoken script → TTS audio): here the report becomes a
short narrated MG video (report → scene beats → motion engine).

Flow:

  1. ``has_report`` gate (same report-first UX as the podcast start route);
  2. source text via the podcast chain's ``_load_source_text`` (report in
     the requested language → other language → translation → parsed text);
  3. :func:`build_abstract_scenes` — beats carrying THREE separate fields
     (see below), each one sized to fit both its frame and its time slot;
  4. the scenes drive the motion engine in scenes-only mode (no SRT —
     loose alignment lets TTS narration stretch each beat to fit);
  5. progress/results ride the motion runtime (poll/file endpoints).

**The three-field scene contract** (owner 2026-07-27). The original
implementation put one paragraph of report prose into ``text`` and let it
serve as narration, on-screen headline AND subtitle at once. That is what
produced 1898-char headlines at 46px on a 1440px frame. A beat now carries:

  * ``text``      — SPOKEN narration (TTS + sidecar SRT), budgeted so it is
                    utterable inside the scene's own duration;
  * ``on_screen`` — the ON-FRAME caption, bounded by the template's measured
                    :func:`~lib.motion_video._template.on_screen_capacity`;
  * ``visual``    — ART DIRECTION for the per-scene author. Untouched
                    semantics: it is NOT drawn as the headline.

**Beats are written, not sliced.** The default path rewrites the report with
an LLM through the SAME script stage the topic recipe uses
(:func:`lib.motion_video._recipe.script_stage_for_source`), so there is one
implementation of "prose → spoken beats" in the tree. Slicing prose on a
character budget is kept ONLY as the zero-LLM fallback
(:func:`slice_abstract_beats`) for when no model is reachable, and its output
must satisfy the same budget as the LLM path.

**No silent clamping.** When a beat's text does not fit its slot the builder
RE-CUTS (splits the beat, or trims the caption) — it never parks the scene on
``_MAX_SCENE_S`` and calls it done. A duration sitting exactly on the ceiling
is a swallowed error, and :func:`lib.motion_video.check_scene_budget` fails
the storyboard for it.
"""

from __future__ import annotations

import json
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['build_abstract_scenes', 'slice_abstract_beats',
           'start_video_abstract']

#: Roughly 250 chars/min narration pace (podcast chain's estimate scale).
_CHARS_PER_SECOND = 4.2
_MIN_SCENE_S = 3.0
#: Ceiling for a single beat. A scene is never PARKED here — reaching it means
#: the beat must be split (the budget gate treats saturation as an error).
_MAX_SCENE_S = 15.0
_DEFAULT_MAX_SCENES = 8

#: Spoken chars that fit one beat without saturating it. Leaves a margin below
#: the ceiling so rounding can never land a scene exactly on it.
_BEAT_CHAR_BUDGET = int((_MAX_SCENE_S - 1.0) * _CHARS_PER_SECOND)

_MD_NOISE_RE = re.compile(
    r'^\s{0,3}#{1,6}\s*|^\s{0,3}[-*+]\s+|^\s{0,3}>\s?|\*\*|__|`{1,3}|'
    r'\[([^\]]+)\]\([^)]*\)|<[^>]+>',
    re.M)

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[。！？!?…])\s*|(?<=[.!?])\s+')


def _clean_markdown(text: str) -> str:
    """Strip markdown structure, keeping readable prose."""
    text = _MD_NOISE_RE.sub(lambda m: m.group(1) or ' ', text)
    lines = [ln.strip() for ln in text.split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or '') if s.strip()]


#: Characters we may break a hard-split on, preferred over a mid-token cut.
#: CJK text has no spaces, so punctuation and ASCII whitespace are the only
#: honest boundaries available without a tokenizer.
_BREAK_CHARS = ' \t，,、；;：:）)】」』—–/·'


def _hard_split(sent: str, budget: int) -> tuple[str, str]:
    """Cut ``sent`` at or before ``budget``, preferring a token boundary.

    Falls back to an exact cut only when no boundary exists in the last third
    of the window — cutting 'Discriminative' into 'Di' + 'scriminative' is how
    a caption turns into noise, so we look for somewhere legitimate first.
    """
    window = sent[:budget]
    floor = max(1, int(budget * 0.6))
    best = -1
    for ch in _BREAK_CHARS:
        idx = window.rfind(ch)
        if idx >= floor:
            best = max(best, idx)
    if best < 0:
        return window.rstrip(), sent[budget:].lstrip()
    # +1 so the boundary char stays with the head when it is punctuation.
    cut = best if window[best] in ' \t' else best + 1
    return sent[:cut].rstrip(), sent[cut:].lstrip()


def _split_to_budget(spoken: str, budget: int) -> list[str]:
    """Split one over-long beat into pieces that each fit ``budget`` chars.

    Prefers sentence boundaries; a single sentence longer than the budget is
    broken at a token boundary (never mid-word), because leaving it whole is
    what the clamp used to do.
    """
    if len(spoken) <= budget:
        return [spoken]
    pieces: list[str] = []
    cur = ''
    for sent in _sentences(spoken) or [spoken]:
        while len(sent) > budget:
            head, sent = _hard_split(sent, budget)
            if not head:
                break
            if cur:
                pieces.append(cur)
                cur = ''
            pieces.append(head)
        if not sent:
            continue
        if not cur:
            cur = sent
        elif len(cur) + 1 + len(sent) <= budget:
            cur = f'{cur} {sent}'
        else:
            pieces.append(cur)
            cur = sent
    if cur:
        pieces.append(cur)
    return pieces or [spoken[:budget]]


def _caption_for(spoken: str, capacity: int) -> str:
    """Derive an on-frame caption from spoken text, within ``capacity``.

    Used by the ZERO-LLM fallback only — the LLM path writes its own caption.
    Takes whole leading sentences while they fit, so the caption is a
    readable clause rather than a mid-word truncation.
    """
    spoken = (spoken or '').strip()
    if not spoken:
        return ''
    if len(spoken) <= capacity:
        return spoken
    out = ''
    for sent in _sentences(spoken):
        if not out:
            out = sent if len(sent) <= capacity else sent[:capacity].rstrip()
            continue
        if len(out) + 1 + len(sent) <= capacity:
            out = f'{out} {sent}'
        else:
            break
    return out or spoken[:capacity].rstrip()


def _timeline(beats: list[dict], *, chars_per_second: float,
              min_scene_s: float, max_scene_s: float) -> list[dict]:
    """Lay budgeted beats onto a contiguous timeline from 0.0.

    Duration comes from the beat's spoken length. Because every beat was
    already split to the char budget, no duration can reach ``max_scene_s`` —
    so there is nothing to clamp. The assertion is the guard: if a beat still
    over-runs we would rather fail loudly here than ship a clamped scene.
    """
    scenes: list[dict] = []
    cursor = 0.0
    for i, beat in enumerate(beats, 1):
        spoken = beat['text']
        dur = max(min_scene_s, len(spoken) / chars_per_second)
        if dur >= max_scene_s:
            raise ValueError(
                f'beat {i} needs {dur:.1f}s (> {max_scene_s}s ceiling) after '
                'budgeting — the split step failed to bound it')
        dur = round(dur, 3)
        scenes.append({
            'id': f'scene-{i:03d}',
            'start': round(cursor, 3),
            'end': round(cursor + dur, 3),
            'text': spoken,
            'on_screen': beat['on_screen'],
            'visual': beat.get('visual', ''),
        })
        cursor = round(cursor + dur, 3)
    return scenes


def slice_abstract_beats(source_text: str, *,
                         max_scenes: int = _DEFAULT_MAX_SCENES,
                         capacity: int = 0) -> list[dict]:
    """Zero-LLM fallback: sentence-packed beats within the char budget.

    Returns beat dicts (``text`` / ``on_screen`` / ``visual``), NOT scenes —
    :func:`_timeline` turns them into a storyboard. Unlike the original
    implementation the per-beat size is driven by what a beat can SAY in its
    slot (``_BEAT_CHAR_BUDGET``), not by ``total_chars // max_scenes``, so a
    longer report yields MORE beats rather than fatter ones.
    """
    from lib.motion_video._template import MIN_FONT_PX, on_screen_capacity

    capacity = capacity or on_screen_capacity(font_px=MIN_FONT_PX)
    clean = _clean_markdown(source_text or '')
    paragraphs = [p for p in re.split(r'\n{2,}|\n', clean) if p.strip()]
    if not paragraphs:
        return []

    beats: list[dict] = []
    for para in paragraphs:
        if len(beats) >= max_scenes:
            break
        for piece in _split_to_budget(para.strip(), _BEAT_CHAR_BUDGET):
            if len(beats) >= max_scenes:
                break
            beats.append({'text': piece,
                          'on_screen': _caption_for(piece, capacity),
                          'visual': ''})
    return beats


def _llm_beats(source_text: str, *, lang: str, max_scenes: int,
               capacity: int) -> list[dict]:
    """Rewrite the report into spoken beats + captions via the script stage.

    Reuses :func:`lib.motion_video._recipe.script_stage_for_source` so the
    tree has ONE implementation of "prose → spoken beats + on-screen captions
    + art direction". Returns [] when the model is unreachable or its output
    fails the shape check, letting the caller fall back to slicing.
    """
    try:
        from lib.motion_video._recipe import script_stage_for_source
        beats = script_stage_for_source(
            source_text, lang=lang, max_scenes=max_scenes,
            char_budget=_BEAT_CHAR_BUDGET, caption_capacity=capacity)
    except Exception as e:
        logger.warning('[Paper:Video] LLM beat rewrite failed (%s) — falling '
                       'back to zero-LLM slicing', e)
        return []
    return beats or []


def build_abstract_scenes(source_text: str, *,
                          max_scenes: int = _DEFAULT_MAX_SCENES,
                          min_scene_s: float = _MIN_SCENE_S,
                          max_scene_s: float = _MAX_SCENE_S,
                          chars_per_second: float = _CHARS_PER_SECOND,
                          lang: str = 'zh',
                          use_llm: bool = True) -> list[dict]:
    """Build a budget-satisfying storyboard from a paper report.

    Beats are written by the shared script stage when a model is reachable and
    sliced deterministically otherwise; either way every beat is bounded to
    what it can SAY in its slot and what its caption can SHOW on the frame,
    then laid out contiguously from 0.0.

    Returns motion-engine scene dicts with ``text`` / ``on_screen`` /
    ``visual``, valid against both :func:`lib.motion_video.check_storyboard`
    and :func:`lib.motion_video.check_scene_budget`.
    """
    from lib.motion_video._template import MIN_FONT_PX, on_screen_capacity

    capacity = on_screen_capacity(font_px=MIN_FONT_PX)
    beats: list[dict] = []
    if use_llm:
        beats = _llm_beats(source_text, lang=lang, max_scenes=max_scenes,
                           capacity=capacity)
    if not beats:
        beats = slice_abstract_beats(source_text, max_scenes=max_scenes,
                                     capacity=capacity)
    if not beats:
        return []

    # Enforce the budget on WHATEVER produced the beats — the LLM is asked for
    # bounded output but never trusted to have obeyed.
    bounded: list[dict] = []
    for beat in beats:
        spoken = str(beat.get('text') or '').strip()
        if not spoken:
            continue
        caption = str(beat.get('on_screen') or '').strip()
        visual = str(beat.get('visual') or '').strip()
        for piece in _split_to_budget(spoken, _BEAT_CHAR_BUDGET):
            cap = caption if caption and len(caption) <= capacity else \
                _caption_for(piece, capacity)
            bounded.append({'text': piece, 'on_screen': cap, 'visual': visual})
            # A split beat's single caption belongs to its first piece only;
            # later pieces derive their own so they don't all repeat one line.
            caption = ''
        if len(bounded) >= max_scenes:
            break
    bounded = bounded[:max_scenes]
    if not bounded:
        return []

    scenes = _timeline(bounded, chars_per_second=chars_per_second,
                       min_scene_s=min_scene_s, max_scene_s=max_scene_s)
    logger.info('[Paper:Video] abstract scenes: %d beat(s), %.1fs total '
                '(source=%s)', len(scenes), scenes[-1]['end'] if scenes else 0,
                'llm' if use_llm and beats else 'sliced')
    return scenes


def start_video_abstract(paper_hash: str, *, lang: str = 'zh',
                         voice: str = '', speed=None,
                         alignment: str = 'loose', narration: bool = True,
                         burn_in: bool = False, quality: str = 'standard',
                         parallel: int = 2, max_scenes: int = _DEFAULT_MAX_SCENES,
                         force: bool = False) -> dict:
    """Start a motion-engine task rendering this paper's video abstract.

    Dedup (§2.1 of docs/PAPER_MEDIA_UX_DESIGN.md): a second call with the
    same (paper_hash, lang, voice, narration, burn_in, quality) joins the
    in-flight task instead of starting a parallel render — same contract
    as the motion main route. ``force=True`` (the frontend's Regenerate
    button) explicitly bypasses the index, like the podcast's ``force``.

    Returns ``{'ok', 'task_id', 'scenes', 'source_kind'}`` (plus
    ``deduped: True`` on a join) or
    ``{'ok': False, 'reason': 'report_required'|'empty_source'|'budget_failed'}``.
    """
    from lib import motion_video as mv
    from lib.motion_video._env import motion_root
    from lib.motion_video.engine import run_motion_task
    from lib.motion_video.runtime import (
        _motion_index_get,
        _motion_index_register,
        _motion_runtime,
        _motion_task_id,
        _new_motion_task,
    )
    from lib.paper.podcast_engine import _load_source_text, has_report

    if not has_report(paper_hash):
        return {'ok': False, 'reason': 'report_required'}

    dedup_key = ('paper', paper_hash, lang, voice, bool(narration),
                 bool(burn_in), quality)
    if not force:
        existing = _motion_index_get(dedup_key)
        if existing:
            logger.info('[Paper:Video] dedup join: %s (paper=%s)',
                        existing, paper_hash[:8])
            return {'ok': True, 'task_id': existing, 'deduped': True,
                    'scenes': 0, 'source_kind': 'joined'}

    text, kind = _load_source_text(paper_hash, lang)
    if not text.strip():
        return {'ok': False, 'reason': 'empty_source'}
    scenes = build_abstract_scenes(text, max_scenes=max_scenes, lang=lang)
    if not scenes:
        return {'ok': False, 'reason': 'empty_source'}

    # Fail BEFORE spending a render: the engine would otherwise compose and
    # burn ~35s/scene on a storyboard we already know cannot read.
    budget_errors = mv.check_scene_budget(scenes, width=1080, height=1440,
                                          max_scene_s=_MAX_SCENE_S,
                                          narration=narration)
    if budget_errors:
        logger.error('[Paper:Video] storyboard failed the scene budget for '
                     'paper=%s: %s', paper_hash[:8], '; '.join(budget_errors[:3]))
        return {'ok': False, 'reason': 'budget_failed',
                'errors': budget_errors[:6]}

    task_id = _motion_task_id()
    _motion_index_register(dedup_key, task_id)
    workdir = os.path.join(motion_root(), 'jobs', task_id)
    os.makedirs(workdir, exist_ok=True)
    scenes_path = os.path.join(workdir, 'scenes.json')
    with open(scenes_path, 'w', encoding='utf-8') as f:
        json.dump(scenes, f, ensure_ascii=False, indent=1)

    task = _new_motion_task(
        task_id, srt_path='', workdir=workdir, voice=voice, speed=speed,
        alignment=alignment, narration=narration, quality=quality,
        parallel=parallel, width=1080, height=1440,
        scenes_path=scenes_path)
    task['burn_in'] = burn_in
    task['burn_in_fontsdir'] = ''
    task['paper_hash'] = paper_hash
    _motion_runtime.spawn(task_id, run_motion_task, task)
    logger.info('[Paper:Video] abstract started: %s (paper=%s scenes=%d '
                'narration=%s)', task_id, paper_hash[:8], len(scenes),
                narration)
    return {'ok': True, 'task_id': task_id, 'scenes': len(scenes),
            'source_kind': kind}
