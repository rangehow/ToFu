"""Shell semantics the delete guard follows beyond control flow.

Each case below was either a hidden delete of a shallow path or a refusal of
a scoped one before the guard expanded words the way the shell does
(``command_analysis._expand_word``) and gave builtins their real effect:

* a ``trap`` action runs later in the same shell, with the state reached
  after the trap was set;
* a child shell (``sh -c``, ``bash <<EOF``) sees the exported names, the
  variables put in its environment (``D=/ sh -c``, ``env``), the functions of
  ``export -f``, its own ``$0`` and positional parameters;
* the positional parameters are set by a function call, ``set --`` and
  ``shift``;
* ``unset`` empties a name; ``set``, ``read``, ``printf -v`` change only
  what they change; ``eval`` runs the text its operands expand to;
* ``${…}`` operators, field splitting and pathname expansion of unquoted
  values, ``$'…'``, ``$((…))`` and substitutions whose output is known;
* ``cd`` to a directory the guard cannot know loses the working directory,
  ``$HOME``, ``~``, ``$PWD``, ``$OLDPWD`` and ``$CDPATH`` count;
* a program word the shell expands (``$RM -rf /``, ``"$@"`` in a wrapper
  function, ``sudo $RM``, ``$EMPTY rm``, ``/bin/r?``) runs the program its
  first field names;
* an alias (dash and ``bash --posix`` expand them in scripts) runs its text;
* a shell runs its ``-c`` string however its name is quoted, and reads what
  ``echo``/``printf`` print into its pipe, with the environment of its own
  pipeline stage;
* bash makes a word of each variant of a brace expression.
"""

from __future__ import annotations

import pytest

from lib.project_mod.command_analysis import (
    _catastrophic_delete_detail,
    dangerous_command_rule,
)

pytestmark = pytest.mark.unit

_RM = chr(114) + chr(109)
_WS = '/tmp/tofu_delguard_ws/sub'


def _verdict(command, environment=None, cwd=_WS):
    return _catastrophic_delete_detail(
        command, cwd=cwd, environment={} if environment is None else environment)


def _hook(command):
    """The cwd-less safety pre-hook's judgement (no environment either)."""
    return _catastrophic_delete_detail(command, environment=None)


