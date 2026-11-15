"""Coverage of the project file index: the files Git considers part of a project.

The index (``tree_index.py``) admits what Git lists for a work tree: every
tracked file plus every untracked file Git does not ignore (``git ls-files``
and ``git ls-files --others --exclude-standard``). ``root_policy`` gathers
those inputs once per build; ``RuleList`` answers Git's verdict for one ignore
file; ``excluded`` applies Git's precedence across files.

Ignore semantics follow Git's ``dir.c`` and ``wildmatch.c`` exactly:
* Within one file the last matching pattern decides; ``!`` re-includes.
* A deeper ``.gitignore`` outranks a shallower one, which outranks
  ``info/exclude`` (from Git's common directory, so linked worktrees share
  it), which outranks ``core.excludesFile``.
* A pattern containing a non-trailing ``/`` matches the path relative to its
  file's directory; ``*``, ``?`` and bracket expressions never cross ``/``,
  while ``**/``, ``/**/`` and a trailing ``/**`` span directories. A pattern
  without one matches the basename at any depth. A trailing ``/`` matches
  directories only. Matching is bytewise, ASCII case-folded under
  ``core.ignoreCase``; a symlinked ``.gitignore`` is not read.
* An excluded directory is never entered, so no negation below it applies.
Tracked files are admitted whatever the ignore rules say, including below an
excluded directory or an ``IGNORE_DIRS`` name; such a directory is listed
only partially (``IgnorePolicy.partial_dirs``), and explicit queries inside it
use the live walk.

Two project-local filters adjust that set:
* ``.ignore`` files (ripgrep/fd's search-scope files, gitignore syntax) outrank
  Git's rules exactly as in ripgrep: when one of their patterns matches, it
  decides, excluding the path even if tracked or, negated, admitting it
  although Git ignores it. They hold the project's model-facing search scope
  (generated bundles stay reachable by explicit path).
* ``IGNORE_DIRS`` names bulk dependency/runtime directories whose untracked
  contents are never walked.

A root nested inside another work tree is indexed as its own tree: the
enclosing repository's ignore files and ``info/exclude`` describe that
repository (Tofu's own ``uploads/`` and ``logs/`` roots hold contents the
enclosing ``.gitignore`` excludes); the user's ``core.excludesFile`` still
applies, and its tracked files below the root are still admitted. A non-Git tree honours the ignore files inside it and
a plain ``.git/info/exclude``. Nested repositories and submodules are walked
like other untracked directories.

Entry points: ``root_policy``, ``IgnorePolicy``, ``load_rules``, ``verdict``,
``excluded``, ``IGNORE_RULE_FILES``. Dependencies: ``lib.git_runtime`` and
``activity_supervised_process`` for the read-only Git queries (rev-parse,
config, ls-files) a build makes.
"""

from __future__ import annotations

import bisect
import os
import re
import stat

from lib.log import get_logger
from lib.project_mod.config import IGNORE_DIRS

logger = get_logger(__name__)

# Per-directory ignore files honoured by both rg and fd.
IGNORE_RULE_FILES = ('.gitignore', '.ignore')

_GLOB_SPECIAL = frozenset(b'*?[\\')
_UTF8_BOM = b'\xef\xbb\xbf'
_NAME, _SUFFIX, _NAME_RE, _PATH, _PATH_RE = range(5)

# wildmatch's [:class:] names over Git's ASCII ctype table (sane_ctype).
_POSIX_CLASSES = {
    b'alnum': rb'\x30-\x39\x41-\x5a\x61-\x7a',
    b'alpha': rb'\x41-\x5a\x61-\x7a',
    b'blank': rb'\x09\x20',
    b'cntrl': rb'\x00-\x1f\x7f',
    b'digit': rb'\x30-\x39',
    b'graph': rb'\x21-\x7e',
    b'lower': rb'\x61-\x7a',
    b'print': rb'\x20-\x7e',
    b'punct': rb'\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e',
    b'space': rb'\x09\x0a\x0d\x20',
    b'upper': rb'\x41-\x5a',
    b'xdigit': rb'\x30-\x39\x41-\x46\x61-\x66',
}


# ═══════════════════════════════════════════════════════
#  Pattern lines → regex sources (dir.c parse + wildmatch.c)
# ═══════════════════════════════════════════════════════

def _trim_trailing_spaces(line):
    """dir.c trim_trailing_spaces: drop unescaped trailing spaces only."""
    if not line.endswith(b' '):
        return line
    last_space = None
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == 0x20:
            if last_space is None:
                last_space = i
        elif c == 0x5c:
            i += 1
            if i >= n:
                return line
            last_space = None
        else:
            last_space = None
        i += 1
    return line if last_space is None else line[:last_space]


