"""lib/motion_video/_concat.py — Scene MP4 normalization + concatenation.

Final assembly of the motion-video pipeline: take the per-scene silent MP4s
and produce ``final.mp4``. Two paths, mirroring auto-motion's rule
("confirm uniform specs first; transcode-normalize when they differ"):

  * **uniform specs** (same codec/size/fps, no audio) → concat demuxer with
    ``-c copy`` (fast + lossless);
  * **mismatched specs** → single-pass re-encode to the first input's spec
    (scale + fps + yuv420p + silent).

The output is written atomically (tmp file + ``os.replace``) and the result
is probe-verified: total duration ≈ Σ scene durations (±0.5s), no audio
track.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['concat_mp4s']


def _uniform_spec(probes: list[dict]) -> bool:
    """True when every probe shares codec/size/fps and carries no audio."""
    if not probes or any(not p for p in probes):
        return False
    first = probes[0]
    for p in probes:
        if p.get('has_audio'):
            return False
        if (p.get('codec') != first.get('codec')
                or p.get('width') != first.get('width')
                or p.get('height') != first.get('height')
                or abs(float(p.get('fps') or 0) - float(first.get('fps') or 0)) > 0.6):
            return False
    return True


def _run_ffmpeg(args: list[str], *, timeout: int, abort_event=None,
                env: dict | None = None) -> dict:
    from lib.motion_video._env import build_render_env
    env = env or build_render_env()
    start = time.time()
    try:
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except FileNotFoundError:
        return {'rc': None, 'err': f'ffmpeg not found: {args[0]}',
                'elapsed': 0.0, 'category': 'env_missing'}
    except Exception as e:
        logger.error('[MotionVideo] ffmpeg spawn failed: %s', e, exc_info=True)
        return {'rc': None, 'err': str(e), 'elapsed': 0.0, 'category': 'io'}

    out_chunks: list[str] = []
    category = ''
    deadline = start + timeout if timeout and timeout > 0 else None
    while True:
        try:
            _out, err = proc.communicate(timeout=1.0)
            out_chunks.append(err or '')
            break
        except subprocess.TimeoutExpired:
            if abort_event is not None and abort_event.is_set():
                category = 'aborted'
                break
            if deadline is not None and time.time() > deadline:
                category = 'timeout'
                break
    if category:
        import signal as _sig
        try:
            os.killpg(proc.pid, _sig.SIGKILL)
        except Exception as e:
            logger.debug('[MotionVideo] killpg failed: %s', e)
        try:
            proc.communicate(timeout=10)
        except Exception as e:
            logger.debug('[MotionVideo] communicate-after-kill failed: %s', e)
    return {'rc': proc.poll(), 'err': ''.join(out_chunks),
            'elapsed': time.time() - start, 'category': category}


def concat_mp4s(inputs: list[str], output: str, *, timeout: int = 1800,
                abort_event=None) -> dict:
    """Concatenate scene MP4s into ``final.mp4`` (atomic + probe-verified).

    Args:
        inputs: ordered scene MP4 paths (≥1).
        output: final MP4 path.
        timeout: wall-clock seconds for each ffmpeg invocation.
        abort_event: optional threading.Event for cooperative cancel.

    Returns a result dict; on success ``{'ok': True, 'output', 'duration',
    'mode', 'elapsed'}`` where mode is ``'copy'`` or ``'reencode'``.
    """
    from lib.motion_video._env import ffmpeg_bin
    from lib.motion_video._gates import probe_video

    if not inputs:
        return {'ok': False, 'category': 'io', 'detail': 'no input scenes'}
    for p in inputs:
        if not os.path.isfile(p):
            return {'ok': False, 'category': 'io',
                    'detail': f'missing scene file: {p}'}
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        return {'ok': False, 'category': 'env_missing',
                'detail': 'ffmpeg not found (pip install imageio-ffmpeg)'}

    probes = [probe_video(p) for p in inputs]
    if any(p is None for p in probes):
        bad = inputs[probes.index(None)]
        return {'ok': False, 'category': 'io',
                'detail': f'cannot probe scene file: {bad}'}
    expected_total = sum(float(p.get('duration') or 0) for p in probes)

    out_dir = os.path.dirname(os.path.abspath(output)) or '.'
    os.makedirs(out_dir, exist_ok=True)
    uniform = _uniform_spec(probes)
    mode = 'copy' if uniform else 'reencode'
    logger.info('[MotionVideo] concat %d scene(s) → %s (mode=%s, Σ=%.2fs)',
                len(inputs), output, mode, expected_total)

    list_fd, list_path = tempfile.mkstemp(prefix='mvconcat-', suffix='.txt',
                                          dir=out_dir, text=True)
    tmp_out = output + '.tmp.mp4'
    try:
        with os.fdopen(list_fd, 'w') as lf:
            for p in inputs:
                safe = os.path.abspath(p).replace("'", "'\\''")
                lf.write(f"file '{safe}'\n")
        args = [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', list_path]
        if uniform:
            args += ['-c', 'copy']
        else:
            first = probes[0]
            args += [
                '-vf', f"scale={first['width']}:{first['height']},setsar=1",
                '-r', str(int(round(float(first.get('fps') or 30)))),
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18',
                '-preset', 'medium', '-an',
            ]
        args += ['-movflags', '+faststart', tmp_out]
        res = _run_ffmpeg(args, timeout=timeout, abort_event=abort_event)
        if res['category'] or res['rc'] != 0:
            return {'ok': False,
                    'category': res['category'] or 'unknown',
                    'detail': res['err'][-1500:]}
        if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
            return {'ok': False, 'category': 'io',
                    'detail': 'ffmpeg produced no output'}
        os.replace(tmp_out, output)
    finally:
        try:
            os.unlink(list_path)
        except OSError as e:
            logger.debug('[MotionVideo] concat list cleanup failed: %s', e)
        if os.path.isfile(tmp_out):
            try:
                os.unlink(tmp_out)
            except OSError as e:
                logger.debug('[MotionVideo] tmp output cleanup failed: %s', e)

    # ── Post-verify: duration ≈ Σ scenes, silent ──
    final_probe = probe_video(output)
    detail = ''
    if final_probe is None:
        return {'ok': False, 'category': 'io',
                'detail': 'post-concat probe failed'}
    got = float(final_probe.get('duration') or 0)
    if expected_total > 0 and abs(got - expected_total) > 0.5:
        detail = (f'duration mismatch: final {got:.3f}s vs Σ scenes '
                  f'{expected_total:.3f}s')
        logger.warning('[MotionVideo] concat verify: %s', detail)
        return {'ok': False, 'category': 'io', 'detail': detail}
    if final_probe.get('has_audio'):
        logger.warning('[MotionVideo] concat verify: final has an audio track')
        return {'ok': False, 'category': 'io',
                'detail': 'final MP4 unexpectedly has an audio track'}

    logger.info('[MotionVideo] concat done: %s (%.2fs)', output, got)
    return {'ok': True, 'output': output, 'duration': round(got, 3),
            'mode': mode, 'elapsed': round(res['elapsed'], 2)}
