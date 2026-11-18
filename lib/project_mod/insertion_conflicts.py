"""Resolve landing conflicts that have exactly one resolution keeping both sides.

Entry point: ``resolve_insertion_conflicts(paths, run, causes=None)``. ``run(args)``
runs Git against the caller's private merge index and returns a completed
process whose ``stdout`` is bytes; it never touches a user's checkout or real
index. ``causes``, when given, receives each still-unmerged path's cause.

Landings of one project are serialized (receiver lanes for reviewed
deliveries, ``scripts/git_land.py`` for lanes), so each merges onto the tip its
predecessor published. Three rules resolve what Git reports as a conflict
although the two landings never changed the same base lines differently:

- Append-only files (the project ``JOURNAL.md``, or any path whose Git
  attribute ``tofu-append-only`` is set; ``-tofu-append-only`` opts out): when
  both sides only appended bytes, the result is the earlier landing's file
  (``ours``, already on the branch) followed by the later landing's appended
  bytes (``theirs``), once: bytes the branch already holds are not appended
  again. Any other edit of such a file falls through to the line rules.
- Insertions at one place: a hunk with an empty base keeps both, ours first;
  an insertion the other side's insertion already holds verbatim lands once.
- Agreeing edits: inside a hunk, edits separated by an unchanged base line,
  identical edits, and a rewrite of the same lines that contains the other
  side's rewrite (the branch already made the change, plus more) take both
  changes. Edits that touch stay a conflict, as in Git.

Any hunk in which both sides changed the same base lines differently is a real
conflict and keeps its path unmerged, as do binary content, symlinks, add/add
(except append-only files), delete and mode-clash entries. Contract:
docs/modules/project_integration.md, "Landings in sequence keep both
insertions" and "Append-only files and agreeing edits land".
"""
from __future__ import annotations

from difflib import SequenceMatcher
import os
from pathlib import Path
import tempfile

_TEXT_MODES = frozenset({'100644', '100755'})
_MARKERS = b'<|=>'
# Git's own conflict-marker width (``merge-file``'s default --marker-size).
_GIT_MARKER_SIZE = 7
APPEND_ONLY_ATTRIBUTE = 'tofu-append-only'
# Tofu's project journal: every agent appends its entry at the end.
APPEND_ONLY_DEFAULT = frozenset({'JOURNAL.md'})


def resolve_insertion_conflicts(paths, run, causes=None) -> list[str]:
    """Stage every conflict with one both-sides resolution; return the paths still unmerged."""
    append_only = _append_only(paths, run)
    remaining = []
    for path in paths:
        cause = _resolve(path, run, path in append_only)
        if cause:
            remaining.append(path)
            if causes is not None:
                causes[path] = cause
    return remaining


def _append_only(paths, run):
    """PATHS declared append-only by the merged ``.gitattributes`` or the default."""
    if not paths:
        return set()
    listed = run(['check-attr', '--cached', '-z', APPEND_ONLY_ATTRIBUTE, '--', *paths])
    values = {}
    if not listed.returncode:
        fields = listed.stdout.split(b'\0')
        for index in range(0, len(fields) - 2, 3):
            values[os.fsdecode(fields[index])] = fields[index + 2].decode()
    return {path for path in paths if values.get(path) == 'set'
            or (values.get(path, 'unspecified') == 'unspecified' and path in APPEND_ONLY_DEFAULT)}


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


def _stage_cause(stages):
    if 1 not in stages and {2, 3} <= set(stages):
        return 'both sides created the file'
    if 2 not in stages:
        return 'deleted on the branch, changed by this landing'
    return 'changed on the branch, deleted by this landing'


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


def _appended(ours, base, theirs):
    """Ours then the bytes theirs appended to BASE; None unless both sides only appended."""
    if not (ours.startswith(base) and theirs.startswith(base)):
        return None
    added = theirs[len(base):]
    if added in ours[len(base):]:
        return ours
    if base[-1:] not in (b'', b'\n') and added.startswith(b'\n') and ours.endswith(b'\n'):
        added = added[1:]  # It ended base's last line, which ours already ended.
    elif ours[-1:] not in (b'', b'\n') and not added.startswith(b'\n'):
        added = b'\n' + added
    return ours + added


