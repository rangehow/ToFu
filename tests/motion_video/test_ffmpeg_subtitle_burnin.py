"""End-to-end check of the registered FFmpeg subtitle burn-in path.

Generates a 2-second ``testsrc`` clip, writes a two-cue SRT, burns the cues
into the video with the ``subtitles`` (libass) filter, and probes the MP4 for
duration, resolution, codec and burned-in ink. Everything runs in the test's
own scratch directory against the project toolchain declared in
``tofu.toolchains.json`` (``TOFU_FFMPEG`` / ``TOFU_FFPROBE`` or ``PATH``); no
network, model, user media or app process is involved.

This proves this host's encoding + subtitle path only. It does not measure
end-user visual quality, decode real footage, or cover macOS.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

WIDTH, HEIGHT, FPS, DURATION = 320, 180, 25, 2
SRT = """1
00:00:00,000 --> 00:00:01,000
First synthetic cue

2
00:00:01,000 --> 00:00:02,000
Second synthetic cue
"""


def _resolve(name: str, env_name: str) -> str:
    override = os.environ.get(env_name, '').strip()
    if override and os.path.isfile(override):
        return override
    return shutil.which(name) or ''


def _run(argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, timeout=120, **kwargs)


def _frame_rgb(ffmpeg: str, video: str, at: float) -> bytes:
    """Decode one frame to raw rgb24 so burned ink is measured, not guessed."""
    result = _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss', str(at),
        '-i', video, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-',
    ])
    assert len(result.stdout) == WIDTH * HEIGHT * 3, (
        f'expected one {WIDTH}x{HEIGHT} rgb24 frame, got {len(result.stdout)} bytes')
    return result.stdout


def test_synthetic_srt_burns_into_mp4(tmp_path, record_property):
    ffmpeg = _resolve('ffmpeg', 'TOFU_FFMPEG')
    if not ffmpeg:
        pytest.skip('no FFmpeg for the subtitle burn-in: TOFU_FFMPEG is unset and '
                    '"ffmpeg" is not on PATH; install the "ffmpeg" entry in '
                    'tofu.toolchains.json (its install recipe is the conda-forge '
                    'ffmpeg package with libx264 + libass)')
    ffprobe = _resolve('ffprobe', 'TOFU_FFPROBE')
    if not ffprobe:
        pytest.skip('FFmpeg is present but ffprobe is missing (TOFU_FFPROBE unset '
                    'and "ffprobe" not on PATH), so the burn-in cannot be verified')

    filters = _run([ffmpeg, '-hide_banner', '-filters']).stdout.decode('utf-8', 'replace')
    if ' subtitles ' not in filters:
        pytest.skip(f'{ffmpeg} has no "subtitles" (libass) filter, so a subtitle '
                    'burn-in cannot be rendered; use a build with --enable-libass')

    scratch = tmp_path / 'subtitle-burn-fixture'
    scratch.mkdir()
    (scratch / 'cues.srt').write_text(SRT, encoding='utf-8')
    source, burned = 'source.mp4', 'burned.mp4'

    _run([
        ffmpeg, '-hide_banner', '-loglevel', 'error',
        '-f', 'lavfi', '-i', f'testsrc=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={DURATION}',
        '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', source,
    ], cwd=scratch)
    burn_command = [
        ffmpeg, '-hide_banner', '-loglevel', 'error',
        '-i', source, '-vf', 'subtitles=filename=cues.srt',
        '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart', '-y', burned,
    ]
    _run(burn_command, cwd=scratch)

    output = scratch / burned
    assert output.is_file() and output.stat().st_size > 0

    probe = json.loads(_run([
        ffprobe, '-v', 'error', '-print_format', 'json',
        '-show_format', '-show_streams', str(output),
    ]).stdout)
    video_streams = [s for s in probe['streams'] if s.get('codec_type') == 'video']
    assert len(video_streams) == 1, probe['streams']
    video = video_streams[0]
    assert video['codec_name'] == 'h264'
    assert (video['width'], video['height']) == (WIDTH, HEIGHT)
    assert float(probe['format']['duration']) == pytest.approx(DURATION, abs=0.15)
    assert float(video['duration']) == pytest.approx(DURATION, abs=0.15)
    assert 'mp4' in probe['format']['format_name']

    before = _frame_rgb(ffmpeg, str(scratch / source), 0.5)
    after = _frame_rgb(ffmpeg, str(output), 0.5)
    changed = [
        (index // 3 % WIDTH, index // 3 // WIDTH)
        for index in range(0, len(before), 3)
        if abs(before[index] - after[index])
        + abs(before[index + 1] - after[index + 1])
        + abs(before[index + 2] - after[index + 2]) > 60
    ]
    # Re-encoding the same frame changes ~1 pixel; a rendered cue changes a
    # contiguous ink region. Require a visible region, not just any difference.
    assert len(changed) > 0.001 * WIDTH * HEIGHT, (
        f'no subtitle ink detected: only {len(changed)} pixels changed at t=0.5s')
    ys = [y for _, y in changed]
    assert sum(ys) / len(ys) > HEIGHT / 2, (
        f'subtitle ink is not in the lower half of the frame: y={min(ys)}..{max(ys)}')

    version = _run([ffmpeg, '-version']).stdout.decode('utf-8', 'replace').splitlines()[0]
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    record_property('ffmpeg_path', ffmpeg)
    record_property('ffmpeg_version', version)
    record_property('burn_command', ' '.join(burn_command))
    record_property('output_sha256', digest)
    record_property('output_bytes', output.stat().st_size)