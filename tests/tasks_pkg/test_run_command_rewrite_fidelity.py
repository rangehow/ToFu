"""tests/tasks_pkg/test_run_command_rewrite_fidelity.py — span rewriters keep
shell data and runtime expansions intact.

run_command bounds direct ``find`` segments (``_bound_scan_segments``) and
splices filesystem ``grep`` segments (``plan_grep_redirect``) in place.
Incidents (2026-10-23):

  * both rewriters split the WHOLE command on newlines and parentheses, so the
    ``python3 - <<'EOF'`` body line ``print(find(1))`` became
    ``print(<timeout> find(1))`` (SyntaxError) and ``cat > f <<'EOF'`` wrote
    the timeout prefix or an ``rg`` argv into the file;
  * the rg translation re-quotes plan-time word text, so
    ``for t in a b; do grep -n "$t" log; done`` searched the literal ``$t``
    and silently returned nothing.

Pinned here:
  1. heredoc bodies stay byte-identical unless a local shell reads a quoted
     body as its script; such bodies and every command outside a body keep
     the rewrite (section 6: one command-structure rule for every rewrite);
  2. the recursive-scan resource guard keeps its verdicts for heredoc shapes;
  3. grep argv words that expand at runtime keep the GNU program-word splice
     (original shell text); literal words keep the rg translation;
  4. the same shapes end to end through ``tool_run_command``.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/tasks_pkg/test_run_command_rewrite_fidelity.py -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from lib.project_mod.command_analysis import _bound_scan_segments, harden_git_inspections
from lib.project_mod.command_network_probe import prepare_file_transfers
from lib.project_mod.copy_engine import plan_cp_splice
from lib.project_mod.grep_redirect import plan_grep_redirect
from lib.project_mod.grep_rg_translate import rg_executable
from lib.project_mod.run_command import _maybe_harden_grep_command, tool_run_command

pytestmark = pytest.mark.unit

_TIMEOUT = shutil.which('timeout')
_PREFIX = '/usr/bin/timeout --signal=TERM --kill-after=2s 40s '
needs_timeout = pytest.mark.skipif(
    _TIMEOUT is None, reason='coreutils timeout required')
needs_rg = pytest.mark.skipif(
    rg_executable() is None, reason='ripgrep required')


@pytest.fixture()
def ws(tmp_path):
    """A workspace two levels deep so ``..`` is an ancestor scan target."""
    base = tmp_path / 'parent' / 'chatui'
    (base / 'logs').mkdir(parents=True)
    (base / 'logs' / 'app.log').write_text(
        'start\nalpha ready\nbeta ready\n', encoding='utf-8')
    (base / 'a.py').write_text('needle = 1\n', encoding='utf-8')
    (base / 'b.py').write_text('other = 2\n', encoding='utf-8')
    return str(base)


def _sh(command, cwd):
    return subprocess.run(['sh', '-c', command], cwd=cwd,
                          capture_output=True, text=True, timeout=30)


def _read(ws, name):
    with open(os.path.join(ws, name), 'rb') as handle:
        return handle.read().decode('utf-8')


# ═════════════════════════════════════════════════════════════════════
#  1. Direct-find budget and heredoc bodies
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('command', [
    "python3 - <<'EOF'\ndef find(x):\n    return x\nprint(find(1))\nEOF",
    "cat > out.sh <<'EOF'\nfind . -maxdepth 0\n(find . -maxdepth 0)\nEOF",
    "cat <<'EOF' > out.sh\nfind . -name x | sort\nEOF\n",
    'node <<"JS"\nconst find = (x) => x;\nfind(1);\nJS\n',
])
def test_find_budget_leaves_literal_heredoc_bodies_unchanged(command):
    assert _bound_scan_segments(command, '/usr/bin/timeout', 40) == (command, 0)


def test_find_budget_keeps_wrapping_segments_around_a_heredoc():
    command = ("find . -maxdepth 0; cat > out.sh <<'EOF'\n"
               "find . -maxdepth 0\nEOF\nfind . -maxdepth 0 | head -1")
    assert _bound_scan_segments(command, '/usr/bin/timeout', 40) == (
        f"{_PREFIX}find . -maxdepth 0; cat > out.sh <<'EOF'\n"
        f"find . -maxdepth 0\nEOF\n{_PREFIX}find . -maxdepth 0 | head -1", 2)


@pytest.mark.parametrize('header', ["bash <<'EOF'", "cat <<'EOF' | sh"])
def test_find_budget_still_bounds_shell_executed_heredoc_bodies(header):
    """A body a shell runs is shell code: the rewrite fails closed."""
    command = f'{header}\nfind . -maxdepth 0\nEOF'
    assert _bound_scan_segments(command, '/usr/bin/timeout', 40) == (
        f'{header}\n{_PREFIX}find . -maxdepth 0\nEOF', 1)


@needs_timeout
def test_bounded_heredoc_command_writes_the_exact_body(ws):
    body = 'find . -maxdepth 0\n(find . -maxdepth 0)\n'
    command = f"cat > out.sh <<'EOF'\n{body}EOF\nfind . -maxdepth 0"
    rewritten, count = _bound_scan_segments(command, _TIMEOUT, 40)
    assert count == 1
    result = _sh(rewritten, ws)
    assert (result.returncode, result.stdout) == (0, '.\n'), result.stderr
    assert _read(ws, 'out.sh') == body


@needs_timeout
def test_bounded_python_heredoc_still_runs(ws):
    command = "python3 - <<'EOF'\ndef find(x):\n    return x\nprint(find(1))\nEOF"
    rewritten, _count = _bound_scan_segments(command, _TIMEOUT, 40)
    result = _sh(rewritten, ws)
    assert (result.returncode, result.stdout) == (0, '1\n'), result.stderr


# ═════════════════════════════════════════════════════════════════════
#  2. Filesystem-grep splice and heredoc bodies
# ═════════════════════════════════════════════════════════════════════

def test_grep_redirect_ignores_literal_heredoc_bodies(ws):
    assert plan_grep_redirect(
        "cat > out.sh <<'EOF'\ngrep -rn needle .\nEOF", ws) is None


def test_grep_redirect_still_splices_shell_executed_heredoc_bodies(ws):
    plan = plan_grep_redirect("cat <<'EOF' | sh\ngrep -rn needle .\nEOF", ws)
    assert plan is not None and plan.n_redirected == 1
    result = _sh(plan.rewritten, ws)
    assert result.stdout == './a.py:1:needle = 1\n', result.stderr


def test_grep_redirect_splices_around_a_heredoc_and_keeps_its_body(ws):
    body = 'grep -rn needle .\n(grep -n needle a.py)\n'
    command = (f"grep -n needle a.py; cat > out.sh <<'EOF'\n{body}EOF\n"
               'grep -c needle out.sh a.py')
    plan = plan_grep_redirect(command, ws)
    assert plan is not None and plan.n_redirected == 2
    assert f"<<'EOF'\n{body}EOF\n" in plan.rewritten
    result = _sh(plan.rewritten, ws)
    assert result.stdout == '1:needle = 1\nout.sh:2\na.py:1\n', result.stderr
    assert _read(ws, 'out.sh') == body


# ═════════════════════════════════════════════════════════════════════
#  3. Runtime expansions in grep argv
# ═════════════════════════════════════════════════════════════════════

def test_rg_translation_keeps_double_quoted_parameter(ws):
    plan = plan_grep_redirect(
        't=alpha; echo x; grep -n "$t" logs/app.log | head -3', ws)
    assert plan is not None and plan.rewritten is not None
    assert '"$t"' in plan.rewritten and "'$t'" not in plan.rewritten
    result = _sh(plan.rewritten, ws)
    assert result.stdout == 'x\n2:alpha ready\n', result.stderr


@pytest.mark.parametrize('command, expected', [
    ('for t in alpha beta; do echo hi; grep -n "$t" logs/app.log | head; done',
     'hi\n2:alpha ready\nhi\n3:beta ready\n'),
    ('t=alpha; grep -n $t logs/app.log', '2:alpha ready\n'),
    ('grep -n ${TOFU_TEST_UNSET_PATTERN:-beta} logs/app.log', '3:beta ready\n'),
    ('grep -n "$(echo beta)" logs/app.log', '3:beta ready\n'),
    ('grep -n "`echo beta`" logs/app.log', '3:beta ready\n'),
    # An unquoted glob pattern expands against the runtime cwd (file alpha).
    ('grep -n alph? logs/app.log', '2:alpha ready\n'),
    ('grep -n needle *.py', 'a.py:1:needle = 1\n'),
    ('grep -rn needle --include=*.py .', './a.py:1:needle = 1\n'),
])
def test_runtime_expanded_grep_words_keep_original_shell_text(
        ws, command, expected):
    with open(os.path.join(ws, 'alpha'), 'w', encoding='utf-8') as handle:
        handle.write('glob target\n')
    plan = plan_grep_redirect(command, ws)
    assert plan is not None and plan.backend == 'gnu'
    result = _sh(plan.rewritten, ws)
    assert result.stdout == expected, result.stderr


@needs_rg
@pytest.mark.parametrize('command', [
    "grep -n 'alpha' logs/app.log",
    'grep -n "alpha ready" logs/app.log',
    "grep -n '$t' logs/app.log",
    'grep -n "\\$t" logs/app.log',
    "grep -rn needle --include='*.py' .",
    'grep -rn alpha logs',
])
def test_literal_grep_words_keep_rg_translation(ws, command):
    plan = plan_grep_redirect(command, ws)
    assert plan is not None and plan.backend == 'rg'


# ═════════════════════════════════════════════════════════════════════
#  4. End to end through tool_run_command
# ═════════════════════════════════════════════════════════════════════

def test_heredoc_scan_lines_stay_data_through_run_command(ws):
    """Body lines were always judged bounded by the resource guard; keeping
    them byte-identical must not turn them into new refusals."""
    body = ('find .. -maxdepth 1 -name chatui\n'
            'grep -rn needle .\n'
            '(find . -maxdepth 0)\n')
    result = tool_run_command(ws, f"cat > notes.sh <<'EOF'\n{body}EOF")
    assert 'blocked by resource guard' not in result
    assert 'Command intercepted' not in result
    assert '[exit code: 0]' in result
    assert _read(ws, 'notes.sh') == body


def test_rewrites_around_a_heredoc_keep_its_body_through_run_command(ws):
    """The find budget runs on the grep-spliced source; both keep the body."""
    body = 'find . -maxdepth 0\ngrep -rn needle .\n'
    result = tool_run_command(
        ws, f"grep -n needle a.py && cat > out.sh <<'EOF'\n{body}EOF\n"
            'find . -maxdepth 0 | head -1')
    assert '1:needle = 1\n.\n' in result
    assert _read(ws, 'out.sh') == body


def test_python_heredoc_runs_unchanged_through_run_command(ws):
    result = tool_run_command(
        ws, "python3 - <<'EOF'\ndef find(x):\n    return x\nprint(find(1))\nEOF")
    assert '[exit code: 0]' in result
    assert '\n1\n' in result


def test_shell_executed_heredoc_find_keeps_its_budget(ws):
    chunks = []
    result = tool_run_command(
        ws, "bash <<'EOF'\nfind .. -maxdepth 1 -name chatui\nEOF",
        on_chunk=lambda *args: chunks.append(args))
    assert 'blocked by resource guard' not in result
    assert '../chatui' in result
    assert any('find scan capped' in str(chunk) for chunk in chunks)


def test_shell_executed_heredoc_scan_is_still_refused(ws):
    result = tool_run_command(ws, "bash <<'EOF'\ngrep -rn needle ..\nEOF")
    assert 'blocked by resource guard' in result


def test_grep_loop_over_shell_variable_through_run_command(ws):
    result = tool_run_command(
        ws, 'for t in alpha beta; do echo hi; grep -n "$t" logs/app.log | head -1; done')
    assert '2:alpha ready' in result
    assert '3:beta ready' in result


# ═════════════════════════════════════════════════════════════════════
#  5. Git read hardening and the cp fast path share the same boundary
# ═════════════════════════════════════════════════════════════════════

_DATA_BODY = 'git log --oneline -1\ncp -r logs copy\n'


def test_git_hardening_leaves_literal_heredoc_bodies_unchanged():
    from lib.project_mod.command_analysis import harden_git_inspections
    command = f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF"
    assert harden_git_inspections(command) == command


def test_git_hardening_still_applies_around_a_heredoc():
    from lib.project_mod.command_analysis import harden_git_inspections
    tail = f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF"
    command = f'git status --short && {tail}'
    hardened = harden_git_inspections(command)
    assert hardened != command
    assert hardened.endswith(tail)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX shell grammar')
@pytest.mark.parametrize('command', [
    # `$( (` opens a subshell; `$((` would open arithmetic expansion.
    'x=$( (git status --short; echo listed) | sort); echo "$x"',
    'x=` (git status --short; echo listed) | sort`; echo "$x"',
    # The escaped trailing blank belongs to the last word.
    'git status --short; echo listed\\ ',
])
def test_git_hardening_keeps_edge_blanks_that_are_shell_syntax(tmp_path, command):
    """Hardening rewrites Git words only; the blanks at the edges of a command
    or substitution body stay, because there they can be syntax."""
    from lib.project_mod.command_analysis import harden_git_inspections
    hardened = harden_git_inspections(command)
    assert '--no-optional-locks' in hardened
    environment = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, env=environment)

    def run(source):
        return subprocess.run(['sh', '-c', source], cwd=tmp_path, env=environment,
                              capture_output=True, text=True)

    original, rewritten = run(command), run(hardened)
    assert original.stdout.startswith('listed'), original
    assert (rewritten.returncode, rewritten.stdout, rewritten.stderr) == (
        original.returncode, original.stdout, original.stderr)


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
def test_cp_fast_path_leaves_literal_heredoc_bodies_unchanged():
    from lib.project_mod.copy_engine import plan_cp_splice
    assert plan_cp_splice(f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF") is None


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
def test_cp_fast_path_still_splices_around_a_heredoc():
    from lib.project_mod.copy_engine import plan_cp_splice
    tail = f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF"
    spliced = plan_cp_splice(f'cp -r logs saved && {tail}')
    assert spliced is not None and not spliced.startswith('cp ')
    assert spliced.endswith(tail)


def test_git_and_cp_heredoc_lines_stay_data_through_run_command(ws):
    result = tool_run_command(ws, f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF")
    assert '[exit code: 0]' in result
    assert _read(ws, 'notes.sh') == _DATA_BODY


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


@pytest.mark.parametrize('header', [
    "python3 - <<-'EOF'",        # tab-stripping delimiter: shape not classified
    'python3 - <<EOF',           # an expanded runtime body is Python, not shell
    "python3 -<<'EOF'",          # attached operator
    "uv run python - <<'EOF'",   # unknown stdin program
])
def test_unclassified_heredoc_bodies_are_never_rewritten(header):
    """A rewriter that cannot prove a body is shell source leaves it as written.

    Reported 2026-10-23: the body line ``cp = 1`` of a Python heredoc became a
    copy-engine invocation (SyntaxError). Rewriting is an optimisation; the
    executed bytes must not change when a body is not provably shell.
    """
    from lib.project_mod.command_analysis import harden_git_inspections
    from lib.project_mod.copy_engine import plan_cp_splice
    command = f'{header}\ncp = 1\nfind = 2\ngit = 3\nEOF'
    if os.name == 'posix':
        assert plan_cp_splice(command) is None
    assert _bound_scan_segments(command, _TIMEOUT or 'timeout', 40) == (command, 0)
    assert harden_git_inspections(command) == command


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
def test_unquoted_shell_heredoc_body_is_never_rewritten():
    """The current shell expands an unquoted body before bash reads it, so the
    body's bytes are not the script bash parses."""
    from lib.project_mod.copy_engine import plan_cp_splice
    assert plan_cp_splice('bash <<EOF\ncp -r src dst\nEOF') is None
    assert plan_cp_splice("bash <<'EOF'\ncp -r src dst\nEOF") is not None


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
@pytest.mark.parametrize('command', [
    'W=src; cp $W/a.py dst.py',          # parameter expansion
    'cp "$SRC" dst.py',
    'cp ${SRC:-a.py} dst.py',
    'cp ~/a.py dst.py',                  # tilde expansion
    'cp *.py dst/',                      # glob expansion
    'cp {a,b}.py dst/',                  # brace expansion
])
def test_cp_fast_path_never_freezes_shell_expansions(command):
    """The engine receives argv verbatim, so an expanding word must stay with the shell.

    Reported 2026-10-23: `W=/abs/dir; cp $W/a /tmp/x` failed with
    `cp: cannot stat '$W/a'` because the splice quoted the word literally.
    """
    from lib.project_mod.copy_engine import plan_cp_splice
    assert plan_cp_splice(command) is None


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
def test_cp_fast_path_keeps_literal_and_quoted_operands():
    from lib.project_mod.copy_engine import plan_cp_splice
    assert plan_cp_splice("echo start && cp -r logs 'saved *dir'") is not None
    assert plan_cp_splice('echo start && cp -r logs saved') is not None


