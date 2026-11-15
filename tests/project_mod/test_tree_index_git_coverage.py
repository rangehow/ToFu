"""The tree index lists what Git lists for a work tree (lib/project_mod/tree_index_coverage.py).

Git's view of a work tree is every tracked file plus every untracked file it
does not ignore: ``git ls-files`` and ``git ls-files --others
--exclude-standard``. These specs compare the index with Git itself on fixture
repositories, so a divergence in negation, re-inclusion, precedence, anchoring,
``**`` or bracket handling fails here instead of silently hiding files from
``find_files``/``search_files``.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'docs/_gen/html/i.html', 'docs/_site/index.html', 'docs/guide.md', 'lib/node_mods/n.js',
    'static/vendor/lib.js',
}

import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.project_mod import read_tools, tree_index, tree_index_coverage
from lib.project_mod.config import IGNORE_DIRS

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which('git') is None, reason='git is not installed'),
]


def _git(root, *args):
    return subprocess.run(
        ['git', '-C', str(root), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _git_paths(root, *args):
    return [os.fsdecode(p) for p in _git(root, 'ls-files', '-z', *args).split(b'\0') if p]


def git_view(root):
    """Regular files Git considers part of *root*: tracked plus untracked-not-ignored."""
    listed = set(_git_paths(root)) | set(_git_paths(root, '--others', '--exclude-standard'))
    return {rel for rel in listed if os.path.isfile(os.path.join(root, rel))
            and not os.path.islink(os.path.join(root, rel))}


def _write(root, rel, text='needle-coverage\n'):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture()
def repo(tmp_path, tmp_path_factory, monkeypatch):
    """An empty work tree with hermetic Git configuration and index storage."""
    home = tmp_path_factory.mktemp('home')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    root = tmp_path / 'repo'
    root.mkdir()
    _git(root, 'init', '-q')
    idx_store = tmp_path_factory.mktemp('idx-store')
    monkeypatch.setattr(tree_index, '_index_dir', lambda: str(idx_store))
    monkeypatch.setattr(tree_index, '_mem', {})
    monkeypatch.setattr(tree_index, '_building', set())
    return root


def _indexed(root):
    tree_index._build_sync(str(root))
    entry = tree_index.acquire(str(root))
    assert entry is not None and entry.complete
    return set(entry.paths)


def test_reincluded_directory_serves_its_tracked_files(repo):
    """``/scripts/*`` ignores every entry of ``scripts/``; ``!/scripts/x/``
    re-includes one directory, whose files Git lists and tracks. The index
    matched the parent's contents rule against every deeper path, so the
    re-included files vanished from find_files and search_files."""
    _write(repo, '.gitignore', '/scripts/*\n!/scripts/x/\n'
                               '/bench/*\n!/bench/tool/\n/bench/tool/*\n!/bench/tool/src/\n')
    for rel in ('scripts/x/run.py', 'scripts/x/lib/util.py', 'scripts/drop.py',
                'scripts/y/c.py', 'bench/tool/src/main.rs', 'bench/tool/README.md'):
        _write(repo, rel, 'needle-reincluded\n')
    _git(repo, 'add', '-A')
    tracked = set(_git_paths(repo))
    assert {'scripts/x/run.py', 'scripts/x/lib/util.py', 'bench/tool/src/main.rs'} <= tracked
    paths = _indexed(repo)
    assert paths == git_view(repo) == tracked
    found = read_tools.tool_find_files(str(repo), '*.py')
    assert 'scripts/x/run.py (' in found and 'scripts/x/lib/util.py (' in found
    assert 'scripts/drop.py' not in found
    hits = read_tools.tool_grep(str(repo), 'needle-reincluded')
    for rel in ('scripts/x/run.py', 'scripts/x/lib/util.py', 'bench/tool/src/main.rs'):
        assert rel in hits
    assert 'scripts/y/c.py' not in hits and 'bench/tool/README.md' not in hits


def _populate_parity_fixture(root):
    _write(root, '.gitignore', '\n'.join([
        '*.log', '!keep.log', '/out/', 'docs/_site/', '/third/*', '!/third/kept/',
        '**/cache/', '!src/**/cache/', 'tmp-*', '/reinc/*', '!/reinc/a/', '/reinc/a/*',
        '!/reinc/a/b/', 'a/**/deep.txt', '\\#hash.txt', 'trailing\\ ', '[Tt]humbs.db',
        '*.py[cod]', '/anchored.txt', 'notes/*.md', '!notes/readme.md', 'JOURNAL.md',
        '/generated/', '']))
    _write(root, 'sub/.gitignore', '*.tmp\n!important.tmp\n/local-only/\ndeep/**/x.txt\n')
    _write(root, 'sub/inner/.gitignore', '!*.log\nsecret\n')
    _write(root, 'reinc/a/b/.gitignore', '*.bak\n')
    _write(root, '.git/info/exclude', '/excluded-by-info.txt\ninfo-dir/\n/AGENTS.md\n')
    for rel in (
            'keep.log', 'a.log', 'src/app.py', 'src/app.pyc', 'out/junk.txt', 'out/sub/j.txt',
            'docs/_site/index.html', 'docs/guide.md', 'third/x.js', 'third/kept/k.js',
            'third/kept/deeper/k2.js', 'lib/cache/c.bin', 'src/mod/cache/kept.bin',
            'tmp-scratch.txt', 'reinc/top.txt', 'reinc/a/skip.txt', 'reinc/a/b/in.txt',
            'reinc/a/b/old.bak', 'reinc/a/b/c/deeper.txt', 'a/deep.txt', 'a/x/y/deep.txt',
            'a/other.txt', '#hash.txt', 'trailing ', 'Thumbs.db', 'thumbs.db',
            'anchored.txt', 'nested/anchored.txt', 'notes/todo.md', 'notes/readme.md',
            'notes/sub/x.md', 'sub/x.tmp', 'sub/important.tmp', 'sub/local-only/l.txt',
            'sub/nested/local-only/l.txt', 'sub/deep/a/b/x.txt', 'sub/deep/x.txt',
            'sub/inner/debug.log', 'sub/inner/secret', 'sub/secret',
            'excluded-by-info.txt', 'info-dir/i.txt', 'sub/info-dir/i.txt',
            'JOURNAL.md', 'AGENTS.md', 'generated/bundle.js',
            # Directory names the index prunes by name; these hold tracked files.
            'src/data/fixture.json', 'native/src/bin/measure.rs', 'static/vendor/lib.js'):
        _write(root, rel)


