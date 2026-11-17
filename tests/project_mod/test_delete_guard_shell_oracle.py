"""The delete guard's candidates contain what a real shell computes.

A differential specification of the guard's shell-state model. Seeded
scripts of literal and derived assignments, ``&&``/``||``, groups, ``if`` and
``case`` branches, ``for``/``while`` loops with ``break``/``continue``,
functions with ``local``/``return``, subshells and pipelines run in dash and
bash with ``printf`` in place of the delete. For every run, the value the
shell gives ``X`` must be a candidate of the guard's ``rm -rf "$X"``, and an
empty value must be one the guard judges (``X`` among its possibly-empty
names). The scripts only assign, test and print.

Generated runs found two gaps the hand-written specs missed: a value built
from an unproven name collapsed to empty as a whole (``E="$D/x"`` hid ``/x``),
and a loop whose value set kept growing never reached a fixpoint.
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
# X can hold at most these 3 literals, empty, and 4 values built from Y: never
# more candidates than the guard's bound, so no value set ever overflows.
_LITERALS = ('/tmp/a', '/', '/mnt')
_CONDITIONS = ('true', 'false', 'test -n "$C"', '[ "$C" = a ]', '[ "$C" = b ]')


class _Script:
    """One seeded script; ``loop``/``function`` say what may jump where."""

    def __init__(self, seed):
        self.random = random.Random(seed)
        self.counters = 0
        self.functions = []

    def assignment(self):
        pick = self.random
        if pick.random() < 0.3:
            return f'Y={pick.choice(_LITERALS + ("",))}'
        roll = pick.random()
        if roll < 0.55:
            return f'X={pick.choice(_LITERALS)}'
        if roll < 0.7:
            return 'X='
        return 'X=$Y' if roll < 0.85 else 'X="$Y/x"'

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
            return pick.choice([f'local {self.assignment()}',
                                f'{pick.choice(_CONDITIONS)} && return'])
        if roll < 0.86:
            name = f'f{len(self.functions)}'
            text = f'{name}() {{ {self.commands(inner, False, True)}; }}'
            self.functions.append(name)  # defined after its body: no recursion
            return text
        if roll < 0.92 and self.functions:
            return pick.choice(self.functions)
        if roll < 0.95:
            return f'( {body()} )'
        if roll < 0.98:
            return f'{self.command(inner, loop, function)} | cat'
        return f'{self.command(inner, loop, function)} & wait'


@pytest.mark.skipif(not _SHELLS, reason='no POSIX shell to compare with')
@pytest.mark.parametrize('seed', range(80))
def test_guard_candidates_contain_the_shell_value(seed):
    script = _Script(seed)
    body = script.commands(script.random.randint(1, 3))
    start = {} if seed % 2 else {'X': script.random.choice(_LITERALS)}
    for shell in _SHELLS:
        for choice in ('', 'a', 'b'):
            environment = {'PATH': os.defpath, 'C': choice, **start}
            run = subprocess.run(
                [shell, '-c', body + '\nprintf "<%s>" "$X"'], env=environment,
                capture_output=True, text=True, timeout=30, check=False)
            assert run.returncode == 0 and run.stdout.endswith('>'), (shell, body, run.stderr)
            actual = run.stdout[run.stdout.rfind('<') + 1:-1]
            operand, paths, unproven = resolve_delete_targets(
                body + '\nrm -rf "$X"', '/tmp/tofu_delguard_ws/sub',
                environment=environment)[-1]
            assert operand == '"$X"' and paths is not None, body
            judged = (os.path.normpath(actual) in paths) if actual else 'X' in unproven
            assert judged, (shell, choice, start, actual, paths, unproven, body)