def test_cp_with_parameter_expansion_copies_the_expanded_path(ws):
    result = tool_run_command(ws, 'W=logs; cp $W/app.log copied.log && cat copied.log')
    assert 'alpha ready' in result, result
    assert _read(ws, 'copied.log') == _read(ws, 'logs/app.log')


# ═════════════════════════════════════════════════════════════════════
#  6. One command-structure rule: rewrites never edit data
# ═════════════════════════════════════════════════════════════════════
#
# Reported 2026-11-13 (FRONTIER-HARNESS-2): `D=$(mktemp -d); cat > $D/x.sh
# <<'EOF'` wrote `n=$(grep -c "a" /tmp/f)` into the file as
# `n=$(/usr/bin/timeout --verbose … grep --exclude-dir … -c "a" /tmp/f)`. The
# `$(` in the heredoc's command line defeated the literal-body proof the grep
# splice relied on, so the splice read the body as commands. Every rewrite now
# edits only the command text command_structure delimits.

_WRAP = ('/usr/bin/timeout', ('--verbose',), 40)


def _rewrites(command, cwd):
    """``(name, rewritten source)`` for every in-place rewrite run_command applies."""
    plan = plan_grep_redirect(command, cwd, timeout_wrap=_WRAP)
    yield 'grep splice', command if plan is None or plan.rewritten is None else plan.rewritten
    yield 'find budget', _bound_scan_segments(command, '/usr/bin/timeout', 40)[0]
    yield 'cp splice', plan_cp_splice(command) or command
    yield 'grep -r flags', _maybe_harden_grep_command(command)
    yield 'Git read hardening', harden_git_inspections(command)
    yield 'transfer hardening', prepare_file_transfers(command)


