"""Public-export publication gate + git push helpers (split out of
``export.py``)."""
import os
import subprocess
from pathlib import Path

from export_pkg._lists import (
    C_CYAN,
    C_END,
    C_GREEN,
    C_YELLOW,
    ROOT,
    _PUBLISH_FORBIDDEN_NAMES,
    _PUBLISH_FORBIDDEN_ROOTS,
    _PUBLISH_FORBIDDEN_SUFFIXES,
    _PUBLISH_MAX_FILE_BYTES,
    _PUBLISH_MAX_TREE_BYTES,
    _PUBLISH_PLACEHOLDERS,
    _PUBLISH_SOURCE_SQL_FILES,
    _SECRETS,
    logger,
)

import re
import tempfile
from datetime import datetime

from export_pkg._excludes import _safe_export_relative_path
from export_pkg._lists import (
    C_BOLD,
    C_DIM,
    C_RED,
)
from export_pkg._verify import _verify_exported_test_surface

from export_pkg._version import (
    _read_version,
)


class ExportPublishSafetyError(RuntimeError):
    """Raised when a public export is unsafe to commit or push."""


# Compile once for both candidate trees and callers verifying immutable blobs.
# The known-secret list remains owned by _lists; no extension or UTF-8 filter
# may weaken this final publication boundary.
_OPENSOURCE_SECRET_PATTERNS = tuple(
    (re.compile(re.escape(secret.encode('utf-8'))), 'Known secret')
    for secret in _SECRETS
) + tuple((re.compile(pattern, re.IGNORECASE), description) for pattern, description in (
    (rb'aigc\.sankuai\.com', 'Internal API endpoint'),
    (rb'(?<![A-Za-z0-9-])sankuai\.com\b', 'Internal domain'),
    (rb'(?<![A-Za-z0-9-])meituan\.com\b', 'Internal domain'),
)) + tuple((re.compile(pattern), description) for pattern, description in (
    (rb'hadoop-aipnlp', 'Internal username'),
    (rb'ruanjunhao04', 'Internal username'),
    (rb'/mnt/dolphinfs/', 'Internal filesystem path'),
    (rb'M-TransferContext-INF-CELL', 'Internal HTTP header name'),
    (rb'gray-release-ai-gpt-test', 'Internal gray-release config value'),
    (rb'ghp_[A-Za-z0-9]{36,}', 'GitHub classic token'),
    (rb'github_pat_[A-Za-z0-9_]{50,}', 'GitHub fine-grained token'),
    (rb'glpat-[A-Za-z0-9_-]{20,}', 'GitLab personal access token'),
    (rb'xox[baprs]-[A-Za-z0-9-]{20,}', 'Slack token'),
    (rb'AKIA[0-9A-Z]{16}', 'AWS access key ID'),
    (rb'(?<![A-Za-z0-9_-])sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}',
     'OpenAI-style secret key'),
    (rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----\s*[A-Za-z0-9+/=]{32,}',
     'Private key material'),
))


def _verify_opensource_blob(content: bytes, label: str) -> None:
    """Reject known/generic secrets in exact bytes, reporting labels only."""
    findings = set()
    for pattern, description in _OPENSOURCE_SECRET_PATTERNS:
        for match in pattern.finditer(content):
            if match.span() in findings:
                continue
            findings.add(match.span())
            line_num = content.count(b'\n', 0, match.start()) + 1
            print(f"    {C_RED}⚠ LEAK:{C_END} {label}:{line_num} — {description}")
    if findings:
        raise ExportPublishSafetyError(
            f'Public export safety scan failed: {len(findings)} leak(s) in {label}')


def _publish_candidate_paths(dest: Path) -> list[Path]:
    """Return files that ``git add -A`` can place in the publication index."""
    if not (dest / '.git').is_dir():
        return [p for p in dest.rglob('*')
                if (p.is_file() or p.is_symlink()) and '.git' not in p.parts]
    argv = ['git', 'ls-files', '-z', '--cached', '--others',
            '--exclude-standard']
    try:
        result = subprocess.run(
            argv,
            cwd=str(dest), capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.error('publish gate could not enumerate git candidates: %s', e)
        raise ExportPublishSafetyError(
            f'Publish gate could not enumerate git candidates: {e}') from e
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b'').decode(
            'utf-8', errors='replace').strip()
        logger.error('publish gate git candidate enumeration failed: %s', detail)
        raise ExportPublishSafetyError(
            f'Publish gate could not enumerate git candidates: {detail}')
    rels = [raw.decode('utf-8', errors='surrogateescape')
            for raw in result.stdout.split(b'\0') if raw]
    return [dest / rel for rel in rels]


