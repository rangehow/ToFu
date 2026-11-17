"""Control flow and the starting environment of the delete guard's shell state.

Incident (2026-11-17): the run_command safety pre-hook refused

    printf … > "$TMPDIR/gh_hdr" 2>/dev/null || { d=$(mktemp -d);
    printf … > "$d/gh_hdr"; TMPDIR=$d; }; curl …; rm -f "$TMPDIR/gh_hdr"

as a delete of ``/gh_hdr``. The shell-state tracker read the parse as
straight-line code with one patch: an assignment whose own separator was
``||`` gained an empty candidate. Inside the ``{ … }`` group that marked
``d`` possibly empty, ``TMPDIR=$d`` inherited the empty value, and the
delete resolved to ``/gh_hdr``. The same model took every other conditional
assignment as certain, so ``test -d x && X=/tmp/a; rm -rf "$X/"``, an
``if``/``case`` branch, a loop body, a function body, ``break`` and
``continue`` all hid a delete of ``/``. It also expanded variables against
the server process environment instead of the environment the command's
shell starts with.

The guard now follows the shell's control flow
(``command_structure.control_structure``): a construct's paths run on their
own copies of the state and the state after it joins every path; loops
iterate to a fixpoint; functions run where they are called. Variables come
from the command's starting environment; a caller that does not know it (the
cwd-less pre-hook) leaves the operands that read it to the executor's pass.
"""

from __future__ import annotations

import pytest

from lib.project_mod.command_analysis import (
    _catastrophic_delete_detail,
    dangerous_command_rule,
    resolve_delete_targets,
)
from lib.project_mod.command_structure import (
    CaseClause,
    CaseEnd,
    control_structure,
    parse_shell_structure,
)

pytestmark = pytest.mark.unit

_RM = chr(114) + chr(109)
_WS = '/tmp/tofu_delguard_ws/sub'
_SCRATCH = {'TMPDIR': '/tmp/tofu-delguard-scratch/task'}

_REPORTED = (
    "printf 'Authorization: Bearer %s\\n' \"$GITHUB_TOKEN\" > \"$TMPDIR/gh_hdr\" "
    "2>/dev/null || { d=$(mktemp -d); printf 'Authorization: Bearer %s\\n' "
    "\"$GITHUB_TOKEN\" > \"$d/gh_hdr\"; TMPDIR=$d; }; curl -sS -m 30 "
    "-H @\"$TMPDIR/gh_hdr\" -H 'Accept: application/vnd.github+json' "
    "'https://api.github.com/repos/rangehow/ToFu/releases?per_page=8' | "
    "python3 -c '\nimport json,sys\nfor r in json.load(sys.stdin):\n"
    "    print(r[\"tag_name\"])\n'; curl -sSI -m 30 "
    "'https://api.github.com/repos/rangehow/ToFu/releases/latest' | "
    "grep -i -E '^(HTTP|x-ratelimit|retry-after)'; "
    f"{_RM} -f \"$TMPDIR/gh_hdr\"")


def _verdict(command, environment=None, cwd=_WS):
    return _catastrophic_delete_detail(
        command, cwd=cwd, environment={} if environment is None else environment)


# ═══════════════════════════════════════════════════════════════════════════
#  The reported command
# ═══════════════════════════════════════════════════════════════════════════

class TestReportedCommand:
    def test_allowed_when_the_command_starts_with_a_tmpdir(self):
        assert _verdict(_REPORTED, _SCRATCH) is None

    def test_refused_when_tmpdir_may_be_unset(self):
        # Without TMPDIR the first printf may write /gh_hdr, and the delete
        # then removes that top-level path: the refusal names the variable.
        detail = _verdict(_REPORTED, {})
        assert detail is not None and detail[1] == 'unproven'
        assert '$TMPDIR' in detail[2] and '/gh_hdr' in detail[2]

    def test_cwd_less_pre_hook_leaves_it_to_the_executor(self):
        assert _catastrophic_delete_detail(_REPORTED, environment=None) is None
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        assert _run_command_safety_hook(
            'run_command', {'command': _REPORTED}, {}) is None


# ═══════════════════════════════════════════════════════════════════════════
#  A path's assignments are certain on that path; after the construct every
#  path joins (former false positives)
# ═══════════════════════════════════════════════════════════════════════════

