"""lib/tasks_pkg/handlers/motion_video.py — Motion-video tool handlers.

Thin dispatch layer over :mod:`lib.motion_video`: path resolution
(project-relative or absolute), abort wiring (the task's ``abort_event``
reaches the render/concat subprocess wrappers), and tool-round
finalization with plain-text badges (per CLAUDE.md §3.4).
"""

from __future__ import annotations

import json
import os

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tools.motion_video import MOTION_VIDEO_TOOL_NAMES


def _resolve(path: str, project_path: str | None) -> str:
    """Resolve a tool path arg: absolute stays, relative joins the project."""
    path = (path or '').strip()
    if not path:
        return ''
    if os.path.isabs(path):
        return path
    if project_path:
        return os.path.join(project_path, path)
    return os.path.abspath(path)


def _fmt(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1)


@tool_registry.tool_set(
    MOTION_VIDEO_TOOL_NAMES,
    category='video',
    description='Motion video (MG animation) generation pipeline')
def _handle_motion_video_tool(task, tc, fn_name, tc_id, fn_args, rn,
                              round_entry, cfg, project_path,
                              project_enabled, all_tools=None):
    """Handle the motion_video_* tool family."""
    from lib import motion_video as mv

    abort_event = task.get('abort_event') if isinstance(task, dict) else None
    proj = project_path if project_enabled else None
    badge = 'ok'

    try:
        if fn_name == 'motion_video_env_check':
            install = fn_args.get('install', True)
            if install:
                mv.ensure_hyperframes(install=True)
                mv.ensure_ffmpeg(install=True)
                mv.ensure_ffprobe(install=True)
            result = mv.probe_env()
            badge = 'ready' if result.get('ok') else 'env-missing'

        elif fn_name == 'motion_video_storyboard_check':
            srt_path = _resolve(fn_args.get('srt_path', ''), proj)
            scenes_path = _resolve(fn_args.get('scenes_path', ''), proj)
            tol = float(fn_args.get('tolerance') or 0.1)
            try:
                with open(srt_path, encoding='utf-8') as f:
                    entries = mv.parse_srt(f.read())
                with open(scenes_path, encoding='utf-8') as f:
                    scenes = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                result = {'ok': False, 'errors': [f'cannot read inputs: {e}']}
                badge = 'failed'
            else:
                if not entries:
                    result = {'ok': False,
                              'errors': ['SRT parsed to zero cues']}
                    badge = 'failed'
                else:
                    errors = mv.check_storyboard(scenes, mv.total_span(entries),
                                                 tol=tol)
                    result = {'ok': not errors, 'errors': errors,
                              'span_s': [round(s, 3) for s in mv.total_span(entries)]}
                    badge = 'ok' if not errors else f'{len(errors)} errors'

        elif fn_name == 'motion_video_check':
            project_dir = _resolve(fn_args.get('project_dir', ''), proj)
            result = mv.check_project(project_dir, abort_event=abort_event)
            badge = 'ok' if result.get('ok') else (result.get('category') or 'failed')

        elif fn_name == 'motion_video_render':
            project_dir = _resolve(fn_args.get('project_dir', ''), proj)
            output = _resolve(fn_args.get('output', ''), proj)
            quality = fn_args.get('quality') or 'standard'
            fps = fn_args.get('fps')
            timeout = int(fn_args.get('timeout') or 1800)
            result = mv.render_project(project_dir, output, quality=quality,
                                       fps=fps, timeout=timeout,
                                       abort_event=abort_event)
            badge = ('rendered' if result.get('ok')
                     else (result.get('category') or 'failed'))

        elif fn_name == 'motion_video_probe':
            path = _resolve(fn_args.get('path', ''), proj)
            info = mv.probe_video(path)
            result = {'ok': info is not None, 'path': path,
                      'probe': info or {}}
            badge = 'ok' if info is not None else 'failed'

        elif fn_name == 'motion_video_concat':
            inputs = [_resolve(p, proj) for p in (fn_args.get('inputs') or [])]
            output = _resolve(fn_args.get('output', ''), proj)
            timeout = int(fn_args.get('timeout') or 1800)
            result = mv.concat_mp4s(inputs, output, timeout=timeout,
                                    abort_event=abort_event)
            badge = ('concatenated' if result.get('ok')
                     else (result.get('category') or 'failed'))

        else:
            result = {'ok': False, 'errors': [f'unknown tool {fn_name}']}
            badge = 'failed'

    except Exception as e:
        logger.error('[MotionVideo] %s failed: %s', fn_name, e, exc_info=True)
        result = {'ok': False, 'category': 'unknown', 'detail': str(e)}
        badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content,
        source='MotionVideo',
        title=fn_name.replace('motion_video_', ''),
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge,
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
