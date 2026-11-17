#!/usr/bin/env python3
"""Prove this host sees the shared checkout's current bytes (host move preflight).

Responsibility: compare every tracked file, as this host reads it, with the Git
index (``git ls-files -s``) and name every file whose content differs or that
cannot be read. A DolphinFS (bgfuse) client keeps a name -> inode lookup and
its attributes cached; another host that replaces a file by rename (``git_land``,
editors, ``os.replace``) unlinks the old inode, so a host still holding the old
lookup reports the old size and mtime and then fails ``open()`` with ENOENT.
Such a host would run old code. docs/runbooks/RELIABILITY_RUNBOOK.md, "七、换主机".

Modes:
  (default)         verify; exit 1 if any tracked file mismatches or is unreadable
  --record          on the serving host: record HEAD, the index digest and the
                    hash of every uncommitted tracked file, so the new host's
                    verify accepts exactly that work and nothing else
  --repair          on the host with the stale view: list each failing file's
                    directory (a FUSE readdirplus replaces a cached lookup whose
                    inode changed), then rewrite every file that still fails
                    with its index bytes (temporary file + atomic rename on this
                    host). Requires a --record matching this HEAD and index and
                    never touches recorded uncommitted work.
  --rematerialize   on the serving host: give clean tracked files identical
                    bytes, mode and mtime under a new inode (copy + atomic
                    rename). Files with local modifications are skipped.

Dependencies: the standard library and the ``git`` executable only, on purpose:
it must run before this host's view of ``lib/`` or ``.venv`` is trusted.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

RECORD_LEAF = Path('data') / 'host-state' / 'checkout-view.json'
GITLINK_MODE = '160000'
SYMLINK_MODE = '120000'


@dataclass(frozen=True)
class Entry:
    mode: str
    blob: str
    path: str


@dataclass
class View:
    head: str
    index_digest: str
    entries: list[Entry]
    ok: int = 0
    unreadable: dict[str, str] = field(default_factory=dict)
    mismatched: dict[str, str] = field(default_factory=dict)   # path -> worktree blob


def _git(project: Path, *arguments: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(['git', '-C', str(project), '--no-optional-locks', *arguments],
                            input=data, capture_output=True)
    if result.returncode:
        raise SystemExit(f'git {" ".join(arguments[:3])} failed: '
                         f'{result.stderr.decode(errors="replace").strip()}')
    return result.stdout


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def index_entries(project: Path) -> tuple[str, str, list[Entry]]:
    raw = _git(project, 'ls-files', '-s', '-z')
    entries = []
    for record in raw.split(b'\0'):
        if not record:
            continue
        meta, path = record.split(b'\t', 1)
        mode, blob, _stage = meta.decode().split()
        entries.append(Entry(mode, blob, os.fsdecode(path)))
    head = _git(project, 'rev-parse', 'HEAD').decode().strip()
    return head, hashlib.sha256(raw).hexdigest(), entries


def _read(project: Path, entry: Entry) -> bytes:
    full = project / entry.path
    if entry.mode == SYMLINK_MODE:
        return os.fsencode(os.readlink(full))
    with open(full, 'rb') as stream:
        data = stream.read()
    size = os.lstat(full).st_size
    if size != len(data):
        raise OSError(errno.ESTALE, f'stat reports {size} bytes, open() read {len(data)}')
    return data


def _filtered_blobs(project: Path, paths: list[str]) -> dict[str, str]:
    """``git hash-object`` with this path's attributes (eol, encoding filters)."""
    if not paths:
        return {}
    out = _git(project, 'hash-object', '--stdin-paths',
               data=''.join(f'{path}\n' for path in paths).encode())
    return dict(zip(paths, out.decode().split()))


