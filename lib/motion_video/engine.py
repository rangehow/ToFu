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

__all__ = ['run_motion_task']


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

        # ── 1. parse ──
        with open(task['srt_path'], encoding='utf-8') as f:
            entries = mv.parse_srt(f.read())
        if not entries:
            raise ValueError('SRT parsed to zero cues')
        span = mv.total_span(entries)
        _emit(task, {'type': 'phase', 'phase': 'parse',
                     'cues': len(entries), 'span_s': [round(s, 3) for s in span]})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # ── 2. storyboard (agent-supplied scenes.json wins; else zero-LLM) ──
        scenes = None
        scenes_path = task.get('scenes_path') or ''
        if scenes_path and os.path.isfile(scenes_path):
            with open(scenes_path, encoding='utf-8') as f:
                scenes = json.load(f)
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

        # ── 4. compose (zero-LLM template) ──
        from lib.motion_video._template import render_scene_html
        scene_dirs: list[str] = []
        total = len(scenes)
        for i, sc in enumerate(scenes, 1):
            dur = target_by_id.get(sc['id'], round(sc['end'] - sc['start'], 3))
            scene_dir = os.path.join(workdir, 'scenes', sc['id'])
            os.makedirs(scene_dir, exist_ok=True)
            html = render_scene_html(sc, width=width, height=height,
                                     duration=dur, scene_index=i,
                                     total_scenes=total)
            errs = mv.check_composition_html(html)
            if errs:
                raise ValueError(f"template composition failed its own gate "
                                 f"for {sc['id']}: {' | '.join(errs)}")
            _write(os.path.join(scene_dir, 'index.html'), html)
            sc['_duration'] = dur
            scene_dirs.append(scene_dir)
        _emit(task, {'type': 'phase', 'phase': 'compose', 'scenes': total})
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

        # ── 8. mux (optional) ──
        final_path = os.path.join(workdir, 'final.mp4')
        if narration:
            wavs = [e['wav'] for e in manifest['scenes'] if e.get('wav')]
            narration_wav = os.path.join(workdir, 'audio', 'narration.wav')
            cn = mv.concat_narrations(wavs, narration_wav)
            if not cn.get('ok'):
                raise RuntimeError('narration concat failed: '
                                   + cn.get('detail', ''))
            mx = mv.mux_audio_video(silent_final, narration_wav, final_path,
                                    abort_event=task.get('abort_event'))
            if not mx.get('ok'):
                raise RuntimeError('mux failed: ' + mx.get('detail', ''))
            _emit(task, {'type': 'phase', 'phase': 'mux',
                         'duration_s': mx.get('duration')})
        else:
            os.replace(silent_final, final_path)

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
        _emit(task, {'type': 'final', 'final_path': final_path,
                     'duration': result['duration'], 'narrated': narration})
        _motion_runtime.finish(task_id, result=result)
        logger.info('[MotionVideo] task %s done: %s (%.2fs, %d scenes, '
                    'narrated=%s)', task_id, final_path, result['duration'],
                    total, narration)

    except Exception as e:
        logger.error('[MotionVideo] task %s failed: %s', task_id, e, exc_info=True)
        _motion_runtime.finish(task_id, error=e,
                               error_context='motion-video:engine')