_WRITTEN_BODY = ('n=$(grep -c "a" /tmp/f)\n'
                 'grep -rn needle .\n'
                 'find . -name x\n'
                 'cp -r logs copy\n'
                 '(grep -n needle a.py)\n')


@pytest.mark.parametrize('header, footer', [
    # The FRONTIER-HARNESS-2 repro: a substitution in the heredoc's command line.
    ("D=$(pwd)/out; mkdir -p $D; cat > $D/x.sh <<'EOF'", ''),
    ("mkdir -p out; tee out/x.sh >/dev/null <<'EOF'", ''),
    ("mkdir -p out; cat > out/x.sh <<-'EOF'", ''),
    # The shape every background command takes (background_commands._wrap_for_record).
    ("mkdir -p out; ( cat > out/x.sh <<'EOF'", '\n) > out/log 2>&1'),
])
def test_quoted_heredoc_body_is_written_byte_for_byte(ws, header, footer):
    result = tool_run_command(ws, f'{header}\n{_WRITTEN_BODY}EOF{footer}')
    assert '[exit code: 0]' in result, result
    assert _read(ws, 'out/x.sh') == _WRITTEN_BODY


_DATA_BODY = ('grep -rn needle .\n'
              'find . -name x\n'
              'cp -r logs copy\n'
              'git diff --stat\n'
              'curl -sS -o out.bin http://example.invalid/x\n'
              '(grep -n needle a.py)\n')


