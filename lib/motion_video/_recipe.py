"""lib/motion_video/_recipe.py — Topic → scenes.json front-half (P4).

The missing first half of the motion-video pipeline
(docs/PRODUCTION_PIPELINE_DESIGN.md §2.2): turn a bare NEWS TOPIC into a
validated ``scenes.json`` the existing engine can render. Three stages,
built on the reusable stage-graph contract (:mod:`lib.production.stages`) so
every stage
is checkpointed and the whole graph is crash-resumable:

    research  → fact cards (each with ≥1 real source URL)   [web_search]
    script    → spoken narration segments (time-budgeted)   [dispatch_chat]
    timeline  → scenes.json with REAL TTS durations         [lib.tts, zero-LLM]

Design decisions (owner-ratified 2026-07-25):

  * **Fact discipline is enforced** (拍板 #4): the ``research`` gate rejects
    any run that produced zero fact cards carrying a real URL, and the
    ``script`` stage always appends a sources scene (片尾来源卡) so the
    finished video credits where its claims came from.
  * **Real durations, not 4.2 chars/s** (owner requirement): ``timeline``
    synthesizes the narration up-front and reads each segment's true audio
    length, so the SRT is measured, not estimated. TTS-degraded hosts fall
    back to a conservative char estimate but keep going (silent video).
  * **Cost is capped** (拍板 #3): ``max_scenes`` bounds scene count; the
    script prompt is a single bounded ``dispatch_chat`` call. Money caps live
    in the wallet layer, not here.

The heavy dependencies (web search, LLM, TTS) are reached through module-
level indirections so tests can monkeypatch them without a network.
"""

from __future__ import annotations

import json
import os
import re

from lib.log import get_logger

from lib.production.stages import Stage, run_stages

logger = get_logger(__name__)

__all__ = ['build_scenes_from_topic', 'RESEARCH', 'SCRIPT', 'TIMELINE',
           'video_recipe_stages']

#: Hard ceilings (拍板 #3 — scene-count cap; no money cap here).
_DEFAULT_MAX_SCENES = 8
_MIN_SCENE_S = 2.5
_MAX_SCENE_S = 15.0
#: Conservative narration pace used ONLY when TTS is unavailable (degraded).
_FALLBACK_CHARS_PER_SECOND = 4.2
#: How many web results to mine per research query.
_RESEARCH_MAX_RESULTS = 6


# ── Seams (monkeypatchable) ───────────────────────────────

def _web_search(query: str, *, user_question: str = ''):
    """Run one web search through the tofu-search facade. Returns results."""
    from lib.tasks_pkg.handlers import search as _facade
    return _facade.perform_web_search(query, user_question=user_question)


def _llm_chat(messages, **kwargs):
    """Non-streaming LLM call through the dispatcher. Returns (content, usage)."""
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, **kwargs)


def _tts_durations(scenes: list[dict], out_dir: str, *, voice=None, speed=None,
                   alignment: str = 'loose', abort_event=None) -> dict:
    """Synthesize per-scene narration + return the alignment manifest."""
    from lib import motion_video as mv
    return mv.synthesize_scene_narrations(
        scenes, out_dir, voice=voice, speed=speed, alignment=alignment,
        abort_event=abort_event)


# ── Fact-card extraction (zero-LLM) ───────────────────────

_URL_RE = re.compile(r'https?://[^\s)>\]"\']+')


def _cards_from_results(results) -> list[dict]:
    """Turn web-search results into fact cards {point, url, title}.

    A card is kept ONLY when it carries a real http(s) URL — the zero-LLM
    fact-discipline gate (拍板 #4) reads this list.
    """
    cards: list[dict] = []
    seen: set[str] = set()
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = (r.get('url') or r.get('link') or '').strip()
        if not url:
            body = str(r.get('content') or r.get('snippet') or '')
            m = _URL_RE.search(body)
            url = m.group(0) if m else ''
        if not url or not url.lower().startswith(('http://', 'https://')):
            continue
        if url in seen:
            continue
        seen.add(url)
        point = (r.get('snippet') or r.get('content') or r.get('title')
                 or '').strip()
        point = re.sub(r'\s+', ' ', point)[:400]
        if not point:
            continue
        cards.append({'point': point, 'url': url,
                      'title': (r.get('title') or '').strip()[:200]})
    return cards


