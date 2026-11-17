"""Resolve landing conflicts in which both sides only inserted lines at one place.

Entry point: ``resolve_insertion_conflicts(paths, run)``. ``run(args)`` runs Git
against the caller's private merge index and returns a completed process whose
``stdout`` is bytes; it never touches a user's checkout or real index.

Landings of one project are serialized (receiver lanes for reviewed
deliveries, ``scripts/git_land.py`` for lanes), so each merges onto the tip its
predecessor published. Concurrent tasks routinely append a section at the same
place of a shared document or list: Git reports that as a conflict although
neither side removed or rewrote a line. Such a hunk has exactly one ordered
resolution that keeps both: the earlier landing's lines (``ours``, already on
the branch) followed by the later landing's lines (``theirs``). Any hunk in
which the two sides changed the same base lines is a real conflict and keeps
its path unmerged, as do binary content, symlinks, add/add, delete and
mode-clash entries. Contract: docs/modules/project_integration.md,
"Landings in sequence keep both insertions".
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

_TEXT_MODES = frozenset({'100644', '100755'})
_MARKERS = b'<|=>'
# Git's own conflict-marker width (``merge-file``'s default --marker-size).
_GIT_MARKER_SIZE = 7


def resolve_insertion_conflicts(paths, run) -> list[str]:
    """Stage both sides of every insertion-only conflict; return the paths still unmerged."""
    remaining = []
    for path in paths:
        if not _resolve(path, run):
            remaining.append(path)
    return remaining


def _stages(path, run):
    listed = run(['ls-files', '-u', '-z', '--', path])
    stages = {}
    for entry in listed.stdout.split(b'\0'):
        if b'\t' not in entry:
            continue
        meta, name = entry.split(b'\t', 1)
        if os.fsdecode(name) != path:
            continue
        mode, oid, stage = meta.decode().split(' ')
        stages[int(stage)] = (mode, oid)
    return stages


def _mode(stages):
    base, ours, theirs = (stages[stage][0] for stage in (1, 2, 3))
    if not {base, ours, theirs} <= _TEXT_MODES:
        return None
    if ours == theirs or theirs == base:
        return ours
    return theirs if ours == base else None


def _marker_size(blobs):
    """One wider than any marker-like run starting a line of the inputs."""
    longest = 0
    for blob in blobs:
        for line in blob.split(b'\n'):
            if line[:1] and line[:1] in _MARKERS:
                longest = max(longest, len(line) - len(line.lstrip(line[:1])))
    return max(_GIT_MARKER_SIZE, longest + 1)


def _keep_both(merged, size):
    """The merge with every empty-base hunk as ours then theirs; None if any hunk is real."""
    start, base, middle, end = (bytes([marker]) * size for marker in b'<|=>')
    resolved, section, hunk = [], None, {}
    for line in merged.splitlines(keepends=True):
        bare = line.rstrip(b'\r\n')
        if section is None and (bare.startswith(start + b' ') or bare == start):
            section, hunk = 'ours', {'ours': [], 'base': [], 'theirs': []}
        elif section == 'ours' and (bare.startswith(base + b' ') or bare == base):
            section = 'base'
        elif section in {'ours', 'base'} and bare == middle:
            section = 'theirs'
        elif section == 'theirs' and (bare.startswith(end + b' ') or bare == end):
            if hunk['base']:
                return None
            resolved += hunk['ours'] + hunk['theirs']
            section = None
        elif section is None:
            resolved.append(line)
        else:
            hunk[section].append(line)
    return None if section is not None else b''.join(resolved)


def _resolve(path, run) -> bool:
    stages = _stages(path, run)
    if set(stages) != {1, 2, 3}:
        return False
    mode = _mode(stages)
    if mode is None:
        return False
    blobs = []
    for stage in (2, 1, 3):
        read = run(['cat-file', 'blob', stages[stage][1]])
        if read.returncode:
            return False
        blobs.append(read.stdout)
    if any(b'\0' in blob for blob in blobs):
        return False
    size = _marker_size(blobs)
    with tempfile.TemporaryDirectory(prefix='tofu-insertion-merge-') as directory:
        names = []
        for name, blob in zip(('ours', 'base', 'theirs'), blobs):
            (Path(directory) / name).write_bytes(blob)
            names.append(str(Path(directory) / name))
        merged = run(['merge-file', '-p', '--diff3', f'--marker-size={size}',
                      '-L', 'ours', '-L', 'base', '-L', 'theirs', *names])
        if merged.returncode < 0 or merged.returncode >= 128:
            return False
        content = merged.stdout if merged.returncode == 0 else _keep_both(merged.stdout, size)
        if content is None:
            return False
        (Path(directory) / 'resolved').write_bytes(content)
        written = run(['hash-object', '-w', '--no-filters', '--', str(Path(directory) / 'resolved')])
    if written.returncode:
        return False
    oid = written.stdout.decode().strip()
    return run(['update-index', '--cacheinfo', f'{mode},{oid},{path}']).returncode == 0