@pytest.mark.parametrize('header', [
    "D=$(pwd); cat > $D/x.sh <<'EOF'",
    "tee x.sh >/dev/null <<'EOF'",
    "cat > x.sh <<-'EOF'",
    "( cat > x.sh <<'EOF'",
    "ssh host bash <<'EOF'",          # the remote shell reads it; locally it is data
    "uv run python - <<'EOF'",
    'cat > x.sh <<EOF',
])
def test_no_rewrite_edits_a_heredoc_body_that_is_data(ws, header):
    command = f'{header}\n{_DATA_BODY}EOF'
    for name, rewritten in _rewrites(command, ws):
        assert f'\n{_DATA_BODY}EOF' in rewritten, (name, rewritten)


def test_python_heredoc_source_and_its_written_file_are_byte_identical(ws):
    """The chain_block.sh shape: the apostrophe in the Python comment flipped
    the old splitter's quote state, exposing the script text the program
    writes, so its grep was spliced."""
    script = ("# relay's health check\n"
              "open('chain_block.sh', 'w').write("
              "'n=$(grep -c \"relay listening\" /tmp/x.out 2>/dev/null)\\n')\n"
              "grep = find = cp = 1\n"
              "print('written', grep + find + cp)\n")
    command = f"cd \"$(pwd)\" && python3 - <<'PY'\n{script}PY"
    for name, rewritten in _rewrites(command, ws):
        assert f'\n{script}PY' in rewritten, (name, rewritten)
    result = tool_run_command(ws, command)
    assert 'written 3' in result, result
    assert _read(ws, 'chain_block.sh') == (
        'n=$(grep -c "relay listening" /tmp/x.out 2>/dev/null)\n')