# ── Stage: research ───────────────────────────────────────

def _run_research(ctx: dict) -> dict:
    topic = ctx['topic']
    lang = ctx.get('lang', 'zh')
    queries = [topic]
    # A light second angle improves grounding without unbounded fan-out.
    queries.append(f'{topic} 原理 背景' if lang == 'zh' else f'{topic} explained background')
    cards: list[dict] = []
    seen: set[str] = set()
    for q in queries:
        try:
            results = _web_search(q, user_question=topic)
        except Exception as e:
            logger.warning('[Recipe:research] query %r failed: %s', q, e)
            continue
        for card in _cards_from_results(results):
            if card['url'] in seen:
                continue
            seen.add(card['url'])
            cards.append(card)
    logger.info('[Recipe:research] topic=%r → %d fact card(s) from %d queries',
                topic[:60], len(cards), len(queries))
    return {'topic': topic, 'cards': cards[:24]}


def _gate_research(ctx: dict, artifact: dict) -> list:
    cards = artifact.get('cards') or []
    if not cards:
        return ['research produced zero fact cards with a real source URL '
                '(fact-discipline gate: every point must be grounded)']
    if not any(c.get('url', '').lower().startswith(('http://', 'https://'))
               for c in cards):
        return ['no fact card carries a real http(s) URL']
    return []


RESEARCH = Stage('research', _run_research, gate=_gate_research, retry=1)


# ── Stage: script ─────────────────────────────────────────

def _build_script_prompt(topic: str, cards: list[dict], *, lang: str,
                         max_scenes: int) -> str:
    numbered = '\n'.join(
        f'[{i}] {c["point"]}  (来源: {c["url"]})'
        for i, c in enumerate(cards, 1))
    if lang == 'zh':
        return (
            '你是一名科普短视频编导。请把下面这些带来源的事实卡片,改写成一段'
            f'口语化、准确、适合配音的科普短视频口播稿,主题是《{topic}》。\n\n'
            '严格要求:\n'
            f'1. 输出 JSON:{{"title": "...", "segments": ["第1段口播", "第2段", ...]}}。\n'
            f'2. segments 数量在 3 到 {max_scenes - 1} 之间(不含片尾来源卡,系统会自动追加)。\n'
            '3. 每段 1~3 句,口语、连贯、可直接配音;不得出现"如图""见下"等书面语。\n'
            '4. 只依据事实卡片,不得编造;不确定就不说。\n'
            '5. 只输出 JSON 本身,不要解释、不要代码围栏。\n\n'
            f'事实卡片:\n{numbered}')
    return (
        'You are a science-explainer video writer. Rewrite the sourced fact '
        f'cards below into a spoken, accurate, voice-over-ready short-video '
        f'script about "{topic}".\n\n'
        'Strict requirements:\n'
        '1. Output JSON: {"title": "...", "segments": ["line 1", "line 2", ...]}.\n'
        f'2. Between 3 and {max_scenes - 1} segments (the sources card is '
        'appended automatically).\n'
        '3. Each segment 1-3 spoken sentences; no "as shown"/"see figure".\n'
        '4. Ground every claim in the cards; invent nothing.\n'
        '5. Output ONLY the JSON — no commentary, no fences.\n\n'
        f'Fact cards:\n{numbered}')


_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)