class TestTrapActions:
    @pytest.mark.parametrize('command', [
        f"trap '{_RM} -rf /' EXIT",
        f"trap -- '{_RM} -rf /mnt' INT TERM",
        f'X=/tmp/a; trap \'{_RM} -rf "$X"\' EXIT; X=/',
        f"( trap '{_RM} -rf /' EXIT; true )",
        f'trap "{_RM} -rf $HOME" EXIT',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command
        assert _hook(command) is not None, command

    def test_a_trap_runs_with_any_later_state(self):
        # When the trap is set, D still holds its starting value, which only
        # the executor knows: the pre-hook leaves the command to it.
        command = f'trap cleanup EXIT; cleanup() {{ {_RM} -rf "$D"; }}; D=/'
        assert _verdict(command) is not None
        assert _hook(command) is None

    @pytest.mark.parametrize('command', [
        f'X=/tmp/a; trap "{_RM} -rf $X" EXIT; X=/',
        f"trap '{_RM} -rf /tmp/a/b' EXIT",
        'trap - EXIT', "trap '' INT", 'trap',
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command

    def test_dangerous_guard_reads_trap_actions(self):
        assert dangerous_command_rule("trap 'reboot' EXIT") is not None
        assert dangerous_command_rule("trap 'echo done' EXIT") is None


class TestChildShells:
    @pytest.mark.parametrize('command', [
        f"export D=/; sh -c '{_RM} -rf \"$D\"'",
        f"declare -x D=/; bash -c '{_RM} -rf \"$D\"'",
        f"D=/; export D; sh -c '{_RM} -rf \"$D\"'",
        f"set -a; D=/; sh -c '{_RM} -rf \"$D\"'",
        f"D=/ sh -c '{_RM} -rf \"$D\"'",
        f"env D=/mnt bash -c '{_RM} -rf \"$D\"'",
        f"D=/ bash <<'EOF'\n{_RM} -rf \"$D\"\nEOF",
        f"sh -c '{_RM} -rf \"$1\"' sh /",
        f"sh -c 'for f in \"$@\"; do {_RM} -rf \"$f\"; done' sh /tmp/a /",
        f"sh -c '{_RM} -rf \"$0\"' /",
        f'f() {{ {_RM} -rf "$1"; }}; export -f f; bash -c \'f /\'',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        f"D=/; sh -c '{_RM} -rf \"$D\"'",
        f"export D=/; export -n D; sh -c '{_RM} -rf \"$D\"'",
        f"sh -c '{_RM} -rf \"$1\"' sh /tmp/a/b",
        f'f() {{ {_RM} -rf "$1"; }}; bash -c \'f /\'',
        f"env -u D sh -c '{_RM} -rf \"$D\"'",
    ])
    def test_allowed(self, command):
        assert _verdict(command, {'D': '/'} if 'env -u' in command else None) is None, command

    def test_the_cwd_less_pre_hook_judges_what_the_command_proves(self):
        assert _hook(f"export D=/; sh -c '{_RM} -rf \"$D\"'") is not None
        assert _hook(f"sh -c '{_RM} -rf \"$1\"' sh /") is not None


class TestPositionalParameters:
    @pytest.mark.parametrize('command', [
        f'f() {{ {_RM} -rf "$1"; }}; f /',
        f'f() {{ {_RM} -rf "$2"; }}; f /tmp/a /mnt',
        f'f() {{ shift; {_RM} -rf "$1"; }}; f /tmp/a /',
        f'f() {{ for d in "$@"; do {_RM} -rf "$d"; done; }}; f /tmp/a /',
        f'f() {{ {_RM} -rf "$@"; }}; f /tmp/a /',
        f'set -- /; {_RM} -rf "$1"',
        f'set -- /tmp/a /; shift; {_RM} -rf "$1"',
        f'f() {{ {_RM} -rf "$1/"; }}; f "$X"',
        f'f() {{ {_RM} -rf "$*"; }}; f /',
        f'{_RM} -rf "$1/"',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        f'f() {{ {_RM} -rf "$1"; }}; f /tmp/a/b',
        f'f() {{ {_RM} -rf "$1"; }}; f',
        f'f() {{ {_RM} -rf "$1"; }}; set -- /; f /tmp/a',
        f'set -- /tmp/a; f() {{ :; }}; f /; {_RM} -rf "$1"',
        f'for d in "$@"; do {_RM} -rf "$d"; done',
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command

    def test_unknown_parameters_spliced_into_more_text(self):
        # "$@" of unknown length followed by an unknown value in one word.
        assert _verdict(f'f() {{ {_RM} -rf "$@"$(cmd)/; }}; f $(cmd)') is not None
        assert _verdict(f'f() {{ {_RM} -rf "$@"$(cmd); }}; f $(cmd)') is None


class TestBuiltinEffects:
    @pytest.mark.parametrize('command', [
        f'D=/tmp/x; set -e; {_RM} -rf "$D/"',
        f'D=/tmp/x; set -euo pipefail; {_RM} -rf "$D/"',
        f'D=/tmp/x; read -r L < f; {_RM} -rf "$D/"',
        f'D=/tmp/x; printf -v Y %s z; {_RM} -rf "$D/"',
        f'D=/tmp/x; let i=1; {_RM} -rf "$D/"',
        f'D=/tmp/x; while read -r f; do echo "$f"; done < list; {_RM} -rf "$D/"',
        f'D=/tmp/x; typeset -i n=1; {_RM} -rf "$D/"',
    ])
    def test_a_builtin_changes_only_what_it_changes(self, command):
        assert _verdict(command) is None, command

    @pytest.mark.parametrize('command', [
        f'D=/tmp/x; unset D; {_RM} -rf "$D/"',
        f'unset TMPDIR; {_RM} -rf "$TMPDIR/x"',
        f'read -r D < f; {_RM} -rf "$D/"',
        f'D=/tmp/x; printf -v D %s ""; {_RM} -rf "$D/"',
        f'D=/tmp/x; getopts ab D; {_RM} -rf "$D/"',
        f'D=/tmp/x; mapfile -t D < f; {_RM} -rf "$D/"',
        f'D=/tmp/x; source ./env.sh; {_RM} -rf "$D/"',
    ])
    def test_refused(self, command):
        assert _verdict(command, {'TMPDIR': '/tmp/scratch/a'}) is not None, command


class TestEval:
    @pytest.mark.parametrize('command', [
        f'D=/; eval "{_RM} -rf $D"',
        f'D=/; eval \'{_RM} -rf "$D"\'',
        f'eval "$(echo {_RM} -rf /)"',
        f'C="{_RM} -rf /mnt"; eval "$C"',
        f'eval {_RM} -rf /',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        f'D=/tmp/a; eval "{_RM} -rf $D"',
        'D=/; eval "echo $D"',
        'eval "X=/"',
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command


class TestWordExpansion:
    @pytest.mark.parametrize('command', [
        f'{_RM} -rf "${{D:-/}}"',
        f'D=/mnt/x; {_RM} -rf "${{D%/*}}"',
        f': "${{D:=/}}"; {_RM} -rf "$D"',
        f'D=/tmp/a; {_RM} -rf "${{D:+/}}"',
        f'D=/tmp/x; {_RM} -rf "${{D/x/}}"',
        f'D=x; {_RM} -rf "/${{D^^}}"',
        f'D=/tmp/a; {_RM} -rf "${{D#/tmp}}/.."',
        f'D="/ /tmp"; {_RM} -rf $D',
        f"D='/mnt/*'; {_RM} -rf $D",
        f'IFS=:; D=/:/tmp; {_RM} -rf $D',
        f"{_RM} -rf $'/'",
        f"cd $'\\x2f' && {_RM} -rf mnt",
        f'{_RM} -rf "$(echo /)"',
        f'{_RM} -rf "$(printf %s /mnt)"',
        f'{_RM} -rf "$(cd /mnt && pwd)/"',
        f'{_RM} -rf "$(dirname /mnt/x)"',
        f'{_RM} -rf "$(basename /x)"/../..',
        f'D=$(cmd); {_RM} -rf "$(dirname "$D/x")"',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        f'{_RM} -rf "${{BUILD:?}}/"',
        f'BUILD=$(make_dir); {_RM} -rf "${{BUILD:?}}/"',
        f'{_RM} -rf "${{OUT:-/tmp/out}}/build"',
        f'D=/tmp/a/b; {_RM} -rf "${{D%/*}}"',
        f'D="/tmp/a /tmp/b"; {_RM} -rf $D',
        f'D="/tmp/a /tmp/b"; {_RM} -rf "$D"',
        f"D='/mnt/*'; {_RM} -rf \"$D\"",
        f'i=1; {_RM} -rf /tmp/$((i))',
        f'{_RM} -rf "$(dirname "$0")/build"',
        f'{_RM} -rf "/tmp/build-$$"',
        f'{_RM} -rf "$(basename /x/y)"',
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command

    def test_git_toplevel_is_the_checkout_holding_the_directory(self, tmp_path):
        checkout = tmp_path / 'repo'
        (checkout / '.git').mkdir(parents=True)
        (checkout / 'sub').mkdir()
        command = f'{_RM} -rf "$(git rev-parse --show-toplevel)/build"'
        assert _verdict(command, cwd=str(checkout / 'sub')) is None
        outside = tmp_path / 'plain'
        outside.mkdir()
        assert _verdict(command, cwd=str(outside)) is not None  # prints nothing
        assert _verdict(command, {'GIT_DIR': '/x'}, cwd=str(checkout / 'sub')) is not None


class TestWorkingDirectory:
    @pytest.mark.parametrize('command', [
        f'cd "$(cmd)" && {_RM} -rf mnt',
        f'X=$(cmd); cd "$X" && {_RM} -rf mnt',
        f'cd "$(dirname "$0")" && {_RM} -rf build',
        f'pushd / && {_RM} -rf mnt',
        f'pushd /tmp/a && popd && {_RM} -rf mnt',
        f'cd / && cd - && {_RM} -rf mnt',
        f'CDPATH=/; cd tmp && {_RM} -rf x',
        f'HOME=/; cd && {_RM} -rf mnt',
        f'HOME=/; {_RM} -rf ~/mnt',
        f'HOME=/; {_RM} -rf "$HOME/mnt"',
        f'PWD=/; {_RM} -rf "$PWD/mnt"',
        f'cd /tmp/a; {_RM} -rf "$OLDPWD/.."',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        f'cd /tmp/a && {_RM} -rf mnt',
        f'cd "$(mktemp -d)" && {_RM} -rf x',
        f'cd "$UNSET_DIR" && {_RM} -rf build',
        f'cd tmp && {_RM} -rf x',
        f'cd "${{D:-/tmp/a}}" && {_RM} -rf mnt',
        f'cd "$(git -C /tmp/a rev-parse --show-toplevel 2>/dev/null || echo /tmp/a)" || exit; '
        f'{_RM} -rf /tmp/a/b',
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command

    def test_cwd_less_pre_hook_leaves_relative_deletes_to_the_executor(self):
        assert _hook(f'cd "$(cmd)" && {_RM} -rf mnt') is None
        assert _hook(f'cd / && {_RM} -rf mnt') is not None


class TestProgramWords:
    @pytest.mark.parametrize('command', [
        f'X={_RM}; $X -rf /',
        f'run() {{ "$@"; }}; run {_RM} -rf /',
        f'S=sudo; $S {_RM} -rf /',
        f'R={_RM}; sudo $R -rf /mnt',
        f'R={_RM}; env A=1 $R -rf /',
        f'E=; $E {_RM} -rf /',
        f'X=$(cmd); $X {_RM} -rf /',
        f'C="{_RM} -rf"; $C /',
        f"SH=bash; $SH -c '{_RM} -rf /'",
        f"f() {{ \"$@\"; }}; f sh -c '{_RM} -rf /'",
        '/bin/r? -rf /',
        f'/*/{_RM} -rf /',
        f'$(which {_RM}) -rf /',
        f'{{{_RM},-rf,/}}',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        'X=ls; $X /',
        'run() { "$@"; }; run ls /',
        '"$@"',
        f'X={_RM}; $X -rf /tmp/a/b',
        f'C="{_RM} -rf"; $C /tmp/a/b',
        '/usr/bin/py* -c 1',
        f"/usr/*/python3 -c 'print(\"{_RM} -rf /\")'",
        '"$(command -v python3)" -c "print(1)"',
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command

    def test_the_environment_names_the_program(self):
        assert _verdict('$RM -rf /', {'RM': _RM}) is not None
        assert _verdict('$RM -rf /', {'RM': 'ls'}) is None
        assert _hook('$RM -rf /') is None  # the executor's pass knows RM


class TestAliases:
    @pytest.mark.parametrize('command', [
        f"alias x='{_RM} -rf /'\nx",
        f"alias x='{_RM} -rf'\nx /mnt",
        f"alias x='{_RM} -rf /'\nY=1 x",
        f"alias s='sudo '\nalias r='{_RM} -rf /'\ns r",
        f"alias x='{_RM} -rf /'\nf() {{ x; }}\nf",
        f"alias x='{_RM} -rf /'\nif true; then x; fi",
        f"alias x='{_RM} -rf /'\n(x)",
        f"alias x='cd /'\nx\n{_RM} -rf mnt",
        f"alias x=bash\nx <<'EOF'\n{_RM} -rf /\nEOF",
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        "alias ll='ls -l'\nll /",
        f"alias x='{_RM} -rf /tmp/a/b'\nx",
        f"alias x='{_RM} -rf /'\n'x'",
        f"alias x='{_RM} -rf /'\nsh -c x",
        f"alias x='{_RM} -rf /'",
        f"alias x='{_RM} -rf /'\necho x",
        f"alias {_RM}='{_RM} -i'\n{_RM} -rf /tmp/a/b",
        f"alias py=python3\npy <<'EOF'\nprint('{_RM} -rf / is a shell command')\nEOF",
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command

    def test_dangerous_guard_reads_alias_values(self):
        assert dangerous_command_rule("alias x='reboot'") is not None
        assert dangerous_command_rule("alias ll='ls -l'") is None


class TestShellInputs:
    @pytest.mark.parametrize('command', [
        f"\"bash\" -c '{_RM} -rf /'",
        f"\\bash -c '{_RM} -rf /'",
        f"echo '{_RM} -rf /' | sh",
        f"printf '%s\\n' '{_RM} -rf /mnt' | bash",
        f'D=/; echo "{_RM} -rf $D" | sudo sh',
        f"echo '{_RM} -rf \"$D\"' | D=/ sh",
        f"cat <<'EOF' | D=/ sh\n{_RM} -rf \"$D\"\nEOF",
        f"echo '{_RM} -rf /' | $SHELL",
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        f"echo '{_RM} -rf /' | cat",
        f"echo '{_RM} -rf /tmp/a/b' | sh",
        f"D=/ cat <<'EOF' | sh\n{_RM} -rf \"$D\"\nEOF",
        f"echo '{_RM} -rf /' > notes.txt",
        f"\"bash\" -c '{_RM} -rf build'",
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command


class TestBraceExpansion:
    @pytest.mark.parametrize('command', [
        f'{_RM} -rf /{{tmp/x,}}',
        f'{_RM} -rf /tmp/{{a,..}}',
        f'set -- /; {_RM} -rf "${{@:1}}"',
        f'f() {{ {_RM} -rf "${{@:2}}"; }}; f /tmp/a /',
    ])
    def test_refused(self, command):
        assert _verdict(command) is not None, command

    @pytest.mark.parametrize('command', [
        f'{_RM} -rf /tmp/a/{{b,c}}',
        f'{_RM} -rf /tmp/a/out{{1..3}}',
        f'set --; {_RM} -rf "$@"',
        f'if [ -n "$C" ]; then O=/tmp/a/b; else O=/tmp/a/c; fi; {_RM} -rf "$O"/{{x,y,z}}',
    ])
    def test_allowed(self, command):
        assert _verdict(command) is None, command