def _publish_tree_findings(
        dest: Path,
) -> tuple[list[str], list[tuple[str, int]], int]:
    """Return forbidden paths, oversized files, and candidate-tree bytes."""
    forbidden: list[str] = []
    oversized: list[tuple[str, int]] = []
    total_bytes = 0
    try:
        for path in _publish_candidate_paths(dest):
            rel = path.relative_to(dest).as_posix()
            if path.is_symlink():
                forbidden.append(f'{rel} (symbolic link)')
                continue
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError as e:
                logger.error('publish gate could not stat %s: %s', path, e)
                raise ExportPublishSafetyError(
                    f'Publish gate could not inspect {rel}: {e}') from e
            total_bytes += size
            if size >= _PUBLISH_MAX_FILE_BYTES:
                oversized.append((rel, size))

            parts = Path(rel).parts
            name = parts[-1]
            suffix = Path(name).suffix.lower()
            env_file = name == '.env' or (
                name.startswith('.env.') and name != '.env.example')
            runtime_root = bool(parts and parts[0] in _PUBLISH_FORBIDDEN_ROOTS)
            forbidden_name = name in _PUBLISH_FORBIDDEN_NAMES
            forbidden_suffix = (suffix in _PUBLISH_FORBIDDEN_SUFFIXES
                                and rel not in _PUBLISH_SOURCE_SQL_FILES)
            if rel not in _PUBLISH_PLACEHOLDERS and (
                    runtime_root or forbidden_name or forbidden_suffix or env_file):
                forbidden.append(rel)
    except OSError as e:
        logger.error('publish gate could not walk %s: %s', dest, e, exc_info=True)
        raise ExportPublishSafetyError(
            f'Publish gate could not inspect export tree: {e}') from e
    return sorted(forbidden), sorted(oversized), total_bytes


def _verify_publish_tree(dest: Path) -> None:
    """Fail closed when a public Git tree contains data, config, or huge files."""
    forbidden, oversized, total_bytes = _publish_tree_findings(dest)
    findings = []
    if forbidden:
        findings.append('forbidden data/config paths: ' + ', '.join(forbidden[:20]))
    if oversized:
        rendered = ', '.join(f'{rel} ({size / 1024 / 1024:.1f} MiB)'
                             for rel, size in oversized[:20])
        findings.append('files at or above 50 MiB: ' + rendered)
    if total_bytes >= _PUBLISH_MAX_TREE_BYTES:
        findings.append(f'publish tree is {total_bytes / 1024 / 1024:.1f} MiB '
                        '(limit: 500 MiB)')
    if findings:
        logger.error('Public export blocked: %s', '; '.join(findings))
        raise ExportPublishSafetyError('Public export blocked: ' + '; '.join(findings))
    print(f"  {C_GREEN}\u2705 Publish tree contains code/templates only "
          f"({total_bytes / 1024 / 1024:.1f} MiB).{C_END}")


# this file is internal-only today, but a credential in any committable file is
# one whitelist mistake away from a public push.
def _load_gh_token() -> str:
    """Resolution order: TOFU_GH_TOKEN env → the credential vault (auto-
    bootstrapping from the legacy .secrets/ file) → the legacy file itself."""
    tok = os.environ.get('TOFU_GH_TOKEN', '').strip()
    if tok:
        return tok
    secrets_dir = ROOT.parent / '.secrets'
    try:
        from lib.credentials_vault import bootstrap_from_legacy, get_entry
        bootstrap_from_legacy(secrets_dir)
        tok = (get_entry('github_token') or '').strip()
        if tok:
            return tok
    except Exception as e:
        logger.warning('[Export] vault read for github_token failed, '
                       'falling back to .secrets file: %s', e)
    p = secrets_dir / 'github_token'
    try:
        tok = p.read_text(encoding='utf-8').strip()
    except OSError as e:
        raise SystemExit(
            f'GitHub token not found: set TOFU_GH_TOKEN, store it in the '
            f'credential vault (Settings → 凭证保管库), or create {p} '
            f'(chmod 600). ({e})')
    if not tok:
        raise SystemExit(f'GitHub token file {p} is empty.')
    return tok


# Compatibility sentinel for callers that historically imported this private
# name.  Credentials must never be resolved while this module is imported:
# clean checkouts and read-only tooling can inspect the export surface without
# possessing release authority.
_GH_TOKEN: str | None = None
_GIT_REPOS = {
    'opensource': {
        'remotes': [
            {
                'name': 'origin',
                'url': 'https://github.com/rangehow/ToFu.git',
                'credential': 'github_token',
            },
            {
                'name': 'niutrans',
                'url': 'https://github.com/NiuTrans/ToFu.git',
                'credential': 'github_token',
            },
        ],
        'branch': 'main',
    },
    'internal': {
        # Internal release repo (corp GitLab): colleagues clone master, which
        # holds the sanitized internal-export snapshot.  SSH key auth — no
        # token credential.  Push stays DISABLED for internal by policy
        # (_git_push guard); the snapshot push is done by _handoff.
        'remotes': [
            {
                'name': 'origin',
                'url': 'ssh://git@git.sankuai.com/~ruanjunhao04/tofu.git',
            },
        ],
        'branch': 'master',
    },
}


