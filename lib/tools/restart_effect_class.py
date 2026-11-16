"""Effect class of a tool call a server restart cut off, for automatic recovery.

Entry: cut_call_effect_class(tool_name, arguments, *, declared, worktree).
Design authority: docs/RUN_CONTINUITY.md, "A restart-cut call is classified by
its declared effect". Pure: it reads only the call's recorded arguments and the
Worker's isolated worktree path, never the filesystem, a process or storage.

Classes, safest first:
- ``read_only`` / ``idempotent``: the tool's own declaration
  (``lib.tools.registry.declared_effect_recovery``), or for ``run_command`` a
  command every segment of which only reads (the Git guard's read-only verdict,
  the read-only utility set).
- ``verifiable``: a ``run_command`` confined to the Worker's own isolated
  worktree with no network side effect: a test run, a local Git write on the
  Worker's own branch, or a literal file utility whose every write target is
  inside the worktree. The resumed Worker re-checks that state itself.
- ``unknown``: everything else (any segment the parser cannot prove, a write or
  working directory outside the worktree, a push or other remote write, an
  arbitrary interpreter), and every call of a task without an isolated worktree
  except a ``run_command`` that only reads.

Only ``unknown`` needs its Coordinator's decision. Dependencies:
lib.project_mod.command_analysis (pure parsers).
"""
from __future__ import annotations

from collections.abc import Mapping
import os
import posixpath

SAFE_CLASSES = frozenset({'read_only', 'idempotent', 'verifiable'})

# Git subcommands that write only the worktree, its index and the checked-out
# branch of the Worker's own checkout. Every remote, shared-ref or repository-
# wide subcommand (push, fetch, pull, clone, remote, tag, branch, worktree,
# submodule, gc, config, update-ref, notes, replace, ...) is absent: it is
# admitted only when the Git guard proves it read-only.
_GIT_LOCAL_WRITES = frozenset({
    'add', 'commit', 'merge', 'rm', 'mv', 'restore', 'checkout', 'switch', 'reset',
    'stash', 'cherry-pick', 'rebase', 'revert', 'apply', 'am', 'clean',
})
# Test and check runners. Their writes are caches and reports of the run.
_TEST_PROGRAMS = frozenset({'pytest', 'py.test', 'vitest', 'jest', 'mocha', 'tox', 'nox'})
_TEST_TARGETS = ('test', 'check', 'lint', 'verify', 'typecheck')
_PACKAGE_RUNNERS = frozenset({'npm', 'pnpm', 'yarn'})
_PYTHON_TEST_MODULES = frozenset({'pytest', 'unittest'})
_PYTHON_TEST_SCRIPTS = frozenset({'scripts/verify.py'})
_FIND_EXECUTES = frozenset({'-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprint0', '-fprintf', '-fls'})


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip('/') + '/')


def _resolve(base: str, value: str) -> str:
    return posixpath.normpath(value if value.startswith('/') else posixpath.join(base, value))


def _python_test(words) -> bool:
    for index, word in enumerate(words[1:], 1):
        if word == '-m':
            return index + 1 < len(words) and words[index + 1] in _PYTHON_TEST_MODULES
        if not word.startswith('-'):
            return posixpath.normpath(word) in _PYTHON_TEST_SCRIPTS
    return False


def _test_run(words) -> bool:
    base = os.path.basename(words[0])
    if base in _TEST_PROGRAMS:
        return True
    if base.startswith('python'):
        return _python_test(words)
    operands = [word for word in words[1:] if not word.startswith('-')]
    if base == 'make':
        return bool(operands) and all(word.startswith(_TEST_TARGETS) for word in operands
                                      if '=' not in word)
    if base in _PACKAGE_RUNNERS:
        script = operands[1] if operands[:1] == ['run'] and len(operands) > 1 else operands[0] if operands else ''
        return script.startswith(_TEST_TARGETS)
    if base == 'npx':
        return bool(operands) and operands[0] in _TEST_PROGRAMS
    if base in ('cargo', 'go'):
        return bool(operands) and operands[0] in ('test', 'check', 'build', 'vet', 'clippy')
    if base == 'node':
        return '--test' in words
    return False


