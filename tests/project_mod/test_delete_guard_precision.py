"""The delete guard refuses only a protected path it can name.

Owner decision 2026-11-17 ("Catastrophic-delete guard precision" in
docs/modules/tools_execution.md): no false refusal and no slowdown, at the
price of missing deletes whose target the guard cannot know. A value it
cannot know (``$(cmd)``, ``read``, an unknown ``cd``, a recursive call, more
values than it tracks) is not assumed empty, text it cannot parse passes,
and a top-level path that names nothing holds nothing to lose. A command
without a delete word is not parsed at all. The shared resolver still
reports what it cannot resolve: the desktop agent's share-root containment
refuses it.
"""

from __future__ import annotations

import os

import pytest

from lib.project_mod import command_analysis
from lib.project_mod.command_analysis import (
    _catastrophic_delete_detail,
    resolve_delete_targets,
)

pytestmark = pytest.mark.unit

_RM = chr(114) + chr(109)
_WS = '/tmp/tofu_delguard_ws/sub'
_ENVIRONMENT = {
    'HOME': os.path.expanduser('~'), 'PATH': '/usr/bin:/bin', 'USER': 'agent',
    'TMPDIR': '/tmp/tofu-delguard-scratch/task', 'VIRTUAL_ENV': '/tmp/tofu-delguard-venv',
}


def _verdict(command):
    return _catastrophic_delete_detail(command, cwd=_WS, environment=_ENVIRONMENT)


# Commands an agent writes every day. Each one was refused by some earlier
# version of the guard or rests on a value the guard cannot know.
_LEGITIMATE = [
    f'{_RM} -rf build dist *.egg-info',
    f'{_RM} -rf node_modules && npm ci',
    f'{_RM} -f "$TMPDIR/gh_hdr"',
    f'{_RM} -rf "$TMPDIR"/cache "$VIRTUAL_ENV/lib/python3.12/site-packages/foo"',
    f'{_RM} -rf ./.pytest_cache __pycache__ ../sibling-build',
    f"find . -name '__pycache__' -type d -prune -exec {_RM} -rf {{}} +",
    f"find . -name '*.pyc' -delete; {_RM} -f coverage.xml",
    f'git {_RM} --cached secrets.txt && git {_RM} -r --cached .',
    f'tmp=$(mktemp -d) && trap \'{_RM} -rf "$tmp"\' EXIT && cp a "$tmp" && ls "$tmp"',
    f'd=$(mktemp -d); cd "$d" && git init -q && {_RM} -rf .git',
    f'for f in *.log; do {_RM} -f "$f"; done',
    f'for d in $(find . -name build -type d); do {_RM} -rf "$d/"; done',
    f"ls | grep '\\.tmp$' | xargs {_RM} -f",
    f'{_RM} -rf "$(git rev-parse --show-toplevel)/build"',
    f'cd "$(dirname "$0")" && {_RM} -rf out',
    f'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && {_RM} -rf build',
    f"OUT=$(python3 -c 'print(1)'); {_RM} -rf \"$OUT/\"",
    f'{_RM} -rf ~/.cache/pip "$HOME/.cache/huggingface/hub/models--x"',
    f"python3 - <<'EOF'\nimport os, shutil, subprocess\nshutil.rmtree('build')\n"
    f"subprocess.run(['{_RM}', '-rf', '/'])\nEOF",
    f'docker {_RM} -f web && docker image {_RM} old',
    f'{_RM} -rf "${{BUILD_DIR:?}}/"',
    f'W=$(mktemp -d); {_RM} -rf "$W/"*',
    f'cleanup() {{ {_RM} -rf "$1"; }}; cleanup /tmp/scratch/x',
    f'clean() {{ {_RM} -rf "$1"/*; }}; clean build',
    f'clean() {{ {_RM} -rf "${{1:?}}"/*; }}  # defined for later'
    'unlink /tmp/tofu-delguard/link; rmdir empty_dir',
    f'timeout 30 {_RM} -rf build; nice -n 10 {_RM} -rf dist',
    f'timeout "$T" {_RM} -rf build',
    f'sudo {_RM} -rf /var/cache/apt/archives/*.deb',
    f'{_RM} -rf /tmp/pytest-of-$USER/garbage',
    f"sh -c '{_RM} -rf \"$1\"' _ build",
    f'bash -c "cd /tmp/w && {_RM} -rf old"',
    f"echo '{_RM} -rf / is dangerous' > notes.md",
    f'grep -rn "{_RM} -rf" docs/ | head',
    f'git commit -m "{_RM} -rf obsolete code"',
    f'{_RM} -rf $(ls -d build*)',
    f'read -r target < list.txt; {_RM} -rf "$target/"',
    f'while read -r f; do {_RM} -f "$f"; done < files.txt',
    f'X=$(cmd); cd "$X" && {_RM} -rf ..',
    f'arr=(a b); for x in "${{arr[@]}}"; do {_RM} -rf "build/$x"; done',
    f"{_RM} -rf 'unterminated",
    f'f() {{ {_RM} -rf "$1"; f "$(dirname "$1")"; }}; f /tmp/a/b/c',
    f'{_RM} -rf /tofu-delguard-absent "$TMPDIR/x"',
]