def scan(project: Path) -> View:
    head, digest, entries = index_entries(project)
    view = View(head, digest, entries)
    candidates: dict[str, str] = {}
    for entry in entries:
        if entry.mode == GITLINK_MODE:
            view.ok += 1
            continue
        try:
            data = _read(project, entry)
        except OSError as error:
            view.unreadable[entry.path] = f'{errno.errorcode.get(error.errno, error.errno)}: {error.strerror}'
            continue
        raw = _blob_id(data)
        # Raw bytes equal to the blob are clean under any filter this repository
        # declares; only differing bytes need Git's attribute-aware hash.
        if raw == entry.blob:
            view.ok += 1
        else:
            candidates[entry.path] = raw
    blobs = {entry.path: entry.blob for entry in entries}
    regular = [path for path in candidates
               if (project / path).is_file() and not (project / path).is_symlink()]
    filtered = _filtered_blobs(project, regular)
    for path, raw in candidates.items():
        actual = filtered.get(path, raw)
        if actual == blobs[path]:
            view.ok += 1
        else:
            view.mismatched[path] = actual
    return view


def _record_path(project: Path) -> Path:
    return project / RECORD_LEAF


def load_record(project: Path) -> dict | None:
    try:
        return json.loads(_record_path(project).read_text())
    except FileNotFoundError:
        return None


def _atomic_write(path: Path, data: bytes, *, mode: int, symlink: bool = False,
                  times: tuple[int, int] | None = None) -> None:
    temporary = path.with_name(f'.{path.name}.view-refresh-{os.getpid()}')
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    if symlink:
        os.symlink(os.fsdecode(data), temporary)
    else:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if times is not None:
            os.utime(temporary, ns=times)
    os.replace(temporary, path)


def classify(view: View, record: dict | None) -> tuple[dict[str, str], list[str], list[str]]:
    """(failures path->reason, accepted uncommitted paths, record problems)."""
    problems = []
    accepted_work: dict[str, str] = {}
    if record is not None:
        if record.get('head') != view.head:
            problems.append(f'record HEAD {record.get("head", "?")[:12]} != this view {view.head[:12]}')
        if record.get('index_digest') != view.index_digest:
            problems.append(f'record index {record.get("index_digest", "?")[:12]} '
                            f'!= this view {view.index_digest[:12]}')
        if not problems:
            accepted_work = record.get('uncommitted', {})
    failures = {path: f'unreadable ({reason})' for path, reason in view.unreadable.items()}
    accepted = []
    for path, actual in view.mismatched.items():
        if accepted_work.get(path) == actual:
            accepted.append(path)
        else:
            failures[path] = f'content {actual[:12]} != index {_index_blob(view, path)[:12]}'
    for path in accepted_work:
        if path not in view.mismatched:
            failures[path] = 'recorded uncommitted work not seen here (shows the index bytes)'
    return failures, sorted(accepted), problems


def _index_blob(view: View, path: str) -> str:
    return next(entry.blob for entry in view.entries if entry.path == path)


def cmd_verify(project: Path, record: dict | None) -> int:
    view = scan(project)
    failures, accepted, problems = classify(view, record)
    print(f'host {socket.gethostname()}; checkout {project}')
    print(f'HEAD {view.head}; index {view.index_digest[:16]}; {len(view.entries)} tracked files')
    if record is None:
        print(f'no record at {RECORD_LEAF}: every local modification counts as a mismatch')
    else:
        print(f'record from {record.get("host")} at {record.get("recorded_at")}: '
              + ('; '.join(problems) if problems else 'HEAD and index match'))
    for path in accepted:
        print(f'[WORK] {path}: uncommitted, same bytes as on {record.get("host")}')
    for path in sorted(failures):
        print(f'[FAIL] {path}: {failures[path]}')
    if failures or problems:
        print(f'{len(failures)} files mismatched or unreadable'
              + (f'; {len(problems)} record mismatches' if problems else '')
              + '. This host does not see the current checkout.')
        return 1
    print(f'View is current: {view.ok} files match the index'
          + (f', {len(accepted)} recorded uncommitted' if accepted else '') + '.')
    return 0


def cmd_record(project: Path) -> int:
    view = scan(project)
    if view.unreadable:
        for path, reason in sorted(view.unreadable.items()):
            print(f'[FAIL] {path}: unreadable ({reason})')
        return 1
    record = {'host': socket.gethostname(), 'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'head': view.head, 'index_digest': view.index_digest,
              'uncommitted': dict(sorted(view.mismatched.items()))}
    target = _record_path(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, (json.dumps(record, indent=1) + '\n').encode(), mode=0o644)
    print(f'recorded {target}: HEAD {view.head[:12]}, index {view.index_digest[:16]}, '
          f'{len(view.mismatched)} uncommitted: {", ".join(view.mismatched) or "none"}')
    return 0