def _pattern_lines(data):
    """Pattern lines of one ignore file, exactly as Git splits and trims them."""
    if data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM):]
    for line in data.split(b'\n'):
        if line.endswith(b'\r'):
            line = line[:-1]
        line = line.split(b'\0', 1)[0]
        if not line or line.startswith(b'#'):
            continue
        line = _trim_trailing_spaces(line)
        if line:
            yield line


def _class_byte(value):
    return re.escape(bytes((value,)))


def _bracket(pattern, start):
    """Translate the bracket expression opening at ``pattern[start]``.

    Returns ``(regex, next_index)``, or ``(None, None)`` where wildmatch aborts
    the whole match (unterminated expression, unknown ``[:name:]``).
    """
    n = len(pattern)
    j = start + 1
    negated = j < n and pattern[j] in b'!^'
    if negated:
        j += 1
    members = []
    previous = None  # a byte that may open a range; None after a range/class
    first = True
    while True:
        if j >= n:
            return None, None
        c = pattern[j]
        if c == 0x5d and not first:  # ']'
            break
        first = False
        if c == 0x5c:  # backslash escapes the next byte
            j += 1
            if j >= n:
                return None, None
            previous = pattern[j]
            members.append(_class_byte(previous))
            j += 1
        elif c == 0x2d and previous is not None and j + 1 < n and pattern[j + 1] != 0x5d:
            high = pattern[j + 1]
            j += 2
            if high == 0x5c:
                if j >= n:
                    return None, None
                high = pattern[j]
                j += 1
            if previous <= high:  # an inverted range matches nothing
                members.append(_class_byte(previous) + b'-' + _class_byte(high))
            previous = None
        elif c == 0x5b and j + 1 < n and pattern[j + 1] == 0x3a:  # '[:'
            close = pattern.find(b']', j + 2)
            if close < 0:
                return None, None
            if close - (j + 2) < 1 or pattern[close - 1] != 0x3a:
                previous = c  # no ':]' — an ordinary '[' member
                members.append(_class_byte(c))
                j += 1
                continue
            chars = _POSIX_CLASSES.get(pattern[j + 2:close - 1])
            if chars is None:
                return None, None
            members.append(chars)
            previous = None
            j = close + 1
        else:
            previous = c
            members.append(_class_byte(c))
            j += 1
    body = b''.join(members)
    # A bracket expression never matches '/' (WM_PATHNAME); basenames hold none.
    return (b'[^/' + body + b']' if negated else b'(?!/)[' + body + b']'), j + 1


def _wildmatch(pattern, pathname):
    """Regex source that fullmatches exactly what Git's wildmatch matches.

    ``pathname`` selects WM_PATHNAME; basename patterns never see a '/'.
    Returns None for a pattern Git can never match.
    """
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == 0x5c:
            if i + 1 >= n:
                return None  # a trailing backslash never matches
            out.append(re.escape(pattern[i + 1:i + 2]))
            i += 2
        elif c == 0x3f:  # '?'
            out.append(b'[^/]')
            i += 1
        elif c == 0x2a:  # '*'
            j = i
            while j < n and pattern[j] == 0x2a:
                j += 1
            spans = (pathname and j - i >= 2 and (i == 0 or pattern[i - 1] == 0x2f)
                     and (j == n or pattern[j] == 0x2f or pattern[j:j + 2] == b'\\/'))
            if not spans:
                out.append(b'[^/]*')
            elif j < n and pattern[j] == 0x2f:
                out.append(b'(?:.*/)?')  # '**/' also matches zero directories
                j += 1
            else:
                out.append(b'.*')
            i = j
        elif c == 0x5b:  # '['
            translated, i = _bracket(pattern, i)
            if translated is None:
                return None
            out.append(translated)
        else:
            out.append(re.escape(bytes((c,))))
            i += 1
    return b''.join(out)


def _compiled_pattern(line):
    """``(negated, dir_only, kind, value)`` for one line, or None if it never matches."""
    negated = line.startswith(b'!')
    if negated:
        line = line[1:]
    dir_only = line.endswith(b'/')
    if dir_only:
        line = line[:-1]
    if not line:
        return None
    if b'/' not in line:
        if _GLOB_SPECIAL.isdisjoint(line):
            return negated, dir_only, _NAME, line
        if line[0] == 0x2a and _GLOB_SPECIAL.isdisjoint(line[1:]):
            return negated, dir_only, _SUFFIX, line[1:]
        source = _wildmatch(line, pathname=False)
        return None if source is None else (negated, dir_only, _NAME_RE, source)
    if line.startswith(b'/'):
        line = line[1:]
    if _GLOB_SPECIAL.isdisjoint(line):
        return negated, dir_only, _PATH, line
    source = _wildmatch(line, pathname=True)
    return None if source is None else (negated, dir_only, _PATH_RE, source)


