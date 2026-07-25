"""lib/motion_video/engine.py — Headless motion-video pipeline worker.

Fully automatic SRT → narrated MG video, driven by a TaskRuntime task
(see :mod:`lib.motion_video.runtime`). Phases (each emits an event):

    parse → storyboard → narrate? → compose → render → concat → sidecar → mux?

Composition authoring uses the zero-LLM template
(:mod:`._template`) — the chat-agent path (P1 tools) stays the creative
one; this engine is the deterministic, API-driveable floor. Scene renders
run through a bounded thread pool (the P3 parallel item): each render is
a subprocess-heavy HyperFrames call, so a small pool (default 2) already
halves wall time without melting the host.

All heavy dependencies are called through the ``lib.motion_video`` facade
so tests can monkeypatch them (same seam contract as lib.tts).
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['run_motion_task', 'run_scene_regen_task', 'run_topic_motion_task',
           'write_job_manifest', 'resume_interrupted_jobs']


def _emit(task: dict, event: dict) -> None:
    from lib.motion_video.runtime import _append_motion_event
    _append_motion_event(task, event)


def _aborted(task: dict) -> bool:
    ev = task.get('abort_event')
    return bool(ev is not None and ev.is_set())


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _render_one(mv, scene_dir: str, mp4_path: str, *, quality: str,
                width: int, height: int, fps: int, expect_dur: float,
                abort_event) -> dict:
    """Render + probe-verify one scene. Returns a per-scene result dict."""
    res = mv.render_project(scene_dir, mp4_path, quality=quality,
                            abort_event=abort_event)
    if not res.get('ok'):
        return {'ok': False, 'category': res.get('category', 'unknown'),
                'detail': res.get('detail', '')}
    probe = mv.probe_video(mp4_path)
    errors = mv.verify_spec(probe, width=width, height=height, fps=fps,
                            duration=expect_dur)
    if errors:
        return {'ok': False, 'category': 'io',
                'detail': 'spec mismatch: ' + '; '.join(errors)}
    return {'ok': True, 'elapsed': res.get('elapsed')}


#: Task fields persisted to job.json so an interrupted job can be re-spawned
#: verbatim after a server restart (crash-resume is a correctness contract).
_MANIFEST_FIELDS = (
    'task_id', 'kind', 'srt_path', 'scenes_path', 'workdir', 'voice', 'speed',
    'alignment', 'narration', 'quality', 'parallel', 'width', 'height',
    'burn_in', 'burn_in_fontsdir', 'topic', 'lang', 'max_scenes', 'paper_hash',
    'scene_author', 'author_rounds', 'author_token_budget',
)


def write_job_manifest(task: dict, *, kind: str, state: str) -> None:
    """Persist the job's params + lifecycle state to ``<workdir>/job.json``.

    This is the disk anchor :func:`resume_interrupted_jobs` scans on startup:
    a job whose manifest says ``running`` when the process died is re-spawned
    (the stage-graph checkpoint + per-scene mp4 skip make the re-run resume
    rather than restart).
    """
    from lib.production.jobs import write_manifest
    write_manifest(task.get('workdir') or '', task, fields=_MANIFEST_FIELDS,
                   kind=kind, state=state, log_label='MotionVideo')


def task_id_of(task: dict) -> str:
    return task.get('task_id') or '?'


def _reusable_manifest(audio_dir: str, scenes: list) -> dict | None:
    """Return a persisted narration manifest iff it still matches the scenes.

    The recipe's timeline stage writes ``<audio_dir>/manifest.json`` after it
    synthesized narration to MEASURE durations. Reuse it here so the engine's
    narrate phase doesn't re-run TTS (and a resumed job doesn't either) — but
    ONLY when every scene id is covered and its wav still exists on disk.
    """
    from lib.json_store import read_json
    m = read_json(os.path.join(audio_dir, 'manifest.json'), default=None)
    if not isinstance(m, dict) or not m.get('ok'):
        return None
    by_id = {e.get('scene_id'): e for e in m.get('scenes') or []}
    for sc in scenes:
        entry = by_id.get(sc.get('id'))
        if not entry or not entry.get('wav') or not os.path.isfile(entry['wav']):
            return None
    return m


def _existing_composition(index_path: str, duration: float) -> str | None:
    """Return an on-disk composition iff it matches this scene's duration.

    Resume path for the compose stage: a scene authored before a crash must
    NOT be re-authored (that would re-spend an agent loop per restart). The
    duration check guards against a stale composition from a run whose
    timeline changed — that one is discarded and re-made.
    """
    if not os.path.isfile(index_path):
        return None
    try:
        with open(index_path, encoding='utf-8') as f:
            html = f.read()
    except OSError as e:
        logger.debug('[MotionVideo] cannot read %s: %s', index_path, e)
        return None
    import re as _re
    m = _re.search(r'data-duration="([0-9.]+)"', html)
    if not m:
        return None
    try:
        if abs(float(m.group(1)) - float(duration)) > 0.01:
            return None
    except ValueError:
        return None
    return html


def _scene_already_rendered(mv, mp4_path: str, *, width: int, height: int,
                            fps: int, expect_dur: float) -> bool:
    """True when a scene's mp4 already exists on disk and passes verify_spec.

    Lets a re-spawned job skip scenes that were fully rendered before the
    crash — the owner's 'already-rendered shots are not re-rendered' contract.
    """
    if not os.path.isfile(mp4_path) or os.path.getsize(mp4_path) == 0:
        return False
    try:
        probe = mv.probe_video(mp4_path)
        return not mv.verify_spec(probe, width=width, height=height, fps=fps,
                                  duration=expect_dur)
    except Exception as e:
        logger.debug('[MotionVideo] resume probe of %s failed: %s', mp4_path, e)
        return False


def run_motion_task(task: dict) -> None:
    """Worker entry — drives the full pipeline for one motion task."""
    from lib import motion_video as mv
    from lib.motion_video.runtime import _motion_runtime

    task_id = task['task_id']
    workdir = task['workdir']
    width, height = task['width'], task['height']
    fps = 30
    try:
        os.makedirs(workdir, exist_ok=True)
        write_job_manifest(task, kind=task.get('kind') or 'scenes',
                           state='running')

        # ── 0. topic front-half (research → script → timeline) ──
        # When the job carries a bare TOPIC (no SRT / scenes), run the recipe
        # to synthesize scenes.json first. The recipe is itself checkpointed
        # (pipeline_state.json), so a crash mid-research resumes there.
        scenes_path = task.get('scenes_path') or ''
        topic = (task.get('topic') or '').strip()
        if topic and not (scenes_path and os.path.isfile(scenes_path)) \
                and not (task.get('srt_path') and os.path.isfile(task['srt_path'])):
            from lib.motion_video._recipe import build_scenes_from_topic
            _emit(task, {'type': 'phase', 'phase': 'research', 'topic': topic})
            tl = build_scenes_from_topic(
                topic, workdir, lang=task.get('lang') or 'zh',
                max_scenes=int(task.get('max_scenes') or 8),
                narration=bool(task.get('narration')),
                voice=task.get('voice') or '', speed=task.get('speed'),
                alignment=task.get('alignment') or 'loose',
                abort_event=task.get('abort_event'),
                emit=lambda ev: _emit(task, {'type': 'recipe', **ev}))
            scenes_path = tl['scenes_path']
            task['scenes_path'] = scenes_path
            _emit(task, {'type': 'phase', 'phase': 'script_done',
                         'scenes': tl['scenes'],
                         'timed_from_audio': tl['timed_from_audio']})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # ── 1. parse (optional when scenes are supplied directly) ──
        entries = []
        span = (0.0, 0.0)
        scenes_path = task.get('scenes_path') or ''
        if task.get('srt_path') and os.path.isfile(task['srt_path']):
            with open(task['srt_path'], encoding='utf-8') as f:
                entries = mv.parse_srt(f.read())
            if not entries:
                raise ValueError('SRT parsed to zero cues')
            span = mv.total_span(entries)
            _emit(task, {'type': 'phase', 'phase': 'parse',
                         'cues': len(entries),
                         'span_s': [round(s, 3) for s in span]})
        elif not (scenes_path and os.path.isfile(scenes_path)):
            raise ValueError('neither srt_path nor scenes_path available')
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # ── 2. storyboard (agent-supplied scenes.json wins; else zero-LLM) ──
        scenes = None
        if scenes_path and os.path.isfile(scenes_path):
            with open(scenes_path, encoding='utf-8') as f:
                scenes = json.load(f)
            if not entries:
                # Scenes-only input: the storyboard is the source of truth —
                # validate internal consistency (contiguity/overlap/sum)
                # against its OWN span.
                if scenes:
                    span = (float(scenes[0]['start']), float(scenes[-1]['end']))
            errors = mv.check_storyboard(scenes, span)
            if errors:
                raise ValueError('scenes.json failed the storyboard gate: '
                                 + ' | '.join(errors[:4]))
        if scenes is None:
            from lib.motion_video._storyboard import build_storyboard
            scenes = build_storyboard(entries)
        _write(os.path.join(workdir, 'scenes.json'),
               json.dumps(scenes, ensure_ascii=False, indent=1))
        _emit(task, {'type': 'phase', 'phase': 'storyboard',
                     'scenes': len(scenes)})

        # ── 3. narration (optional) ──
        narration = bool(task.get('narration'))
        manifest: dict = {}
        if narration:
            audio_dir = os.path.join(workdir, 'audio')
            # Reuse a manifest already produced by the recipe timeline stage
            # (topic jobs synthesize TTS up-front to measure durations) so we
            # never double-synthesize, and a resumed job skips narration too.
            manifest = _reusable_manifest(audio_dir, scenes) or {}
            if manifest.get('ok'):
                _emit(task, {'type': 'phase', 'phase': 'narrate',
                             'degraded': False, 'reused': True,
                             'scenes': [{'scene_id': e['scene_id'],
                                         'audio_s': e['audio_duration'],
                                         'target_s': e['target_duration'],
                                         'overflow_s': e['overflow']}
                                        for e in manifest['scenes']]})
            else:
                try:
                    manifest = mv.synthesize_scene_narrations(
                        scenes, audio_dir, voice=task.get('voice') or None,
                        speed=task.get('speed'),
                        alignment=task.get('alignment') or 'loose',
                        abort_event=task.get('abort_event'))
                except mv.NarrationAborted:
                    _motion_runtime.finish(task_id)
                    return
                if not manifest.get('ok'):
                    narration = False
                    _emit(task, {'type': 'phase', 'phase': 'narrate',
                                 'degraded': True,
                                 'detail': manifest.get('detail', '')})
                    logger.warning('[MotionVideo] narration degraded: %s',
                                   manifest.get('detail'))
                else:
                    _emit(task, {'type': 'phase', 'phase': 'narrate',
                                 'degraded': False,
                                 'scenes': [
                                     {'scene_id': e['scene_id'],
                                      'audio_s': e['audio_duration'],
                                      'target_s': e['target_duration'],
                                      'overflow_s': e['overflow']}
                                     for e in manifest['scenes']]})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        target_by_id = {e['scene_id']: e['target_duration']
                        for e in manifest.get('scenes', [])} if manifest.get('ok') else {}

        # ── 4. compose (per-scene author when enabled, else zero-LLM template) ──
        from lib.motion_video._scene_author import (author_scene,
                                                    scene_author_enabled)
        from lib.motion_video._template import render_scene_html
        authoring = scene_author_enabled(task)
        scene_dirs: list[str] = []
        authored = 0
        total = len(scenes)
        for i, sc in enumerate(scenes, 1):
            dur = target_by_id.get(sc['id'], round(sc['end'] - sc['start'], 3))
            scene_dir = os.path.join(workdir, 'scenes', sc['id'])
            os.makedirs(scene_dir, exist_ok=True)
            index_path = os.path.join(scene_dir, 'index.html')
            # Resume: a composition already on disk for this scene is kept —
            # never re-author (that would re-spend an agent loop per restart).
            existing = _existing_composition(index_path, dur)
            if existing is not None:
                html = existing
            elif authoring:
                res = author_scene(sc, scene_dir, width=width, height=height,
                                   duration=dur, scene_index=i,
                                   total_scenes=total,
                                   max_rounds=int(task.get('author_rounds') or 4),
                                   token_budget=int(task.get('author_token_budget')
                                                    or 60000),
                                   abort_event=task.get('abort_event'))
                html = res['html']
                if res['mode'] == 'authored':
                    authored += 1
                _emit(task, {'type': 'scene_authored', 'scene_id': sc['id'],
                             'mode': res['mode'], 'rounds': res.get('rounds', 0),
                             'tokens': res.get('tokens', 0),
                             'detail': res.get('detail', '')[:200],
                             'done': i, 'total': total})
            else:
                html = render_scene_html(sc, width=width, height=height,
                                         duration=dur, scene_index=i,
                                         total_scenes=total)
            errs = mv.check_composition_html(html)
            if errs:
                raise ValueError(f"template composition failed its own gate "
                                 f"for {sc['id']}: {' | '.join(errs)}")
            _write(index_path, html)
            sc['_duration'] = dur
            scene_dirs.append(scene_dir)
            if _aborted(task):
                _motion_runtime.finish(task_id)
                return
        _emit(task, {'type': 'phase', 'phase': 'compose', 'scenes': total,
                     'authored': authored,
                     'templated': total - authored})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # ── 5. render (bounded parallel) ──
        parallel = max(1, int(task.get('parallel') or 2))
        quality = task.get('quality') or 'standard'
        mp4s: dict[str, str] = {}
        failures: list[dict] = []
        with ThreadPoolExecutor(max_workers=parallel,
                                thread_name_prefix='mv-render') as pool:
            futures = {}
            for sc, scene_dir in zip(scenes, scene_dirs):
                if _aborted(task):
                    break
                mp4_path = os.path.join(scene_dir, f"{sc['id']}.mp4")
                if _scene_already_rendered(mv, mp4_path, width=width,
                                           height=height, fps=fps,
                                           expect_dur=sc['_duration']):
                    mp4s[sc['id']] = mp4_path
                    _emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                                 'ok': True, 'resumed': True,
                                 'done': len(mp4s), 'total': total})
                    continue
                fut = pool.submit(
                    _render_one, mv, scene_dir, mp4_path,
                    quality=quality, width=width, height=height, fps=fps,
                    expect_dur=sc['_duration'],
                    abort_event=task.get('abort_event'))
                futures[fut] = (sc, mp4_path)
            for fut in as_completed(futures):
                sc, mp4_path = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    logger.error('[MotionVideo] scene %s render crashed: %s',
                                 sc['id'], e, exc_info=True)
                    r = {'ok': False, 'category': 'unknown', 'detail': str(e)}
                if r.get('ok'):
                    mp4s[sc['id']] = mp4_path
                    _emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                                 'ok': True, 'elapsed': r.get('elapsed'),
                                 'done': len(mp4s), 'total': total})
                else:
                    failures.append({'scene_id': sc['id'],
                                     'category': r.get('category'),
                                     'detail': (r.get('detail') or '')[:300]})
                    _emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                                 'ok': False,
                                 'category': r.get('category')})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return
        if failures:
            first = failures[0]
            raise RuntimeError(
                f"scene {first['scene_id']} render failed "
                f"({first['category']}): {first['detail']}")

        # ── 6. concat (silent) ──
        ordered = [mp4s[sc['id']] for sc in scenes]
        silent_final = os.path.join(workdir, 'final_silent.mp4')
        res = mv.concat_mp4s(ordered, silent_final,
                             abort_event=task.get('abort_event'))
        if not res.get('ok'):
            raise RuntimeError('concat failed: ' + res.get('detail', ''))
        _emit(task, {'type': 'phase', 'phase': 'concat',
                     'duration_s': res.get('duration'), 'mode': res.get('mode')})

        # ── 7. sidecar SRT (loose-adjusted timeline when narrated) ──
        sidecar = os.path.join(workdir, 'final.srt')
        cursor = scenes[0]['start']
        lines: list[str] = []
        for i, sc in enumerate(scenes, 1):
            dur = sc['_duration']
            lines.append(str(i))
            lines.append(f"{mv.format_timestamp(cursor)} --> "
                         f"{mv.format_timestamp(cursor + dur)}")
            lines.append(sc.get('text') or '')
            lines.append('')
            cursor += dur
        _write(sidecar, '\n'.join(lines))

        # ── 7b. optional subtitle burn-in (re-encode) ──
        video_final = silent_final
        if task.get('burn_in'):
            burned = os.path.join(workdir, 'final_burned.mp4')
            br = mv.burn_in_subtitles(
                silent_final, sidecar, burned,
                fontsdir=task.get('burn_in_fontsdir') or '',
                abort_event=task.get('abort_event'))
            if not br.get('ok'):
                raise RuntimeError('burn-in failed: ' + br.get('detail', ''))
            video_final = burned
            _emit(task, {'type': 'phase', 'phase': 'burn_in',
                         'duration_s': br.get('duration')})

        # ── 8. mux (optional) ──
        final_path = os.path.join(workdir, 'final.mp4')
        if narration:
            wavs = [e['wav'] for e in manifest['scenes'] if e.get('wav')]
            narration_wav = os.path.join(workdir, 'audio', 'narration.wav')
            cn = mv.concat_narrations(wavs, narration_wav)
            if not cn.get('ok'):
                raise RuntimeError('narration concat failed: '
                                   + cn.get('detail', ''))
            mx = mv.mux_audio_video(video_final, narration_wav, final_path,
                                    abort_event=task.get('abort_event'))
            if not mx.get('ok'):
                raise RuntimeError('mux failed: ' + mx.get('detail', ''))
            _emit(task, {'type': 'phase', 'phase': 'mux',
                         'duration_s': mx.get('duration')})
        else:
            os.replace(video_final, final_path)

        probe = mv.probe_video(final_path)
        result = {
            'final_path': final_path,
            'srt_path': sidecar,
            'duration': round(float((probe or {}).get('duration') or 0), 3),
            'scenes': total,
            'narrated': narration,
            'workdir': workdir,
            'mode': 'engine',
        }
        task['result'] = result
        write_job_manifest(task, kind=task.get('kind') or 'scenes',
                           state='done')
        _emit(task, {'type': 'final', 'final_path': final_path,
                     'duration': result['duration'], 'narrated': narration})
        _motion_runtime.finish(task_id, result=result)
        logger.info('[MotionVideo] task %s done: %s (%.2fs, %d scenes, '
                    'narrated=%s)', task_id, final_path, result['duration'],
                    total, narration)

    except Exception as e:
        logger.error('[MotionVideo] task %s failed: %s', task_id, e, exc_info=True)
        try:
            write_job_manifest(task, kind=task.get('kind') or 'scenes',
                               state='error')
        except Exception as _me:
            logger.debug('[MotionVideo] manifest error-state write failed: %s', _me)
        _motion_runtime.finish(task_id, error=e,
                               error_context='motion-video:engine')


def run_topic_motion_task(task: dict) -> None:
    """Worker entry alias — a topic-driven job is just a motion task whose
    front half is the recipe. Kept as a named symbol so callers/log lines and
    the resume scanner can distinguish topic jobs from scenes/SRT jobs."""
    task.setdefault('kind', 'topic')
    run_motion_task(task)


def resume_interrupted_jobs() -> int:
    """Re-spawn motion jobs left ``running`` on disk by a crashed process.

    Scans ``<motion_root>/jobs/*/job.json``; any manifest in the ``running``
    state whose task is not live in the runtime is re-spawned with its
    persisted params. The stage-graph checkpoint (pipeline_state.json) and the
    per-scene mp4 skip make the re-run resume rather than restart — the
    owner's crash-resume correctness contract. Returns the count re-spawned.

    Best-effort and idempotent: called once at startup. Never raises.
    """
    from lib.motion_video._env import motion_root
    from lib.motion_video.runtime import _motion_runtime, _new_motion_task
    from lib.production.jobs import resume_running_jobs

    def _respawn(task_id: str, workdir: str, m: dict) -> None:
        task = _new_motion_task(
            task_id, srt_path=m.get('srt_path') or '', workdir=workdir,
            voice=m.get('voice') or '', speed=m.get('speed'),
            alignment=m.get('alignment') or 'loose',
            narration=bool(m.get('narration', True)),
            quality=m.get('quality') or 'standard',
            parallel=int(m.get('parallel') or 2),
            width=int(m.get('width') or 1080),
            height=int(m.get('height') or 1440),
            scenes_path=m.get('scenes_path') or '')
        for k in ('burn_in', 'burn_in_fontsdir', 'topic', 'lang',
                  'max_scenes', 'paper_hash', 'kind', 'scene_author',
                  'author_rounds', 'author_token_budget'):
            if m.get(k) is not None:
                task[k] = m[k]
        _motion_runtime.spawn(task_id, run_motion_task, task)

    return resume_running_jobs(
        os.path.join(motion_root(), 'jobs'),
        is_live=lambda tid: _motion_runtime.get(tid) is not None,
        respawn=_respawn, log_label='MotionVideo')


def run_scene_regen_task(task: dict) -> None:
    """Worker entry — re-render ONE scene of a finished job, then re-assemble.

    Task shape: ``workdir`` (the finished job's dir), ``scene_id``,
    ``regen_of`` (the original task id, echoed into the result), plus the
    usual width/height/quality/narration/burn_in fields. The scene's
    EXISTING composition (index.html — agent- or template-authored) is
    re-rendered as-is; durations, narration and storyboard are untouched,
    so re-concat / re-burn / re-mux produce a drop-in replacement
    ``final.mp4`` at the original job's stable URL.
    """
    from lib import motion_video as mv
    from lib.motion_video.runtime import _motion_runtime

    task_id = task['task_id']
    workdir = task['workdir']
    scene_id = task['scene_id']
    try:
        scenes_file = os.path.join(workdir, 'scenes.json')
        with open(scenes_file, encoding='utf-8') as f:
            scenes = json.load(f)
        target = next((sc for sc in scenes if sc.get('id') == scene_id), None)
        if target is None:
            raise ValueError(f'scene {scene_id!r} not in scenes.json')
        scene_dir = os.path.join(workdir, 'scenes', scene_id)
        index_html = os.path.join(scene_dir, 'index.html')
        if not os.path.isfile(index_html):
            raise ValueError(f'scene {scene_id!r} has no composition to re-render')
        import re as _re
        m = _re.search(r'data-duration="([0-9.]+)"',
                       open(index_html, encoding='utf-8').read())
        expect_dur = float(m.group(1)) if m else (
            float(target['end']) - float(target['start']))
        _emit(task, {'type': 'phase', 'phase': 'regen',
                     'scene_id': scene_id, 'regen_of': task.get('regen_of')})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        mp4_path = os.path.join(scene_dir, f'{scene_id}.mp4')
        r = _render_one(mv, scene_dir, mp4_path,
                        quality=task.get('quality') or 'standard',
                        width=task['width'], height=task['height'], fps=30,
                        expect_dur=expect_dur,
                        abort_event=task.get('abort_event'))
        if not r.get('ok'):
            raise RuntimeError(f"scene {scene_id} re-render failed "
                               f"({r.get('category')}): {r.get('detail', '')[:300]}")
        _emit(task, {'type': 'scene_done', 'scene_id': scene_id, 'ok': True,
                     'elapsed': r.get('elapsed')})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # Re-assemble with the unchanged siblings.
        ordered = []
        for sc in scenes:
            p = os.path.join(workdir, 'scenes', sc['id'], f"{sc['id']}.mp4")
            if not os.path.isfile(p):
                raise ValueError(f"sibling scene {sc['id']!r} mp4 missing — "
                                 'cannot re-assemble')
            ordered.append(p)
        silent_final = os.path.join(workdir, 'final_silent.mp4')
        res = mv.concat_mp4s(ordered, silent_final,
                             abort_event=task.get('abort_event'))
        if not res.get('ok'):
            raise RuntimeError('re-concat failed: ' + res.get('detail', ''))

        video_final = silent_final
        if task.get('burn_in'):
            sidecar = os.path.join(workdir, 'final.srt')
            burned = os.path.join(workdir, 'final_burned.mp4')
            br = mv.burn_in_subtitles(
                silent_final, sidecar, burned,
                fontsdir=task.get('burn_in_fontsdir') or '',
                abort_event=task.get('abort_event'))
            if not br.get('ok'):
                raise RuntimeError('re-burn failed: ' + br.get('detail', ''))
            video_final = burned

        final_path = os.path.join(workdir, 'final.mp4')
        if task.get('narration'):
            narration_wav = os.path.join(workdir, 'audio', 'narration.wav')
            mx = mv.mux_audio_video(video_final, narration_wav, final_path,
                                    abort_event=task.get('abort_event'))
            if not mx.get('ok'):
                raise RuntimeError('re-mux failed: ' + mx.get('detail', ''))
        else:
            os.replace(video_final, final_path)

        probe = mv.probe_video(final_path)
        result = {'final_path': final_path,
                  'regen_of': task.get('regen_of'),
                  'scene_id': scene_id,
                  'duration': round(float((probe or {}).get('duration') or 0), 3)}
        _emit(task, {'type': 'final', 'final_path': final_path,
                     'scene_id': scene_id, 'regen_of': task.get('regen_of')})
        _motion_runtime.finish(task_id, result=result)
        logger.info('[MotionVideo] regen %s of job %s done', scene_id,
                    task.get('regen_of'))
    except Exception as e:
        logger.error('[MotionVideo] regen task %s failed: %s', task_id, e,
                     exc_info=True)
        _motion_runtime.finish(task_id, error=e,
                               error_context='motion-video:regen')
