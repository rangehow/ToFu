"""The delete guard's candidates contain what a real shell computes.

A differential specification of the guard's shell-state model. Seeded
scripts run in dash and bash as ``sh -c SCRIPT sh ARG…``, with ``printf`` in
place of the delete. They combine literal and derived assignments,
``&&``/``||``, groups, ``if`` and ``case`` branches, ``for``/``while`` loops
with ``break``/``continue``, functions called with arguments (``local``,
``return``), subshells, pipelines, the positional parameters (``set --``,
``shift``), ``unset``, ``eval``, the ``${…}`` operators and the command
substitutions whose output the guard knows. For each run, ``/r/$X`` and
``/r/$Z`` with the shell's values must be among the guard's candidates for
``rm -rf "/r/$X"`` and ``rm -rf "/r/$Z"`` in the same ``sh -c`` command. The
scripts only assign, test and print.

Generated runs found gaps the hand-written specs missed: a value built from
an unproven name collapsed to empty as a whole (``E="$D/x"`` hid ``/x``), a
loop whose value set kept growing never reached a fixpoint, and a ``local``
run on only some paths of a function body.

A second property covers what runs the delete: seeded scripts reach ``rm``
through program words (``$R``, ``$E rm``, ``command``, ``env``), ``"$@"`` in
wrapper functions, aliases, a shell reading ``echo``/``printf`` output or a
``-c`` string, and brace words, in dash, ``bash --posix`` (which expand
aliases) and bash (which does not, nor dash braces). The scripts run with a
PATH holding only a ``rm`` that prints its operands: every operand it
receives must be among the guard's candidates for the same script.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess

import pytest

from lib.project_mod.command_analysis import resolve_delete_targets

pytestmark = pytest.mark.unit

_SHELLS = [shell for shell in ('dash', 'bash') if shutil.which(shell)]
# X holds at most these 3 literals, empty, and 4 values built from Y; Z at
# most 7 values derived from Y and the positional parameters: never more
# candidates than the guard's bound, so no value set overflows.
_LITERALS = ('/tmp/a', '/', '/mnt')
_DERIVED = (
    'Z=${Y%/*}', 'Z=${Y:-/tmp/b}', 'Z="${Y:-/tmp/b}"', 'Z=$(dirname "$Y")',
    'Z=$(echo $Y)', 'Z="$(printf %s "$Y")"', 'Z=$1', 'Z="$2"', 'Z=', 'unset Z',
    'eval "Z=\\$Y"', 'eval "Z=$Y"', 'eval Z=\\"\\$1\\"', ': "${Z:=/tmp/a}"',
    'Z=$(cd /tmp && pwd)',
)
_POSITIONAL = ('set -- /tmp/a /mnt', 'set -- /', 'set --',
               'if [ $# -gt 0 ]; then shift; fi')
_CONDITIONS = ('true', 'false', 'test -n "$C"', '[ "$C" = a ]', '[ "$C" = b ]')


class _Script:
    """One seeded script; ``loop``/``function`` say what may jump where."""

    def __init__(self, seed):
        self.random = random.Random(seed)
        self.counters = 0
        self.functions = []

    def simple(self):
        """An assignment of X or Y."""
        pick = self.random
        if pick.random() < 0.3:
            return f'Y={pick.choice(_LITERALS + ("",))}'
        roll = pick.random()
        if roll < 0.55:
            return f'X={pick.choice(_LITERALS)}'
        if roll < 0.7:
            return 'X='
        return 'X=$Y' if roll < 0.85 else 'X="$Y/x"'

    def assignment(self):
        pick = self.random
        roll = pick.random()
        if roll < 0.35:
            return pick.choice(_DERIVED)
        if roll < 0.45:
            return pick.choice(_POSITIONAL)
        if roll < 0.5:
            return pick.choice(('unset Y', 'unset X'))
        return self.simple()

    def commands(self, depth, loop=False, function=False):
        return '; '.join(self.command(depth, loop, function)
                         for _ in range(self.random.randint(1, 3)))

    def command(self, depth, loop=False, function=False):
        pick, inner = self.random, depth - 1
        if depth <= 0:
            return self.assignment() if pick.random() < 0.7 else pick.choice(_CONDITIONS)
        roll = pick.random()
        body = lambda: self.commands(inner, loop, function)  # noqa: E731
        if roll < 0.25:
            return self.assignment()
        if roll < 0.32:
            return f'{{ {body()}; }}'
        if roll < 0.42:
            text = f'if {pick.choice(_CONDITIONS)}; then {body()}; '
            if pick.random() < 0.3:
                text += f'elif {pick.choice(_CONDITIONS)}; then {body()}; '
            if pick.random() < 0.5:
                text += f'else {body()}; '
            return text + 'fi'
        if roll < 0.52:
            operator = pick.choice(['&&', '||'])
            return (f'{self.command(inner, loop, function)} {operator} '
                    f'{self.command(inner, loop, function)}')
        if roll < 0.58:
            words = ' '.join(pick.sample(_LITERALS, pick.randint(1, 3)))
            return f'for v in {words}; do {self.commands(inner, True, function)}; done'
        if roll < 0.64:
            self.counters += 1
            counter = f'n{self.counters}'
            return (f'{counter}=; while [ "${counter}" != xx ]; do '
                    f'{counter}=x${counter}; {self.commands(inner, True, function)}; done')
        if roll < 0.70:
            return (f'case "$C" in a) {body()};; b) {body()};; '
                    f'{pick.choice(["*", "c"])}) {body()};; esac')
        if roll < 0.76 and loop:
            return f'{pick.choice(_CONDITIONS)} && {pick.choice(["break", "continue"])}'
        if roll < 0.80 and function:
            return pick.choice([f'local {self.simple()}',
                                f'{pick.choice(_CONDITIONS)} && return'])
        if roll < 0.86:
            name = f'f{len(self.functions)}'
            text = f'{name}() {{ {self.commands(inner, False, True)}; }}'
            self.functions.append(name)  # defined after its body: no recursion
            return text
        if roll < 0.92 and self.functions:
            arguments = ' '.join(pick.sample(_LITERALS, pick.randint(0, 2)))
            return f'{pick.choice(self.functions)} {arguments}'.rstrip()
        if roll < 0.95:
            return f'( {body()} )'
        if roll < 0.98:
            return f'{self.command(inner, loop, function)} | cat'
        return f'{self.command(inner, loop, function)} & wait'


def _quoted(text):
    return "'" + text.replace("'", "'\\''") + "'"


_RUNNERS = [[shutil.which(argv[0]), *argv[1:]] for argv in (['dash'], ['bash', '--posix'], ['bash'])
            if shutil.which(argv[0])]
_VALUES = ('/', '/tmp/a/b', '/mnt', '')
# P never empties: a program word alone would run X.
_PROGRAM_SETUP = (
    'R=rm', 'R=ls', 'R=', 'E=', 'E=command', 'P="rm -rf"', 'P="ls -d"',
    "alias a='rm -rf '", "alias a='ls '", "alias a='command '", 'alias b=rm',
    'alias b=ls', "alias b='rm -rf'",
    'run() { "$@"; }', 'wrap() { $R "$@"; }', 'del() { rm -rf "$@"; }',
)
# {x} is the operand word, {u} its text inside double quotes.
_PROGRAM_DELETES = (
    '$R -rf {x}', '$P {x}', 'run rm -rf {x}', 'run $R -rf {x}', 'run del {x}',
    'wrap -rf {x}', 'a {x}', 'b -rf {x}', 'a b {x}', '$E rm -rf {x}', '$E $R -rf {x}',
    'env $R -rf {x}', 'echo "rm -rf {u}" | sh', 'printf "%s\\n" "rm -rf {u}" | sh',
    'sh -c \'rm -rf "$1"\' sh {x}', 'S=$R; $S -rf {x}', 'rm -rf {x}{{/x,}}',
)


def _delete_line(pick, marks):
    """A delete whose operand ends in a mark of its own, so only its own
    events can account for what it hands rm."""
    marks.append(f'$X/k{len(marks)}')
    return pick.choice(_PROGRAM_DELETES).format(x=f'"{marks[-1]}"', u=marks[-1])


def _program_lines(pick, depth, marks):
    lines = []
    for _ in range(pick.randint(1, 3)):
        roll = pick.random()
        if roll < 0.2 and depth:
            text = f'if {pick.choice(_CONDITIONS)}; then\n{_program_lines(pick, depth - 1, marks)}\n'
            if pick.random() < 0.5:
                text += f'else\n{_program_lines(pick, depth - 1, marks)}\n'
            lines.append(text + 'fi')
        elif roll < 0.4:
            lines.append(f'X={pick.choice(_VALUES)}')
        elif roll < 0.8:
            lines.append(pick.choice(_PROGRAM_SETUP))
        else:
            lines.append(_delete_line(pick, marks))
    return '\n'.join(lines)


@pytest.fixture(scope='module')
def fake_rm_path(tmp_path_factory):
    """A PATH holding only a ``rm`` that prints its operands, a no-op ``ls``
    and the ``sh`` and ``env`` the scripts start: no real delete can run."""
    directory = tmp_path_factory.mktemp('fake-bin')
    (directory / 'rm').write_text('#!/bin/sh\nfor a do printf "<%s>\\n" "$a"; done\n')
    (directory / 'ls').write_text('#!/bin/sh\n:\n')
    for name in ('rm', 'ls'):
        (directory / name).chmod(0o755)
    (directory / 'sh').symlink_to(shutil.which('dash') or shutil.which('bash'))
    (directory / 'env').symlink_to(shutil.which('env'))
    return str(directory)


@pytest.mark.skipif(not _RUNNERS, reason='no POSIX shell to compare with')
@pytest.mark.parametrize('seed', range(80))
def test_guard_candidates_contain_what_the_shell_hands_rm(seed, fake_rm_path, tmp_path):
    pick, marks = random.Random(seed), []
    script = ('R=rm; P="ls -d"; E=; X=/tmp/a/b\n' + _program_lines(pick, 2, marks)
              + '\n' + _delete_line(pick, marks))
    assert '/bin' not in script  # every program comes from the fake PATH
    for argv in _RUNNERS:
        for choice in ('', 'a', 'b'):
            environment = {'PATH': fake_rm_path, 'C': choice}
            run = subprocess.run([*argv, '-c', script], cwd=tmp_path, env=environment,
                                 capture_output=True, text=True, timeout=30, check=False)
            operands = [line[1:-1] for line in run.stdout.splitlines()
                        if line.startswith('<') and line.endswith('>')]
            candidates = {path for _operand, paths, _unproven in resolve_delete_targets(
                script, str(tmp_path), environment=environment) for path in paths or ()}
            for operand in operands:
                if operand and not operand.startswith('-'):
                    path = os.path.normpath(os.path.join(tmp_path, operand))
                    assert path in candidates, (
                        argv, choice, operand, sorted(candidates), script)


@pytest.mark.skipif(not _SHELLS, reason='no POSIX shell to compare with')
@pytest.mark.parametrize('seed', range(120))
def test_guard_candidates_contain_the_shell_value(seed):
    script = _Script(seed)
    body = script.commands(script.random.randint(1, 3))
    start = {} if seed % 2 else {'X': script.random.choice(_LITERALS)}
    arguments = list(script.random.sample(_LITERALS, script.random.randint(0, 2)))
    for shell in _SHELLS:
        for choice in ('', 'a', 'b'):
            environment = {'PATH': os.defpath, 'C': choice, **start}
            run = subprocess.run(
                [shell, '-c', body + '\nprintf "<%s|%s>" "$X" "$Z"', 'sh', *arguments],
                env=environment, capture_output=True, text=True, timeout=30, check=False)
            assert run.returncode == 0 and run.stdout.endswith('>'), (shell, body, run.stderr)
            values = run.stdout[run.stdout.rfind('<') + 1:-1].split('|')
            for name, actual in zip('XZ', values):
                command = ' '.join(['sh -c', _quoted(f'{body}\nrm -rf "/r/${name}"'), 'sh',
                                    *arguments])
                operand, paths, unproven = resolve_delete_targets(
                    command, '/tmp/tofu_delguard_ws/sub', environment=environment)[-1]
                assert paths is not None, (name, body)
                assert os.path.normpath('/r/' + actual) in paths, (
                    shell, choice, start, arguments, name, actual, paths, unproven, body)