# ═══════════════════════════════════════════════════════
#  One ignore file: last matching pattern
# ═══════════════════════════════════════════════════════

def _alternation(alternatives, fold):
    """Compile ``(index, source)`` pairs so ``fullmatch`` selects the highest index.

    Alternatives are listed from the last pattern to the first and each is a
    whole-subject match, so the first alternative that matches is the last
    matching pattern. Returns ``(regex, group_to_index, highest_index)``.
    """
    if not alternatives:
        return None, None, -1
    ordered = alternatives[::-1]
    flags = re.DOTALL | (re.IGNORECASE if fold else 0)
    regex = re.compile(b'|'.join(b'(' + source + b')' for _index, source in ordered), flags)
    return regex, (None,) + tuple(index for index, _source in ordered), ordered[0][0]


class _Selection:
    """The patterns that apply to one entry type (files, or directories)."""

    __slots__ = ('names', 'paths', 'suffixes', 'name_re', 'name_groups', 'name_top',
                 'path_re', 'path_groups', 'path_top')

    def __init__(self, entries, fold):
        names, paths, suffixes, name_alternatives, path_alternatives = {}, {}, [], [], []
        for index, kind, value in entries:  # ascending pattern order
            if kind == _NAME:
                names[value] = index
            elif kind == _PATH:
                paths[value] = index
            elif kind == _SUFFIX:
                suffixes.append((value, index))
            elif kind == _NAME_RE:
                name_alternatives.append((index, value))
            else:
                path_alternatives.append((index, value))
        self.names, self.paths = names, paths
        self.suffixes = tuple(suffixes[::-1])
        self.name_re, self.name_groups, self.name_top = _alternation(name_alternatives, fold)
        self.path_re, self.path_groups, self.path_top = _alternation(path_alternatives, fold)

    def last(self, subject, name):
        """Index of the last pattern matching (*subject*, *name*), or -1."""
        best = self.names.get(name, -1)
        found = self.paths.get(subject, -1)
        if found > best:
            best = found
        for suffix, index in self.suffixes:
            if index <= best:
                break
            if name.endswith(suffix):
                best = index
                break
        if self.name_top > best:
            match = self.name_re.fullmatch(name)
            if match is not None:
                best = max(best, self.name_groups[match.lastindex])
        if self.path_top > best:
            match = self.path_re.fullmatch(subject)
            if match is not None:
                best = max(best, self.path_groups[match.lastindex])
        return best


class RuleList:
    """One ignore file's patterns: Git's last-matching-pattern verdict.

    ``strip`` is the byte length of the file's directory prefix (plus '/')
    within the index root; patterns match the path relative to it.
    """

    __slots__ = ('strip', 'fold', 'negated', 'files', 'dirs')

    def __init__(self, data, strip, fold):
        self.strip, self.fold = strip, fold
        self.negated = []
        file_entries, dir_entries = [], []
        for line in _pattern_lines(data):
            compiled = _compiled_pattern(line)
            if compiled is None:
                continue
            negated, dir_only, kind, value = compiled
            if fold and kind in (_NAME, _SUFFIX, _PATH):
                value = value.lower()
            index = len(self.negated)
            self.negated.append(negated)
            dir_entries.append((index, kind, value))
            if not dir_only:
                file_entries.append((index, kind, value))
        self.files = _Selection(file_entries, fold)
        self.dirs = _Selection(dir_entries, fold)

    def verdict(self, rel_b, name_b, is_dir):
        """True: excluded; False: re-included; None: no pattern matches."""
        subject = rel_b[self.strip:] if self.strip else rel_b
        if self.fold:
            subject, name_b = subject.lower(), name_b.lower()
        best = (self.dirs if is_dir else self.files).last(subject, name_b)
        return None if best < 0 else not self.negated[best]


def verdict(lists, rel_b, name_b, is_dir):
    """The deciding verdict across *lists*, ordered from highest precedence down.

    True: excluded; False: re-included; None: no pattern matches.
    """
    for rules in lists:
        decided = rules.verdict(rel_b, name_b, is_dir)
        if decided is not None:
            return decided
    return None


