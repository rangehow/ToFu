"""lib/motion_video/_env.py — Render-chain environment manager.

Single source of truth for locating (and, when missing, bootstrapping) the
four external binaries the HyperFrames render chain needs:

  * **node/npm**   — HyperFrames is an npm CLI; Node >= 22 required.
  * **hyperframes**— the ``hyperframes`` CLI itself. Resolution order:
    ``TOFU_HYPERFRAMES_BIN`` env → the managed install at
    ``<data>/motion_video/node/node_modules/.bin/hyperframes`` → PATH.
    :func:`ensure_hyperframes` auto-installs a pinned version into the
    managed dir with ``npm install --ignore-scripts`` (the postinstall of
    the transitive ``onnxruntime-node`` dep hits api.nuget.org, which is
    unreachable on some corporate networks; the scripts only matter for
    the transcribe/remove-background features we do not use).
  * **ffmpeg**     — ``TOFU_FFMPEG`` env → ``imageio_ffmpeg``'s bundled
    static binary (pip package, no root needed) → PATH.
  * **ffprobe**    — ``TOFU_FFPROBE`` env → PATH. OPTIONAL: ``probe_video``
    falls back to parsing ``ffmpeg -i`` stderr when absent.
  * **chrome**     — ``HYPERFRAMES_BROWSER_PATH`` (honoured natively by the
    CLI) → newest Playwright chromium under ``~/.cache/ms-playwright``.

The headless-Chromium shared-library trap (libatk/libgbm missing on
minimal servers) is handled by prepending the running Python's
``sys.prefix/lib`` to ``LD_LIBRARY_PATH`` when that directory actually
carries the GUI libs (the conda-env recipe) — see
:func:`build_render_env`.

Everything is probed lazily and logged; nothing here raises on a missing
piece — :func:`probe_env` reports an ``issues`` list instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'PINNED_HYPERFRAMES',
    'motion_root',
    'node_root',
    'node_bin',
    'npm_bin',
    'hyperframes_bin',
    'ensure_hyperframes',
    'ffmpeg_bin',
    'ffprobe_bin',
    'chrome_bin',
    'build_render_env',
    'probe_env',
]

#: HyperFrames version pinned for the managed install (P0-verified).
PINNED_HYPERFRAMES = '0.7.71'

#: Minimum Node major version required by the HyperFrames CLI.
_MIN_NODE_MAJOR = 22


# ── Roots ─────────────────────────────────────────────────

def motion_root() -> str:
    """Writable root for motion-video state (managed node install, workdirs)."""
    from lib.runtime_paths import data_root
    path = os.path.join(data_root(), 'motion_video')
    os.makedirs(path, exist_ok=True)
    return path


def node_root() -> str:
    """Directory holding the managed npm project for the HyperFrames CLI."""
    path = os.path.join(motion_root(), 'node')
    os.makedirs(path, exist_ok=True)
    return path


# ── Binary resolution ─────────────────────────────────────

def node_bin() -> str:
    return shutil.which('node') or ''


def npm_bin() -> str:
    return shutil.which('npm') or ''


def hyperframes_bin() -> str:
    """Locate the hyperframes CLI (env → managed install → PATH)."""
    override = os.environ.get('TOFU_HYPERFRAMES_BIN', '').strip()
    if override and os.path.isfile(override):
        return override
    managed = os.path.join(node_root(), 'node_modules', '.bin', 'hyperframes')
    if os.path.isfile(managed):
        return managed
    return shutil.which('hyperframes') or ''


def ffmpeg_bin() -> str:
    """Locate an ffmpeg binary (env → imageio-ffmpeg static build → PATH)."""
    override = os.environ.get('TOFU_FFMPEG', '').strip()
    if override and os.path.isfile(override):
        return override
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception as e:
        logger.debug('[MotionVideo] imageio_ffmpeg unavailable: %s', e)
    return shutil.which('ffmpeg') or ''


def ffprobe_bin() -> str:
    """Locate ffprobe (env → PATH). Optional — probe has an ffmpeg fallback."""
    override = os.environ.get('TOFU_FFPROBE', '').strip()
    if override and os.path.isfile(override):
        return override
    return shutil.which('ffprobe') or ''


def _playwright_chrome_candidates() -> list[str]:
    """All Playwright-cached Chrome executables, newest build first."""
    base = os.path.expanduser(os.path.join('~', '.cache', 'ms-playwright'))
    cands: list[tuple[int, str]] = []
    try:
        builds = os.listdir(base)
    except OSError as e:
        logger.debug('[MotionVideo] cannot list %s: %s', base, e)
        return []
    for b in builds:
        if not (b.startswith('chromium-') or b.startswith('chromium_')):
            continue
        try:
            rev = int(b.rsplit('-', 1)[-1].rsplit('_', 1)[-1])
        except ValueError:
            rev = 0
        for rel in ('chrome-linux64/chrome', 'chrome-linux/chrome',
                    'chrome-headless-shell-linux64/chrome-headless-shell'):
            p = os.path.join(base, b, rel)
            if os.path.isfile(p):
                cands.append((rev, p))
    cands.sort(key=lambda t: -t[0])
    return [p for _, p in cands]


def chrome_bin() -> str:
    """Locate a Chromium executable for HyperFrames capture."""
    override = os.environ.get('HYPERFRAMES_BROWSER_PATH', '').strip()
    if override and os.path.isfile(override):
        return override
    cands = _playwright_chrome_candidates()
    if cands:
        return cands[0]
    for name in ('google-chrome', 'chromium', 'chromium-browser'):
        p = shutil.which(name)
        if p:
            return p
    return ''


def ensure_hyperframes(*, install: bool = True, timeout: int = 900) -> str:
    """Return the hyperframes CLI path, installing the pinned version if absent.

    The managed install lives in ``<data>/motion_video/node`` and deliberately
    uses ``--ignore-scripts`` — the transitive ``onnxruntime-node`` postinstall
    downloads from api.nuget.org (unreachable on some networks) and is only
    needed for the transcribe / remove-background features, not rendering.

    Returns the CLI path ('' when unavailable and ``install`` is False or the
    install failed). Never raises; failures are logged + reported as ''.
    """
    existing = hyperframes_bin()
    if existing:
        return existing
    if not install:
        return ''
    npm = npm_bin()
    if not npm:
        logger.warning('[MotionVideo] cannot install hyperframes — npm not found')
        return ''
    root = node_root()
    try:
        if not os.path.isfile(os.path.join(root, 'package.json')):
            subprocess.run([npm, 'init', '-y'], cwd=root, check=True,
                           capture_output=True, timeout=120)
        logger.info('[MotionVideo] installing hyperframes@%s into %s '
                    '(this is a one-time bootstrap)', PINNED_HYPERFRAMES, root)
        proc = subprocess.run(
            [npm, 'install', f'hyperframes@{PINNED_HYPERFRAMES}',
             '--no-fund', '--no-audit', '--ignore-scripts'],
            cwd=root, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            logger.warning('[MotionVideo] hyperframes install failed rc=%s: %.500s',
                           proc.returncode, proc.stderr)
            return ''
    except subprocess.TimeoutExpired:
        logger.warning('[MotionVideo] hyperframes install timed out after %ss', timeout)
        return ''
    except Exception as e:
        logger.warning('[MotionVideo] hyperframes install error: %s', e, exc_info=True)
        return ''
    path = hyperframes_bin()
    if path:
        logger.info('[MotionVideo] hyperframes installed at %s', path)
    else:
        logger.warning('[MotionVideo] hyperframes install finished but CLI not found in %s',
                       root)
    return path


# ── Render environment ────────────────────────────────────

def _conda_gui_lib_dir() -> str:
    """``sys.prefix/lib`` when it actually carries the headless-Chrome GUI libs."""
    lib = os.path.join(sys.prefix, 'lib')
    if os.path.isfile(os.path.join(lib, 'libatk-1.0.so.0')):
        return lib
    return ''


def build_render_env(base: dict | None = None) -> dict:
    """Build the subprocess environment for hyperframes / ffmpeg calls.

    On top of the inherited environment:

    * ``PATH`` is prepended with the resolved ffmpeg's directory so the CLI
      finds it (and ffprobe when it sits alongside).
    * ``HYPERFRAMES_BROWSER_PATH`` points at the resolved Chrome so the CLI
      skips its own (potentially blocked) browser download.
    * ``LD_LIBRARY_PATH`` is prepended with ``sys.prefix/lib`` when that dir
      carries the GUI libs headless Chrome needs (libatk & friends) — the
      rootless-conda recipe.
    """
    env = dict(base if base is not None else os.environ)
    ff = ffmpeg_bin()
    if ff:
        ff_dir = os.path.dirname(ff)
        env['PATH'] = ff_dir + os.pathsep + env.get('PATH', '')
        probe = ffprobe_bin()
        if probe:
            probe_dir = os.path.dirname(probe)
            if probe_dir != ff_dir:
                env['PATH'] = probe_dir + os.pathsep + env['PATH']
    chrome = chrome_bin()
    if chrome:
        env['HYPERFRAMES_BROWSER_PATH'] = chrome
    gui_lib = _conda_gui_lib_dir()
    if gui_lib:
        existing = env.get('LD_LIBRARY_PATH', '')
        env['LD_LIBRARY_PATH'] = (gui_lib + os.pathsep + existing) if existing else gui_lib
    return env


# ── Full probe ────────────────────────────────────────────

def _node_major() -> int:
    node = node_bin()
    if not node:
        return 0
    try:
        out = subprocess.run([node, '--version'], capture_output=True,
                             text=True, timeout=15)
        ver = out.stdout.strip().lstrip('v')
        return int(ver.split('.', 1)[0])
    except Exception as e:
        logger.debug('[MotionVideo] node --version failed: %s', e)
        return 0


def probe_env() -> dict:
    """Probe every render-chain dependency. Returns a status dict::

        {'ok': bool, 'node': 'v24.13.0'|'',
         'hyperframes': path|'', 'ffmpeg': path|'', 'ffprobe': path|'',
         'chrome': path|'', 'issues': [str, ...]}

    ``ok`` is True only when the four REQUIRED pieces (node>=22, hyperframes,
    ffmpeg, chrome) all resolve. Never raises; never installs (that is
    :func:`ensure_hyperframes`'s job).
    """
    issues: list[str] = []
    major = _node_major()
    node_ver = ''
    if major:
        node_ver = f'v{major}'  # major is enough for the gate; full string unnecessary
    if not node_bin():
        issues.append('node not found on PATH (Node.js >= 22 required)')
    elif major < _MIN_NODE_MAJOR:
        issues.append(f'node too old (need >= {_MIN_NODE_MAJOR}, found major {major})')

    hf = hyperframes_bin()
    if not hf:
        issues.append('hyperframes CLI not found (call motion_video_env_check '
                      'with install=true, or set TOFU_HYPERFRAMES_BIN)')
    ff = ffmpeg_bin()
    if not ff:
        issues.append('ffmpeg not found (pip install imageio-ffmpeg, or set TOFU_FFMPEG)')
    chrome = chrome_bin()
    if not chrome:
        issues.append('no Chrome/Chromium found (set HYPERFRAMES_BROWSER_PATH, '
                      'or install Playwright chromium)')
    probe = ffprobe_bin()
    return {
        'ok': not issues,
        'node': node_ver,
        'hyperframes': hf,
        'ffmpeg': ff,
        'ffprobe': probe,
        'chrome': chrome,
        'issues': issues,
    }
