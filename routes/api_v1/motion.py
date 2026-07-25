"""routes/api_v1/motion.py — Motion-video headless API.

Routes:
  GET  /api/v1/motion/status                     — render-chain env + TTS probe
  POST /api/v1/motion/videos                     — start (or dedup-join) a video task
  GET  /api/v1/motion/videos/poll/<task_id>      — cursor poll (factory)
  POST /api/v1/motion/videos/abort/<task_id>     — abort (factory)
  GET  /api/v1/motion/videos/<task_id>/file      — final MP4 (Range) [sync]
  GET  /api/v1/motion/videos/<task_id>/file?part=srt — aligned sidecar SRT

Body for POST /videos::

    {
      "srt": "<SRT text>",              // or "srt_path": "/abs/path.srt"
      "scenes_path": "/abs/scenes.json" // optional agent-made storyboard
      "narration": true,                // TTS voice-over (degrades to silent)
      "voice": "", "speed": null,       // TTS overrides
      "alignment": "loose",             // loose | strict
      "quality": "standard",            // draft | standard | high
      "parallel": 2,                    // scene render pool (1..4)
      "aspect": "1080x1440"             // 1080x1440 | 1080x1920 | 1920x1080 | 1080x1080
    }

The engine (:mod:`lib.motion_video.engine`) is the zero-LLM fallback path;
the chat-agent flow (motion_video_* tools) remains the creative one.
"""

from __future__ import annotations

import hashlib
import os

from flask import Blueprint, jsonify, send_file

from lib.api_response import api_bad_request, api_not_found
from lib.log import get_logger
from lib.motion_video.runtime import _motion_runtime
from lib.openapi import api_meta
from lib.request_parser import async_parse_body
from routes._task_routes import register_task_routes

from .auth import require_auth

logger = get_logger(__name__)

api_v1_motion_bp = Blueprint('api_v1_motion', __name__)

_ASPECTS = {
    '1080x1440': (1080, 1440),
    '1080x1920': (1080, 1920),
    '1920x1080': (1920, 1080),
    '1080x1080': (1080, 1080),
}
_QUALITIES = ('draft', 'standard', 'high')
_ALIGNMENTS = ('loose', 'strict')
_MAX_SRT_BYTES = 2 * 1024 * 1024


@api_v1_motion_bp.route('/api/v1/motion/status', methods=['GET'])
@require_auth
@api_meta(summary='Motion-video environment probe',
          description='Render-chain readiness (node/hyperframes/ffmpeg/'
                      'ffprobe/Chrome) + TTS availability.',
          tags=['motion'])
async def motion_status():
    from lib import motion_video as mv
    env = mv.probe_env()
    try:
        import lib.tts as _tts
        env['tts_available'] = bool(_tts.tts_available())
    except Exception as e:
        logger.debug('[Motion.v1] tts probe failed: %s', e)
        env['tts_available'] = False
    return jsonify(env)


@api_v1_motion_bp.route('/api/v1/motion/videos', methods=['POST'])
@require_auth
@api_meta(summary='Start (or join) a motion-video task',
          description='Dedup key: (srt sha, voice, alignment, aspect, '
                      'narration). A second identical POST joins the '
                      'in-flight task.',
          tags=['motion'])
