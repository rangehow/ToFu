"""lib/desktop_dist/winbuilder.py — build the WINDOWS payload on this Linux server.

Half A of the two-half build (docs/DESKTOP_CLIENT_BUILD_DESIGN.md §4.2):
the client-INDEPENDENT frozen payload, cached per (git_sha, deps stamp) so
the per-client wrapper (iscc, S3) is cheap. PyInstaller cannot
cross-compile, so the payload is built by a real Windows Python inside the
userspace Wine toolchain (wintoolchain.py — the four container traps are
solved THERE; this module adds the two traps the BUILD itself measured).

Pipeline (mirrors .github/workflows/build-desktop.yml's Windows leg):
  git archive HEAD        — the COMMITTED tree, never the shared dirty one
  nuget CPython (nupkg)   — a full Windows Python as a plain zip: no
                            installer, so no 32-bit bootstrapper is needed
                            (the open WoW64 question stays out of this path)
  pip (CI's exact recipe) — tofu-search first (sibling → vendor → index,
                            same order as builder.py), then requirements +
                            the build extras
  scripts/gen_desktop_icons.py
  pyinstaller tofu.spec
  TOFU_SMOKE=1 Tofu.exe   — the CI's own verdict, reused as the sentinel

Two traps measured while building this (2026-08-01), both pinned by tests:

1.  **wine swallows the Windows process exit code.** With the preloader
    removed (seccomp SIGSYS), the loader's direct-exec fallback returns 0
    for ``sys.exit(3)`` — measured; proot itself is faithful (native guest
    ``exit 3`` → 3). So NO step here may be judged by its exit code: every
    wine step runs as ``cmd /c "<inner> && echo <step-sentinel>"`` and the
    runner asserts the sentinel in stdout. A step that cannot print its
    sentinel failed, whatever the process status says.
2.  **The host's Python env poisons the guest.** ``PIP_REQUIRE_VIRTUALENV=1``
    (set by the corporate conda env) passed straight through proot+wine and
    killed the FIRST pip rehearsal with "Could not find an activated
    virtualenv (required)" — which the exit-code trap then hid. Every wine
    step runs with a scrubbed env: an allowlist (proxy + locale + WINEPREFIX),
    never PIP_*/PYTHON*/CONDA*/VIRTUAL_ENV.

Payload cache: ``<data_root>/desktop_toolchain/payloads/`` — the FUSE store
for cross-boot reuse; the WORK tree lives under the toolchain's local root
(local disk, see wintoolchain.py).

Half B (the wrapper) is NSIS, not Inno: EVERY 32-bit Windows app measured
(Inno's own installer, innounp) HANGS under the new WoW64 without the
preloader, and Inno 7's container resists 7-Zip 23.01 — iscc is
unobtainable AND unrunnable here (measured 2026-08-01). makensis runs
natively on Linux (conda-forge nsis): no wine, no display, deterministic.
The two installer authorings (CI's Inno .iss, our .nsi) are bound by
tests/test_installer_parity.py — the semantic contract, not the tool, is
the single source of truth.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time

from lib.log import get_logger, log_context

from . import store, wintoolchain

logger = get_logger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

_NUPKG_URL = 'https://www.nuget.org/api/v2/package/python/3.12.10'
_NUPKG_SHA256 = ('0eb85c2dfccccf1b17352de4c397f691'
                 '94035b7d37149eacc16f1147d93de3b8')
# The Windows python inside the guest (nuget layout: tools/python.exe).
_WINPY_GUEST = '/opt/winpy/tools/python.exe'
_WINPY_Z = 'Z:\\opt\\winpy\\tools\\python.exe'

# The build extras the CI installs beyond requirements.txt (build-desktop.yml
# "Install dependencies" step, verbatim). Part of the deps stamp.
_BUILD_EXTRAS = ('pyinstaller', 'pystray', 'pillow', 'psycopg2-binary',
                 'pyautogui', 'pyperclip', 'psutil')

# Env allowlist for wine steps (trap 2). Everything else — PIP_*,
# PYTHON*, CONDA*, VIRTUAL_ENV — is host poison for a Windows python.
_ENV_ALLOW = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
              'no_proxy', 'NO_PROXY', 'WINEPREFIX', 'HOME',
              'USER', 'LANG', 'LC_ALL', 'TZ')

_BUILD_TIMEOUT_S = int(os.environ.get('TOFU_DESKTOP_WINBUILD_TIMEOUT_S', 5400))

_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def is_running() -> bool:
    t = _worker
    return bool(t and t.is_alive())


def state() -> dict:
    b = store.load_manifest().get('winbuild')
    return b if isinstance(b, dict) else {'state': 'idle'}


def _set_state(**kw) -> None:
    m = store.load_manifest()
    b = m.get('winbuild') if isinstance(m.get('winbuild'), dict) else {}
    b.update(kw)
    m['winbuild'] = b
    store.save_manifest(m)


def start(reason: str = 'manual') -> dict:
    """Kick a background payload build (single-flight)."""
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return state()
        _worker = threading.Thread(target=_run_safe, args=(reason,),
                                   name='desktop-winbuilder', daemon=True)
        _worker.start()
    return state()


def _run_safe(reason: str) -> None:
    try:
        _run(reason)
    except Exception as e:
        logger.error('[WinBuild] build crashed: %s', e, exc_info=True)
        _set_state(state='error', error=f'crash: {e}')


# ── Payload identity + cache ─────────────────────────────────────────

def deps_stamp() -> str:
    """The dependency half of the payload id.

    requirements.txt + tofu.spec + the build-extras list: anything that
    changes what the frozen payload contains. A code-only change (same
    requirements) reuses the cached payload — that is what makes
    per-client wrapper builds (S3) cheap.
    """
    h = hashlib.sha256()
    for name in ('requirements.txt', 'tofu.spec'):
        with open(os.path.join(_REPO_ROOT, name), 'rb') as f:
            h.update(f.read())
    h.update('\0'.join(_BUILD_EXTRAS).encode())
    return h.hexdigest()[:16]


def _payloads_dir() -> str:
    d = os.path.join(wintoolchain.data_root(), 'desktop_toolchain',
                     'payloads')
    os.makedirs(d, exist_ok=True)
    return d


def payload_path(sha: str, stamp: str | None = None) -> str:
    stamp = stamp or deps_stamp()
    return os.path.join(_payloads_dir(),
                        f'payload-{sha[:12]}-{stamp}.tar.gz')


def cached_payload(sha: str, stamp: str | None = None) -> str | None:
    p = payload_path(sha, stamp)
    return p if os.path.isfile(p) else None


# ── The build ─────────────────────────────────────────────────────────

def _run(reason: str) -> None:
    version = _read_version()
    sha = _git('rev-parse', 'HEAD').strip()
    stamp = deps_stamp()
    t0 = time.time()
    cached = cached_payload(sha, stamp)
    if cached:
        logger.info('[WinBuild] payload cache hit for %s (%s)',
                    sha[:12], cached)
        _set_state(state='ok', version=version, git_sha=sha,
                   deps_stamp=stamp, reason=reason, cached=True,
                   payload=cached, started_at=t0,
                   finished_at=time.time(), error=None)
        return
    _set_state(state='running', version=version, git_sha=sha,
               deps_stamp=stamp, reason=reason, cached=False,
               started_at=t0, finished_at=None, error=None)
    # The work tree must live INSIDE the guest rootfs — wine only reaches
    # host paths through Z: (= the rootfs), so a workdir outside it is
    # unreadable to the Windows python (guest_z would rightly refuse).
    workdir = os.path.join(wintoolchain.rootfs_dir(), 'work',
                           f'{sha[:12]}-{int(t0)}')
    os.makedirs(workdir, exist_ok=True)
    log_path = os.path.join(_payloads_dir(), 'winbuild.log')
    with open(log_path, 'w', encoding='utf-8') as log_fh, \
            log_context('desktop_winbuild', logger=logger):
        _set_state(log=log_path)
        try:
            wintoolchain.provision()
            dist = _pipeline(workdir, version, sha, log_fh)
            dest = _cache_payload(dist, sha, stamp, log_fh)
        except Exception as e:
            logger.error('[WinBuild] build failed (log: %s): %s',
                         log_path, e)
            _set_state(state='error', error=str(e),
                       finished_at=time.time())
            return
    shutil.rmtree(workdir, ignore_errors=True)
    _set_state(state='ok', payload=dest, finished_at=time.time(),
               error=None)
    logger.info('[WinBuild] payload ok in %.0fs: %s',
                time.time() - t0, dest)


def _pipeline(workdir: str, version: str, sha: str, log_fh) -> str:
    """The heavy half (fakeable in tests — the recording half stays real)."""
    src = os.path.join(workdir, 'src')
    os.makedirs(src, exist_ok=True)
    _sh(f'git -C {_REPO_ROOT} archive HEAD | tar -x -C {src}', log_fh,
        shell=True)

    winpy = _ensure_winpython(log_fh)
    _ensure_guest_hosts(log_fh)

    # tofu-search FIRST (same exemption as builder.py — the floor is on no
    # index): sibling checkout → vendor wheel → index name.
    ts_src = _tofu_search_source(workdir)
    pip_env = _wine_env(_pip_index_env())
    _wpystep('pip-tofu-search',
             f'{winpy} -m pip install --no-warn-script-location '
             f'--no-deps {_z(ts_src)}', log_fh, env=pip_env)

    from . import builder as _linux_builder
    build_reqs = _linux_builder._requirements_without_tofu_search(
        os.path.join(src, 'requirements.txt'), workdir)
    extras = ' '.join(_BUILD_EXTRAS)
    _wpystep('pip-requirements',
             f'{winpy} -m pip install --no-warn-script-location '
             f'-r {_z(build_reqs)} {extras}',
             log_fh, cwd=src, env=pip_env, timeout=_BUILD_TIMEOUT_S)

    _wpystep('gen-icons', f'{winpy} scripts/gen_desktop_icons.py',
             log_fh, cwd=src)

    dist = os.path.join(workdir, 'dist')
    _wpystep('pyinstaller',
             f'{winpy} -m PyInstaller tofu.spec --distpath {_z(dist)} '
             f'--workpath {_z(os.path.join(workdir, "tmp"))} --noconfirm',
             log_fh, cwd=src, timeout=_BUILD_TIMEOUT_S)

    # Boot smoke — the launcher's own marker IS the sentinel (TOFU_SMOKE_OK).
    # TOFU_DB_PATH + stripped PG_* keep the smoke child off the production
    # PostgreSQL (builder.py's measured lesson, verbatim).
    env = _wine_env({'TOFU_SMOKE': '1',
                     'TOFU_DB_PATH': _z(os.path.join(workdir, 'smoke.db'))})
    out = _wstep('smoke', f'{_z(os.path.join(dist, "Tofu", "Tofu.exe"))}',
                 log_fh, sentinel='TOFU_SMOKE_OK', env=env,
                 timeout=600)
    import re as _re
    err = (out.stderr or b'').decode('utf-8', 'replace')
    if _re.search(r'Traceback \(most recent call last\)'
                  r'|ModuleNotFoundError|ImportError', err):
        raise RuntimeError('boot smoke stderr carries a traceback — '
                           'see winbuild.log')
    for residue in ('data', 'logs', 'uploads', 'project_sessions'):
        shutil.rmtree(os.path.join(dist, 'Tofu', residue),
                      ignore_errors=True)
    # Tar from a private staging copy (builder.py's measured lesson:
    # smoke-shutdown writes race tar's traversal on shared storage).
    staging = os.path.join(workdir, 'staging')
    shutil.copytree(os.path.join(dist, 'Tofu'),
                    os.path.join(staging, 'Tofu'))
    return staging


def _cache_payload(staging: str, sha: str, stamp: str, log_fh) -> str:
    """Tar the payload into the cache (atomic .part → rename)."""
    dest = payload_path(sha, stamp)
    part = dest + '.part'
    _sh(f'tar czf {part} -C {staging} Tofu', log_fh)
    os.replace(part, dest)
    size = os.path.getsize(dest)
    logger.info('[WinBuild] payload cached: %s (%d bytes)', dest, size)
    return dest


# ── Wine step runners (traps 1 & 2 baked in) ─────────────────────────

def _wine_env(extra: dict | None = None) -> dict:
    """Scrubbed env for wine steps (trap 2: host python vars are poison).

    PATH is set to a minimal guest-standard value, NOT inherited and NOT
    absent: proot resolves the `env` command through PATH, and an absent
    PATH makes it die with `'env' not found ($PATH=(null))` (measured on
    the first real build). Unix paths are inert for the Windows python —
    every command we issue uses absolute Z: paths or cmd builtins.
    """
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    env['WINEPREFIX'] = wintoolchain._GUEST_WINEPREFIX
    env['PATH'] = '/usr/local/bin:/usr/bin:/bin'
    for k, v in (extra or {}).items():
        env[k] = v
    return env


def _z(host_path: str) -> str:
    """Host path inside the rootfs → wine Z: path (wintoolchain.guest_z)."""
    return wintoolchain.guest_z(host_path)


def _wstep(name: str, inner: str, log_fh, *, sentinel: str | None = None,
           env: dict | None = None, timeout: int = 1800):
    """Run ONE wine step as cmd /c with a success sentinel (trap 1).

    The exit code is MEANINGLESS under our preloader-less wine (measured:
    sys.exit(3) → 0), so the verdict is the sentinel in stdout. A step
    that cannot print it failed, whatever the process status claims.
    """
    sentinel = sentinel or f'TOFU_STEP_OK_{name}'
    argv = wintoolchain._wine_argv(
        'cmd', '/c', f'{inner} && echo {sentinel}')
    logger.info('[WinBuild] step %s: %.200s', name, inner)
    out = subprocess.run(argv, capture_output=True, timeout=timeout,
                         env=env or _wine_env())
    stdout = (out.stdout or b'').decode('utf-8', 'replace')
    stderr = (out.stderr or b'').decode('utf-8', 'replace')
    log_fh.write(f'── step {name} ──\n{stdout}\n{stderr}\n')
    log_fh.flush()
    if sentinel not in stdout:
        raise RuntimeError(
            f'wine step {name!r} failed (no sentinel; exit codes are '
            f'unreliable under preloader-less wine). Log tail: '
            f'{stdout[-300:]!r}')
    return out


def _wpystep(name: str, inner: str, log_fh, *, cwd: str | None = None,
             env: dict | None = None, timeout: int = 1800):
    """A wine step with an optional cwd (cmd /c needs the cd inline)."""
    if cwd:
        inner = f'cd /d {_z(cwd)} && {inner}'
    return _wstep(name, inner, log_fh, env=env, timeout=timeout)


def _host_pip_config() -> tuple[str, list, list]:
    """(index_url, extra_index_urls, trusted_hosts) from `pip config list`."""
    try:
        out = subprocess.run(['pip', 'config', 'list'],
                             capture_output=True, timeout=30)
        text = out.stdout.decode('utf-8', 'replace')
    except Exception as e:
        logger.debug('[WinBuild] pip config list failed (%s)', e)
        text = ''
    index_url, extra, trusted = '', [], []
    for line in text.splitlines():
        if '=' not in line:
            continue
        key, _, val = line.partition('=')
        key = key.strip().split(':')[-1]  # drop ':env:'-style prefixes
        val = val.strip().strip("'").strip('"')
        # pip renders multi-value entries with literal \n separators.
        vals = [v for v in val.split('\\n') if v]
        if key == 'global.index-url' and vals:
            index_url = vals[0]
        elif key in ('install.extra-index-url', 'global.extra-index-url'):
            extra += vals
        elif key in ('install.trusted-host', 'global.trusted-host'):
            trusted += vals
    return index_url, extra, trusted


_HOSTS_BEGIN = '# TOFU-WINBUILD-HOSTS-BEGIN'
_HOSTS_END = '# TOFU-WINBUILD-HOSTS-END'


def _ensure_guest_hosts(log_fh) -> None:
    """Inject the pip index hosts into the guest's /etc/hosts.

    Wine's resolver is broken in this guest for DNS-requiring names
    (measured: python getaddrinfo fails for pip.sankuai.com on every
    family while the guest's NATIVE getent resolves it 5/5 — and an
    /etc/hosts entry makes wine resolve instantly). Resolution for the
    pip index hosts must therefore never reach DNS: resolve them on the
    HOST and pin them in the guest's hosts file. Idempotent marked block,
    regenerated each build so IP changes propagate.
    """
    from urllib.parse import urlparse
    index_url, extra, _trusted = _host_pip_config()
    urls = [u for u in ([index_url] + extra) if u] or [
        'https://pypi.org/simple', 'https://files.pythonhosted.org']
    lines = []
    for u in urls:
        host = urlparse(u).hostname
        if not host:
            continue
        out = subprocess.run(['getent', 'hosts', host],
                             capture_output=True, timeout=15)
        ip = (out.stdout.decode('utf-8', 'replace').split() or [''])[0] \
            if out.returncode == 0 else ''
        if ip:
            lines.append(f'{ip}\t{host}')
        else:
            logger.warning('[WinBuild] could not resolve %s on the host — '
                           'guest pip may fail for it', host)
    if not lines:
        return
    hosts_path = os.path.join(wintoolchain.rootfs_dir(), 'etc', 'hosts')
    try:
        with open(hosts_path, encoding='utf-8') as f:
            content = f.read()
    except OSError:
        content = ''
    import re as _re
    content = _re.sub(
        _re.escape(_HOSTS_BEGIN) + '.*?' + _re.escape(_HOSTS_END) + '\n?',
        '', content, flags=_re.S)
    block = (_HOSTS_BEGIN + '\n' + '\n'.join(lines) + '\n'
             + _HOSTS_END + '\n')
    with open(hosts_path, 'w', encoding='utf-8') as f:
        f.write(content.rstrip('\n') + '\n' + block)
    log_fh.write(f'── guest hosts injected: {lines} ──\n')
    log_fh.flush()


def _pip_index_env() -> dict:
    """The HOST's pip index config as ENV for the guest's pip steps.

    ENV, not CLI args, on purpose: pip's PEP 517 build isolation spawns a
    SUB-pip for build dependencies that inherits pip CONFIG (env/files),
    never the parent's CLI flags — measured: the outer pip took
    --index-url fine and the sub-pip still fetched setuptools from
    pypi.org, dying on the corporate proxy's MITM cert. PIP_INDEX_URL et
    al. reach both.

    The guest has no pip config files, so without this it defaults to
    pypi.org — which this proxy MITMs (the host avoids it via internal
    HTTP mirrors in `pip config list`). On hosts with no pip config, fall
    back to the CI's pypi.org extra-index.

    With a mirror configured, pypi.org STAYS as the extra-index AND joins
    trusted-host: the mirrors have gaps (measured: pymupdf_layout==1.27.2.3
    is on no internal mirror — requirements.txt's own comment prescribes
    pypi.org for it), and the proxy's MITM cert is the same trust posture
    the host's own pip.conf already applies to the internal mirrors.
    """
    index_url, extra, trusted = _host_pip_config()
    if not index_url:
        logger.info('[WinBuild] no host pip index config — CI pypi.org '
                    'fallback')
        return {'PIP_EXTRA_INDEX_URL': 'https://pypi.org/simple'}
    extra.append('https://pypi.org/simple')
    trusted += ['pypi.org', 'files.pythonhosted.org']
    env = {'PIP_INDEX_URL': index_url}
    if extra:
        env['PIP_EXTRA_INDEX_URL'] = ' '.join(extra)
    if trusted:
        env['PIP_TRUSTED_HOST'] = ' '.join(trusted)
    logger.info('[WinBuild] guest pip mirrors host config: %s (+%d extras)',
                index_url, len(extra))
    return env


# ── Small helpers (mirroring builder.py conventions) ─────────────────

def _ensure_winpython(log_fh) -> str:
    """The nuget CPython, extracted into the guest (idempotent)."""
    dest_dir = os.path.join(wintoolchain.rootfs_dir(), 'opt', 'winpy')
    exe = os.path.join(dest_dir, 'tools', 'python.exe')
    if not os.path.isfile(exe):
        nupkg = os.path.join(wintoolchain.cache_dir(), 'python-nuget.nupkg')
        if not os.path.isfile(nupkg):
            wintoolchain._download(_NUPKG_URL, nupkg, _NUPKG_SHA256)
        os.makedirs(dest_dir, exist_ok=True)
        _sh(f'unzip -q -o {nupkg} -d {dest_dir} "tools/*"', log_fh)
    if not os.path.isfile(exe):
        raise RuntimeError(f'nuget python missing after extract: {exe}')
    return _WINPY_Z


def _tofu_search_source(workdir: str) -> str:
    """Sibling checkout → vendor wheel → index name (builder.py's order).

    The sibling checkout lives OUTSIDE the guest rootfs, so it is copied
    into the workdir (wine can only reach Z:).
    """
    import glob
    sibling = os.path.join(_REPO_ROOT, '..', 'tofu-search')
    if os.path.isfile(os.path.join(sibling, 'pyproject.toml')):
        dest = os.path.join(workdir, 'tofu-search')
        if not os.path.isfile(os.path.join(dest, 'pyproject.toml')):
            shutil.copytree(os.path.abspath(sibling), dest)
        return dest
    wheels = sorted(glob.glob(
        os.path.join(_REPO_ROOT, 'vendor', 'tofu_search-*.whl')))
    if wheels:
        dest = os.path.join(workdir, os.path.basename(wheels[-1]))
        shutil.copy2(wheels[-1], dest)
        return dest
    return 'tofu-search'


def _read_version() -> str:
    with open(os.path.join(_REPO_ROOT, 'VERSION'), encoding='utf-8') as f:
        v = f.read().strip()
    if not v:
        raise RuntimeError('VERSION file is empty')
    return v


def _git(*args) -> str:
    out = subprocess.run(['git', '-C', _REPO_ROOT, *args],
                         capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f'git {args[0]} failed: {out.stderr[:200]!r}')
    return out.stdout.decode('utf-8', 'replace')


def _sh(cmd: str, log_fh, *, shell: bool = False,
        timeout: int = _BUILD_TIMEOUT_S):
    """A NATIVE pipeline step (git/tar/unzip) with output in the log."""
    import shlex
    logger.info('[WinBuild] native step: %.200s', cmd)
    out = subprocess.run(cmd if shell else shlex.split(cmd),
                         shell=shell, capture_output=True, timeout=timeout)
    for stream, tag in ((out.stdout, 'out'), (out.stderr, 'err')):
        if stream:
            log_fh.write(f'── {tag} ({cmd[:60]}) ──\n')
            log_fh.write(stream.decode('utf-8', 'replace') + '\n')
    log_fh.flush()
    if out.returncode != 0:
        raise RuntimeError(f'native step failed (rc={out.returncode}): '
                           f'{cmd[:120]}')
    return out


# ── Half B: the per-client wrapper (NSIS, native) ─────────────────────

_NSI_TEMPLATE = os.path.join(_REPO_ROOT, 'desktop', 'installer.nsi.tmpl')
_NSI_PLACEHOLDERS = ('@APP_VERSION@', '@PAYLOAD_DIR@', '@OUT_FILE@',
                     '@ASSET_DIR@')


def _ensure_makensis(log_fh) -> str:
    """Locate makensis: env override, then the provisioned tools prefix.

    Provisioned ONCE from conda-forge (native binary — no wine, no
    display). conda/mamba comes from the host env; the tools prefix lives
    in the download cache so it survives /tmp wipes.
    """
    override = os.environ.get('TOFU_MAKENSIS', '').strip()
    if override:
        return override
    exe = os.path.join(wintoolchain.cache_dir(), 'tools', 'bin', 'makensis')
    if os.path.isfile(exe):
        return exe
    conda = shutil.which('mamba') or shutil.which('conda')
    if not conda:
        raise RuntimeError('makensis not provisioned and no conda/mamba '
                           'to install nsis with')
    prefix = os.path.join(wintoolchain.cache_dir(), 'tools')
    # `create`, not `install`: mamba refuses install into a prefix that is
    # not yet a conda env ("Environment must first be created", measured).
    _sh(f'{conda} create -y -p {prefix} -c conda-forge nsis', log_fh,
        timeout=1800)
    if not os.path.isfile(exe):
        raise RuntimeError(f'makensis missing after provision: {exe}')
    return exe


def _render_nsi(version: str, payload_dir: str, out_file: str) -> str:
    """Render the NSIS template; every placeholder MUST be substituted."""
    with open(_NSI_TEMPLATE, encoding='utf-8') as f:
        text = f.read()
    asset_dir = os.path.join(_REPO_ROOT, 'static', 'icons')
    text = (text.replace('@APP_VERSION@', version)
                .replace('@PAYLOAD_DIR@', payload_dir)
                .replace('@OUT_FILE@', out_file)
                .replace('@ASSET_DIR@', asset_dir))
    missing = [p for p in _NSI_PLACEHOLDERS if p in text]
    if missing:
        raise RuntimeError(f'NSI placeholders left unrendered: {missing}')
    return text


def _write_preseed(payload_dir: str, server_url: str) -> str | None:
    """preseed_server.json next to Tofu.exe — the launcher's first-run
    import contract (desktop/launcher.py). Non-secret: the URL only."""
    if not server_url:
        return None
    import json
    dest = os.path.join(payload_dir, 'preseed_server.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump({'v': 1, 'url': server_url.rstrip('/')}, f)
    logger.info('[WinBuild] preseeded server url into payload')
    return dest


def wrap_payload(payload_tar: str, version: str, sha: str, log_fh, *,
                 server_url: str = '', workdir: str | None = None) -> str:
    """Half B: payload tarball → Tofu-Setup-<ver>-win64.exe in the store.

    Native steps only (untar / makensis) — the whole point of NSIS is
    that this half needs no wine. Recorded as source='built' so the
    selector prefers it over the mirrored release (built wins ties, and
    our version is newer anyway).
    """
    workdir = workdir or os.path.join(
        wintoolchain.rootfs_dir(), 'work', f'wrap-{int(time.time())}')
    payload_dir = os.path.join(workdir, 'payload')
    os.makedirs(payload_dir, exist_ok=True)
    _sh(f'tar xzf {payload_tar} -C {payload_dir} --strip-components=1',
        log_fh)
    if not os.path.isfile(os.path.join(payload_dir, 'Tofu.exe')):
        raise RuntimeError(f'payload has no Tofu.exe: {payload_tar}')
    _write_preseed(payload_dir, server_url)

    name = f'Tofu-Setup-{version}-win64.exe'
    out_file = os.path.join(workdir, name)
    nsi = _render_nsi(version, payload_dir, out_file)
    script = os.path.join(workdir, 'installer.nsi')
    with open(script, 'w', encoding='utf-8') as f:
        f.write(nsi)
    makensis = _ensure_makensis(log_fh)
    _sh(f'{makensis} -V2 {script}', log_fh, timeout=3600)
    if not os.path.isfile(out_file):
        raise RuntimeError(f'makensis produced no {name}')

    dest = os.path.join(store._store_dir(), name)
    shutil.copy2(out_file, dest)
    size = os.path.getsize(dest)
    h = hashlib.sha256()
    with open(dest, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    store.record_artifact({
        'os': 'windows', 'arch': 'x86_64', 'label': 'Windows installer',
        'filename': name, 'size': size, 'sha256': h.hexdigest(),
        'source': 'built', 'version': version,
        'fetched_at': time.time(), 'git_sha': sha,
        **({'preseed': {'url': server_url}} if server_url else {}),
    })
    logger.info('[WinBuild] installer built: %s (%d bytes)', name, size)
    return dest


def build_installer(reason: str = 'manual', server_url: str = '') -> dict:
    """Full S2+S3 orchestration: payload (cached when possible) → wrapper.

    The wrapper re-runs even when the payload is cached — it is the cheap
    half, and the preseed may differ per client.
    """
    _run(reason)
    st = state()
    if st.get('state') != 'ok':
        return st
    version = st['version']
    sha = st['git_sha']
    log_path = os.path.join(_payloads_dir(), 'winbuild.log')
    with open(log_path, 'a', encoding='utf-8') as log_fh:
        dest = wrap_payload(st['payload'], version, sha, log_fh,
                            server_url=server_url)
    _set_state(installer=dest, wrapped_at=time.time())
    return state()


__all__ = ['start', 'state', 'is_running', 'cached_payload',
           'payload_path', 'deps_stamp', 'build_installer',
           'wrap_payload']
