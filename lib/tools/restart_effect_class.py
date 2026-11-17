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

Only ``unknown`` needs its Coordinator's decision.

A stricter question decides whether a resume may run a cut call again without
the model reading about the cut (docs/RUN_CONTINUITY.md, "A restart-cut batch
the model simply issues again"): ``repeatable_restart_cut_batch`` admits a
batch only when every call in it is harmless to run twice: never started, a
``read_only``/``idempotent`` declaration, or a ``run_command`` whose every
segment only reads or is a test/check run confined to the isolated worktree.
``verifiable`` Git and file writes are not repeatable (a second ``git reset``
or ``mv`` changes state again).

Dependencies: lib.project_mod.command_analysis (pure parsers),
lib.tool_results (batch identity), lib.tools.registry (declared recovery).
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
        # A test run's writes are its own caches and reports: running it again
        # is harmless, unlike the other verifiable writes.
        return ('rerunnable' if confined else 'unknown'), cwd
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


def _segment_classes(command, cwd: str, worktree: str) -> list[str]:
    """Every segment's class (``rerunnable`` marks a confined test run); ``['unknown']`` on doubt."""
    from lib.project_mod import command_analysis as analysis
    if not isinstance(command, str) or not command.strip():
        return ['unknown']
    try:
        if analysis._git_guard_classify(command, require_known_segments=True) == 'readonly':
            return ['read_only']
        if analysis.command_segment_executes_substitution(command) or '<<' in command:
            return ['unknown']
        classes = []
        for segment in analysis._split_pipeline(command):
            verdict, cwd = _segment_class(segment, cwd, worktree)
            classes.append(verdict)
    except Exception:
        return ['unknown']  # A parser refusal proves nothing.
    return classes


def run_command_effect_class(command: str, cwd: str, worktree: str) -> str:
    """The effect class of one shell command run at ``cwd`` in ``worktree``."""
    classes = _segment_classes(command, cwd, worktree)
    if 'unknown' in classes:
        return 'unknown'
    return 'verifiable' if {'verifiable', 'rerunnable'} & set(classes) else 'read_only'


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


def _run_command_repeatable(arguments, worktree: str | None) -> bool:
    if isinstance(arguments, str):
        import json
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return False
    if not isinstance(arguments, Mapping):
        return False
    from lib.tools.project import RUN_COMMAND_DIRECTORY_KEYS
    directory = next((arguments[key] for key in RUN_COMMAND_DIRECTORY_KEYS
                      if isinstance(arguments.get(key), str) and arguments[key]), None)
    if not worktree:
        return run_command_effect_class(
            arguments.get('command'), _resolve('/', directory or '.'), '\0') == 'read_only'
    root = posixpath.normpath(worktree)
    cwd = _resolve(root, directory or root)
    if not _inside(cwd, root):
        return False
    return set(_segment_classes(arguments.get('command'), cwd, root)) <= {'read_only', 'rerunnable'}


def repeatable_cut_call(row: Mapping, *, worktree: str | None) -> bool:
    """Whether running this recorded call once more is harmless, whatever it already did."""
    effect = row.get('executionEffect')
    effect = effect if isinstance(effect, Mapping) else {}
    if effect.get('state') == 'not_started':
        return True
    name = str(row.get('toolName') or '')
    declared = effect.get('recovery')
    if not isinstance(declared, str) or not declared:
        from lib.tools.registry import declared_effect_recovery
        declared = declared_effect_recovery(name) or 'reconcile'
    if declared in ('read_only', 'idempotent'):
        return True
    return name == 'run_command' and _run_command_repeatable(row.get('toolArgs'), worktree)


#: Durable marker on a tool occurrence settled while a server restart or a
#: storage fence stopped its task: the model never read that result.
RESTART_CUT_FIELD = '_restartCut'


def _cut(row: Mapping) -> bool:
    from lib.tool_results.tool_round_replay import UNRECORDED_TOOL_RESULT_FIELD
    return (row.get(RESTART_CUT_FIELD) is True or row.get(UNRECORDED_TOOL_RESULT_FIELD) is True
            or not isinstance(row.get('toolContent'), str))


def repeatable_restart_cut_batch(rows, *, worktree: str | None) -> tuple[int, ...]:
    """Positions of the Turn's last tool batch when a restart cut it and every call is repeatable.

    ``rows`` are a Turn's tool-round occurrences, oldest first. The last batch
    (one model response's calls) qualifies when at least one of its calls was
    cut (``RESTART_CUT_FIELD``, announced but never recorded, or left without
    a result by a dead process) and every call is ``repeatable_cut_call``.
    Dropping exactly those occurrences ends the model's view at the request
    that produced the batch, so the resumed model issues it again through the
    normal gateway: it reads no cut wording, and its first request repeats the
    interrupted one. Empty otherwise. Display rows without a call identity stay.
    """
    from lib.tool_results.tool_round_identity import execution_batch_keys
    rows = list(rows) if isinstance(rows, (list, tuple)) else []
    calls = [index for index, row in enumerate(rows)
             if isinstance(row, Mapping) and isinstance(row.get('toolCallId'), str) and row['toolCallId']]
    if not calls:
        return ()
    keys = execution_batch_keys(rows)
    batch = tuple(index for index in calls if keys[index] == keys[calls[-1]])
    if not any(_cut(rows[index]) for index in batch):
        return ()
    if not all(repeatable_cut_call(rows[index], worktree=worktree) for index in batch):
        return ()
    return batch


def without_positions(rows, positions) -> list:
    dropped = set(positions)
    return [row for index, row in enumerate(rows) if index not in dropped]


__all__ = ['RESTART_CUT_FIELD', 'SAFE_CLASSES', 'cut_call_effect_class', 'repeatable_cut_call',
           'repeatable_restart_cut_batch', 'run_command_effect_class', 'without_positions']