async def start_motion_task():
    from lib.motion_video.runtime import (
        _cleanup_stale_motion_tasks,
        _motion_index_get,
        _motion_index_register,
        _motion_task_id,
        _new_motion_task,
    )
    from lib.motion_video._env import motion_root
    from lib.motion_video.engine import run_motion_task

    body = await async_parse_body()
    srt_text = (body.get('srt') or '').strip()
    srt_path_in = (body.get('srt_path') or '').strip()
    if not srt_text and not srt_path_in:
        return api_bad_request('srt or srt_path is required', field='srt')
    if len(srt_text.encode('utf-8')) > _MAX_SRT_BYTES:
        return api_bad_request('srt too large (2 MB max)', field='srt')

    aspect = (body.get('aspect') or '1080x1440').strip()
    if aspect not in _ASPECTS:
        return api_bad_request(
            f'unknown aspect {aspect!r} (one of {sorted(_ASPECTS)})',
            field='aspect')
    width, height = _ASPECTS[aspect]
    quality = (body.get('quality') or 'standard').strip()
    if quality not in _QUALITIES:
        return api_bad_request('quality must be draft|standard|high',
                               field='quality')
    alignment = (body.get('alignment') or 'loose').strip()
    if alignment not in _ALIGNMENTS:
        return api_bad_request('alignment must be loose|strict',
                               field='alignment')
    narration = bool(body.get('narration', True))
    voice = (body.get('voice') or '').strip()
    speed = body.get('speed')
    if speed is not None:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            return api_bad_request('speed must be a number', field='speed')
    try:
        parallel = int(body.get('parallel') or 2)
    except (TypeError, ValueError):
        return api_bad_request('parallel must be an int', field='parallel')
    parallel = max(1, min(parallel, 4))
    scenes_path = (body.get('scenes_path') or '').strip()
    if scenes_path and not os.path.isfile(scenes_path):
        return api_bad_request('scenes_path is not a file',
                               field='scenes_path')

    # ── Dedup ──
    _cleanup_stale_motion_tasks()
    srt_material = srt_text or srt_path_in
    srt_sha = hashlib.sha256(srt_material.encode('utf-8')).hexdigest()[:16]
    key = (srt_sha, voice, alignment, aspect, narration, quality)
    existing = _motion_index_get(key)
    if existing:
        logger.info('[Motion.v1] dedup join: %s', existing)
        return jsonify({'ok': True, 'task_id': existing, 'deduped': True})

    task_id = _motion_task_id()
    workdir = os.path.join(motion_root(), 'jobs', task_id)
    os.makedirs(workdir, exist_ok=True)

    if srt_text:
        srt_path = os.path.join(workdir, 'transcription.srt')
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(srt_text)
    else:
        srt_path = srt_path_in
        if not os.path.isfile(srt_path):
            return api_bad_request('srt_path is not a file', field='srt_path')

    task = _new_motion_task(
        task_id, srt_path=srt_path, workdir=workdir, voice=voice,
        speed=speed, alignment=alignment, narration=narration,
        quality=quality, parallel=parallel, width=width, height=height,
        scenes_path=scenes_path)
    _motion_index_register(key, task_id)
    _motion_runtime.spawn(task_id, run_motion_task, task)
    logger.info('[Motion.v1] started %s (aspect=%s narration=%s parallel=%d)',
                task_id, aspect, narration, parallel)
    return jsonify({'ok': True, 'task_id': task_id, 'deduped': False})


register_task_routes(api_v1_motion_bp, _motion_runtime,
    url_prefix='/api/v1/motion/videos')


@api_v1_motion_bp.route('/api/v1/motion/videos/<task_id>/file', methods=['GET'])
@require_auth
def serve_motion_file(task_id):
    """Serve the final MP4 (or ?part=srt sidecar) with Range support.

    SYNC on purpose: pure file serving whose only blocking call is the
    sync-safe ``send_file`` shim (same carve-out as serve_paper_image).
    Path safety: we serve exactly the path recorded in the task result —
    never client-supplied path material.
    """
    task = _motion_runtime.get(task_id)
    if not task:
        return api_not_found('not_found')
    result = task.get('result') or {}
    from flask import request as _req
    part = (_req.args.get('part') or 'mp4').strip()
    path = result.get('srt_path') if part == 'srt' else result.get('final_path')
    if not path or not os.path.isfile(path):
        return api_not_found('file_not_ready')
    mimetype = 'application/x-subrip' if part == 'srt' else 'video/mp4'
    return send_file(path, mimetype=mimetype, conditional=True)


__all__ = ['api_v1_motion_bp']