def _is_nonff_push_rejection(err_text: str) -> bool:
    """True when a failed ``git push`` was a non-fast-forward REJECTION.

    A force-push only makes sense when the remote rejected the update because
    its history diverged (the normal case for a freshly re-``git init``'d
    export tree that shares no ancestry with the mirror). For any OTHER push
    failure — authentication, DNS/connection, permission, protocol, missing
    repo — force-pushing is useless at best and destructive at worst, so the
    caller re-raises instead. Matching is on the git stderr text (lower-cased).

    Args:
        err_text: The combined git stderr/stdout from the failed push.

    Returns:
        True iff the text carries a recognised non-fast-forward / rejected
        signal; False for auth/network/other errors (and empty input).
    """
    if not err_text:
        return False
    low = err_text.lower()
    # Signals git emits specifically for a diverged / behind rejection.
    reject_markers = (
        'non-fast-forward',
        '[rejected]',
        'failed to push some refs',
        'fetch first',
        'tip of your current branch is behind',
        'updates were rejected',
    )
    if not any(m in low for m in reject_markers):
        return False
    # Guard against false positives: an auth/network failure never carries a
    # non-ff marker, but if git ever combines messages, an explicit auth/host
    # failure should NOT be treated as a safe-to-force divergence.
    hard_failures = (
        'authentication failed',
        'could not read username',
        'permission denied',
        'could not resolve host',
        'connection refused',
        'repository not found',
    )
    if any(h in low for h in hard_failures):
        return False
    return True


def _tag_push_action(ls_remote_out: str, local_sha: str, force: bool) -> str:
    """Compare peeled commits; published tags are immutable even with force.

    ``force`` is retained for callers' compatibility but only governs branch
    publication, never release-tag movement. Conflicts must fail the release.
    """
    if not ls_remote_out.strip():
        return 'push'
    refs = {}
    for line in ls_remote_out.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith('refs/tags/') or fields[1] in refs:
            raise ExportPublishSafetyError('Malformed published tag probe result')
        refs[fields[1]] = fields[0]
    peeled = [sha for ref, sha in refs.items() if ref.endswith('^{}')]
    direct = [sha for ref, sha in refs.items() if not ref.endswith('^{}')]
    if len(direct) != 1 or len(peeled) > 1:
        raise ExportPublishSafetyError('Ambiguous published tag probe result')
    remote_commit = peeled[0] if peeled else direct[0]
    return 'skip-same' if remote_commit == local_sha.strip() else 'conflict'


def _push_branch(git_run, rname: str, branch: str, force: bool = False,
                 *, candidate: str | None = None) -> None:
    """Push ``branch`` to ``rname`` — the default path NEVER destroys remote history.

    Policy (owner-ratified 2026-07-25,  — \"most long-term
    robust\"): when the remote rejects the push as non-fast-forward (the
    normal case for a freshly re-``git init``'d export tree, which shares no
    ancestry with the mirror), the default resolution PRESERVES the remote
    history: fetch the diverged tip and merge it with ``-s ours
    --allow-unrelated-histories`` — the merge result tree is byte-identical
    to the export snapshot (the mirror's content is always the latest
    export), while the remote's own commits stay reachable in the DAG,
    turning the retry into a fast-forward. This also works on remotes whose
    branch is PROTECTED against force-push (GitHub protected branches reject
    ``--force`` outright; the preserve-merge pushes fine).

    ``force=True`` (explicit ``--force``) restores the old overwrite behavior
    for a deliberate mirror reset. Any failure that is NOT a divergence
    (auth / network / permission / missing repo) re-raises unchanged —
    forcing or merging there would be useless-or-destructive.

    Every push disables ``push.followTags``: reachable annotated local tags
    carry unchecked messages, taggers and signatures outside the admitted refs.

    Args:
        git_run: callable running a git argv list in the export repo,
            raising RuntimeError on non-zero exit (the ``_run`` closure of
            ``_git_push``, or a test harness equivalent).
        rname: remote name, or an authenticated URL (used for push/fetch
            only; never written to the destination's git config).
        branch: branch name.
        force: explicit overwrite opt-in.
    """
    display = _redact_url_credentials(rname)
    if candidate:
        if force:
            raise ExportPublishSafetyError('History publication cannot force a fixed candidate')
        try:
            git_run(['git', 'push', '--no-follow-tags', rname, f'{candidate}:refs/heads/{branch}'])
        except RuntimeError as error:
            if _is_nonff_push_rejection(str(error)):
                raise ExportPublishSafetyError(
                    'Public remote advanced beyond the reviewed candidate; review before retrying') from error
            raise
        return
    try:
        git_run(['git', 'push', '--no-follow-tags', rname, branch])

        return
    except RuntimeError as push_err:
        if '://' in rname:
            # URL target: -u would persist the URL (credentials included)
            # into branch.<name>.remote in .git/config, so classify the
            # failure directly instead of retrying with upstream tracking.
            if not _is_nonff_push_rejection(str(push_err)):
                raise
        else:
            # First push to a new branch — try setting upstream.
            try:
                git_run(['git', 'push', '--no-follow-tags', '-u', rname, branch])
                return
            except RuntimeError as upstream_err:
                if not _is_nonff_push_rejection(str(upstream_err)):
                    raise
    if force:
        print(f"  {C_YELLOW}\u26a0 History diverged on {display}/{branch}. "
              f"--force given: force-pushing (previous remote commits will be lost).{C_END}")
        logger.warning('Force-pushing %s/%s after non-ff rejection (--force).',
                       display, branch)
        git_run(['git', 'push', '--no-follow-tags', '--force', rname, branch])
        return
    # Preserve remote history: graft the diverged tip into our DAG with an
    # empty-content merge (-s ours keeps OUR tree byte-identical; their
    # commits stay reachable), making the retry a plain fast-forward.
    print(f"  {C_CYAN}\u2139 Remote {display}/{branch} diverged — preserving remote "
          f"history via ours-merge (content = this export; use --force to overwrite).{C_END}")
    logger.info('Non-ff divergence on %s/%s — preserving remote history via '
                'ours-merge (no force).', display, branch)
    git_run(['git', 'fetch', rname, branch])
    git_run(['git', 'merge', '-s', 'ours', '--allow-unrelated-histories',
             'FETCH_HEAD', '-m',
             f'Preserve {display}/{branch} history before export (content unchanged)'])
    git_run(['git', 'push', '--no-follow-tags', rname, branch])