def test_index_lists_exactly_what_git_lists(repo):
    """Parity with ``git ls-files`` + untracked-not-ignored on a fixture with
    nested .gitignore files, info/exclude, negations, re-includes, anchoring,
    ``**``, classes, escapes, force-added files under excluded directories and
    tracked files below directory names the index prunes (``data``, ``bin``,
    ``vendor``)."""
    _populate_parity_fixture(repo)
    _git(repo, 'add', '-A')
    # Tracked although ignored: force-added files and files below an excluded
    # or pruned directory; the walk cannot discover them from ignore rules.
    _git(repo, 'add', '-f', 'a.log', 'out/sub/j.txt', 'JOURNAL.md', 'AGENTS.md',
         'generated/bundle.js', 'reinc/a/skip.txt')
    for rel in ('late-untracked.txt', 'sub/late.txt', 'third/kept/late.js',
                'late.log', 'out/late.txt', 'sub/late.tmp'):
        _write(repo, rel)
    expected = git_view(repo)
    assert {'a.log', 'out/sub/j.txt', 'JOURNAL.md', 'AGENTS.md', 'reinc/a/b/c/deeper.txt',
            'third/kept/deeper/k2.js', 'sub/inner/debug.log', 'src/data/fixture.json',
            'native/src/bin/measure.rs', 'static/vendor/lib.js', 'late-untracked.txt',
            'sub/late.txt', 'third/kept/late.js', 'src/mod/cache/kept.bin'} <= expected
    assert not expected & {'out/junk.txt', 'sub/x.tmp', 'reinc/a/b/old.bak', 'late.log',
                           'out/late.txt', 'sub/late.tmp', 'excluded-by-info.txt',
                           'sub/local-only/l.txt', 'lib/cache/c.bin'}
    assert _indexed(repo) == expected


