"""Version read / bump / commit helpers (split out of ``export.py``)."""
import subprocess

from export_pkg._lists import C_END, C_GREEN, C_YELLOW, ROOT, logger

# ═══════════════════════════════════════════════════════════
#  Version
# ═══════════════════════════════════════════════════════════

_VERSION_FILE = ROOT / 'VERSION'


def _read_version() -> str:
    """Read the current version from VERSION file."""
    try:
        return _VERSION_FILE.read_text(encoding='utf-8').strip()
    except Exception as e:
        logger.debug('[export] VERSION file read failed (%s), defaulting: %s',
                     _VERSION_FILE, e)
        return '0.0.0'


def _next_version(current: str, part: str = 'patch') -> str:
    """Compute the next version from ``current`` (pure — no file I/O).

    Args:
        current: The current version string (e.g. ``'0.13.0'``).
        part: 'major', 'minor', or 'patch'.

    Returns:
        The bumped version string.
    """
    parts = current.split('.')
    if len(parts) != 3:
        parts = ['0', '0', '0']
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

    if part == 'major':
        major += 1; minor = 0; patch = 0
    elif part == 'minor':
        minor += 1; patch = 0
    else:
        patch += 1

    return f'{major}.{minor}.{patch}'


def _bump_version(part: str = 'patch') -> str:
    """Bump version (major/minor/patch) and write back to VERSION file.

    Args:
        part: 'major', 'minor', or 'patch'.

    Returns:
        The new version string.
    """
    current = _read_version()
    new_ver = _next_version(current, part)
    _VERSION_FILE.write_text(new_ver + '\n', encoding='utf-8')
    logger.info('Version bumped: %s → %s', current, new_ver)
    return new_ver


def _commit_version_to_origin(version: str) -> None:
    """Commit the bumped ``VERSION`` file into THIS (origin) git tree.

    ``_bump_version`` writes the new number into the origin ``VERSION`` file,
    but only the *exported* copy was ever committed + tagged. That left the
    origin's VERSION change uncommitted and the published tag drifting ahead
    of origin's last commit — so the origin machine's own self-update checker
    would later see ``latest_tag > origin_VERSION`` and nag about an update it
    itself produced. Committing the bump here keeps origin and the published
    tag in lockstep.

    Commits ONLY the ``VERSION`` file (``git commit VERSION``), so any other
    in-progress edits in the origin tree are left untouched. Never raises —
    the export itself must not fail because the origin commit didn't go
    through; failures are logged and surfaced to the operator.

    Args:
        version: The new version string (without the leading ``v``).
    """
    if not (ROOT / '.git').exists():
        logger.info('Origin is not a git checkout — skipping VERSION commit '
                    '(VERSION file was still bumped to %s)', version)
        return

    def _git(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(['git', *cmd], cwd=str(ROOT),
                              capture_output=True, text=True)

    try:
        # Nothing staged/changed for VERSION? Then it's already committed
        # (e.g. a no-op re-run) — don't create an empty commit.
        diff = _git(['diff', '--quiet', '--', 'VERSION'])
        if diff.returncode == 0:
            logger.info('Origin VERSION already committed at %s — nothing to '
                        'commit', version)
            return
        commit = _git(['commit', 'VERSION', '-m', f'v{version}'])
        if commit.returncode != 0:
            err = (commit.stderr or commit.stdout or '').strip()
            logger.error('Failed to commit VERSION to origin: %s', err)
            print(f"  {C_YELLOW}\u26a0 Could not commit VERSION to origin "
                  f"(commit it manually): {err}{C_END}")
            return
        logger.info('Committed VERSION bump v%s to origin tree', version)
        print(f"  {C_GREEN}\u2713 Committed VERSION v{version} to origin{C_END}")
    except (FileNotFoundError, OSError) as e:
        logger.error('git unavailable while committing origin VERSION: %s', e)
        print(f"  {C_YELLOW}\u26a0 git unavailable — VERSION v{version} bumped "
              f"but not committed to origin{C_END}")


# ═══════════════════════════════════════════════════════════
