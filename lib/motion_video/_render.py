"""lib/motion_video/_render.py — HyperFrames CLI subprocess wrapper.

Runs the ``hyperframes`` CLI (lint / validate / inspect / render) as a
subprocess with the managed environment from :mod:`lib.motion_video._env`
(ffmpeg on PATH, ``HYPERFRAMES_BROWSER_PATH``, conda GUI libs on
``LD_LIBRARY_PATH``), a wall-clock timeout, and cooperative abort via a
``threading.Event`` (the task runtime's ``abort_event``).

Every public function returns a plain dict result (never raises) so the
agent's self-repair loop gets structured feedback:

    {'ok': bool, 'category': str, 'detail': str, ...}

``category`` is one of: ``env_missing`` / ``lint`` / ``chrome`` /
``timeout`` / ``aborted`` / ``io`` / ``unknown`` — the structured failure
diagnosis the design doc calls for (auto-motion leaves you reading raw
logs; we classify).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['lint_project', 'validate_project', 'inspect_project',
           'check_project', 'render_project']

_TAIL = 1500


def _run_cli(args: list[str], *, cwd: str, timeout: int,
             abort_event=None, env: dict | None = None) -> dict:
    """Run ``hyperframes <args>`` with timeout + abort polling.

    Returns ``{'rc': int|None, 'out': str, 'err': str, 'elapsed': float,
    'category': str}`` — category is '' on a clean exit classification.
    """
    from lib.motion_video._env import build_render_env
    env = env or build_render_env()
    start = time.time()
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
    except FileNotFoundError:
        return {'rc': None, 'out': '', 'err': f'executable not found: {args[0]}',
                'elapsed': 0.0, 'category': 'env_missing'}
    except Exception as e:
        logger.error('[MotionVideo] spawn failed for %s: %s', args[0], e, exc_info=True)
        return {'rc': None, 'out': '', 'err': str(e), 'elapsed': 0.0, 'category': 'io'}

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    category = ''
    deadline = start + timeout if timeout and timeout > 0 else None
    while True:
        try:
            out, err = proc.communicate(timeout=1.0)
            out_chunks.append(out or '')
            err_chunks.append(err or '')
            break
        except subprocess.TimeoutExpired:
            if abort_event is not None and abort_event.is_set():
                category = 'aborted'
                break
            if deadline is not None and time.time() > deadline:
                category = 'timeout'
                break
    if category:
        logger.warning('[MotionVideo] %s %s — killing process group %s',
                       args[0], category, proc.pid)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception as e:
            logger.debug('[MotionVideo] killpg failed: %s', e)
        try:
            out, err = proc.communicate(timeout=10)
            out_chunks.append(out or '')
            err_chunks.append(err or '')
        except Exception as e:
            logger.debug('[MotionVideo] communicate-after-kill failed: %s', e)
    return {'rc': proc.poll(), 'out': ''.join(out_chunks),
            'err': ''.join(err_chunks), 'elapsed': time.time() - start,
            'category': category}


def _classify_failure(res: dict) -> str:
    """Map a non-zero CLI result onto a failure category."""
    if res['category']:
        return res['category']
    blob = (res['err'] + '\n' + res['out']).lower()
    if 'ffmpeg not found' in blob or 'ffprobe not found' in blob:
        return 'env_missing'
    if 'chrome' in blob or 'browser' in blob or 'puppeteer' in blob:
        return 'chrome'
    return 'unknown'


def _cli_or_env_error() -> str:
    """Resolve the hyperframes binary or return '' (caller builds env_missing)."""
    from lib.motion_video._env import hyperframes_bin
    return hyperframes_bin()


def _env_missing_result(tool: str) -> dict:
    return {'ok': False, 'category': 'env_missing',
            'detail': 'hyperframes CLI not found — run motion_video_env_check '
                      f'with install=true first (before {tool})'}


# ── Quality gates (lint / validate / inspect) ─────────────

def _gate(subcmd: str, project_dir: str, *, timeout: int,
          abort_event=None) -> dict:
    cli = _cli_or_env_error()
    if not cli:
        return _env_missing_result(subcmd)
    res = _run_cli([cli, subcmd, project_dir, '--json'], cwd=project_dir,
                   timeout=timeout, abort_event=abort_event)
    if res['category']:
        return {'ok': False, 'category': res['category'],
                'detail': (res['err'] or res['out'])[-_TAIL:]}
    findings: list[dict] = []
    parse_note = ''
    try:
        data = json.loads(res['out'])
        findings = data.get('findings') or []
    except Exception:
        # Non-JSON output (older CLI / human format): fall back to exit code.
        parse_note = 'non-json output; gated on exit code'
    errors = [f for f in findings if f.get('severity') == 'error']
    ok = res['rc'] == 0 and not errors
    return {
        'ok': ok,
        'category': '' if ok else ('lint' if subcmd == 'lint' else _classify_failure(res)),
        'errors': [f.get('message', '') for f in errors],
        'warnings': [f.get('message', '') for f in findings
                     if f.get('severity') == 'warning'],
        'fix_hints': [f.get('fixHint', '') for f in errors if f.get('fixHint')],
        'detail': parse_note or (res['err'][-_TAIL:] if not ok else ''),
        'elapsed': round(res['elapsed'], 2),
    }


def lint_project(project_dir: str, *, timeout: int = 180, abort_event=None) -> dict:
    """``hyperframes lint`` — static composition checks."""
    return _gate('lint', project_dir, timeout=timeout, abort_event=abort_event)


def validate_project(project_dir: str, *, timeout: int = 300, abort_event=None) -> dict:
    """``hyperframes validate`` — headless-Chrome runtime errors + contrast."""
    return _gate('validate', project_dir, timeout=timeout, abort_event=abort_event)


def inspect_project(project_dir: str, *, timeout: int = 300, abort_event=None) -> dict:
    """``hyperframes inspect`` — layout overflow across timeline samples."""
    return _gate('inspect', project_dir, timeout=timeout, abort_event=abort_event)


def check_project(project_dir: str, *, timeout: int = 300, abort_event=None) -> dict:
    """Run all three static gates and merge the results."""
    merged = {'ok': True, 'category': '', 'errors': [], 'warnings': [],
              'fix_hints': [], 'gates': {}, 'elapsed': 0.0}
    for name, fn in (('lint', lint_project),
                     ('validate', validate_project),
                     ('inspect', inspect_project)):
        r = fn(project_dir, timeout=timeout, abort_event=abort_event)
        merged['gates'][name] = r
        merged['elapsed'] += r.get('elapsed', 0.0)
        merged['errors'].extend(f'[{name}] {e}' for e in r.get('errors', []))
        merged['warnings'].extend(f'[{name}] {w}' for w in r.get('warnings', []))
        merged['fix_hints'].extend(h for h in r.get('fix_hints', []) if h)
        if not r.get('ok'):
            merged['ok'] = False
            if not merged['category']:
                merged['category'] = r.get('category', 'unknown')
            # env_missing / aborted / timeout are fatal — stop early.
            if r.get('category') in ('env_missing', 'aborted', 'timeout'):
                break
    merged['elapsed'] = round(merged['elapsed'], 2)
    return merged


# ── Render ────────────────────────────────────────────────

_RENDERED_RE = re.compile(r'rendered in ([0-9.]+)s')


def render_project(project_dir: str, output: str, *, quality: str = 'standard',
                   fps: int | None = None, timeout: int = 1800,
                   abort_event=None) -> dict:
    """``hyperframes render`` one composition project → MP4.

    Args:
        project_dir: hyperframes project dir (holds index.html + hyperframes.json).
        output: MP4 output path (absolute, or relative to project_dir).
        quality: draft | standard | high.
        fps: optional 24/30/60 override.
        timeout: wall-clock seconds (0 = no limit).
        abort_event: optional threading.Event for cooperative cancel.

    Post-checks: the output file exists and is non-empty (the CLI's own
    contract — see the HyperFrames CLI skill's post-render verification).
    """
    cli = _cli_or_env_error()
    if not cli:
        return _env_missing_result('render')
    if quality not in ('draft', 'standard', 'high'):
        return {'ok': False, 'category': 'io',
                'detail': f'invalid quality {quality!r} (draft|standard|high)'}
    args = [cli, 'render', project_dir, '--quality', quality, '-o', output]
    if fps:
        args += ['--fps', str(fps)]
    logger.info('[MotionVideo] render start: %s (quality=%s fps=%s)',
                project_dir, quality, fps or 'default')
    res = _run_cli(args, cwd=project_dir, timeout=timeout, abort_event=abort_event)
    ok = res['rc'] == 0 and not res['category'] \
        and os.path.isfile(output) and os.path.getsize(output) > 0
    rendered_s = None
    m = _RENDERED_RE.search(res['out'])
    if m:
        rendered_s = float(m.group(1))
    result = {
        'ok': ok,
        'category': '' if ok else _classify_failure(res),
        'output': output if ok else '',
        'elapsed': round(res['elapsed'], 2),
        'render_time_s': rendered_s,
        'detail': '' if ok else (res['err'] or res['out'])[-_TAIL:],
    }
    if ok:
        logger.info('[MotionVideo] render done: %s (%.1fs wall)', output, res['elapsed'])
    else:
        logger.warning('[MotionVideo] render failed (%s): %.500s',
                       result['category'], result['detail'])
    return result
