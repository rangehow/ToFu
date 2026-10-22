"""Internal / personal-mode export helpers (split out of ``export.py``)."""
import json
import re
import shutil
from pathlib import Path

from export_pkg import _state
from export_pkg._excludes import _is_text_file
from export_pkg._lists import (
    C_DIM as C_DIM,
    C_END as C_END,
    C_GREEN as C_GREEN,
    C_RED as C_RED,
    C_YELLOW as C_YELLOW,
    _INSTALL_SH_PROXY_BEGIN as _INSTALL_SH_PROXY_BEGIN,
    _INSTALL_SH_PROXY_END as _INSTALL_SH_PROXY_END,
    _INTERNAL_CONFIG_KEYS as _INTERNAL_CONFIG_KEYS,
    _INTERNAL_EXCLUDED_API_KEYS as _INTERNAL_EXCLUDED_API_KEYS,
    is_agent_artifact as is_agent_artifact,
    ROOT as ROOT,
    logger as logger,
)
from export_pkg._sanitize import (
    _sanitize_defaults_for_export,
    _sanitize_source_opensource,
)
def _copy_file(src: Path, dst: Path, mode: str, relpath: str) -> str:
    """Copy a single file, applying sanitization if needed.

    Returns a status string: 'copied', 'sanitized', or 'skipped:reason'.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    if mode in ('internal', 'opensource') and _is_text_file(str(src)):
        try:
            content = src.read_text(encoding='utf-8', errors='replace')
            # Apply default-settings sanitization (internal & opensource)
            sanitized = _sanitize_defaults_for_export(content, relpath, version=_state._EXPORT_VERSION)
            # Apply full secret/endpoint sanitization (opensource only)
            if mode == 'opensource':
                sanitized = _sanitize_source_opensource(sanitized, relpath)
            dst.write_text(sanitized, encoding='utf-8')
            if sanitized != content:
                return 'sanitized'
            return 'copied'
        except Exception as e:
            logger.warning('Failed to sanitize %s: %s — copying as-is', relpath, e)
            shutil.copy2(src, dst)
            return 'copied (sanitize failed)'
    else:
        shutil.copy2(src, dst)
        return 'copied'



# ═══════════════════════════════════════════════════════════
#  Internal mode: seed shared config (providers, proxy, models)
# ═══════════════════════════════════════════════════════════
def _seed_internal_config(dest: Path):
    """Seed server_config.json for internal exports.

    Copies shared settings (providers with API keys, proxy, models) from the
    source project so colleagues can run the app immediately.  Personal data
    (feishu config, learned model limits) is stripped.

    If the destination already has a server_config.json, it is NOT overwritten —
    the user may have customized it.
    """
    dest_config = dest / 'data' / 'config' / 'server_config.json'
    # Always re-seed: the destination's server_config.json is a derived artifact
    # of the source, not a user-edited file. Keeping a stale copy across
    # re-exports means new models / providers in source never reach the
    # exported copy (e.g. adding a model to the Meituan provider would be
    # silently dropped on re-export). Overwrite every time to guarantee the
    # exported config reflects current source state.
    if dest_config.exists():
        logger.info('Overwriting existing server_config.json to reflect current source state')

    src_config = ROOT / 'data' / 'config' / 'server_config.json'
    if not src_config.exists():
        logger.warning('Source server_config.json not found — skipping internal config seed')
        return

    try:
        with open(src_config, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('Failed to read source server_config.json: %s', e)
        return

    # Extract only the shared keys
    seeded = {}
    for key in _INTERNAL_CONFIG_KEYS:
        if key in cfg:
            seeded[key] = cfg[key]

    # No-egress target hosts need the corp proxy even when the exporter's
    # own config doesn't use one (the exporting shell has it in env instead).
    seeded = _with_internal_proxy_default(seeded, 'internal')

    if not seeded:
        logger.warning('No shared config keys found in source — skipping seed')
        return

    # ── Filter providers: only keep Meituan (sankuai) for internal exports ──
    # All api_keys on the Meituan provider are shared with colleagues as-is.
    if 'providers' in seeded:
        providers = [
            p for p in seeded['providers']
            if p.get('id') == 'sankuai'
        ]
        for provider in providers:
            keys = provider.get('api_keys')
            if keys:
                provider['api_keys'] = [
                    k for k in keys if k not in _INTERNAL_EXCLUDED_API_KEYS
                ]
        seeded['providers'] = providers

    # Write the seeded config
    dest_config.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_config, 'w', encoding='utf-8') as f:
        json.dump(seeded, f, indent=2, ensure_ascii=False)
        f.write('\n')

    provider_names = [p.get('name', '?') for p in seeded.get('providers', [])]
    logger.info('Seeded internal server_config.json with %d keys, providers: %s',
                len(seeded), provider_names)
    print(f"  {C_GREEN}🔑 Seeded server_config.json with {len(seeded)} shared settings{C_END}")
    print(f"     {C_DIM}Providers: {', '.join(provider_names)}{C_END}")


# Default corp egress proxy for INTERNAL exports. GPU/codelab hosts often
# have no external DNS at all (sh02, 2026-09-10: conda.anaconda.org /
# astral.sh / pypi.org all unresolvable) — without a baked proxy the
# install dies at the first conda solve. Bare IP:port, no credentials;
# export_pkg is sanctioned tracked-but-never-published
# (tests/export/test_export_pkg_publish_exclusion.py), so this never leaks to the
# public repo. Verified reachable and allowing conda/pypi/astral (HTTP 200)
# from the corp network.
_INTERNAL_CORP_PROXY = 'http://10.213.87.132:8080'
_INTERNAL_CORP_PROXY_BYPASS = [
    '.sankuai.com', '.meituan.com', '.neixin.cn', '.sankuai.info',
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
]


def _with_internal_proxy_default(cfg: dict, mode: str) -> dict:
    """Fill in the corp egress proxy for INTERNAL exports when the source
    config carries none. An explicit source proxy always wins; personal
    exports stay verbatim (the user's host may be off the corp network)."""
    if mode != 'internal':
        return cfg
    proxy_cfg = cfg.get('proxy_config') or {}
    if proxy_cfg.get('http_proxy') or proxy_cfg.get('https_proxy'):
        return cfg
    cfg = dict(cfg)
    cfg['proxy_config'] = {**proxy_cfg,
                           'http_proxy': _INTERNAL_CORP_PROXY,
                           'https_proxy': _INTERNAL_CORP_PROXY}
    if not cfg.get('proxy_bypass_domains'):
        cfg['proxy_bypass_domains'] = list(_INTERNAL_CORP_PROXY_BYPASS)
    return cfg

def _proxy_env_lines_from_config(cfg: dict):
    """Build corp proxy/mirror ``export VAR="..."`` lines from server_config.

    Shared by ``_patch_install_sh_proxy`` (install.sh injection) and
    ``export_pkg._handoff`` (bootstrap.sh generation).  Returns
    ``(env_lines, proxy_label, no_proxy_value)``; ``env_lines`` is ``[]``
    when the config carries no proxy, the other two then being ``('', '')``.
    """
    proxy_cfg = cfg.get('proxy_config') or {}
    http_proxy = proxy_cfg.get('http_proxy') or ''
    https_proxy = proxy_cfg.get('https_proxy') or http_proxy
    if not http_proxy and not https_proxy:
        return [], '', ''

    # Bypass list → comma-separated no_proxy. Always include localhost.
    bypass = list(cfg.get('proxy_bypass_domains') or [])
    no_proxy_parts = ['localhost', '127.0.0.1', '::1'] + bypass
    no_proxy_value = ','.join(dict.fromkeys(no_proxy_parts))  # de-dupe, keep order

    lines = []
    if http_proxy:
        lines.append(f'export http_proxy="{http_proxy}"')
        lines.append(f'export HTTP_PROXY="{http_proxy}"')
    if https_proxy:
        lines.append(f'export https_proxy="{https_proxy}"')
        lines.append(f'export HTTPS_PROXY="{https_proxy}"')
    if no_proxy_value:
        lines.append(f'export no_proxy="{no_proxy_value}"')
        lines.append(f'export NO_PROXY="{no_proxy_value}"')
    # Conda-forge mirror redirect: many corp proxies 403 conda.anaconda.org.
    # If `conda_mirror` is set in server_config.json, export it so install.sh
    # writes a sibling .condarc that redirects conda-forge to the mirror.
    # Defaults to the Sankuai internal mirror for any host inside the corp
    # network — safe because it's only consulted when CONDA_OWNED_BY_US=1.
    conda_mirror = cfg.get('conda_mirror') or 'https://mirrors.sankuai.com/conda/cloud'
    if conda_mirror:
        lines.append(f'export TOFU_CONDA_MIRROR="{conda_mirror}"')
    # PyPI mirror redirect — same story as conda-forge; proxy 403s pypi.org.
    pypi_index = cfg.get('pypi_index') or 'https://mirrors.sankuai.com/pypi/web/simple/'
    if pypi_index:
        lines.append(f'export TOFU_PYPI_INDEX="{pypi_index}"')
    # npm registry redirect — same story again; proxy blocks
    # registry.npmjs.org, and npm's install of the OPTIONAL vite/typecheck
    # devDependencies would otherwise hang for minutes. Only emitted when a
    # mirror is configured; without it install.sh still fail-fasts + falls
    # back to the Python minifier.
    npm_registry = cfg.get('npm_registry') or ''
    if npm_registry:
        lines.append(f'export TOFU_NPM_REGISTRY="{npm_registry}"')
    return lines, http_proxy or https_proxy, no_proxy_value

# ═══════════════════════════════════════════════════════════
#  Internal / Personal mode: bake the corp proxy into install.sh
# ═══════════════════════════════════════════════════════════
#
# install.sh runs BEFORE server.py ever boots, so it cannot read
# ``data/config/server_config.json``.  Yet its first acts (``git clone``,
# ``conda install``, ``pip install``, ``playwright install``) all need
# outbound HTTPS — which on the corp network requires the internal proxy.
#
# For ``personal`` and ``internal`` exports we therefore patch the
# destination's install.sh in place: a small ``export http_proxy=… ;
# export https_proxy=… ; export no_proxy=…`` block is injected right
# after ``set -euo pipefail``.  The block is wrapped in BEGIN/END markers
# so re-exports replace the previous block instead of accumulating dupes.
#
def _patch_install_sh_proxy(dest: Path, mode: str):
    """Bake corp proxy env-vars into ``install.sh`` for personal/internal exports.

    Reads ``proxy_config`` and ``proxy_bypass_domains`` from the source
    ``server_config.json`` and prepends a small ``export http_proxy=…``
    block to the destination's ``install.sh``.  Idempotent — re-runs strip
    the prior block before injecting the fresh one.
    """
    if mode not in ('personal', 'internal'):
        return

    install_sh = dest / 'install.sh'
    if not install_sh.exists():
        logger.debug('No install.sh in dest — skipping proxy injection')
        return

    src_config = ROOT / 'data' / 'config' / 'server_config.json'
    if not src_config.exists():
        logger.warning('Source server_config.json missing — cannot bake proxy into install.sh')
        return

    try:
        with open(src_config, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('Failed to read source server_config.json for proxy injection: %s', e)
        return

    cfg = _with_internal_proxy_default(cfg, mode)
    env_lines, proxy_label, no_proxy_value = _proxy_env_lines_from_config(cfg)
    if not env_lines:
        logger.info('No proxy_config in source — skipping install.sh proxy injection')
        return

    block_lines = [
        _INSTALL_SH_PROXY_BEGIN,
        '# Auto-injected by export.py (mode=' + mode + ').  Required by',
        '# git clone / conda / pip / playwright on the corp network.',
        '# Remove or comment out if you do NOT need the internal proxy.',
    ] + env_lines + [_INSTALL_SH_PROXY_END]
    block = '\n'.join(block_lines) + '\n'

    try:
        text = install_sh.read_text(encoding='utf-8')
    except OSError as e:
        logger.warning('Failed to read dest install.sh: %s', e)
        return

    # Strip any prior injected block (idempotent).
    if _INSTALL_SH_PROXY_BEGIN in text and _INSTALL_SH_PROXY_END in text:
        start = text.index(_INSTALL_SH_PROXY_BEGIN)
        end = text.index(_INSTALL_SH_PROXY_END) + len(_INSTALL_SH_PROXY_END)
        # Also swallow the trailing newline so we don't pile up blanks.
        if end < len(text) and text[end] == '\n':
            end += 1
        text = text[:start] + text[end:]

    # Anchor: insert right after `set -euo pipefail` so the proxy is in
    # effect for every subsequent step.  Fallback: prepend after shebang.
    anchor = 'set -euo pipefail\n'
    if anchor in text:
        idx = text.index(anchor) + len(anchor)
        new_text = text[:idx] + '\n' + block + text[idx:]
    else:
        # Insert after the first line (shebang) if anchor is missing.
        first_nl = text.find('\n')
        if first_nl < 0:
            new_text = block + text
        else:
            new_text = text[:first_nl + 1] + block + text[first_nl + 1:]

    try:
        install_sh.write_text(new_text, encoding='utf-8')
        # Preserve executable bit (write_text loses no perms, but be explicit).
        install_sh.chmod(install_sh.stat().st_mode | 0o111)
    except OSError as e:
        logger.warning('Failed to write patched install.sh: %s', e)
        return

    logger.info('Patched install.sh with proxy %s no_proxy=%s',
                proxy_label or '(none)', no_proxy_value or '(none)')
    print(f"  {C_GREEN}🌐 Baked corp proxy into install.sh{C_END}")
    print(f"     {C_DIM}{proxy_label}  bypass: {no_proxy_value}{C_END}")


# ═══════════════════════════════════════════════════════════
#  Post-export setup: create empty required dirs & templates
# ═══════════════════════════════════════════════════════════

def _bundle_internal_mcp_repos(dest: Path, mode: str):
    """Copy sibling ``hope-mcp`` / ``xuecheng-mcp`` repos into ``<dest>/vendor/``.

    These are private Meituan-internal MCP servers not published to PyPI,
    so a fresh install otherwise has no ``hope-mcp`` / ``xuecheng-mcp``
    on PATH and the MCP tab's "Install" button fails with
    ``launcher '...-mcp' is not on PATH``.

    Only bundled in ``personal`` and ``internal`` modes — opensource
    exports never ship internal repos.

    Skips cleanly if a sibling repo isn't found (developer ran export
    from a dir that doesn't have both siblings checked out).
    """
    if mode not in ('personal', 'internal'):
        return

    import shutil

    vendor_dir = dest / 'vendor'
    vendor_dir.mkdir(parents=True, exist_ok=True)

    # Preserve a .gitignore'd marker so the rest of the codebase can opt
    # to ignore vendor/ if desired (it's intentionally shipped, not built).
    (vendor_dir / '.gitkeep').touch()

    bundled = []
    for sibling_name in ('hope-mcp', 'xuecheng-mcp', 'llm-mcp'):
        src = ROOT.parent / sibling_name
        if not src.exists() or not (src / 'pyproject.toml').exists():
            logger.info('Sibling MCP repo not found — skipping: %s', src)
            continue

        target = vendor_dir / sibling_name
        if target.exists():
            # Clean re-copy so stale files don't linger across re-exports.
            shutil.rmtree(target, ignore_errors=True)

        # Copy sources only — skip build artifacts / caches / VCS.
        def _ignore(_root, names):
            drop = set()
            for n in names:
                if n in {'__pycache__', '.git', '.venv', 'venv', 'dist',
                          'build', '.pytest_cache', '.mypy_cache',
                          '.tox', 'node_modules', '.eggs',
                          '.chatui'}:
                    drop.add(n)
                elif is_agent_artifact(n):  # .tofu/.tofu_trash/… — per-host agent state
                    drop.add(n)
                elif n.endswith(('.pyc', '.pyo', '.egg-info')):
                    drop.add(n)
            return drop

        try:
            shutil.copytree(src, target, ignore=_ignore, symlinks=False)
            bundled.append(sibling_name)
            logger.info('Bundled MCP repo: %s → %s', src, target)
        except OSError as e:
            logger.warning('Failed to bundle %s: %s', sibling_name, e)

    if bundled:
        print(f"  {C_GREEN}📦 Bundled internal MCP repos → vendor/{C_END}")
        print(f"     {C_DIM}{', '.join(bundled)}{C_END}")


def _bundle_tofu_search_wheel(dest: Path, mode: str):
    """Copy the prebuilt ``tofu-search`` wheel into ``<dest>/vendor/``.

    Bundled for EVERY mode, opensource included. The previous opensource
    skip rested on "a vanilla host reaches public PyPI fine" — measured
    false since the floor moved to 0.5.3: public PyPI tops at 0.5.1, so an
    opensource export's ``pip install -r requirements.txt`` (and the CI
    legs' identical step) could not resolve at all. The wheel is this
    project's own MIT artifact, so shipping it is license-clean; once a
    floor-satisfying release IS published the bundle is simply redundant.
    ``tests/release/test_requirements_public_resolvable.py`` is the guard that
    keeps the publish honest in the meantime.

    The wheel is sourced from the canonical monorepo member's ``dist/``
    directory (``<ROOT>/packages/tofu-search/dist/tofu_search-*.whl``).
    Refuses publication if the required wheel is missing or stale.
    """

    import shutil

    src_repo = ROOT / 'packages' / 'tofu-search'
    dist_dir = src_repo / 'dist'
    if not dist_dir.is_dir():
        raise RuntimeError('Required tofu-search wheel missing; build packages/tofu-search before export')

    wheels = sorted(dist_dir.glob('tofu_search-*.whl'),
                    key=lambda path: tuple(int(part) for part in path.name.split('-')[1].split('.')))
    if not wheels:
        raise RuntimeError('Required tofu-search wheel missing; build packages/tofu-search before export')

    # Highest version wins (sorted lexically is good enough for 0.x; the
    # install.sh side also `sort -V | tail -1` if several land in vendor/).
    wheel = wheels[-1]

    # ── Staleness guard ──────────────────────────────────────────────────
    # The wheel in dist/ is whatever was LAST built; if someone bumped the
    # tofu-search source version (pyproject.toml) or raised the floor in our
    # requirements.txt but forgot to `python -m build`, dist/ still holds an
    # OLD wheel. Bundling it silently ships a build below our own floor →
    # server boots and may crash on a symbol that only exists in the newer
    # release (this exact trap shipped a 0.3.0 wheel against a >=0.3.2 floor /
    # 0.3.2 source). Don't fail the export (the wheel may still be usable),
    # but make the drift IMPOSSIBLE to miss so the operator rebuilds dist/.
    def _wheel_ver(name: str) -> tuple:
        m = re.search(r'tofu_search-([0-9]+(?:\.[0-9]+)*)', name)
        parts = (m.group(1).split('.') if m else ['0']) + ['0', '0', '0']
        return tuple(int(''.join(c for c in p if c.isdigit()) or 0) for p in parts[:3])

    def _ver_tuple(s: str) -> tuple:
        parts = (str(s).split('+')[0].split('.') if s else ['0']) + ['0', '0', '0']
        return tuple(int(''.join(c for c in p if c.isdigit()) or 0) for p in parts[:3])

    wheel_v = _wheel_ver(wheel.name)
    expected = []  # (label, version_tuple, raw)
    try:
        pyproj = (src_repo / 'pyproject.toml').read_text(encoding='utf-8', errors='ignore')
        m = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', pyproj)
        if m:
            expected.append(('tofu-search source pyproject.toml', _ver_tuple(m.group(1)), m.group(1)))
    except OSError as e:
        logger.debug('Could not read tofu-search pyproject.toml for staleness check: %s', e)
    try:
        reqs = (ROOT / 'requirements.txt').read_text(encoding='utf-8', errors='ignore')
        m = re.search(r'(?mi)^\s*tofu-search\s*>=\s*([0-9][0-9.]*)', reqs)
        if m:
            expected.append(("Tofu requirements.txt floor (>=)", _ver_tuple(m.group(1)), m.group(1)))
    except OSError as e:
        logger.debug('Could not read requirements.txt for staleness check: %s', e)

    for label, ev, raw in expected:
        if wheel_v < ev:
            msg = (f"bundled tofu-search wheel {wheel.name} is STALE: "
                   f"{'.'.join(map(str, wheel_v))} < {raw} ({label})")
            raise RuntimeError(msg)

    vendor_dir = dest / 'vendor'
    vendor_dir.mkdir(parents=True, exist_ok=True)
    (vendor_dir / '.gitkeep').touch()

    # Drop any stale tofu_search wheels from a prior export so install.sh's
    # glob doesn't pick an older version.
    for old in vendor_dir.glob('tofu_search-*.whl'):
        try:
            old.unlink()
        except OSError as e:
            logger.debug('Could not remove stale wheel %s: %s', old, e)

    target = vendor_dir / wheel.name
    try:
        shutil.copy2(wheel, target)
        import hashlib
        import json
        from bootstrap_pkg.install_artifacts import wheel_version
        version = wheel_version(target)
        release_manifest = {
            'schema': 1,
            'artifacts': {'tofu_search': {
                'version': version, 'filename': target.name,
                'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            }},
        }
        if (dest / 'VERSION').is_file():
            release_manifest['tofu_version'] = (dest / 'VERSION').read_text().strip()
        (vendor_dir / 'install-manifest.json').write_text(
            json.dumps(release_manifest, indent=2) + '\n')

        logger.info('Bundled tofu-search wheel: %s → %s', wheel, target)
        print(f"  {C_GREEN}📦 Bundled tofu-search wheel → vendor/{C_END}")
        print(f"     {C_DIM}{wheel.name}{C_END}")
    except OSError as e:
        raise RuntimeError('Required tofu-search wheel could not be bundled') from e


# MCP entries that resolve on ANY host: bare launchers (served from the
# bundled vendor/ repos) and public npx packages. Entries pinned to a source
# host — venv sys.path wrappers pulling vault tokens (github), personal
# session env (overleaf OVERLEAF_SESSION) — are dropped: they would be broken
# on the destination and would carry one operator's secrets into the export.
_PORTABLE_MCP_COMMANDS = frozenset({'hope-mcp', 'xuecheng-mcp', 'llm-mcp', 'npx'})


def _stage_portable_mcp_config(target: Path) -> None:
    """Write a host-portable subset of ``mcp_servers.json`` under ``target``.

    ``target`` is either an export destination (internal tree export) or a
    handoff staging root; both want ``<target>/data/config/mcp_servers.json``.

    The bulk copy strips ``mcp_servers.json`` (it holds personal tokens), so
    without this an internal destination boots with NO MCP registrations even
    though ``vendor/`` carries the server code. Only enabled entries that
    launch via bare commands or public npx packages survive.
    """
    src = ROOT / 'data' / 'config' / 'mcp_servers.json'
    try:
        config = json.loads(src.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(config, dict):
        return
    portable = {
        name: entry for name, entry in config.items()
        if isinstance(entry, dict)
        and entry.get('enabled')
        and entry.get('command') in _PORTABLE_MCP_COMMANDS
    }
    if not portable:
        return
    cfg_dir = target / 'data' / 'config'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.joinpath('mcp_servers.json').write_text(
        json.dumps(portable, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"  {C_GREEN}🔌 Carried portable MCP registrations → data/config/{C_END}")
    print(f"     {C_DIM}{', '.join(sorted(portable))}{C_END}")


# Skill packages are USER STATE, not product content: the bulk copy strips
# every ``.tofu*`` artifact and the whole root ``data/`` tree (chat DB, pgdata,
# configs), so an internal export would otherwise arrive with no installed
# skills. These roots are restored verbatim by ``_copy_installed_skills`` —
# they are the exact layout ``lib/skills/paths.py`` scans on the destination.
# Opensource never carries them: a public tree must not inherit one operator's
# catalogue (locked by tests/skills/test_skill_roots_contract.py).
_SKILL_STATE_ROOTS = (
    ('.tofu/skills', '.tofu/skills'),    # project-runtime installs (mtskills i, Settings UI)
    ('data/skills', 'data/skills'),      # owner-global + legacy global store
)


def _carried_skill_state_roots(mode: str) -> tuple[str, ...]:
    """Source-relative skill roots this export mode carries (empty = none)."""
    if mode != 'internal':
        return ()
    return tuple(src for src, _dst in _SKILL_STATE_ROOTS)


def _count_skill_packages(root: Path) -> int:
    """Installed packages under one root (any depth: ``<root>/<id>/SKILL.md``
    for the project store, ``<root>/users/<owner>/<id>/SKILL.md`` for global)."""
    return sum(1 for _ in root.rglob('SKILL.md'))


def _copy_installed_skills(dest: Path, mode: str) -> None:
    """Restore installed skill packages into an internal export destination.

    Runs after the skeleton created ``dest/data``. Directory names are kept
    verbatim (``skills/users/<owner>``) so the destination's skill scanner
    finds the same ids at the same precedence with no extra configuration.
    A copy failure raises: silently shipping a tree with no skills is the
    exact gap this pass exists to close.
    """
    roots = _carried_skill_state_roots(mode)
    if not roots:
        return

    def _ignored(name: str) -> bool:
        return (name in {'__pycache__', '.git', '.venv', 'venv', '.pytest_cache',
                         '.mypy_cache', '.ruff_cache', 'node_modules',
                         '.install.lock'}
                or name.endswith(('.pyc', '.pyo')))

    def _ignore(_root, names):
        return {n for n in names if _ignored(n)}

    def _reject_link(path: Path) -> None:
        if path.is_symlink():
            raise RuntimeError(f'Installed skill export refuses symlink: {path}')

    def _check_components(path: Path, boundary: Path) -> None:
        """Check lexical ancestors too; a safe leaf may sit below a link."""
        _reject_link(boundary)
        current = boundary
        for component in path.relative_to(boundary).parts:
            current = current / component
            _reject_link(current)

    def _preflight(src: Path, dst: Path) -> None:
        """Validate the full carried tree before replacing any live package."""
        _check_components(src, ROOT)
        _check_components(dst, dest)
        for child in sorted(src.iterdir(), key=lambda path: path.name):
            if _ignored(child.name):
                continue
            _reject_link(child)
            _check_components(dst / child.name, dest)
            if child.is_dir():
                _preflight(child, dst / child.name)

    # Link rejection matches package installation: preserving a link would
    # create unusable/escaping packages; dereferencing it leaks unrelated data.
    # Validate every root before changing any destination package.
    for src_rel, dst_rel in _SKILL_STATE_ROOTS:
        src = ROOT / src_rel
        _check_components(src, ROOT)
        if src.is_dir():
            _preflight(src, dest / dst_rel)

    def _merge(src: Path, dst: Path) -> None:
        """Copy ``src`` over ``dst`` one PACKAGE directory at a time.

        A package directory (one holding ``SKILL.md``) is replaced whole, so a
        changed skill propagates instead of leaving stale files behind. Every
        other directory is descended into, never wiped: the destination may be
        a live install whose own packages are that user's state and must
        survive a re-export intact.
        """
        _check_components(src, ROOT)
        _check_components(dst, dest)
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir(), key=lambda p: p.name):
            if _ignored(child.name):
                continue
            dst_child = dst / child.name
            _reject_link(child)
            _check_components(dst_child, dest)
            if child.is_dir() and not child.is_symlink():
                if (child / 'SKILL.md').is_file():
                    if dst_child.exists():
                        shutil.rmtree(dst_child, ignore_errors=True)
                    shutil.copytree(child, dst_child, ignore=_safe_copy_ignore,
                                    copy_function=_safe_copy_file, symlinks=False)
                else:
                    _merge(child, dst_child)
            else:
                _safe_copy_file(child, dst_child)

    def _safe_copy_ignore(directory, names):
        # Recheck when copytree visits each directory, not only at preflight.
        _check_components(Path(directory), ROOT)
        for name in names:
            if not _ignored(name):
                _reject_link(Path(directory) / name)
        return _ignore(directory, names)

    def _safe_copy_file(src, dst):
        _check_components(Path(src), ROOT)
        _check_components(Path(dst), dest)
        return shutil.copy2(src, dst, follow_symlinks=False)

    carried = []
    for src_rel, dst_rel in _SKILL_STATE_ROOTS:
        src = ROOT / src_rel
        if not src.is_dir():
            continue
        target = dest / dst_rel
        try:
            _merge(src, target)
        except OSError as e:
            raise RuntimeError(
                f'Installed skills could not be carried into the export '
                f'({src_rel}): {e}') from e
        carried.append(f'{dst_rel} ({_count_skill_packages(target)} package(s))')
        logger.info('Carried installed skills: %s → %s', src, target)

    if carried:
        print(f"  {C_GREEN}🧠 Carried installed skills → destination{C_END}")
        print(f"     {C_DIM}{', '.join(carried)}{C_END}")

def _portablize_bundled_mcp_config(dest: Path, mode: str):
    """Rewrite hope/xuecheng entries in the exported ``mcp_servers.json``.

    Personal mode copies the live ``data/config/mcp_servers.json`` verbatim,
    and on the source machine those two servers are pinned to absolute
    ``uvx --from /mnt/.../hope-mcp`` paths.  Those paths don't exist on a
    different destination machine, so the launch fails there even though
    ``install.sh`` pip-installs the bundled ``vendor/`` repos and puts bare
    ``hope-mcp`` / ``xuecheng-mcp`` launchers on PATH.

    Rewrite the entries to the bare command (matching the registry default
    and the vendor install) so the export is portable to any machine. The
    ``env`` block (credentials) is preserved untouched.

    Only runs for ``personal`` and ``internal`` modes — opensource never
    bundles these repos.
    """
    if mode not in ('personal', 'internal'):
        return

    cfg_path = dest / 'data' / 'config' / 'mcp_servers.json'
    if not cfg_path.exists():
        return

    try:
        config = json.loads(cfg_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('Could not read %s for MCP portablization: %s', cfg_path, e)
        return

    if not isinstance(config, dict):
        return

    rewritten = []
    for name in ('hope', 'xuecheng', 'llm'):
        entry = config.get(name)
        if not isinstance(entry, dict):
            continue
        bare_cmd = f'{name}-mcp'
        if entry.get('command') == bare_cmd and not entry.get('args'):
            continue
        entry['command'] = bare_cmd
        entry['args'] = []
        rewritten.append(name)

    if not rewritten:
        return

    try:
        cfg_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding='utf-8')
    except OSError as e:
        logger.warning('Could not write portablized MCP config %s: %s', cfg_path, e)
        return

    logger.info('Portablized bundled MCP config entries: %s', ', '.join(rewritten))
    print(f"  {C_GREEN}🔧 Made bundled MCP config portable → bare launchers{C_END}")
    print(f"     {C_DIM}{', '.join(rewritten)}{C_END}")


def _purge_runtime_artifacts(dest: Path):
    """Delete transient runtime artifacts that may exist in the dest dir.

    If someone ran ``python server.py`` inside a previous export, log files,
    lockfiles, and runtime stats will have been created.  These are
    regenerable noise and safe to remove on every re-export.

    **Chat history is PRESERVED**:
      - ``data/chatui.db`` (SQLite) and its side files
      - ``data/pgdata/`` (Postgres cluster)
      - ``data/config/server_config.json`` and other user configs
    These belong to whoever is running the destination copy and must NOT
    be destroyed by a re-export.  We only wipe things that are either
    pure noise (logs, lockfiles) or tied to the source machine's identity
    (API-key usage stats).
    """
    def _rm_tree(p: Path):
        if not p.exists():
            return
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            else:
                import shutil
                shutil.rmtree(p)
        except OSError as e:
            logger.warning('Could not purge runtime artifact %s: %s', p, e)

    # Whole-tree nukes — logs only.  Databases and pgdata are preserved.
    _rm_tree(dest / 'logs')

    # Under data/: clean lockfile + stray log files, but LEAVE *.db alone.
    data_dir = dest / 'data'
    if data_dir.exists():
        for pattern in ('.server.lock', '*.log'):
            for f in data_dir.glob(pattern):
                _rm_tree(f)
        # Runtime API-key stats are tied to the SOURCE machine's key usage
        # counters — they don't belong in a fresh export.  Still wipe them.
        runtime_files = ('key_stats.json', '.key_stats.json')
        config_dir = data_dir / 'config'
        if config_dir.exists():
            for rf in runtime_files:
                _rm_tree(config_dir / rf)

    # NOTE: uploads/ is also preserved now.  It belongs to the user of the
    # destination copy; re-exporting must not wipe their uploaded images.
