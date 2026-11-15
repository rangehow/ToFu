"""Tofu's Git inspections never take a checkout's ``index.lock``.

WHY (2026-11-12/13): zero-byte ``index.lock`` files with no owner blocked
Workers' commits. Git removes its lock on exit and on catchable signals, never
when killed: ``subprocess.run(timeout=...)`` kills with SIGKILL, and a SIGTERM
that arrives during a FUSE stall is overtaken by the follow-up SIGKILL.
Porcelain ``git diff`` against the working tree and ``git describe --dirty``
refresh and rewrite the whole index under that lock even with
``--no-optional-locks`` (Git 2.34.1); plain ``git status`` takes it too. A read
that never takes the lock cannot strand it, however its process dies.

RULE: every Git ``status``, ``diff`` or ``describe`` that Tofu starts as a
subprocess goes through ``git_inspection_execution.prepare_git_index_read`` or
is lock-free by construction:

* ``status`` with ``--no-optional-locks``, or run by a Git runner whose module
  sets ``GIT_OPTIONAL_LOCKS=0`` in the environment it passes;
* ``diff`` that never reads the working tree: ``--cached``/``--staged``,
  ``--no-index``, two revisions or a range;
* ``describe`` without ``--dirty``/``--broken``.

Scope: every tracked Python module. In test modules (any ``tests/`` directory)
the rule applies to ``conftest.py``, ``tests/support/`` and any test that runs
Git in the checkout under test (no ``cwd``/``-C``, or one derived from
``__file__``). A test that runs Git in its own temporary repository is outside
the rule: no other writer shares that index. The checker reads argv where it is written: direct argv passed to a
process call, and calls of Git runners (``_git(root, 'diff', ...)``,
``_run_git(['status'])``) resolved within the module or through its imports.
"""
from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Sources of the synthetic repository the guard's own specs build, not repository files.
_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/fingerprint.py', 'lib/manifest.py', 'lib/reads.py', 'lib/routed.py', 'lib/runner.py',
    'tests/test_checkout_probe.py', 'tests/test_fixture_repository.py',
}

ROOT = Path(__file__).resolve().parents[2]
_INSPECTIONS = frozenset({'status', 'diff', 'describe'})
# Global options whose value is the next argument (git(1)).
_VALUED_OPTIONS = frozenset({'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'})
_SPAWNER = re.compile(r'run|popen|exec|spawn|output|call|git', re.IGNORECASE)
_LOCKS_OFF = re.compile(r"""GIT_OPTIONAL_LOCKS['"]?\s*[:=]\s*['"]0['"]""")
_REMEDY = ('read through git_inspection_execution.prepare_git_index_read '
           '(status may pass --no-optional-locks instead)')