def cmd_repair(project: Path, record: dict | None) -> int:
    view = scan(project)
    failures, _accepted, problems = classify(view, record)
    if record is None or problems:
        print('Refused: --repair needs a record of this exact HEAD and index '
              f'({"; ".join(problems) or "no record"}). Run --record on the serving host.')
        return 1
    uncommitted = record.get('uncommitted', {})
    targets = [path for path in failures if path not in uncommitted]
    for directory in sorted({str(Path(path).parent) for path in targets}):
        try:
            os.listdir(project / directory)
        except OSError as error:
            print(f'[WARN] listing {directory}: {error}')
    view = scan(project)
    failures, _accepted, _problems = classify(view, record)
    entries = {entry.path: entry for entry in view.entries}
    rewritten = 0
    for path in sorted(failures):
        if path in uncommitted:
            print(f'[KEEP] {path}: recorded uncommitted work is never rewritten')
            continue
        entry = entries[path]
        data = _git(project, 'cat-file', '--filters', f'--path={path}', entry.blob)
        _atomic_write(project / path, data, mode=0o755 if entry.mode == '100755' else 0o644,
                      symlink=entry.mode == SYMLINK_MODE)
        rewritten += 1
        print(f'[REWRITE] {path}: index bytes {entry.blob[:12]}')
    print(f'listed directories, then rewrote {rewritten} files; verifying again')
    return cmd_verify(project, record)


def cmd_rematerialize(project: Path, since: str | None, prefixes: list[str]) -> int:
    view = scan(project)
    if view.unreadable:
        for path, reason in sorted(view.unreadable.items()):
            print(f'[FAIL] {path}: unreadable ({reason})')
        return 1
    selected = {entry.path: entry for entry in view.entries
                if entry.mode not in (GITLINK_MODE,) and entry.path not in view.mismatched}
    if since:
        changed = _git(project, 'diff', '--name-only', '--no-renames', '-z', since, 'HEAD')
        wanted = {os.fsdecode(path) for path in changed.split(b'\0') if path}
        selected = {path: entry for path, entry in selected.items() if path in wanted}
    order = sorted(selected, key=lambda path: (
        next((rank for rank, prefix in enumerate(prefixes) if path.startswith(prefix)),
             len(prefixes)), path))
    moved = skipped = 0
    for path in order:
        entry, full = selected[path], project / path
        before = os.lstat(full)
        data = _read(project, entry)
        if _blob_id(data) != entry.blob and entry.mode != SYMLINK_MODE:
            skipped += 1  # filtered-clean bytes; rewriting them is not identity-preserving
            continue
        after = os.lstat(full)
        if (after.st_ino, after.st_size, after.st_mtime_ns) != (before.st_ino, before.st_size,
                                                                before.st_mtime_ns):
            print(f'[SKIP] {path}: changed while being copied')
            skipped += 1
            continue
        _atomic_write(full, data, mode=before.st_mode & 0o7777,
                      symlink=entry.mode == SYMLINK_MODE,
                      times=(before.st_atime_ns, before.st_mtime_ns))
        moved += 1
    print(f're-materialized {moved} clean tracked files under new inodes; '
          f'skipped {skipped}; left {len(view.mismatched)} locally modified: '
          f'{", ".join(sorted(view.mismatched)) or "none"}')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    modes = parser.add_mutually_exclusive_group()
    for flag in ('record', 'repair', 'rematerialize'):
        modes.add_argument(f'--{flag}', dest='mode', action='store_const', const=flag)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--since', help='--rematerialize: only files changed from this revision to HEAD')
    parser.add_argument('--first', action='append', default=[],
                        help='--rematerialize: path prefix to process first (repeatable)')
    args = parser.parse_args(argv)
    project = args.project.expanduser().resolve()
    if args.mode == 'record':
        return cmd_record(project)
    if args.mode == 'rematerialize':
        return cmd_rematerialize(project, args.since, args.first)
    if args.mode == 'repair':
        return cmd_repair(project, load_record(project))
    return cmd_verify(project, load_record(project))


if __name__ == '__main__':
    raise SystemExit(main())
