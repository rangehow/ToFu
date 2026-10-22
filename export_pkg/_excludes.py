"""Per-file exclusion predicate + text-file detection (split out of ``export.py``)."""
import codecs
from pathlib import Path

from export_pkg._lists import (
    ALWAYS_EXCLUDE_DIRS,
    ALWAYS_EXCLUDE_FILES,
    ALWAYS_EXCLUDE_GLOBS,
    ALWAYS_EXCLUDE_ROOT_ONLY_DIRS,
    ALWAYS_EXCLUDE_ROOT_ONLY_FILES,
    OPENSOURCE_EXTRA_EXCLUDE_DIRS,
    OPENSOURCE_EXTRA_EXCLUDE_FILES,
    PERSONAL_EXCLUDE_DIRS,
    PERSONAL_EXCLUDE_FILES,
    PERSONAL_EXCLUDE_GLOBS,
    PERSONAL_EXCLUDE_GLOBS_HEAVY,
    _TOP_LEVEL_ONLY_EXCLUDE_DIRS,
    _TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS,
    is_agent_artifact,
    logger,
)
def _should_exclude(relpath: str, filename: str, mode: str) -> str | None:
    """Check if a file/dir should be excluded. Returns reason or None."""
    parts = Path(relpath).parts

    # ── Agent-written artifacts (.tofu*) — all modes ──
    # Prefix-based via the central registry so CURRENT and FUTURE
    # ``.tofu``-prefixed artifacts (.tofu/, .tofu_trash/, .tofu_sandbox/,
    # .tofu_env.json, …) are always stripped without re-listing each name.
    for part in parts:
        if is_agent_artifact(part):
            return f'agent artifact: {part}'
    if is_agent_artifact(filename):
        return f'agent artifact: {filename}'

    # ── Top-level-only exclusions (all modes) ──
    if parts and parts[0] in _TOP_LEVEL_ONLY_EXCLUDE_DIRS:
        return f'excluded dir: {parts[0]} (top-level only)'

    # ── Personal mode: minimal exclusions ──
    if mode == 'personal':
        for part in parts:
            if part in PERSONAL_EXCLUDE_DIRS:
                return f'excluded dir: {part}'
        if filename in PERSONAL_EXCLUDE_FILES:
            return f'excluded file: {filename}'
        for glob_pat in PERSONAL_EXCLUDE_GLOBS:
            if _glob_match(filename, glob_pat):
                return f'excluded glob: {glob_pat}'
        for glob_pat in PERSONAL_EXCLUDE_GLOBS_HEAVY:
            if _glob_match(filename, glob_pat):
                return f'excluded glob: {glob_pat}'
        return None

    # ── Internal / Opensource: full exclusions ──
    # Root-anchored dir exclusions (e.g. 'data' — nested data/ dirs ship).
    if parts and parts[0] in ALWAYS_EXCLUDE_ROOT_ONLY_DIRS:
        return f'excluded dir: {parts[0]} (top-level only)'

    # Opensource-only root-anchored dirs (scripts/ — nested packages ship).
    if mode == 'opensource' and parts and parts[0] in _TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS:
        return f'excluded dir: {parts[0]} (top-level only, opensource)'

    # Check dir exclusions
    for part in parts:
        if part in ALWAYS_EXCLUDE_DIRS:
            return f'excluded dir: {part}'
        if mode == 'opensource' and part in OPENSOURCE_EXTRA_EXCLUDE_DIRS:
            return f'opensource excluded dir: {part}'

    # Check file exclusions
    if filename in ALWAYS_EXCLUDE_FILES:
        return f'excluded file: {filename}'
    # Root-anchored file exclusions (nested same-named modules ship).
    # Internal/opensource only, like the ALWAYS sets above: personal mode is a
    # full self-backup and deliberately keeps the toolchain too.
    if len(parts) == 1 and filename in ALWAYS_EXCLUDE_ROOT_ONLY_FILES:
        return f'excluded file: {filename} (root only)'
    if mode == 'opensource' and filename in OPENSOURCE_EXTRA_EXCLUDE_FILES:
        return f'opensource excluded file: {filename}'

    # Check glob patterns
    for glob_pat in ALWAYS_EXCLUDE_GLOBS:
        if _glob_match(filename, glob_pat):
            return f'excluded glob: {glob_pat}'

    return None


def _glob_match(filename: str, pattern: str) -> bool:
    """Glob matching for file names (supports *.ext, prefix*, and *mid* patterns)."""
    if '*' not in pattern:
        return filename == pattern
    # Use fnmatch for full glob support
    from fnmatch import fnmatch
    return fnmatch(filename, pattern)


def _safe_export_relative_path(rel: str) -> Path:
    """Reject traversal, metadata aliases, and non-portable extraction names."""
    rel.encode('utf-8')  # Surrogate-escaped/non-UTF-8 Git names are not portable.
    parts = rel.split('/')
    if (not rel or '\\' in rel or ':' in rel
            or any(ord(char) < 32 or ord(char) == 127 for char in rel)
            or any(part in {'', '.', '..'} or part.casefold() == '.git'
                   or part.endswith((' ', '.')) for part in parts)):
        raise ValueError(f'Unsafe export-relative path: {rel!r}')
    return Path(*parts)


def _is_text_file(filepath: str) -> bool:
    """Accept UTF-8 source by content, including new suffixes and launchers.

    A filename allowlist is not a sanitization boundary. Validate every byte
    with a bounded incremental decoder so a binary prefix or invalid trailing
    data cannot be rewritten through replacement decoding. Known filenames
    remain useful for callers asking about a not-yet-materialized source path.
    Publication secret verification independently scans ALL candidate bytes.
    """
    path = Path(filepath)
    if path.is_symlink():
        return False
    if path.is_file():
        decoder = codecs.getincrementaldecoder('utf-8')()
        try:
            with path.open('rb') as source:
                while chunk := source.read(65536):
                    if b'\0' in chunk:
                        return False
                    decoder.decode(chunk)
                decoder.decode(b'', final=True)
            return True
        except (OSError, UnicodeDecodeError):
            return False
    return path.suffix.lower() in {
        '.py', '.js', '.ts', '.tsx', '.rs', '.mjs', '.cjs', '.svg', '.sql',
        '.lock', '.swift', '.kt', '.html', '.css', '.md', '.txt', '.json',
        '.yaml', '.yml', '.toml', '.cfg', '.conf', '.ini', '.sh', '.bash',
        '.env', '.example', '.gitignore', '.xml', '.csv', '.rst',
    } or not path.suffix




# Module-level version for sanitization pipeline (set in export_project)
def _scrub_dest_pgdata_identity(dest: Path) -> None:
    """Remove host-identity markers from ``dest/data/pgdata/`` if present.

    These files tell PostgreSQL bootstrap "this cluster belongs to host X
    on port Y".  If carried over from a source machine, the destination
    would silently connect to the source's PG via FUSE cross-machine
    routing and read the source's data — a privacy / correctness bug.

    This is idempotent: missing files are fine.  No-op if there is no
    pgdata at the destination.
    """
    pgd = dest / 'data' / 'pgdata'
    if not pgd.is_dir():
        return
    for name in ('.pg_owner_host', 'postmaster.pid', 'postmaster.opts'):
        f = pgd / name
        try:
            if f.exists():
                f.unlink()
                logger.info('Scrubbed dest pgdata identity file: %s', name)
        except OSError as e:
            logger.warning('Could not scrub dest pgdata/%s: %s', name, e)