def _word(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _identifiers(node) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _name(node) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ''


def _is_git(node) -> bool:
    return _word(node) == 'git' or (_word(node) is None and 'git' in _name(node).lower())


def _split(elements):
    """(literal global options, '-C' values, subcommand or None when dynamic, words after it)."""
    options, directories, index = [], [], 0
    while index < len(elements):
        word = _word(elements[index])
        if word is None or not word.startswith('-'):
            return options, directories, word, elements[index + 1:]
        options.append(word)
        if word in _VALUED_OPTIONS:
            if word == '-C' and index + 1 < len(elements):
                directories.append(elements[index + 1])
            index += 2
        else:
            index += 1
    return options, directories, None, []


def _is_range(node) -> bool:
    if isinstance(node, ast.JoinedStr):
        return any('..' in (_word(value) or '') for value in node.values)
    return '..' in (_word(node) or '')


def _lock_free(subcommand, options, words, *, locks_off: bool) -> bool:
    flags = set(options) | {word for word in map(_word, words) if word}
    if subcommand == 'status':
        return locks_off or '--no-optional-locks' in flags
    if subcommand == 'describe':
        return not any(flag.split('=', 1)[0] in ('--dirty', '--broken') for flag in flags)
    if flags & {'--cached', '--staged', '--no-index'}:
        return True
    revisions = []
    for node in words:
        word = _word(node)
        if word == '--':
            break
        if word is not None and word.startswith('-'):
            continue
        if isinstance(node, ast.Starred):
            return False  # An unknown number of revisions may be one.
        revisions.append(node)
    return len(revisions) >= 2 or any(map(_is_range, revisions))


@dataclass
class _Module:
    path: str
    source: str

    @cached_property
    def tree(self) -> ast.Module:
        return ast.parse(self.source)

    @cached_property
    def package(self) -> list[str]:
        return self.path[:-3].split('/')[:-1]

    @cached_property
    def _definitions(self) -> dict[str, list]:
        index: dict[str, list] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(node.name, []).append(node)
        return index

    def definitions(self, name):
        return self._definitions.get(name, [])

    @cached_property
    def _origins(self) -> dict[str, tuple[str, str | None]]:
        index: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                base = '.'.join(self.package[:len(self.package) - node.level + 1]
                                if node.level else [])
                module = '.'.join(part for part in (base, node.module or '') if part)
                for alias in node.names:
                    index.setdefault(alias.asname or alias.name, (module, alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname or '.' not in alias.name:
                        index.setdefault(alias.asname or alias.name, (alias.name, None))
        return index

    def origin(self, name):
        """(dotted module, attribute or None) that NAME is imported as here."""
        return self._origins.get(name)

    @cached_property
    def checkout_names(self) -> set[str]:
        """Module-level names bound to the checkout under test (derived from __file__)."""
        names: set[str] = set()
        for node in self.tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                used = _identifiers(node.value)
                if '__file__' in used or used & names:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names.update(target.id for target in targets if isinstance(target, ast.Name))
        return names


class _Tree:
    def __init__(self, read):
        self._read = read
        self._modules: dict[str, _Module | None] = {}

    def module(self, path: str) -> _Module | None:
        if path not in self._modules:
            source = self._read(path)
            self._modules[path] = None if source is None else _Module(path, source)
        return self._modules[path]

    def dotted(self, name: str) -> _Module | None:
        base = name.replace('.', '/')
        return self.module(base + '.py') or self.module(base + '/__init__.py')

    def runner(self, module: _Module, callee):
        """(module, definitions) of the Git runner a call names; None when unresolved."""
        if isinstance(callee, ast.Name):
            if module.definitions(callee.id):
                return module, module.definitions(callee.id)
            origin = module.origin(callee.id)
            if origin and origin[1]:
                other = self.dotted(origin[0])
                if other and other.definitions(origin[1]):
                    return other, other.definitions(origin[1])
        elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
            if callee.value.id in ('self', 'cls') and module.definitions(callee.attr):
                return module, module.definitions(callee.attr)
            origin = module.origin(callee.value.id)
            if origin:
                other = self.dotted(origin[0] if origin[1] is None else f'{origin[0]}.{origin[1]}')
                if other and other.definitions(callee.attr):
                    return other, other.definitions(callee.attr)
        return None

    def violations(self, path: str) -> list[str]:
        module = self.module(path)
        parts = path.split('/')
        everywhere = ('tests' not in parts[:-1] or parts[-1] == 'conftest.py'
                      or path.startswith('tests/support/'))
        found = []
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                problem = self._process_call(module, node, everywhere) or self._runner_call(
                    module, node, everywhere)
                if problem:
                    found.append(f'{path}:{node.lineno}: {problem}')
        return found

    def _process_call(self, module, node, everywhere):
        if not _SPAWNER.search(_name(node.func)):
            return None
        argument = node.args[0] if node.args else next(
            (keyword.value for keyword in node.keywords if keyword.arg == 'args'), None)
        if not (isinstance(argument, (ast.List, ast.Tuple)) and argument.elts
                and _is_git(argument.elts[0])):
            return None
        options, directories, subcommand, words = _split(argument.elts[1:])
        if subcommand not in _INSPECTIONS:
            return None
        targets = directories + [keyword.value for keyword in node.keywords if keyword.arg == 'cwd']
        if not everywhere and targets and not any(
                _identifiers(target) & module.checkout_names for target in targets):
            return None  # A test's own temporary repository.
        locks_off = (any(keyword.arg == 'env' for keyword in node.keywords)
                     and bool(_LOCKS_OFF.search(module.source)))
        if _lock_free(subcommand, options, words, locks_off=locks_off):
            return None
        return f'git {subcommand} can take index.lock: {_REMEDY}'

    def _runner_call(self, module, node, everywhere):
        callee = _name(node.func)
        if 'git' not in callee.lower() or callee == 'prepare_git_index_read':
            return None
        for position, argument in enumerate(node.args):
            if isinstance(argument, (ast.List, ast.Tuple)):
                options, _, subcommand, words = _split(argument.elts)
            elif _word(argument) is not None:
                options, _, subcommand, words = _split(node.args[position:])
            else:
                continue
            break
        else:
            return None
        if subcommand not in _INSPECTIONS:
            return None
        resolved = self.runner(module, node.func)
        if not everywhere:
            targets = node.args[:position]
            local = resolved is not None and resolved[0] is module and any(
                _identifiers(definition) & module.checkout_names for definition in resolved[1])
            if not local and not any(_identifiers(target) & module.checkout_names
                                     for target in targets):
                return None  # A test's own temporary repository.
        routed = locks_off = False
        if resolved is not None:
            runner_module, definitions = resolved
            routed = any('prepare_git_index_read' in {_name(child) for child in ast.walk(definition)
                                                       if isinstance(child, ast.Call)}
                         for definition in definitions)
            locks_off = routed or bool(_LOCKS_OFF.search(runner_module.source)) or any(
                '--no-optional-locks' in ast.unparse(definition) for definition in definitions)
        if routed or _lock_free(subcommand, options, words, locks_off=locks_off):
            return None
        return f'git {subcommand} through {callee}() can take index.lock: {_REMEDY}'


def _read_checkout(path: str) -> str | None:
    try:
        return (ROOT / path).read_text(encoding='utf-8')
    except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError):
        return None


def test_no_git_inspection_can_take_a_checkout_index_lock():
    # Every checked shape names its subcommand as a quoted literal, next to a
    # quoted 'git' or an identifier containing git that is called or listed.
    listed = subprocess.run(
        ['git', '-C', str(ROOT), 'grep', '--untracked', '-l', '-z', '--all-match', '-E',
         '-e', r"""['"](status|diff|describe)['"]""",
         '-e', r"""['"]git['"]|[A-Za-z_]*[Gg][Ii][Tt][A-Za-z_]*[[:space:]]*[(,]""",
         '--', '*.py'],
        capture_output=True, text=True)
    assert listed.returncode in (0, 1), listed.stderr
    tree = _Tree(_read_checkout)
    found = [problem for path in listed.stdout.split('\0') if path and tree.module(path)
             for problem in tree.violations(path)]
    assert found == [], '\n'.join(found)


_HISTORICAL = {
    # The session-start probe that stranded locks on 2026-11-12.
    'tests/conftest.py': (
        "import os, subprocess\n"
        "_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        "def probe(rel):\n"
        "    return subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', rel],\n"
        "                          cwd=_ROOT_DIR, capture_output=True, timeout=15)\n"),
    'lib/fingerprint.py': (
        "from lib.runner import _run_git\n"
        "def digest():\n"
        "    return _run_git(['diff', 'HEAD']).stdout\n"),
    'lib/manifest.py': (
        "import subprocess\n"
        "def dirty(root):\n"
        "    return subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, timeout=5)\n"
        "def describe(root):\n"
        "    return subprocess.run(['git', '-C', root, 'describe', '--dirty'])\n"),
    'tools/snapshot.py': (
        "import subprocess\n"
        "def dirty(src):\n"
        "    return subprocess.run(['git', 'diff', '--name-only', '-z', 'HEAD'],\n"
        "                          cwd=src, timeout=30).stdout\n"),
    'tests/test_checkout_probe.py': (
        "import subprocess\n"
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "def test_generated_outputs_are_committed():\n"
        "    subprocess.run(['git', 'diff', '--exit-code', '--', 'static'], cwd=ROOT, check=True)\n"),
}

_LOCK_FREE = {
    'lib/routed.py': (
        "import subprocess\n"
        "from lib.project_mod.git_inspection_execution import prepare_git_index_read\n"
        "def _git(root, *args):\n"
        "    with prepare_git_index_read('git', list(args), root, environment={}) as (argv, env):\n"
        "        return subprocess.run(['git', '-C', root, *argv], env=env, timeout=30)\n"
        "def fingerprint(root):\n"
        "    return _git(root, 'diff', 'HEAD')\n"),
    'lib/reads.py': (
        "import os, subprocess\n"
        "def _git(cwd, *args):\n"
        "    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}\n"
        "    return subprocess.run(['git', *args], cwd=cwd, env=env, timeout=5)\n"
        "def reads(root, old, new, revision):\n"
        "    _git(root, 'status', '--porcelain')\n"
        "    subprocess.run(['git', '-C', root, '--no-optional-locks', 'status'], timeout=5)\n"
        "    subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=root, timeout=5)\n"
        "    subprocess.run(['git', 'diff', '--name-only', old, new], cwd=root, timeout=5)\n"
        "    subprocess.run(['git', 'diff', f'{revision}...HEAD', '--'], cwd=root, timeout=5)\n"
        "    subprocess.run(['git', 'describe', '--tags'], cwd=root, timeout=5)\n"),
    'tests/test_fixture_repository.py': (
        "import subprocess\n"
        "def test_status_of_a_private_repository(tmp_path):\n"
        "    subprocess.run(['git', 'status', '--porcelain'], cwd=tmp_path, check=True)\n"),
    'packages/kit/tests/test_fixture.py': (
        "import subprocess\n"
        "def test_diff_of_a_private_repository(tmp_path):\n"
        "    subprocess.run(['git', 'diff', 'HEAD'], cwd=tmp_path, check=True)\n"),
}


_RUNNERS = {
    'lib/runner.py': (
        "import subprocess\n"
        "def _run_git(args, timeout=30):\n"
        "    return subprocess.run(['git', *args], timeout=timeout)\n"),
}


def _synthetic_tree() -> _Tree:
    sources = {**_HISTORICAL, **_LOCK_FREE, **_RUNNERS}
    return _Tree(sources.get)


@pytest.mark.parametrize('path', sorted(_HISTORICAL))
def test_the_checker_names_index_locking_reads(path):
    assert _synthetic_tree().violations(path)


@pytest.mark.parametrize('path', sorted(_LOCK_FREE))
def test_the_checker_accepts_lock_free_reads(path):
    assert _synthetic_tree().violations(path) == []