def excluded(lists, rel_b, name_b, is_dir):
    return verdict(lists, rel_b, name_b, is_dir) is True


def load_rules(path, strip, fold, *, nofollow=False):
    """Compile one ignore file, or None when it is absent, unreadable or empty.

    Only a regular file is read (a FIFO named ``.gitignore`` never blocks a
    walk); ``nofollow`` refuses a symlink as Git does for per-directory files.
    """
    if not path:
        return None
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
    if nofollow:
        flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if not isinstance(error, (FileNotFoundError, NotADirectoryError)):
            logger.debug('[TreeIndex] ignore file %s unreadable: %s', path, error)
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as error:
        logger.debug('[TreeIndex] ignore file %s unreadable: %s', path, error)
        return None
    finally:
        os.close(fd)
    rules = RuleList(b''.join(chunks), strip, fold)
    return rules if rules.negated else None


# ═══════════════════════════════════════════════════════
#  One build's coverage inputs
# ═══════════════════════════════════════════════════════

class IgnorePolicy:
    """Ignore files and tracked paths behind one index build.

    ``gitignore``/``dot_ignore`` are the root-scope rule lists ordered by
    precedence. ``tracked`` is the sorted list of tracked paths (bytes,
    relative to the root) the walk admits regardless of ignore rules; the
    index drops it after the build. ``nested`` maps a directory to the lists
    its own files add, ``partial_dirs`` collects the pruned directories whose
    tracked files the walk listed. Both grow from walk workers.
    """

    __slots__ = ('root', 'fold', 'gitignore', 'dot_ignore', 'tracked', 'nested',
                 'nested_complete', 'partial_dirs')

    def __init__(self, root, fold, gitignore, dot_ignore, tracked):
        self.root, self.fold = root, fold
        self.gitignore, self.dot_ignore = gitignore, dot_ignore
        self.tracked = tracked
        self.nested = {}
        self.nested_complete = False
        self.partial_dirs = []

    def is_tracked(self, rel_b):
        tracked = self.tracked
        if not tracked:
            return False
        i = bisect.bisect_left(tracked, rel_b)
        return i < len(tracked) and tracked[i] == rel_b

    def has_tracked_below(self, rel_dir_b):
        tracked = self.tracked
        if not tracked:
            return False
        prefix = rel_dir_b + b'/'
        i = bisect.bisect_left(tracked, prefix)
        return i < len(tracked) and tracked[i].startswith(prefix)

    def enter(self, rel_dir, entries, tracked_only, gitignore, dot_ignore):
        """Extend the inherited lists with the rule files of directory *rel_dir*.

        *entries* maps rule-file names to scandir entries. A directory listed
        only for its tracked files needs no ``.gitignore``.
        """
        strip = len(os.fsencode(rel_dir)) + 1
        own_gitignore = own_dot = None
        entry = entries.get('.gitignore')
        if entry is not None and not tracked_only:
            own_gitignore = load_rules(entry.path, strip, self.fold, nofollow=True)
        entry = entries.get('.ignore')
        if entry is not None:
            own_dot = load_rules(entry.path, strip, False)
        if own_gitignore is not None or own_dot is not None:
            self.nested[rel_dir] = (own_gitignore, own_dot)
        return _extend(gitignore, dot_ignore, own_gitignore, own_dot)

    def _lists_below(self, rel_dir, gitignore, dot_ignore):
        if rel_dir in self.nested or self.nested_complete:
            own = self.nested.get(rel_dir, (None, None))
        else:
            # Snapshot loaded from disk: read this directory's rule files once.
            base = os.path.join(self.root, rel_dir)
            strip = len(os.fsencode(rel_dir)) + 1
            own = (load_rules(os.path.join(base, '.gitignore'), strip, self.fold, nofollow=True),
                   load_rules(os.path.join(base, '.ignore'), strip, False))
            self.nested[rel_dir] = own
        return _extend(gitignore, dot_ignore, *own)

    def admits_untracked(self, rel):
        """Would a rebuild admit *rel* as an untracked file? (the write hook)."""
        parts = rel.split('/')
        gitignore, dot_ignore = self.gitignore, self.dot_ignore
        last = len(parts) - 1
        for depth, part in enumerate(parts):
            is_dir = depth < last
            if part == '.git' or (is_dir and part in IGNORE_DIRS):
                return False
            rel_dir = '/'.join(parts[:depth + 1])
            rel_b, name_b = os.fsencode(rel_dir), os.fsencode(part)
            dot = verdict(dot_ignore, rel_b, name_b, is_dir) if dot_ignore else None
            if dot or (dot is None and excluded(gitignore, rel_b, name_b, is_dir)):
                return False
            if is_dir:
                gitignore, dot_ignore = self._lists_below(rel_dir, gitignore, dot_ignore)
        return True