def test_tracked_files_below_pruned_directories_keep_explicit_paths_live(repo):
    """A directory the walk prunes (excluded, or named in IGNORE_DIRS) but that
    holds tracked files is listed only partially: an explicit path inside it
    still searches every file there through the live backend."""
    _write(repo, '.gitignore', '/out/\n')
    _write(repo, 'out/tracked.txt', 'needle-partial\n')
    _write(repo, 'node_modules/.keep', 'needle-partial\n')
    _git(repo, 'add', '-f', 'out/tracked.txt', 'node_modules/.keep')
    _write(repo, 'out/untracked.txt', 'needle-partial\n')
    _write(repo, 'node_modules/pkg/index.js', 'needle-partial\n')
    paths = _indexed(repo)
    assert {'out/tracked.txt', 'node_modules/.keep'} <= paths
    assert not paths & {'out/untracked.txt', 'node_modules/pkg/index.js'}
    default = read_tools.tool_grep(str(repo), 'needle-partial')
    assert 'out/tracked.txt' in default and 'node_modules/.keep' in default
    assert 'out/untracked.txt' not in default and 'node_modules/pkg' not in default
    explicit = read_tools.tool_grep(str(repo), 'needle-partial', rel_path='node_modules')
    assert 'node_modules/pkg/index.js' in explicit
    found = read_tools.tool_find_files(str(repo), '*.txt', rel_path='out')
    assert 'out/untracked.txt (' in found and 'out/tracked.txt (' in found
    tree_index._mem.clear()
    reloaded = tree_index.acquire(str(repo))
    assert reloaded is not None and not tree_index.covers(reloaded, 'out')
    assert tree_index.covers(reloaded, '') and not tree_index.covers(reloaded, 'node_modules/pkg')


def test_dot_ignore_outranks_git_exactly_as_in_ripgrep(repo):
    """``.ignore`` holds the project's model-facing search scope with ripgrep's
    precedence: a matching pattern decides over every Git rule, so it drops
    tracked files and its negations admit files and directories Git ignores;
    a deeper ``.ignore`` outranks a shallower one. ripgrep is the oracle."""
    _write(repo, '.gitignore', '*.log\n/cache/\n')
    _write(repo, '.ignore', 'gen/\nsrc/bundle.js\n*.min.js\n!keep.min.js\n!debug.log\n!cache/\n')
    _write(repo, 'web/.ignore', '!a.min.js\n')
    for rel in ('gen/out.js', 'src/bundle.js', 'src/app.js', 'web/a.min.js', 'web/b.min.js',
                'web/keep.min.js', 'debug.log', 'other.log', 'cache/c.txt'):
        _write(repo, rel)
    _git(repo, 'add', '-A')
    _git(repo, 'add', '-f', 'gen/out.js')
    paths = _indexed(repo)
    assert paths == {'.gitignore', '.ignore', 'web/.ignore', 'src/app.js', 'web/a.min.js',
                     'web/keep.min.js', 'debug.log', 'cache/c.txt'}
    if shutil.which('rg'):
        listed = subprocess.run(
            ['rg', '--files', '--hidden', '--no-config', '-g', '!.git', '.'], cwd=repo,
            check=True, stdout=subprocess.PIPE, text=True).stdout.split()
        assert paths == {rel[2:] if rel.startswith('./') else rel for rel in listed}


def test_note_write_admits_new_file_in_reincluded_directory(repo):
    _write(repo, '.gitignore', '/scripts/*\n!/scripts/x/\n')
    _write(repo, 'scripts/x/run.py')
    _git(repo, 'add', '-A')
    _indexed(repo)
    for rel in ('scripts/x/new.py', 'scripts/drop.py', 'node_modules/x.js'):
        _write(repo, rel)
        tree_index.note_write(os.path.join(repo, rel))
    paths = set(tree_index.acquire(str(repo)).paths)
    assert 'scripts/x/new.py' in paths
    assert not paths & {'scripts/drop.py', 'node_modules/x.js'}