def _outside_substitutions(text):
    return re.sub(r'\$\([^()]*\)', '$()', text)


def test_unquoted_heredoc_rewrites_nothing_outside_command_substitutions(ws):
    body = ('grep -rn needle .\n'
            'find . -name x\n'
            'cp -r logs copy\n'
            'n=$(grep -c needle a.py)\n')
    command = f'cat > out.sh <<EOF\n{body}EOF'
    for name, rewritten in _rewrites(command, ws):
        assert _outside_substitutions(rewritten) == _outside_substitutions(command), (
            name, rewritten)
    result = tool_run_command(ws, command)
    assert '[exit code: 0]' in result, result
    assert _read(ws, 'out.sh') == 'grep -rn needle .\nfind . -name x\ncp -r logs copy\nn=1\n'


_QUOTED_ARGUMENT = 'grep -rn needle .; find . -name y; cp -r logs copy'


@pytest.mark.parametrize('command', [
    f'echo "{_QUOTED_ARGUMENT}" > out.txt',
    f"python3 -c \"print('{_QUOTED_ARGUMENT}')\" > out.txt",
    # An apostrophe in a comment or in an unquoted heredoc body desynced the
    # old splitter's quote state and exposed the quoted argument as commands.
    f"echo start # it's\necho 'x; {_QUOTED_ARGUMENT}' > out.txt",
    f"cat > notes.txt <<EOF\nit's\nEOF\necho 'x; {_QUOTED_ARGUMENT}' > out.txt",
])
def test_quoted_string_arguments_are_never_rewritten(ws, command):
    for name, rewritten in _rewrites(command, ws):
        assert rewritten == command, (name, rewritten)
    result = tool_run_command(ws, command)
    assert '[exit code: 0]' in result, result
    assert _read(ws, 'out.txt').endswith(f'{_QUOTED_ARGUMENT}\n')