class TestBranchesJoinTheirPaths:
    @pytest.mark.parametrize('command', [
        f'T=/tmp/base; false || {{ X=/tmp/a; T=$X; }}; {_RM} -rf "$T/"',
        f'false || {{ d=$(mktemp -d); TMPDIR=$d; }}; {_RM} -f "$TMPDIR/x"',
        f'if [ -d /x ]; then D=/tmp/a; else D=/tmp/b; fi; {_RM} -rf "$D/"',
        f'if a; then D=/tmp/a; elif b; then D=/tmp/b; else D=/tmp/c; fi; '
        f'{_RM} -rf "$D/"',
        f'test -d /tmp/a && D=/tmp/a || D=/tmp/b; {_RM} -rf "$D/"',
        f'case "$1" in a) D=/tmp/a;; *) D=/tmp/b;; esac; {_RM} -rf "$D/"',
        f'for d in /tmp/a /tmp/b; do X=$d; done; {_RM} -rf "$X/"',
        f'f() {{ D=$(mktemp -d); }}; f; {_RM} -rf "$D/"',
        f'while read -r f; do {_RM} -f "$f"; done < list.txt',
    ])
    def test_allowed(self, command):
        assert _verdict(command, _SCRATCH) is None, command


# ═══════════════════════════════════════════════════════════════════════════
#  An assignment that may not run never replaces the earlier value
#  (former false negatives: each deletes / or /x in a real shell)
# ═══════════════════════════════════════════════════════════════════════════

