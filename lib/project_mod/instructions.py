"""Applicable project guidance acquisition, scoped to declared execution roots.

read_root_guidance loads root instruction files. read_scoped_guidance adds only
nested AGENTS.md on a target's directory chain, never siblings or server/cwd
ancestors; read_rule_file re-reads one known rule file. All three read through
_read_rule, the one reader of project rule files. A rule symlink whose target
resolves inside the declared root is read like that file (CLAUDE.md ->
AGENTS.md). A rule file that exists but cannot be read is a typed skip row
(SKIP_KINDS) that renders as a visible notice; it never fails the task and is
never silently dropped. Only the aggregate acquisition bound is an error, and
nothing is clipped. Acquisition is read-only; guidance cannot grant filesystem
access. render_rule is the model-visible text of one file. Callers own delivery
and task lifetime, not this module.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import errno
import os
import stat

from lib.log import get_logger

logger = get_logger(__name__)

ROOT_RULE_FILES = ('CLAUDE.md', '.cursorrules', 'AGENTS.md', 'COPILOT.md')
MAX_GUIDANCE_BYTES = 1024 * 1024
MAX_GUIDANCE_FILES = 128
# Why an existing rule file was not read. Each kind renders as a visible notice.
SKIP_KINDS = ('outside_root', 'dangling_symlink', 'symlink_loop', 'unreadable',
              'changed_during_read')


def _skipped(path: Path, root: Path, kind: str, reason: str) -> dict:
    """A rule file that exists but was not read; it renders as a notice, never as rules."""
    identity = '\0'.join(('skipped', str(path), kind, reason)).encode('utf-8', 'surrogateescape')
    return {'path': str(path), 'root': str(root), 'scope': str(path.parent), 'content': '',
            'skipped': kind, 'reason': reason, 'hash': sha256(identity).hexdigest()}


def _read_rule(path: Path, root: Path, *, remaining_bytes=MAX_GUIDANCE_BYTES,
               remaining_files=MAX_GUIDANCE_FILES) -> dict | None:
    """One rule file's row, its typed skip row, or None for verified absence."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        # A parent that is a regular file (a linked worktree's `.git`) holds no rule.
        return None
    except OSError as error:
        return _skipped(path, root, 'symlink_loop' if error.errno == errno.ELOOP else 'unreadable',
                        str(error))
    if stat.S_ISLNK(info.st_mode):
        try:
            info = os.stat(path)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ENOTDIR):
                return _skipped(path, root, 'dangling_symlink', 'its target does not exist')
            return _skipped(path, root, 'symlink_loop' if error.errno == errno.ELOOP else 'unreadable',
                            str(error))
    # Read the resolved file, never the link: a rule symlink may not import
    # guidance from a different project. (Path.resolve would raise on a loop.)
    source = Path(os.path.realpath(path))
    if not source.is_relative_to(root):
        return _skipped(path, root, 'outside_root',
                        f'it resolves to {source}, outside the declared root {root}')
    if not stat.S_ISREG(info.st_mode):
        return _skipped(path, root, 'unreadable', 'not a regular file')
    if remaining_files <= 0 or info.st_size > remaining_bytes:
        raise ValueError(f'Project guidance exceeds the remaining acquisition bound: {path}')
    if os.name == 'posix':
        # Open the resolved file through no-follow directory descriptors: a
        # component swapped after validation (a link, a moved directory) fails
        # the open instead of importing foreign bytes. openat2(RESOLVE_BENEATH)
        # would need Linux 5.6+ and is not exposed by the os module.
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for component in source.relative_to(root).parts[:-1]:
                    next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    dir_fd=fd)
            finally:
                os.close(fd)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                return _skipped(path, root, 'changed_during_read',
                                f'a path component changed after validation: {error}')
            return _skipped(path, root, 'unreadable', str(error))
        with os.fdopen(source_fd, 'rb') as opened_source:
            opened = os.fstat(opened_source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return _skipped(path, root, 'changed_during_read',
                                'it is no longer a regular file')
            if opened.st_size > remaining_bytes:
                raise ValueError(f'Project guidance exceeds the remaining acquisition bound: {path}')
            try:
                raw = opened_source.read(remaining_bytes + 1)
            except OSError as error:
                return _skipped(path, root, 'unreadable', str(error))
    else:
        # Reparse points are not read on Windows; runtime file guards retain
        # platform-specific write containment independently of this read port.
        if (path.is_symlink() or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            return _skipped(path, root, 'unreadable', 'reparse points are not read on this platform')
        try:
            with path.open('rb') as opened_source:
                raw = opened_source.read(remaining_bytes + 1)
        except OSError as error:
            return _skipped(path, root, 'unreadable', str(error))
    if len(raw) > remaining_bytes:
        raise ValueError(f'Project guidance exceeds the acquisition bound: {path}')
    text = raw.decode('utf-8', errors='replace')
    return {'path': str(path), 'root': str(root), 'scope': str(path.parent), 'content': text,
            'hash': sha256(raw).hexdigest()}


def _append_rule(rows: list[dict], path: Path, root: Path, reads: dict | None = None) -> None:
    remaining = MAX_GUIDANCE_BYTES - sum(len(row['content'].encode('utf-8')) for row in rows)
    request = (path, root, remaining, MAX_GUIDANCE_FILES - len(rows))
    if reads is not None and request in reads:
        row = reads[request]
    else:
        row = _read_rule(path, root, remaining_bytes=remaining, remaining_files=request[3])
        if reads is not None:
            reads[request] = row
    if row is not None:
        rows.append(row)
        check_guidance_bounds(rows)


def read_root_guidance(root: str, *, reads: dict | None = None) -> list[dict]:
    return _root_rules(Path(root).expanduser().resolve(), reads)


def _root_rules(root_path: Path, reads: dict | None) -> list[dict]:
    rows = []
    for name in ROOT_RULE_FILES:
        _append_rule(rows, root_path / name, root_path, reads)
    check_guidance_bounds(rows)
    return rows


def read_rule_file(path: str, root: str) -> dict | None:
    """Re-read one previously acquired rule file; None means verified absence."""
    return _read_rule(Path(path), Path(root).expanduser().resolve())


def read_scoped_guidance(root: str, target: str, *, reads: dict | None = None) -> list[dict]:
    """Root and nested rules for one target.

    ``reads`` is one acquisition's memo of rule reads keyed by their exact
    arguments, so an acquisition naming several targets reads each file once.
    """
    return resolved_scoped_guidance(Path(root).expanduser().resolve(),
                                    Path(target).expanduser().resolve(), reads=reads)


def resolved_scoped_guidance(root_path: Path, selected: Path, *,
                             reads: dict | None = None) -> list[dict]:
    """``read_scoped_guidance`` for a root and target the caller already resolved.

    A tool call's guidance check resolves both once (``_tool_targets``);
    resolving a resolved path again only repeats its ``lstat`` walk.
    """
    if not selected.is_relative_to(root_path):
        raise PermissionError('Instruction target is outside its declared root')
    rows = _root_rules(root_path, reads)
    directory = selected if selected.is_dir() else selected.parent
    chain = [directory, *directory.parents]
    for ancestor in reversed(chain[:chain.index(root_path)] if root_path in chain else []):
        _append_rule(rows, ancestor / 'AGENTS.md', root_path, reads)
    check_guidance_bounds(rows)
    return rows


def check_guidance_bounds(rows: list[dict]) -> None:
    if (len(rows) > MAX_GUIDANCE_FILES or
            sum(len(row['content'].encode('utf-8')) for row in rows) > MAX_GUIDANCE_BYTES):
        raise ValueError('Applicable project guidance exceeds the acquisition bound; nothing was omitted')


def skip_notice(row: dict) -> str:
    """The visible notice of a skip row: the file exists and none of its rules were read."""
    return (f"project rule file {row['path']} skipped: {row['skipped']} ({row['reason']}); "
            f"none of its rules for {row['scope']} were read.")


def render_rule(row: dict) -> str:
    """One rule file's model-visible section; an empty file renders nothing."""
    if row.get('skipped'):
        return f"Source: {row['path']}\n{skip_notice(row)}"
    if not row['content'].strip():
        return ''
    return (f"Project Intelligence — {Path(row['path']).name}\n"
            f"Source: {row['path']}\nApplies only within: {row['scope']}\n"
            "Nested AGENTS.md specializes its subtree; guidance does not grant access.\n\n"
            + row['content'].strip())


def render_guidance(rows: list[dict]) -> str:
    """Standalone context's guidance text; each skip notice it carries is logged."""
    check_guidance_bounds(rows)
    for row in rows:
        if row.get('skipped'):
            logger.warning('[ProjectGuidance] %s', skip_notice(row))
    return '\n'.join('\n' + text for text in map(render_rule, rows) if text)
