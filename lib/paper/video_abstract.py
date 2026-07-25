"""lib/paper/video_abstract.py — Paper → narrated video abstract (P3).

The motion-video pipeline's paper entry point, sibling to the podcast
chain (report → spoken script → TTS audio): here the report becomes a
short narrated MG video (report → scene beats → motion engine).

Flow:

  1. ``has_report`` gate (same report-first UX as the podcast start route);
  2. source text via the podcast chain's ``_load_source_text`` (report in
     the requested language → other language → translation → parsed text);
  3. :func:`build_abstract_scenes` — zero-LLM scene beats: markdown noise
     stripped, paragraphs grouped under a scene-count cap, durations
     estimated from text length (chars-per-second, same ~250 chars/min
     scale the podcast chain uses), clamped to [min, max] and contiguous
     from 0;
  4. the scenes drive the motion engine in scenes-only mode (no SRT —
     loose alignment lets TTS narration stretch each beat to fit);
  5. progress/results ride the motion runtime (poll/file endpoints).
"""

from __future__ import annotations

import json
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['build_abstract_scenes', 'start_video_abstract']

#: Roughly 250 chars/min narration pace (podcast chain's estimate scale).
_CHARS_PER_SECOND = 4.2
_MIN_SCENE_S = 3.0
_MAX_SCENE_S = 15.0
_DEFAULT_MAX_SCENES = 8

_MD_NOISE_RE = re.compile(
    r'^\s{0,3}#{1,6}\s*|^\s{0,3}[-*+]\s+|^\s{0,3}>\s?|\*\*|__|`{1,3}|'
    r'\[([^\]]+)\]\([^)]*\)|<[^>]+>',
    re.M)


def _clean_markdown(text: str) -> str:
    """Strip markdown structure, keeping readable prose."""
    text = _MD_NOISE_RE.sub(lambda m: m.group(1) or ' ', text)
    lines = [ln.strip() for ln in text.split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def build_abstract_scenes(source_text: str, *,
                          max_scenes: int = _DEFAULT_MAX_SCENES,
                          min_scene_s: float = _MIN_SCENE_S,
                          max_scene_s: float = _MAX_SCENE_S,
                          chars_per_second: float = _CHARS_PER_SECOND
                          ) -> list[dict]:
    """Zero-LLM scene beats from a paper report.

    Paragraphs accumulate into beats; a beat closes when it reaches the
    per-beat char budget (total/max_scenes, floor 120 chars). Duration is
    ``chars / chars_per_second`` clamped to [min_scene_s, max_scene_s];
    beats are contiguous from 0.0. Returns motion-engine scene dicts
    (id/start/end/text), valid by construction against their own span.
    """
    clean = _clean_markdown(source_text or '')
    paragraphs = [p for p in re.split(r'\n{2,}|\n', clean) if p.strip()]
    if not paragraphs:
        return []
    total_chars = sum(len(p) for p in paragraphs)
    budget = max(120, total_chars // max(1, max_scenes))

    beats: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paragraphs:
        if cur and cur_len + len(p) > budget and len(beats) < max_scenes - 1:
            beats.append(' '.join(cur))
            cur, cur_len = [], 0
        cur.append(p.strip())
        cur_len += len(p)
    if cur:
        beats.append(' '.join(cur))

    scenes: list[dict] = []
    cursor = 0.0
    for i, beat in enumerate(beats, 1):
        est = len(beat) / chars_per_second
        dur = min(max(est, min_scene_s), max_scene_s)
        scenes.append({'id': f'scene-{i:03d}',
                       'start': round(cursor, 3),
                       'end': round(cursor + dur, 3),
                       'text': beat,
                       'visual': ''})
        cursor += dur
    logger.info('[Paper:Video] abstract scenes: %d beat(s), %.1fs total',
                len(scenes), cursor)
    return scenes


def start_video_abstract(paper_hash: str, *, lang: str = 'zh',
                         voice: str = '', speed=None,
                         alignment: str = 'loose', narration: bool = True,
                         burn_in: bool = False, quality: str = 'standard',
                         parallel: int = 2, max_scenes: int = _DEFAULT_MAX_SCENES
                         ) -> dict:
    """Start a motion-engine task rendering this paper's video abstract.

    Returns ``{'ok', 'task_id', 'scenes', 'source_kind'}`` or
    ``{'ok': False, 'reason': 'report_required'|'empty_source'}``.
    """
    from lib.motion_video._env import motion_root
    from lib.motion_video.engine import run_motion_task
    from lib.motion_video.runtime import (
        _motion_runtime,
        _motion_task_id,
        _new_motion_task,
    )
    from lib.paper.podcast_engine import _load_source_text, has_report

    if not has_report(paper_hash):
        return {'ok': False, 'reason': 'report_required'}
    text, kind = _load_source_text(paper_hash, lang)
    if not text.strip():
        return {'ok': False, 'reason': 'empty_source'}
    scenes = build_abstract_scenes(text, max_scenes=max_scenes)
    if not scenes:
        return {'ok': False, 'reason': 'empty_source'}

    task_id = _motion_task_id()
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