def _edits(base, side):
    matcher = SequenceMatcher(None, base, side, autojunk=False)
    return [(i1, i2, side[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != 'equal']


def _interfere(left, right):
    """Edits that overlap or touch, as Git judges them; insertions at one place excepted.

    Adjacent edits stay a conflict: an edit beside lines the other side
    changed may depend on them (e.g. on another writer's uncommitted bytes).
    """
    (s1, e1, _), (s2, e2, _) = left, right
    if s1 == e1 == s2 == e2:
        return False  # Insertions at one place keep both, ours first.
    return s1 <= e2 and s2 <= e1


def _contains(lines, part):
    return bool(part) and any(lines[index:index + len(part)] == part
                              for index in range(len(lines) - len(part) + 1))


def _insert_both(ours, theirs):
    """Both insertions at one place, ours first; once when one already holds the other."""
    if _contains(ours, theirs):
        return ours
    if _contains(theirs, ours):
        return theirs
    return ours + theirs


def _merge_hunk(ours, base, theirs):
    """(merged lines, []) or (None, base ranges both sides changed differently)."""
    if not base:
        return _insert_both(ours, theirs), []
    kept, conflicts = _edits(base, ours), []
    mine = list(kept)
    for edit in _edits(base, theirs):
        if edit in mine:
            continue
        same_place = [other for other in mine if other[0] == other[1] == edit[0] == edit[1]]
        if same_place:
            kept[kept.index(same_place[0])] = (edit[0], edit[1], _insert_both(same_place[0][2], edit[2]))
            continue
        clash = [other for other in mine if _interfere(other, edit)]
        if not clash:
            kept.append(edit)
            continue
        if len(clash) == 1 and clash[0][:2] == edit[:2]:
            if _contains(clash[0][2], edit[2]):
                continue  # The branch already made this change, with more.
            if _contains(edit[2], clash[0][2]):
                kept[kept.index(clash[0])] = edit
                continue
        conflicts.append((min(edit[0], *(other[0] for other in clash)),
                          max(edit[1], *(other[1] for other in clash))))
    if conflicts:
        return None, conflicts
    merged, position = [], 0
    for start, end, lines in sorted(kept, key=lambda item: (item[0], item[0] != item[1])):
        merged += base[position:start] + lines
        position = max(position, end)
    return merged + base[position:], []


def _base_lines(base_lines, block, ranges):
    """1-based base line ranges of RANGES inside BLOCK, when BLOCK occurs once in the base."""
    def bare(lines):
        return [line.rstrip(b'\r\n') for line in lines]
    whole, wanted = bare(base_lines), bare(block)
    found = [index for index in range(len(whole) - len(wanted) + 1)
             if whole[index:index + len(wanted)] == wanted]
    if len(found) != 1:
        return []
    return [(found[0] + start + 1, found[0] + end) for start, end in ranges]


def _merge_hunks(merged, size, base_blob):
    """(merged bytes, '') or (None, cause) from ``merge-file --diff3`` output."""
    start, base, middle, end = (bytes([marker]) * size for marker in b'<|=>')
    resolved, section, hunk, overlaps, located = [], None, {}, 0, []
    base_lines = base_blob.splitlines(keepends=True)
    for line in merged.splitlines(keepends=True):
        bare = line.rstrip(b'\r\n')
        if section is None and (bare.startswith(start + b' ') or bare == start):
            section, hunk = 'ours', {'ours': [], 'base': [], 'theirs': []}
        elif section == 'ours' and (bare.startswith(base + b' ') or bare == base):
            section = 'base'
        elif section in {'ours', 'base'} and bare == middle:
            section = 'theirs'
        elif section == 'theirs' and (bare.startswith(end + b' ') or bare == end):
            lines, conflicts = _merge_hunk(hunk['ours'], hunk['base'], hunk['theirs'])
            if lines is None:
                overlaps += 1
                located += _base_lines(base_lines, hunk['base'], conflicts) or [None]
            else:
                resolved += lines
            section = None
        elif section is None:
            resolved.append(line)
        else:
            hunk[section].append(line)
    if section is not None:
        return None, 'unreadable merge result'
    if not overlaps:
        return b''.join(resolved), ''
    spans = [f'{first}' if first == last else f'{first}-{last}' for first, last in filter(None, located)]
    if not spans:
        return None, 'both sides changed the same lines'
    unlocated = located.count(None)
    return None, (f"both sides changed line{'s' if len(spans) > 1 or '-' in spans[0] else ''} "
                  + ', '.join(spans) + (f" and {unlocated} more place{'s' if unlocated > 1 else ''}" if unlocated else ''))


def _resolve(path, run, append_only) -> str:
    """'' once PATH is staged resolved, else the cause it stays unmerged."""
    stages = _stages(path, run)
    if append_only and 1 not in stages and {2, 3} <= set(stages) and stages[2][0] == stages[3][0]:
        stages[1] = (stages[2][0], '')
    if set(stages) != {1, 2, 3}:
        return _stage_cause(stages)
    mode = _mode(stages)
    if mode is None:
        return 'file mode or type changed on both sides'
    blobs = []
    for stage in (2, 1, 3):
        if not stages[stage][1]:
            blobs.append(b'')
            continue
        read = run(['cat-file', 'blob', stages[stage][1]])
        if read.returncode:
            return 'a conflicting version is unreadable'
        blobs.append(read.stdout)
    if any(b'\0' in blob for blob in blobs):
        return 'binary content changed on both sides'
    content = _appended(*blobs) if append_only else None
    with tempfile.TemporaryDirectory(prefix='tofu-insertion-merge-') as directory:
        if content is None:
            if not stages[1][1]:
                return 'both sides created the file'
            size = _marker_size(blobs)
            names = []
            for name, blob in zip(('ours', 'base', 'theirs'), blobs):
                (Path(directory) / name).write_bytes(blob)
                names.append(str(Path(directory) / name))
            merged = run(['merge-file', '-p', '--diff3', f'--marker-size={size}',
                          '-L', 'ours', '-L', 'base', '-L', 'theirs', *names])
            if merged.returncode < 0 or merged.returncode >= 128:
                return 'git merge-file failed'
            content, cause = ((merged.stdout, '') if merged.returncode == 0
                              else _merge_hunks(merged.stdout, size, blobs[1]))
            if content is None:
                return cause
        (Path(directory) / 'resolved').write_bytes(content)
        written = run(['hash-object', '-w', '--no-filters', '--', str(Path(directory) / 'resolved')])
    if written.returncode:
        return 'the resolution could not be written'
    oid = written.stdout.decode().strip()
    if run(['update-index', '--cacheinfo', f'{mode},{oid},{path}']).returncode:
        return 'the resolution could not be staged'
    return ''
