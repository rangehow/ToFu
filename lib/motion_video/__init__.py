"""lib/motion_video — Motion video (MG animation) generation pipeline.

Tofu's native absorb-and-surpass of the vibe-motion/auto-motion workflow
(SRT transcript → semantic storyboard → per-scene HyperFrames composition →
render → concat → final.mp4). See docs/MOTION_VIDEO_DESIGN.md.

Layers:

  * :mod:`._env`    — render-chain environment (node / hyperframes CLI /
                      ffmpeg / ffprobe / Chrome), incl. the managed
                      auto-install of the pinned HyperFrames CLI.
  * :mod:`._srt`    — SRT parsing (millisecond precision).
  * :mod:`._gates`  — zero-LLM validation: storyboard timeline gates,
                      composition static contract, media spec verification.
  * :mod:`._render` — HyperFrames CLI subprocess wrapper (env injection,
                      timeout, cooperative abort, failure classification).
  * :mod:`._concat` — scene normalization + concat → final.mp4 (atomic).

The ``guide/`` directory holds the vendored in-tree knowledge the chat
agent reads (workflow + composition contract + skeleton). The full
vibe-motion skill packs (29 motion rules, 13 blueprints, 20+ design frame
presets) are installable from the Settings → Skills catalog.
"""

from __future__ import annotations

from lib.motion_video._concat import concat_mp4s
from lib.motion_video._env import (
    PINNED_HYPERFRAMES,
    build_render_env,
    chrome_bin,
    ensure_hyperframes,
    ffmpeg_bin,
    ffprobe_bin,
    hyperframes_bin,
    motion_root,
    probe_env,
)
from lib.motion_video._gates import (
    check_composition_html,
    check_storyboard,
    probe_video,
    verify_spec,
)
from lib.motion_video._render import (
    check_project,
    inspect_project,
    lint_project,
    render_project,
    validate_project,
)
from lib.motion_video._srt import (
    SrtEntry,
    format_timestamp,
    parse_srt,
    parse_timestamp,
    total_span,
)

__all__ = [
    'PINNED_HYPERFRAMES',
    'motion_root',
    'probe_env',
    'build_render_env',
    'hyperframes_bin',
    'ensure_hyperframes',
    'ffmpeg_bin',
    'ffprobe_bin',
    'chrome_bin',
    'SrtEntry',
    'parse_srt',
    'parse_timestamp',
    'format_timestamp',
    'total_span',
    'check_storyboard',
    'check_composition_html',
    'probe_video',
    'verify_spec',
    'lint_project',
    'validate_project',
    'inspect_project',
    'check_project',
    'render_project',
    'concat_mp4s',
]