def test_root_nested_in_another_work_tree_is_its_own_tree(repo):
    """Tofu registers roots such as ``uploads/`` and ``logs/`` whose contents the
    enclosing repository ignores; those exclusions describe that repository, so
    such a root is indexed by its own ignore files plus its tracked files."""
    _write(repo, '.gitignore', '/uploads/*\n!/uploads/images/\n/uploads/images/*\n'
                               '!/uploads/images/.gitkeep\n*.tmp\n')
    _write(repo, 'uploads/images/.gitkeep', '')
    _git(repo, 'add', '-A')
    nested = repo / 'uploads'
    _write(nested, 'images/photo.txt')
    _write(nested, 'report.pdf.txt')
    _write(nested, 'draft.tmp')
    _write(nested, '.gitignore', '*.skip\n')
    _write(nested, 'x.skip')
    tree_index._build_sync(str(nested))
    paths = set(tree_index.acquire(str(nested)).paths)
    assert paths == {'images/.gitkeep', 'images/photo.txt', 'report.pdf.txt',
                     'draft.tmp', '.gitignore'}


def test_linked_worktree_honours_the_common_info_exclude(repo, tmp_path):
    _write(repo, '.git/info/exclude', '/scratch.md\n/AGENTS.md\n')
    _write(repo, 'AGENTS.md')
    _write(repo, 'src/a.py')
    _git(repo, 'add', '-f', 'AGENTS.md', 'src/a.py')
    _git(repo, '-c', 'user.email=t@example.invalid', '-c', 'user.name=t',
         'commit', '-q', '-m', 'fixture')
    worktree = tmp_path / 'linked'
    _git(repo, 'worktree', 'add', '-q', str(worktree))
    _write(worktree, 'scratch.md')
    _write(worktree, 'notes.md')
    assert os.path.isfile(worktree / '.git')
    tree_index._build_sync(str(worktree))
    paths = set(tree_index.acquire(str(worktree)).paths)
    assert paths == git_view(worktree) == {'AGENTS.md', 'src/a.py', 'notes.md'}


# Rule lines and paths for the differential spec. No fixture path has an
# IGNORE_DIRS component: below such a name only tracked files are indexed.
_DIFFERENTIAL_LINES = [
    'node_mods', '*.pyc', 'gen/', '/out', 'docs/_gen', 'logs*', '*~', '.env', '!.env.example',
    '!keep/', 'cache?', 'a/**/b', '**/tmpd', '[Tt]humbs.db', 'foo[]x]', '[^]bar', '[!a]z',
    '\\#literal', 'deep/nested/', '*', 'x[', '# comment', '', '/scripts/*', '!/scripts/x/',
    '!scripts/x/keep.py', 'src/**', '!src/**/', '**/gen/**', 'a/*/c', '!a/b/', '*.txt',
    '!*.txt', 'sub/', '!sub/inner/', 'sub/*', '!/sub/inner', 'inner', '[[:digit:]]*',
    '[a-c]?', '\\!z', '**', '/**/b', 'name\\ with\\ space ', '*.p[!y]c', 'x/**', '!x/y/**',
    # Bracket, escape and line-trimming corners of wildmatch/dir.c.
    '[]a]', '[!]a]x', '[a-]', '[[:alpha:]][[:digit:]]', '[[:bogus:]]*', '[[:alpha]x', '[z-a]q',
    'a\\*b', '*\\', '\\', '***/deep', 'a/**/', '**/x/**', '!**/keep/**', '?', '.*', '!.env*',
    'cache?/', '/a/*', '!/a/m/', 'cachefile\r', 'b2   ', 'q/**/f', '[[:upper:]]*.db',
]
_DIFFERENTIAL_FILES = [
    'a.pyc', 'pkg/b.pyc', 'x.pyc/inner', 'gen/deep/x.py', 'src/gen/y.py', 'out', 'out2/x',
    'src/out/z', 'docs/_gen/html/i.html', 'logs.txt', 'x/logsy', 'file~', '.env',
    '.env.example', 'keep/a', 'cache1', 'cachefile', 'a/b', 'a/x/y/b', 'a/m/c', 'a/q/c',
    'tmpd/f', 'q/tmpd/f', 'Thumbs.db', 'thumbs.db', 'foo]x', 'foox', '^bar', ']bar', '!z',
    'bz', '#literal', 'deep/nested/file', 'x[', 'scripts/x/keep.py', 'scripts/x/other.py',
    'scripts/y.py', 'scripts/z/w.py', 'src/a/b/c.txt', 'src/q.txt', 'sub/c.txt',
    'sub/inner/d.txt', 'sub/inner/inner', 'node_mods/m.js', 'lib/node_mods/n.js', '1st.md',
    'b2', 'name with space', 'mod.pxc', 'x/y/z/w.md', 'x/k.md', 'misc/b',
    ']', 'ax', 'zq', 'z', 'a*b', 'aab', '-', 'q/deep', 'sub/scripts/x/keep.py',
    'sub/scripts/y.py', 'sub/a/x/b', 'sub/gen/q.py', 'sub/x.pyc', 'sub/keep/k.txt',
    'nl\nnode_mods', 'trail\n',
]