def _extend(gitignore, dot_ignore, own_gitignore, own_dot):
    if own_gitignore is not None:
        gitignore = (own_gitignore,) + gitignore
    if own_dot is not None:
        dot_ignore = (own_dot,) + dot_ignore
    return gitignore, dot_ignore


def _git_view(root, *, tracked):
    """``(top_level, info_exclude, excludes_file, fold, tracked_paths)`` or None.

    None means *root* is not inside a work tree (or Git is unavailable), and
    the walk honours only the ignore files inside it. A stalled Git raises, so
    the build fails like any other unreadable tree.
    """
    from lib.git_checkpoint_policy import git_policy_environment
    from lib.git_runtime import (
        GIT_METADATA_CWD, GIT_STALL_SECONDS, GitExecutableUnavailable, git_executable)
    from lib.project_mod.activity_supervised_process import run_activity_supervised

    try:
        git = git_executable()
    except GitExecutableUnavailable as error:
        logger.debug('[TreeIndex] no Git for %s: %s', root, error)
        return None
    environment = git_policy_environment()
    environment['GIT_OPTIONAL_LOCKS'] = '0'

    def run(*args):
        return run_activity_supervised(
            [git, '-c', 'core.fsmonitor=false', '-C', root, *args],
            cwd=GIT_METADATA_CWD, env=environment, stall_seconds=GIT_STALL_SECONDS,
            binary=True)

    located = run('rev-parse', '--is-inside-work-tree', '--show-prefix',
                  '--git-path', 'info/exclude')
    lines = located.stdout.split(b'\n')
    if located.returncode != 0 or len(lines) != 4 or lines[0] != b'true' or lines[3]:
        return None
    top_level = lines[1] == b''
    info_exclude = os.path.join(root, os.fsdecode(lines[2]))
    fold, excludes_file = False, _default_excludes_file()
    configured = run('config', '-z', '--get-regexp', r'^core\.(excludesfile|ignorecase)$')
    if configured.returncode == 0:
        for record in configured.stdout.split(b'\0'):
            key, assigned, value = record.partition(b'\n')
            if key == b'core.ignorecase':
                fold = _git_bool(value) if assigned else True
            elif key == b'core.excludesfile':
                excludes_file = os.path.join(
                    root, os.path.expanduser(os.fsdecode(value))) if value else None
    tracked_paths = []
    if tracked:
        listed = run('ls-files', '-z')
        if listed.returncode != 0:
            detail = os.fsdecode(listed.stderr or b'').strip()[:300]
            raise RuntimeError(f'git ls-files failed for {root}: {detail}')
        tracked_paths = sorted(path for path in listed.stdout.split(b'\0') if path)
    return top_level, info_exclude, excludes_file, fold, tracked_paths


def _default_excludes_file():
    """Git's ``core.excludesFile`` default: ``$XDG_CONFIG_HOME/git/ignore``."""
    config_home = os.environ.get('XDG_CONFIG_HOME')
    if not config_home:
        home = os.environ.get('HOME')
        if not home:
            return None
        config_home = os.path.join(home, '.config')
    return os.path.join(config_home, 'git', 'ignore')


def _git_bool(value):
    lowered = value.strip().lower()
    if lowered in (b'true', b'yes', b'on'):
        return True
    if lowered in (b'false', b'no', b'off', b''):
        return False
    try:
        return int(lowered) != 0
    except ValueError:
        return False


def root_policy(root, *, tracked=True):
    """Collect the coverage inputs of *root* for one build (or the write hook)."""
    view = _git_view(root, tracked=tracked)
    if view is None:
        fold, tracked_paths = False, []
        sources = (os.path.join(root, '.git', 'info', 'exclude'),)
    else:
        top_level, info_exclude, excludes_file, fold, tracked_paths = view
        # info/exclude is anchored at the enclosing top level, not at a nested root.
        sources = (info_exclude, excludes_file) if top_level else (excludes_file,)
    gitignore = [load_rules(os.path.join(root, '.gitignore'), 0, fold, nofollow=True)]
    gitignore += [load_rules(source, 0, fold) for source in sources]
    dot_ignore = load_rules(os.path.join(root, '.ignore'), 0, False)
    return IgnorePolicy(
        root, fold, tuple(rules for rules in gitignore if rules is not None),
        () if dot_ignore is None else (dot_ignore,), tracked_paths)