# Deletes the guard can name, which must stay refused.
_CATASTROPHIC = [
    f'{_RM} -rf /',
    f'{_RM} -rf /*',
    f'{_RM} -rf --no-preserve-root /',
    f'{_RM} -rf ~',
    f'{_RM} -rf "$HOME"',
    f'{_RM} -rf /usr',
    f'sudo {_RM} -rf /etc',
    f'cd / && {_RM} -rf usr',
    f'D=; {_RM} -rf "$D/"*',
    f'{_RM} -rf "$TOFU_DELGUARD_UNSET/"*',
    f'{_RM} -rf .',
    f'{_RM} -rf ..',
    f'{_RM} -rf "$PWD"',
    f'X={_RM}; $X -rf /',
    f'run() {{ "$@"; }}; run {_RM} -rf /',
    f"alias x='{_RM} -rf /'\nx",
    f"bash -c '{_RM} -rf /'",
    f"echo '{_RM} -rf /' | sh",
    f'eval "{_RM} -rf /"',
    f"trap '{_RM} -rf /' EXIT",
    f'f() {{ {_RM} -rf "$1/"; }}; f',
    f'for d in / /tmp/a; do {_RM} -rf "$d"; done',
    f'if [ -n "$X" ]; then D=/; fi; {_RM} -rf "$D"',
    f'{_RM} -rf /tmp/{{a,..}}',
    f'! cd /tofu-delguard-absent && {_RM} -rf *',  # rm runs where cd failed
    f'command_not_found_handle() {{ {_RM} -rf /*; }}; tofu-missing-command',
    f'timeout $T {_RM} -rf /',
    f'sudo --bogus {_RM} -rf /',
]


@pytest.mark.parametrize('command', _LEGITIMATE)
def test_everyday_deletes_pass(command):
    assert _verdict(command) is None, command


@pytest.mark.parametrize('command', _CATASTROPHIC)
def test_deletes_of_a_named_protected_path_are_refused(command):
    assert _verdict(command) is not None, command


class TestTopLevelPathsHoldWhatTheyName:
    def test_an_existing_top_level_path_is_refused(self):
        assert _verdict(f'{_RM} -f /usr') is not None
        assert _verdict(f'{_RM} -rf /tm*') is not None  # matches /tmp

    def test_a_top_level_path_that_names_nothing_passes(self):
        assert _verdict(f'{_RM} -f /tofu-delguard-absent') is None
        assert _verdict(f'{_RM} -rf /tofu-delguard-absent*') is None

    def test_existence_is_read_when_the_command_is_judged(self, tmp_path, monkeypatch):
        top = tmp_path / 'top'
        top.mkdir()
        monkeypatch.setattr(command_analysis, '_MIN_DELETE_DEPTH',
                            len(top.parts) - 1 + command_analysis._MIN_DELETE_DEPTH)
        assert _verdict(f'{_RM} -rf {top}') is not None
        top.rmdir()
        assert _verdict(f'{_RM} -rf {top}') is None


class TestCommandsWithoutADeleteWordAreNotRead:
    def test_no_parse(self, monkeypatch):
        def parse(_text):
            raise AssertionError('a command without a delete word was parsed')
        monkeypatch.setattr(command_analysis, 'parse_shell_structure', parse)
        for command in ('git status && git log --oneline -3', 'ls -la /', 'cd / && make',
                        "python3 - <<'EOF'\nprint('format', 'transform')\nEOF"):
            assert _verdict(command) is None, command


class TestTheResolverStillReportsWhatItCannotResolve:
    """The desktop agent's share-root containment refuses these targets."""

    def test_an_unknown_value(self):
        [(_operand, paths, unproven)] = resolve_delete_targets(
            f'D=$(cmd); {_RM} -rf "$D/"', _WS, environment=_ENVIRONMENT)
        assert paths == ['/'] and unproven == {'D'}

    @pytest.mark.parametrize('command', [
        f"{_RM} -rf 'unterminated",
        f'timeout $T {_RM} -rf build',
        f'f() {{ {_RM} -rf "$1"; f x; }}; f build',
        f'$(which {_RM}) -rf build',
    ])
    def test_text_it_cannot_read(self, command):
        targets = resolve_delete_targets(command, _WS, environment=_ENVIRONMENT)
        assert any(paths is None for _operand, paths, _unproven in targets), command