# Lower-precedence sources every case shares, in Git's order below the cases'
# own files: the repository's root .gitignore, info/exclude, core.excludesFile.
_SHARED_SOURCES = (
    ('.gitignore', ['*.pyc', '!a.pyc', 'tmpd/', '!thumbs.db', 'z']),
    ('.git/info/exclude', ['*.md', '!1st.md', '*.js', 'cachefile', 'Thumbs.db']),
    ('global-excludes', ['!*.js', '*.db', '1st.md', '!cachefile', 'mod.pxc', '!z']),
)


def test_walk_agrees_with_git_on_random_rule_sets(repo):
    """Differential spec with Git as the oracle: every case directory holds the
    same files under its own ``.gitignore`` (each single line, then random
    sets) and a nested ``sub/.gitignore``; the shared sources rank below them.
    One ``git ls-files --others`` and one walk answer every case."""
    assert not any(part in IGNORE_DIRS for rel in _DIFFERENTIAL_FILES
                   for part in rel.split('/')[:-1])
    for rel, lines in _SHARED_SOURCES:
        _write(repo, rel, '\n'.join(lines) + '\n')
    _git(repo, 'config', 'core.excludesFile', str(repo / 'global-excludes'))
    rng = random.Random(20261111)
    cases = [([line], []) for line in _DIFFERENTIAL_LINES]
    cases += [([], [line]) for line in _DIFFERENTIAL_LINES]
    cases += [(rng.sample(_DIFFERENTIAL_LINES, 6), rng.sample(_DIFFERENTIAL_LINES, 3))
              for _ in range(80)]
    for number, (case_lines, nested_lines) in enumerate(cases):
        for rel in _DIFFERENTIAL_FILES:
            _write(repo, f'case{number}/{rel}')
        _write(repo, f'case{number}/.gitignore', '\n'.join(case_lines) + '\n')
        _write(repo, f'case{number}/sub/.gitignore', '\n'.join(nested_lines) + '\n')
    expected = set(_git_paths(repo, '--others', '--exclude-standard'))
    policy = tree_index_coverage.root_policy(str(repo), tracked=False)  # nothing is tracked
    rows, complete = tree_index._walk_tree(str(repo), policy=policy)
    assert complete
    indexed = {row[0] for row in rows}
    for number, case in enumerate(cases):
        prefix = f'case{number}/'
        assert ({rel for rel in indexed if rel.startswith(prefix)}
                == {rel for rel in expected if rel.startswith(prefix)}), case
    assert indexed == expected