def _push_tag(git_run, rname: str, tag_name: str, force: bool = False) -> None:
    """Push only this immutable release tag; conflicts and Git errors are fatal."""
    display = _redact_url_credentials(rname)
    ref = f'refs/tags/{tag_name}'
    out = git_run(['git', 'ls-remote', '--tags', rname, ref, f'{ref}^{{}}'])
    local = git_run(['git', 'rev-parse', f'{ref}^{{commit}}'])
    action = _tag_push_action(out, local, force)
    if action == 'conflict':
        raise ExportPublishSafetyError(
            f'Conflicting published tag {tag_name} on {display}; refusing to move it')
    if action == 'push':
        git_run(['git', 'push', '--no-follow-tags', rname, ref])
    else:
        logger.info('Tag %s already on %s at the same peeled commit — nothing to do',
                    tag_name, display)


# ═══════════════════════════════════════════════════════════
#  Post-export verification, publication gate and push
# ═══════════════════════════════════════════════════════════
def _verify_opensource(dest: Path):
    """Post-export verification: scan exported files for leaked secrets.

    This is a hard publication gate: detected leaks and scanner failures abort
    before the destination repository can be staged or pushed.

    It reads the same candidate set that ``git add -A`` can stage. Ignored
    runtime data in a destination that doubles as a live install is neither
    published nor scanned. Any candidate enumeration/read failure aborts.
    """
    print(f"\n  {C_BOLD}\U0001f50d Post-export secret scan\u2026{C_END}")

    failures = []
    try:
        candidate_files = _publish_candidate_paths(dest)
    except ExportPublishSafetyError:
        raise
    except OSError as e:
        logger.error('secret scan could not enumerate candidates: %s', e)
        raise ExportPublishSafetyError(
            f'Secret scan could not enumerate candidates: {e}') from e

    # Scan the exact publication candidate set, not preserved/ignored runtime
    # state in a destination that doubles as a live install.
    for fpath in candidate_files:
        rel = fpath.relative_to(dest).as_posix()
        if fpath.is_symlink():
            failures.append(f'Unsafe secret-scan candidate: {rel} (symbolic link)')
            continue
        # A cached path removed from the worktree is staged as a deletion.
        if not fpath.exists():
            continue
        if not fpath.is_file():
            failures.append(f'Unsafe secret-scan candidate: {rel} (not a regular file)')
            continue
        try:
            _verify_opensource_blob(fpath.read_bytes(), rel)
        except ExportPublishSafetyError as error:
            failures.append(str(error))
        except OSError as error:
            logger.error('secret scan could not read %s: %s', rel, error)
            failures.append(f'Secret scan could not read {rel}: {error}')

    if failures:
        print(f"  {C_RED}Publishing is blocked until the findings are fixed.{C_END}\n")
        raise ExportPublishSafetyError('; '.join(failures[:20]))
    print(f"  {C_GREEN}\u2705 No secrets found in exported files.{C_END}\n")