def _parse_script(content: str) -> dict:
    text = (content or '').strip()
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        raise ValueError('no JSON object in script reply')
    raw = json.loads(m.group(0))
    if not isinstance(raw, dict):
        raise ValueError('script JSON is not an object')
    segs = raw.get('segments')
    if not isinstance(segs, list):
        raise ValueError('script JSON has no segments array')
    return raw


def _sources_line(cards: list[dict], lang: str) -> str:
    hosts: list[str] = []
    seen: set[str] = set()
    for c in cards:
        from urllib.parse import urlparse
        try:
            host = urlparse(c['url']).netloc.replace('www.', '')
        except Exception:
            host = ''
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
        if len(hosts) >= 4:
            break
    joined = ' · '.join(hosts) if hosts else ''
    if lang == 'zh':
        return f'资料来源:{joined}' if joined else '资料来源见简介'
    return f'Sources: {joined}' if joined else 'Sources in description'


def _run_script(ctx: dict) -> dict:
    topic = ctx['topic']
    lang = ctx.get('lang', 'zh')
    max_scenes = ctx.get('max_scenes', _DEFAULT_MAX_SCENES)
    cards = ctx['artifacts']['research']['cards']
    prompt = _build_script_prompt(topic, cards, lang=lang, max_scenes=max_scenes)
    content, usage = _llm_chat(
        [{'role': 'user', 'content': prompt}],
        max_tokens=4096, temperature=0.4,
        log_prefix='[Recipe:script]')
    raw = _parse_script(content)
    segments = [re.sub(r'\s+', ' ', str(s)).strip()
                for s in raw.get('segments') or [] if str(s).strip()]
    segments = segments[:max_scenes - 1]  # leave room for the sources card
    title = (raw.get('title') or topic).strip()
    # 拍板 #4: always credit the sources at the end (片尾来源卡).
    segments.append(_sources_line(cards, lang))
    logger.info('[Recipe:script] topic=%r → %d segment(s) (+sources), title=%r',
                topic[:60], len(segments), title[:60])
    return {'title': title, 'segments': segments,
            'usage': usage if isinstance(usage, dict) else {}}


def _gate_script(ctx: dict, artifact: dict) -> list:
    segs = artifact.get('segments') or []
    if len(segs) < 2:
        return [f'script has too few segments ({len(segs)}; need ≥2)']
    if any(not s.strip() for s in segs):
        return ['script has an empty segment']
    return []


SCRIPT = Stage('script', _run_script, gate=_gate_script, retry=1)


# ── Stage: timeline ───────────────────────────────────────

def _provisional_scenes(segments: list[str]) -> list[dict]:
    """A first-cut storyboard (contiguous from 0) used only to drive TTS.

    Durations here are placeholders; the real durations come from the TTS
    manifest and are written back before this becomes the final scenes.json.
    """
    scenes: list[dict] = []
    cursor = 0.0
    for i, seg in enumerate(segments, 1):
        est = max(_MIN_SCENE_S, min(len(seg) / _FALLBACK_CHARS_PER_SECOND,
                                    _MAX_SCENE_S))
        scenes.append({'id': f'scene-{i:03d}',
                       'start': round(cursor, 3),
                       'end': round(cursor + est, 3),
                       'text': seg, 'visual': ''})
        cursor += est
    return scenes


def _rescore_from_manifest(scenes: list[dict], manifest: dict) -> list[dict]:
    """Rewrite scene start/end from the TTS manifest's real durations."""
    by_id = {e['scene_id']: e for e in manifest.get('scenes', [])}
    cursor = 0.0
    for sc in scenes:
        entry = by_id.get(sc['id'])
        dur = (float(entry['target_duration']) if entry
               else float(sc['end']) - float(sc['start']))
        dur = max(_MIN_SCENE_S, round(dur, 3))
        sc['start'] = round(cursor, 3)
        sc['end'] = round(cursor + dur, 3)
        cursor += dur
    return scenes