@pytest.mark.parametrize('command', [
    f'echo hi # see; {_QUOTED_ARGUMENT}',
    'echo hi # see $(git status) and `git log`',
])
def test_comment_text_is_never_rewritten(ws, command):
    for name, rewritten in _rewrites(command, ws):
        assert rewritten == command, (name, rewritten)


def test_grep_flags_are_never_read_from_a_comment():
    assert _maybe_harden_grep_command('grep -n needle a.py # -r') == (
        'grep -n needle a.py # -r')


_DATA_NEIGHBOUR = ("D=$(pwd); cat > $D/n.sh <<'EOF'\n"
                   'grep -rn needle .\nfind . -name x\ncp -r logs copy\nEOF\n')


@pytest.mark.parametrize('command, rewrite', [
    ('grep -rn needle .', 'grep splice'),
    ('n=$(grep -c needle a.py); echo $n', 'grep splice'),
    ('# grep -rn needle .\ngrep -rn needle .', 'grep splice'),
    ('find . -maxdepth 0 | head -1', 'find budget'),
    ('cp -r logs saved && echo done', 'cp splice'),
    ('grep -rn needle lib', 'grep -r flags'),
    ('git status --short', 'Git read hardening'),
    ('curl -sS -o out.bin http://example.invalid/x', 'transfer hardening'),
    ("bash <<'EOF'\nfind . -maxdepth 0\nEOF", 'find budget'),
    ("cat <<'EOF' | sh\ngrep -rn needle .\nEOF", 'grep splice'),
    (_DATA_NEIGHBOUR + 'grep -rn needle .', 'grep splice'),
    (_DATA_NEIGHBOUR + 'find . -maxdepth 0', 'find budget'),
    (_DATA_NEIGHBOUR + 'cp -r logs saved', 'cp splice'),
    ('cat <<< x; find . -maxdepth 0', 'find budget'),
    ('cat <<< x; cp -r logs saved', 'cp splice'),
])
def test_command_position_rewrites_still_apply(ws, command, rewrite):
    """A command next to data keeps its rewrite; only the data stays as written."""
    rewritten = dict(_rewrites(command, ws))[rewrite]
    assert rewritten != command
    if command.startswith(_DATA_NEIGHBOUR):
        assert rewritten.startswith(_DATA_NEIGHBOUR), rewritten


def test_git_read_preparation_leaves_heredoc_data_intact(tmp_path):
    """A line already in hardened form is still data inside a heredoc body."""
    from lib.project_mod.git_inspection_execution import prepare_git_inspections
    environment = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, env=environment)
    (tmp_path / 'a.txt').write_text('a\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'a.txt'], cwd=tmp_path, check=True, env=environment)
    data_line = harden_git_inspections('git diff --stat')
    assert data_line != 'git diff --stat'
    command = f"D=$(pwd); cat > $D/g.sh <<'EOF'\n{data_line}\nEOF"
    with prepare_git_inspections(command, str(tmp_path), environment=environment) as prepared:
        assert prepared == command


def test_heredoc_data_is_never_refused_by_the_scan_or_grep_guard(ws):
    body = 'grep -rn needle ..\nfind .. -name chatui\n'
    for header in ("cat > notes.sh <<'EOF'", "D=$(pwd); cat > $D/notes.sh <<'EOF'"):
        result = tool_run_command(ws, f'{header}\n{body}EOF')
        assert 'blocked by resource guard' not in result, result
        assert 'Command intercepted' not in result, result
        assert _read(ws, 'notes.sh') == body