def _verify_staged_publish_snapshot(dest: Path, *, snapshot_dest: Path | None = None) -> str:
    """Freeze and verify exact blobs; optionally retain the gated snapshot."""
    def _git(args: list[str], *, text: bool = True):
        try:
            result = subprocess.run(
                ['git', *args], cwd=str(dest), capture_output=True,
                text=text, timeout=120,
                env={**os.environ, 'GIT_NO_REPLACE_OBJECTS': '1'},
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.error('staged publish verification failed to run git: %s', e)
            raise ExportPublishSafetyError(
                f'Staged publish verification could not run git: {e}') from e
        if result.returncode != 0:
            stderr = result.stderr if text else result.stderr.decode(
                'utf-8', errors='replace')
            stdout = result.stdout if text else result.stdout.decode(
                'utf-8', errors='replace')
            detail = (stderr or stdout or '').strip()
            logger.error('staged publish verification git failure: %s', detail)
            raise ExportPublishSafetyError(
                f'Staged publish verification git failure: {detail}')
        return result

    def entries_from(raw_entries: bytes, *, index: bool) -> list[tuple[Path, str, bytes]]:
        entries = []
        seen = set()
        for entry in raw_entries.split(b'\0'):
            if not entry:
                continue
            meta, separator, raw_path = entry.partition(b'\t')
            fields = meta.split()
            valid_kind = (len(fields) == 3 and
                          (fields[2] == b'0' if index else fields[1] == b'blob'))
            oid = fields[1] if index and len(fields) == 3 else (
                fields[2] if len(fields) == 3 else b'')
            if (not separator or not valid_kind or fields[0] not in {b'100644', b'100755'}
                    or re.fullmatch(rb'[0-9a-f]{40}|[0-9a-f]{64}', oid) is None):
                raise ExportPublishSafetyError(
                    'Public export blocked: unsupported Git entries: ' + repr(raw_path))
            try:
                relative = _safe_export_relative_path(raw_path.decode('utf-8'))
            except (ValueError, UnicodeError) as error:
                raise ExportPublishSafetyError(
                    'Public export blocked: unsafe Git paths: ' + repr(raw_path)) from error
            if relative in seen:
                raise ExportPublishSafetyError(
                    'Public export blocked: duplicate Git paths: ' + relative.as_posix())
            seen.add(relative)
            entries.append((relative, oid.decode('ascii'), fields[0]))
        paths = {relative for relative, _, _ in entries}
        if any(parent in paths for relative in paths for parent in relative.parents):
            raise ExportPublishSafetyError('Public export blocked: overlapping Git paths')
        return entries

    git_dir = Path(_git(['rev-parse', '--git-common-dir']).stdout.strip())
    if not git_dir.is_absolute():
        git_dir = dest / git_dir
    if any((git_dir / rel).exists() for rel in ('shallow', 'info/grafts', 'objects/info/alternates')):
        raise ExportPublishSafetyError('Publication refuses shallow, grafted or alternate Git objects')
    if _git(['for-each-ref', '--format=%(refname)', 'refs/replace']).stdout.strip():
        raise ExportPublishSafetyError('Publication refuses replaced Git objects')
    # Validate index entries before write-tree (unmerged entries, symlinks,
    # gitlinks, and traversal never reach extraction). Freeze again to a tree
    # and enumerate THAT immutable tree: a sibling index mutation cannot make
    # our ls-files preimage differ silently from the returned verified tree.
    entries_from(_git(['ls-files', '-s', '-z'], text=False).stdout, index=True)
    tree = _git(['write-tree']).stdout.strip()
    entries = entries_from(_git(['ls-tree', '-r', '-z', tree], text=False).stdout, index=False)
    with tempfile.TemporaryDirectory(prefix='tofu-publish-index-') as td:
        snapshot = Path(td) / 'tree'
        snapshot.mkdir()
        try:
            # cat-file reads exact object bytes. git archive honors export-ignore
            # and export-subst (including .git/info/attributes), and checkout
            # filters can alter blobs; neither is a valid publication gate.
            total_bytes = 0
            for relative, oid, mode in entries:
                size = int(_git(['cat-file', '-s', oid]).stdout.strip())
                total_bytes += size
                if size >= _PUBLISH_MAX_FILE_BYTES or total_bytes >= _PUBLISH_MAX_TREE_BYTES:
                    raise ExportPublishSafetyError(
                        f'Staged publish snapshot exceeds byte limits: {relative.as_posix()}')
                content = _git(['cat-file', 'blob', oid], text=False).stdout
                if len(content) != size:
                    raise ExportPublishSafetyError(
                        f'Staged publish blob size mismatch: {relative.as_posix()}')
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    output.write(content)
                target.chmod(0o755 if mode == b'100755' else 0o644)
        except (OSError, ValueError) as error:
            logger.error('staged publish snapshot extraction failed: %s', error)
            raise ExportPublishSafetyError(
                f'Staged publish snapshot extraction failed: {error}') from error
        _verify_publish_tree(snapshot)
        _verify_opensource(snapshot)
        _verify_exported_test_surface(snapshot)
        if snapshot_dest is not None:
            import shutil
            if snapshot_dest.exists():
                raise ExportPublishSafetyError('Gated snapshot destination must be new')
            shutil.copytree(snapshot, snapshot_dest)
    return tree



def _redact_url_credentials(text: str) -> str:
    """Strip URL userinfo (``https://<token>@host`` → ``https://***@host``).

    Publish credentials ride as URL userinfo; anything that can reach the
    console, logs, or an exception message must pass through here first.
    """
    return re.sub(r'https://[^/@\s]+@', 'https://***@', text)

def _resolve_authenticated_remotes(remotes: list[dict]) -> list[dict]:
    """Resolve credentials only for the remotes that explicitly require them.

    Importing the export tool is a read-only operation and must not require
    release credentials.  This resolver runs after the publication tree gate
    and immediately before any git mutation.  Unknown credential schemes,
    malformed authenticated URLs, and missing tokens all fail closed.
    """
    resolved = []
    github_token = None
    for remote_cfg in remotes:
        remote = dict(remote_cfg)
        credential = remote.pop('credential', None)
        if credential is None:
            resolved.append(remote)
            continue
        if credential != 'github_token':
            raise ExportPublishSafetyError(
                f'Unsupported publish credential: {credential}')
        url = str(remote.get('url') or '')
        if not url.startswith('https://'):
            raise ExportPublishSafetyError(
                'GitHub publish remotes requiring credentials must use HTTPS')
        if github_token is None:
            github_token = _load_gh_token()
            if not github_token.strip():
                raise SystemExit('GitHub token resolver returned an empty token.')
        # The authenticated URL is used for push/fetch only and never leaves
        # process memory — ``url`` stays credential-free so nothing can
        # persist it to .git/config.
        remote['auth_url'] = f'https://{github_token}@{url.removeprefix("https://")}'
        resolved.append(remote)
    return resolved


def _verify_history_push_endpoints(dest: Path) -> None:
    """Admitted ancestry must already exist at the actual receiving endpoint.

    Git applies pushInsteadOf only on push, never ls-remote; named remotes can
    also have separate pushurl(s). Inspect effective configuration (including
    global/includes/environment), before mutating remotes or trusting an anchor.
    """
    environment = {**os.environ, 'GIT_NO_REPLACE_OBJECTS': '1'}
    # Legacy GIT_CONFIG redirects only `git config`, not transport commands.
    # Inspect their effective configuration rather than an unrelated file.
    environment.pop('GIT_CONFIG', None)
    try:
        result = subprocess.run(
            ['git', 'config', '--includes', '--name-only', '--get-regexp',
             r'^(url\..*\.pushinsteadof|remote\..*\.pushurl)$'],
            cwd=dest, capture_output=True, timeout=60,
            env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExportPublishSafetyError('Could not verify actual history push endpoints') from error
    if result.returncode not in (0, 1):
        raise ExportPublishSafetyError('Could not verify actual history push endpoints')
    if result.returncode == 0:
        # Keys can themselves contain credentials. Never print raw config.
        raise ExportPublishSafetyError(
            'History publication refuses push-only URL rewrites or separate push URLs; '
            'use identical explicit probe and push endpoints')


def _git_push(dest: Path, mode: str, commit_msg: str | None = None,
              is_release: bool = False, branch_override: str | None = None,
              force: bool = False, *, history_builder=None,
              expected_tree: str | None = None):
    """Initialize git (if needed), commit all files, and push to upstream(s).

    Supports pushing to multiple remotes (e.g. opensource pushes to both
    rangehow/ToFu and NiuTrans/ToFu).  The commit is created once, then
    pushed to each configured remote in turn.

    Uses normal (non-force) push since .git is preserved across re-exports,
    maintaining proper incremental commit history.

    Git tags are only created when is_release=True (i.e. --bump was used).
    This prevents random version tags from being created on every push.

    Args:
        dest: The exported project directory.
        mode: 'opensource' or 'internal'.
        commit_msg: Custom commit message.  Falls back to a timestamped default.
        is_release: If True, create a git tag for the current version.
        branch_override: Override the default branch name.
    """
    # Internal mode is never pushed to any git remote (user policy).
    if mode == 'internal':
        logger.info('Git push disabled for internal mode; skipping.')
        print(f"  {C_DIM}Internal mode: git push disabled by policy.{C_END}")
        return

    repo_cfg = _GIT_REPOS.get(mode)
    if not repo_cfg:
        logger.info('No git repo configured for mode=%s, skipping push.', mode)
        return

    remotes = repo_cfg.get('remotes', [])
    # Backward compat: single 'remote' key → wrap in list
    if not remotes and 'remote' in repo_cfg:
        remotes = [{'name': 'origin', 'url': repo_cfg['remote']}]
    if not remotes:
        logger.info('No remotes configured for mode=%s, skipping push.', mode)
        return

    branch = branch_override or repo_cfg['branch']

    if history_builder:
        _verify_history_push_endpoints(dest)

    # Re-check immediately before touching the index. The export verification
    # ran earlier, but a shared/FUSE destination can change between that pass
    # and ``git add -A``; the last responsible moment is here.
    _verify_publish_tree(dest)

    # Acquiring a publish credential is itself an authority boundary. Keep it
    # out of module import and resolve it only after the tree is safe, but
    # before git init/config/index operations can mutate the destination.
    remotes = _resolve_authenticated_remotes(remotes)

    # Display summary
    display_names = ', '.join(r['name'] for r in remotes)
    print(f"\n  {C_BOLD}\U0001f680 Git push \u2014 {len(remotes)} remote(s): "
          f"{display_names} ({branch}){C_END}")

    def _run(cmd: list[str], *, allowed_returncodes: tuple[int, ...] = (0,), **kw):
        """Run a git command in the dest directory, raise on failure."""
        result = subprocess.run(
            cmd, cwd=str(dest), capture_output=True, text=True,
            env={**os.environ, 'GIT_NO_REPLACE_OBJECTS': '1'}, **kw
        )
        if result.returncode not in allowed_returncodes:
            err = (result.stderr or result.stdout or '').strip()
            raise RuntimeError(
                f"git command failed: "
                f"{_redact_url_credentials(' '.join(cmd))}\n"
                f"{_redact_url_credentials(err)}")
        return result.stdout.strip()

    try:
        # Init repo if .git doesn't exist
        git_dir = dest / '.git'
        if not git_dir.is_dir():
            _run(['git', 'init'])
            _run(['git', 'checkout', '-b', branch])
            logger.info('Initialized new git repo in %s (branch %s)', dest, branch)
        else:
            # Ensure correct branch
            current = _run(['git', 'symbolic-ref', '--short', 'HEAD'])
            if current != branch:
                if history_builder:
                    raise ExportPublishSafetyError('History publication refuses an unexpected destination branch')
                _run(['git', 'checkout', '-B', branch])

        # Configure git identity (needed for fresh repos)
        _run(['git', 'config', 'user.email', 'rangehow@users.noreply.github.com'])
        _run(['git', 'config', 'user.name', 'rangehow'])

        # Configure all remotes
        for remote_cfg in remotes:
            rname = remote_cfg['name']
            rurl = remote_cfg['url']
            if re.search(r'https://[^/@\s]+@', rurl):
                raise ExportPublishSafetyError(
                    f'Publish remote {rname} URL embeds credentials; refusing '
                    f'to persist it to .git/config')
            try:
                _run(['git', 'remote', 'remove', rname])
            except RuntimeError:
                pass  # no remote yet — fine
            _run(['git', 'remote', 'add', rname, rurl])

        # Stage, freeze the index to a Git tree, then verify those exact bytes.
        # Worktree mutations after ``git add`` cannot hide an indexed leak.
        if expected_tree is None:
            _run(['git', 'add', '-A'])
        verified_tree = _verify_staged_publish_snapshot(dest)
        if expected_tree is not None and verified_tree != expected_tree:
            raise ExportPublishSafetyError('Publication index differs from the previously gated candidate')
        if not commit_msg:
            ts = datetime.now().strftime('%Y-%m-%d %H:%M')
            commit_msg = f'Export ({mode}) — {ts}'
        # Detect an unchanged tree explicitly; a failed commit can be a hook,
        # lock, identity, or I/O error, never evidence of "nothing to commit".
        head = _run(['git', 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}'],
                    allowed_returncodes=(0, 1))
        if history_builder:
            remote_tips = []
            for remote in remotes:
                target = remote.get('auth_url') or remote['name']
                refs = _run(['git', 'ls-remote', target, f'refs/heads/{branch}'])
                if not refs:
                    remote_tips.append(None)
                    continue
                rows = [row.split() for row in refs.splitlines()]
                if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != f'refs/heads/{branch}':
                    raise ExportPublishSafetyError('Malformed public branch probe result')
                remote_tip = rows[0][0]
                remote_tips.append(remote_tip)
                if remote_tip != head:
                    if not head or _run(['git', 'cat-file', '-t', remote_tip], allowed_returncodes=(0, 128)) != 'commit':
                        raise ExportPublishSafetyError('Public remote has an unknown candidate ancestor advance')
                    if _run(['git', 'merge-base', remote_tip, head]) != remote_tip:
                        raise ExportPublishSafetyError('Public remote diverged from the reviewed candidate ancestry')
            # A partial retry can have our rebuilt candidate on one target
            # and the previously acknowledged parent on another. Only their
            # actual common ancestry is evidence; an absent branch grants none.
            published_anchor = head if head and all(remote_tips) else None
            if published_anchor:
                for remote_tip in remote_tips:
                    published_anchor = _run(['git', 'merge-base', published_anchor, remote_tip])
                    if re.fullmatch(r'[0-9a-f]{40}', published_anchor) is None:
                        raise ExportPublishSafetyError('Invalid common receiving ancestry evidence')
            rebuilt = history_builder(dest, verified_tree, commit_msg, published_tip=published_anchor)
            if rebuilt != _run(['git', 'rev-parse', 'HEAD']):
                raise ExportPublishSafetyError('History builder did not settle the expected public tip')
        elif not head or _run(['git', 'diff', '--cached', '--name-only', '-z', 'HEAD', '--']):
            _run(['git', 'commit', '-m', commit_msg])
        else:
            print(f"  {C_DIM}Nothing changed; retrying publication of the existing ref.{C_END}")
        committed_tree = _run(['git', 'rev-parse', 'HEAD^{tree}'])
        if committed_tree != verified_tree:
            raise ExportPublishSafetyError(
                'Git index changed after publication verification: '
                f'verified {verified_tree}, committed {committed_tree}')

        candidate = _run(['git', 'rev-parse', 'HEAD'])
        # Check all remotes before publishing any. A partial retry may see
        # the candidate on one remote and its older parent on another.
        if history_builder:
            for remote in remotes:
                target = remote.get('auth_url') or remote['name']
                refs = _run(['git', 'ls-remote', target, f'refs/heads/{branch}'])
                if not refs:
                    continue
                rows = [row.split() for row in refs.splitlines()]
                if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != f'refs/heads/{branch}':
                    raise ExportPublishSafetyError('Malformed public branch probe result')
                remote_tip = rows[0][0]
                if remote_tip != candidate:
                    if _run(['git', 'cat-file', '-t', remote_tip], allowed_returncodes=(0, 128)) != 'commit':
                        raise ExportPublishSafetyError('Public remote has an unknown candidate ancestor advance')
                    if _run(['git', 'merge-base', remote_tip, candidate]) != remote_tip:
                        raise ExportPublishSafetyError('Public remote diverged from the reviewed candidate ancestry')

        # Tag with version — only when this is an explicit release (--bump)
        tag_name = None
        if is_release:
            version = (_run(['git', 'show', verified_tree + ':VERSION'])
                       if history_builder or expected_tree else _read_version())
            if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version) is None:
                raise ExportPublishSafetyError('Invalid verified release version')
            tag_name = f'v{version}'
            ref = f'refs/tags/{tag_name}'
            existing_tag = _run(['git', 'tag', '--list', tag_name])
            if existing_tag:
                local_commit = _run(['git', 'rev-parse', f'{ref}^{{commit}}'])
                if local_commit != _run(['git', 'rev-parse', 'HEAD']):
                    raise ExportPublishSafetyError(
                        f'Conflicting existing local release tag {tag_name}; refusing to move it')
            else:
                _run(['git', 'tag', '-a', tag_name, '-m', f'Release {tag_name}'])
                print(f"  {C_GREEN}\U0001f3f7\ufe0f  Tagged: {tag_name}{C_END}")

        if history_builder:
            _verify_history_push_endpoints(dest)

        # Push to each remote
        for remote_cfg in remotes:
            rname = remote_cfg['name']
            # Push/fetch against the in-memory authenticated URL when the
            # remote declares a credential; .git/config only ever holds the
            # clean URL registered above.
            target = remote_cfg.get('auth_url') or rname
            display_url = _redact_url_credentials(remote_cfg['url'])
            if history_builder:
                _verify_history_push_endpoints(dest)
                _push_branch(_run, target, branch, force=force, candidate=candidate)
            else:
                _push_branch(_run, target, branch, force=force)

            # Push tag to this remote (never silently moves a published tag)
            if tag_name:
                _push_tag(_run, target, tag_name, force=force)

            print(f"  {C_GREEN}\u2705 Pushed to {display_url} ({branch}){C_END}")

        print()  # blank line after all remotes
    except ExportPublishSafetyError:
        logger.error('Git publication aborted by safety gate', exc_info=True)
        raise
    except RuntimeError as e:
        print(f"  {C_RED}\u26a0 Git push failed: {e}{C_END}\n")
        logger.error('Git push failed for mode=%s: %s', mode, e)
        raise
    except FileNotFoundError:
        print(f"  {C_RED}\u26a0 git not found \u2014 install git to enable auto-push.{C_END}\n")
        raise