def _run_timeline(ctx: dict) -> dict:
    segments = ctx['artifacts']['script']['segments']
    scenes = _provisional_scenes(segments)
    audio_dir = os.path.join(ctx['workdir'], 'audio')
    manifest = {'ok': False, 'degraded': True}
    if ctx.get('narration', True):
        try:
            manifest = _tts_durations(
                scenes, audio_dir, voice=ctx.get('voice') or None,
                speed=ctx.get('speed'), alignment=ctx.get('alignment', 'loose'),
                abort_event=ctx.get('abort_event'))
        except Exception as e:
            logger.warning('[Recipe:timeline] TTS pass failed (%s) — '
                           'falling back to char-estimated durations', e)
            manifest = {'ok': False, 'degraded': True}
    if manifest.get('ok'):
        scenes = _rescore_from_manifest(scenes, manifest)
        # Persist the manifest so the engine's narrate stage REUSES this audio
        # instead of re-synthesizing (resumable + no double-TTS).
        from lib.json_store import write_json_atomic as _wja
        _wja(os.path.join(audio_dir, 'manifest.json'), manifest)
        logger.info('[Recipe:timeline] %d scene(s) timed from real TTS audio',
                    len(scenes))
    else:
        logger.info('[Recipe:timeline] %d scene(s), char-estimated durations '
                    '(TTS %s)', len(scenes),
                    'degraded' if manifest.get('degraded') else 'off')
    scenes_path = os.path.join(ctx['workdir'], 'scenes.json')
    from lib.json_store import write_json_atomic
    write_json_atomic(scenes_path, scenes)
    return {'scenes_path': scenes_path, 'scenes': len(scenes),
            'timed_from_audio': bool(manifest.get('ok')),
            'span_s': round(scenes[-1]['end'] - scenes[0]['start'], 3)
            if scenes else 0.0}


def _gate_timeline(ctx: dict, artifact: dict) -> list:
    path = artifact.get('scenes_path')
    if not path or not os.path.isfile(path):
        return ['timeline did not write scenes.json']
    try:
        with open(path, encoding='utf-8') as f:
            scenes = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [f'scenes.json unreadable: {e}']
    if not scenes:
        return ['scenes.json is empty']
    from lib import motion_video as mv
    span = (float(scenes[0]['start']), float(scenes[-1]['end']))
    return mv.check_storyboard(scenes, span)


TIMELINE = Stage('timeline', _run_timeline, gate=_gate_timeline, retry=0)


def video_recipe_stages() -> list:
    """The ordered front-half stage list: research → script → timeline."""
    return [RESEARCH, SCRIPT, TIMELINE]


# ── Public entry ──────────────────────────────────────────

def build_scenes_from_topic(topic: str, workdir: str, *, lang: str = 'zh',
                            max_scenes: int = _DEFAULT_MAX_SCENES,
                            narration: bool = True, voice: str = '',
                            speed=None, alignment: str = 'loose',
                            abort_event=None,
                            emit=None) -> dict:
    """Run research → script → timeline; return the timeline artifact.

    The stage graph is checkpointed at ``<workdir>/pipeline_state.json`` so a
    crash resumes at the first unfinished stage (already-synthesized audio and
    the written scenes.json are not recomputed).

    Returns ``{'scenes_path', 'scenes', 'timed_from_audio', 'span_s'}``.
    Raises StageFailed / StageAborted on unrecoverable failure.
    """
    os.makedirs(workdir, exist_ok=True)
    ctx = {
        'topic': topic, 'workdir': workdir, 'lang': lang,
        'max_scenes': max_scenes, 'narration': narration, 'voice': voice,
        'speed': speed, 'alignment': alignment, 'abort_event': abort_event,
    }
    state_path = os.path.join(workdir, 'pipeline_state.json')
    artifacts = run_stages(
        video_recipe_stages(), ctx, state_path=state_path, emit=emit,
        abort_check=(lambda: bool(abort_event is not None
                                  and abort_event.is_set())))
    return artifacts['timeline']