def _segment_class(segment: str, cwd: str, worktree: str) -> tuple[str, str]:
    """The class of one pipeline/chain segment and the working directory after it."""
    from lib.project_mod import command_analysis as analysis
    segment = analysis.peeled_command(segment.strip())
    if not segment:
        return 'read_only', cwd
    targets = analysis.command_segment_targets(segment)
    words = list(targets.words)
    while words and analysis._ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words.pop(0)
    words = analysis.unwrap_process_wrappers(words)
    if words and os.path.basename(words[0]) == 'env':
        words = words[1:]
        while words and analysis._ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words.pop(0)
        if words and words[0].startswith('-'):
            return 'unknown', cwd
    if not words:
        return 'read_only', cwd
    if any(word.startswith(('$', '`')) or '$(' in word for word in words):
        return 'unknown', cwd  # The program or an operand is computed at run time.
    base = os.path.basename(words[0])
    if base == 'cd':
        if len(words) != 2 or words[1].startswith('-'):
            return 'unknown', cwd
        moved = _resolve(cwd, words[1])
        return ('read_only', moved) if _inside(moved, worktree) else ('unknown', cwd)
    written = [_resolve(cwd, target) for target in targets.targets if target != '/dev/null']
    confined = all(_inside(path, worktree) for path in written)
    if base == 'git':
        parts = analysis.git_invocation_parts(words[1:])
        verdict = analysis._classify_git_invocation(words[1:], require_known_options=True)
        if verdict == 'readonly' and not written:
            return 'read_only', cwd
        if verdict in ('safe_write', 'tree_destroying') and parts and parts[0] in _GIT_LOCAL_WRITES and confined:
            return 'verifiable', cwd
        return 'unknown', cwd
    if _test_run(words):
        return ('verifiable' if confined else 'unknown'), cwd
    if not targets.complete:
        return 'unknown', cwd
    if base == 'find' and _FIND_EXECUTES & set(words[1:]):
        return 'unknown', cwd
    if base in analysis._READONLY_COMMANDS and not written and not analysis._is_destructive_command(segment):
        return 'read_only', cwd
    if base in _FILE_UTILITIES and written and confined:
        return 'verifiable', cwd
    return 'unknown', cwd


# Literal file utilities whose complete write set the target parser names.
_FILE_UTILITIES = frozenset({
    'mkdir', 'touch', 'cp', 'mv', 'rm', 'rmdir', 'ln', 'tee', 'sed', 'chmod', 'find', 'truncate',
    'cat', 'echo', 'printf', 'ls', 'grep', 'rg', 'head', 'tail', 'sort', 'jq', 'diff',
})


def run_command_effect_class(command: str, cwd: str, worktree: str) -> str:
    """The effect class of one shell command run at ``cwd`` in ``worktree``."""
    from lib.project_mod import command_analysis as analysis
    if not isinstance(command, str) or not command.strip():
        return 'unknown'
    try:
        if analysis._git_guard_classify(command, require_known_segments=True) == 'readonly':
            return 'read_only'
        if analysis.command_segment_executes_substitution(command) or '<<' in command:
            return 'unknown'
        classes = []
        for segment in analysis._split_pipeline(command):
            verdict, cwd = _segment_class(segment, cwd, worktree)
            classes.append(verdict)
    except Exception:
        return 'unknown'  # A parser refusal proves nothing.
    if 'unknown' in classes:
        return 'unknown'
    return 'verifiable' if 'verifiable' in classes else 'read_only'


def cut_call_effect_class(tool_name: str, arguments, *, declared: str, worktree: str | None) -> str:
    """The class of one cut call: ``read_only``, ``idempotent``, ``verifiable`` or ``unknown``.

    ``declared`` is the call's recorded effect-recovery declaration;
    ``arguments`` its recorded arguments (a JSON object or its text);
    ``worktree`` the Worker's isolated worktree, None outside one.
    """
    if declared in ('read_only', 'idempotent'):
        return declared
    if tool_name != 'run_command':
        return 'unknown'
    if isinstance(arguments, str):
        import json
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return 'unknown'
    if not isinstance(arguments, Mapping):
        return 'unknown'
    from lib.tools.project import RUN_COMMAND_DIRECTORY_KEYS
    directory = next((arguments[key] for key in RUN_COMMAND_DIRECTORY_KEYS
                      if isinstance(arguments.get(key), str) and arguments[key]), None)
    if not worktree:
        # Outside an isolated worktree only a command that reads is safe; no write is confined.
        verdict = run_command_effect_class(arguments.get('command'), _resolve('/', directory or '.'), '\0')
        return 'read_only' if verdict == 'read_only' else 'unknown'
    root = posixpath.normpath(worktree)
    cwd = _resolve(root, directory or root)
    if not _inside(cwd, root):
        return 'unknown'
    return run_command_effect_class(arguments.get('command'), cwd, root)


__all__ = ['SAFE_CLASSES', 'cut_call_effect_class', 'run_command_effect_class']