class TestConditionalAssignmentsMayNotRun:
    @pytest.mark.parametrize('command', [
        f'test -d /nope && X=/tmp/a; {_RM} -rf "$X/"',
        f'false || {{ true; X=/tmp/a; }}; {_RM} -rf "$X/"',
        f'if false; then X=/tmp/a; fi; {_RM} -rf "$X/"',
        f'D=/; false || D=/tmp/a; {_RM} -rf "$D"',
        f'case $1 in a) X=/tmp/a;; esac; {_RM} -rf "$X/"',
        f'case $1 in a) X=/;& b) :;; esac; {_RM} -rf "$X"',
        f'X=/tmp/a && true & {_RM} -rf "$X/"',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    def test_the_value_on_a_certain_path_is_judged_as_written(self):
        # A literal candidate from one path is judged as itself: the refusal
        # names the shallow path, not an empty-variable collapse.
        detail = _verdict(f'D=/mnt; false || D=/tmp/a; {_RM} -rf "$D"')
        assert detail is not None and detail[1] == 'shallow'
        assert '/mnt' in detail[2]


class TestLoopsReachAFixpoint:
    @pytest.mark.parametrize('command', [
        f'X=/tmp/a; while true; do {_RM} -rf "$X/"; X=; done',
        f'while false; do X=/tmp/a; done; {_RM} -rf "$X/"',
        f'while true; do X=/; break; X=/tmp/a; done; {_RM} -rf "$X"',
        f'for i in 1 2; do X=/; continue; X=/tmp/a; done; {_RM} -rf "$X"',
        f'for i in 1; do for j in 2; do X=/; break 2; done; X=/tmp/a; done; '
        f'{_RM} -rf "$X"',
        f"for d in '/'; do {_RM} -rf \"$d\"; done",
        f'for d in /tmp/a ~; do {_RM} -rf "$d"; done',
        f'for d in $(ls); do X=/tmp/a; done; {_RM} -rf "$X/"',
        f'until false; do X=/tmp/a; done; {_RM} -rf "$X/"',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    def test_literal_list_runs_its_body(self):
        assert _verdict(
            f'for d in /tmp/a /tmp/b; do X=$d; done; {_RM} -rf "$X/"') is None

    @pytest.mark.parametrize('command', [
        f'n=; while [ "$n" != xx ]; do n=x$n; done; {_RM} -rf /tmp/a/b',
        f'X=/tmp; for i in 1 2; do X="$X/d"; done; {_RM} -rf "$X"',
        # The generated program that cycled forever when an overflowing value
        # set fell back to "dynamic" instead of staying overflowed.
        'n1=; while [ "$n1" != xx ]; do n1=x$n1; f0() { if true; then Y=/tmp/b; '
        'Y=/tmp/a; fi; if [ "$C" = a ]; then X=/mnt; X=/tmp/a; [ "$C" = b ]; fi; '
        'if test -n "$C"; then Y=; X=/tmp/b; X=/tmp/a; else Y=/; X=/; X="$X/x"; '
        f'fi; }}; X=$Y; done; ( X=/ ); {_RM} -rf /tmp/a/b',
    ])
    def test_values_that_keep_growing_reach_a_fixpoint(self, command):
        assert _verdict(command) is None


class TestFunctionsRunWhereTheyAreCalled:
    @pytest.mark.parametrize('command', [
        f'f() {{ X=/tmp/a; }}; {_RM} -rf "$X/"',
        f'X=/tmp/a; f() {{ X=/; }}; f; {_RM} -rf "$X"',
        f'D=/tmp/a; f() {{ {_RM} -rf "$D/"; }}; D=; f',
        f'X=/; f() {{ local X=/tmp/a; }}; f; {_RM} -rf "$X"',
        # `local` on one path only: the other path's global assignment stays.
        f'f() {{ if test -n "$C"; then local X=/tmp/a; fi; X=/; }}; f; {_RM} -rf "$X"',
        f'f() {{ X=/; return; X=/tmp/a; }}; f; {_RM} -rf "$X"',
        f'g() {{ {_RM} -rf /; }}',
        f'function g {{ {_RM} -rf /mnt; }}; g',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    def test_a_recursive_function_is_refused_when_the_command_deletes(self):
        detail = _verdict(f'f() {{ {_RM} -rf "$1"; f x; }}; f /tmp/a')
        assert detail is not None and detail[1] == 'unproven'
        assert 'calls itself' in detail[2]
        assert _verdict('f() { echo; f; }; f') is None


class TestDerivedValuesKeepTheirCollapse:
    """Found by differential fuzzing against dash: a value built from an
    unproven name collapsed to empty as a whole, so after ``E="$D/x"`` the
    operand ``"$E"`` was judged empty while the shell deletes ``/x``."""

    def test_derived_value_is_judged_by_its_collapse(self):
        detail = _verdict(f'D=$(some_cmd); E="$D/x"; {_RM} -rf "$E"')
        assert detail is not None and detail[1] == 'unproven'
        assert '$D' in detail[2] and '/x' in detail[2]

    @pytest.mark.parametrize('command', [
        f'E="$TOFU_DELGUARD_UNSET/x"; {_RM} -rf "$E"',
        f'D=$(some_cmd); E=$D/x; F="$E"; {_RM} -rf "$F"',
        f'D=$(some_cmd); cd "$D/x" && {_RM} -rf ..',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        f'D=$(some_cmd); E="$D/build/out"; {_RM} -rf "$E"',
        f'D=$(some_cmd); E="$D"; {_RM} -rf "$E"',
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command


# ═══════════════════════════════════════════════════════════════════════════
#  Variables come from the environment the command's shell starts with
# ═══════════════════════════════════════════════════════════════════════════

class TestStartingEnvironment:
    def test_set_variable_resolves_from_the_given_environment(self):
        assert _verdict(f'{_RM} -rf "$TMPDIR/x"', _SCRATCH) is None

    def test_unset_variable_may_expand_empty(self):
        detail = _verdict(f'{_RM} -rf "$TMPDIR/x"', {})
        assert detail is not None and detail[1] == 'unproven'

    def test_process_environment_is_not_consulted(self, monkeypatch):
        monkeypatch.setenv('TOFU_DELGUARD_DIR', '/tmp/tofu-delguard-env')
        assert _verdict(f'{_RM} -rf "$TOFU_DELGUARD_DIR/"', {}) is not None

    def test_process_environment_is_the_default(self, monkeypatch):
        monkeypatch.setenv('TOFU_DELGUARD_DIR', '/tmp/tofu-delguard-env')
        assert _catastrophic_delete_detail(
            f'{_RM} -rf "$TOFU_DELGUARD_DIR/"', cwd=_WS) is None
        assert resolve_delete_targets(
            f'{_RM} -rf "$TOFU_DELGUARD_DIR/x"', cwd=_WS) == [
                ('"$TOFU_DELGUARD_DIR/x"', ['/tmp/tofu-delguard-env/x'], set())]

    @pytest.mark.parametrize('command', [
        f'{_RM} -rf "$TMPDIR/x"',
        f'{_RM} -rf "$PWD/build"',
        f'D=$TMPDIR/x; {_RM} -rf "$D/.."',
        f'false || D=/tmp/a; {_RM} -rf "$D/x"',
    ])
    def test_unknown_environment_leaves_its_operands_to_the_executor(self, command):
        assert _catastrophic_delete_detail(command, environment=None) is None

    @pytest.mark.parametrize('command', [
        f'{_RM} -rf /mnt',
        f'D=/; {_RM} -rf "$D"',
        f'D=$(some_cmd); {_RM} -rf $D/',
        f'{_RM} -rf $HOME',
        f'{_RM} -rf "$TMPDIR" /mnt',
    ])
    def test_unknown_environment_still_judges_what_the_command_proves(self, command):
        assert _catastrophic_delete_detail(command, environment=None) is not None


class TestExecutorJudgesWithTheChildEnvironment:
    def _child_environment(self, monkeypatch, change):
        from lib.project_mod import run_command
        build = run_command._get_cmd_env

        def child_environment(*args, **kwargs):
            environment = build(*args, **kwargs)
            change(environment)
            return environment
        monkeypatch.setattr(run_command, '_get_cmd_env', child_environment)
        monkeypatch.setattr(run_command, '_RM_TRASH_ENABLED', False)

    def test_variable_only_the_child_has_is_resolved(self, monkeypatch, tmp_path):
        from lib.project_mod.run_command import tool_run_command
        monkeypatch.delenv('TOFU_DELGUARD_SCRATCH', raising=False)
        self._child_environment(monkeypatch, lambda environment: environment.update(
            TOFU_DELGUARD_SCRATCH=str(tmp_path)))
        result = tool_run_command(
            str(tmp_path), f'{_RM} -f "$TOFU_DELGUARD_SCRATCH/probe"')
        assert 'blocked for safety' not in result
        assert '[exit code: 0]' in result

    def test_variable_the_child_lacks_is_refused(self, monkeypatch, tmp_path):
        from lib.project_mod.run_command import tool_run_command

        def _tripwire(*_args, **_kwargs):
            raise RuntimeError('subprocess spawned for a refused command')
        monkeypatch.setenv('TOFU_DELGUARD_SCRATCH', str(tmp_path))
        self._child_environment(monkeypatch, lambda environment: environment.pop(
            'TOFU_DELGUARD_SCRATCH', None))
        monkeypatch.setattr('subprocess.Popen', _tripwire)
        result = tool_run_command(
            str(tmp_path), f'{_RM} -rf "$TOFU_DELGUARD_SCRATCH/"')
        assert result.startswith('Error: Command blocked for safety')
        assert '$TOFU_DELGUARD_SCRATCH' in result


# ═══════════════════════════════════════════════════════════════════════════
#  The structure the guard follows
# ═══════════════════════════════════════════════════════════════════════════

def _read(source):
    return control_structure(parse_shell_structure(source))


def _first_command(source):
    return _read(source)[0].pipelines[0][1].commands[0]


class TestControlStructure:
    def test_and_or_lists_pipelines_and_background(self):
        first, second = _read('a && b || c | d & e')
        assert [operator for operator, _ in first.pipelines] == ['', '&&', '||']
        assert len(first.pipelines[2][1].commands) == 2
        assert first.background and not second.background

    @pytest.mark.parametrize('source, kind', [
        ('{ a; }', 'group'),
        ('if a; then b; elif c; then d; else e; fi', 'if'),
        ('while a; do b; done', 'while'),
        ('until a; do b; done', 'until'),
        ('for x in a b; do c; done', 'for'),
        ('for x do c; done', 'for'),
        ('for ((i=0; i<2; i++)); do c; done', 'for'),
        ('for x in a; { c; }', 'for'),
        ('select x in a; do c; done', 'for'),
        ('case x in a) b;; esac', 'case'),
        ('f() { a; }', 'function'),
        ('function f { a; }', 'function'),
        ('function f\n{\n a\n}', 'function'),
        ('time { a; }', 'group'),
        ('! { a; }', 'group'),
    ])
    def test_compound_commands(self, source, kind):
        assert _first_command(source).kind == kind

    @pytest.mark.parametrize('source', [
        'fi', 'if a; then b', '{ a', 'done', 'a; }', 'while a; do b; fi',
        '{echo;}',
    ])
    def test_reserved_words_that_do_not_pair_up_are_unreadable(self, source):
        assert _read(source) is None

    def test_case_clauses_are_marked(self):
        items = parse_shell_structure(
            'case x in a|*) b;;& c) d;& e) f;; esac > out')
        markers = [item for item in items if isinstance(item, (CaseClause, CaseEnd))]
        assert [type(item) for item in markers] == [
            CaseClause, CaseClause, CaseClause, CaseEnd]
        assert [item.separator for item in markers] == ['', ';;&', ';&', ';;']
        assert markers[0].patterns == ('a', '*')
        assert markers[-1].redirects == (('>', 'out'),)

    @pytest.mark.parametrize('command, refused', [
        ('case x in a) reboot;; esac', True),
        ('case x in reboot) :;; esac', False),
        ('case x in a) :;; esac > /dev/sda', True),
    ])
    def test_dangerous_guard_reads_case_markers_as_before(self, command, refused):
        assert (dangerous_command_rule(command) is not None) is refused

    def test_case_pattern_substitutions_run(self):
        assert _verdict(f'case x in $({_RM} -rf /)) :;; esac') is not None
