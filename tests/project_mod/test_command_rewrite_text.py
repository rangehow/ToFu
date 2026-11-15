"""tests/project_mod/test_command_rewrite_text.py — the one rule every
run_command rewrite follows: only command text may be edited.

``command_structure.shell_rewrite_text`` returns the command text of a source
as an offset-preserving view (everything else blanked, newlines kept) and the
simple-command ranges; ``command_heredoc.shell_script_heredocs`` names the
heredoc bodies a local shell reads as its script. ``command_analysis.
command_rewrite_text`` joins them for the rewriters (FRONTIER-HARNESS-2,
2026-11-13; docs/modules/tools_execution.md, "Command rewrites edit only
command text").
"""

from __future__ import annotations

import pytest

from lib.project_mod.command_analysis import command_rewrite_text
from lib.project_mod.command_heredoc import shell_script_heredocs
from lib.project_mod.command_structure import (
    ShellStructureUnknown,
    parse_shell_structure,
    shell_rewrite_text,
)

pytestmark = pytest.mark.unit


def _view(command):
    text = command_rewrite_text(command)
    assert text is not None, command
    assert len(text.view) == len(command)
    assert [i for i, c in enumerate(text.view) if c == '\n'] == [
        i for i, c in enumerate(command) if c == '\n']
    return text.view


def _blank(text):
    return ''.join('\n' if char == '\n' else ' ' for char in text)


@pytest.mark.parametrize('header', [
    "cat > f <<'EOF'",
    'cat > f <<EOF',
    'cat > f <<"EOF"',
    'cat > f <<\\EOF',
    "D=$(pwd); cat > $D/f <<'EOF'",
    "tee f >/dev/null <<'EOF'",
    "python3 - <<'EOF'",
    "uv run python - <<'EOF'",
    "ssh host bash <<'EOF'",
    "docker exec -i c bash <<'EOF'",
    'bash <<EOF',                       # expanded before bash reads it
    "bash -c 'cat' <<'EOF'",            # stdin is data for the -c script
    "bash script.sh <<'EOF'",
    "$SHELL <<'EOF'",
    "cat <<'EOF' | tee f | sh",          # the bytes are written to f too
    "cat <<'EOF' > f | sh",
    "cat f - <<'EOF' | sh",
    "bash <<'EOF' < other",
])
def test_heredoc_body_and_delimiter_line_are_data(header):
    body = 'grep -rn x lib\n# not a comment here\n'
    command = f'{header}\n{body}EOF\necho after'
    assert _view(command) == f'{header}\n{_blank(body)}   \necho after'


def test_tab_stripping_heredoc_is_data():
    command = "cat > f <<-'EOF'\n\tgrep -rn x lib\n\tEOF\necho after"
    assert _view(command) == "cat > f <<-'EOF'\n" + _blank('\tgrep -rn x lib\n\tEOF') + '\necho after'


@pytest.mark.parametrize('header', [
    "bash <<'EOF'",
    'sh <<"EOF"',
    "bash -s -- a b <<'EOF'",
    "timeout 60 bash -s <<'EOF'",
    "env A=1 nice -n 5 sh <<'EOF'",
    "sudo bash <<'EOF'",
    "bash <<'EOF' > log 2>&1",
    "cat <<'EOF' | sh",
    "cat - <<'EOF' | bash -s",
])
def test_quoted_body_a_local_shell_reads_is_command_text(header):
    body = 'grep -rn x lib\n'
    command = f'{header}\n{body}EOF'
    assert _view(command) == f'{header}\n{body}   '


def test_script_body_follows_the_same_rule_inside():
    inner = "cat > f <<'IN'\ngrep -rn x lib\nIN\n"
    command = f"bash <<'EOF'\necho a # note\n{inner}find . -name y\nEOF"
    view = _view(command)
    assert view == ("bash <<'EOF'\necho a       \ncat > f <<'IN'\n"
                    + _blank('grep -rn x lib\nIN') + '\nfind . -name y\n   ')
    starts = [command[start:end].strip() for start, end in command_rewrite_text(command).ranges]
    assert 'echo a' in starts and "cat > f <<'IN'" in starts and 'find . -name y' in starts
    assert not any('grep' in command_text for command_text in starts)


def test_unparseable_script_body_stays_data():
    command = "bash <<'EOF'\necho 'unclosed\nEOF\necho after"
    assert _view(command) == "bash <<'EOF'\n" + _blank("echo 'unclosed") + '\n   \necho after'


@pytest.mark.parametrize('command, expected', [
    ('echo hi # grep -rn x lib', 'echo hi                 '),
    ('# grep -rn x lib\necho ok', '                \necho ok'),
    ('echo a#b', 'echo a#b'),
    ('echo ${#x} $#', 'echo ${#x} $#'),
    ('echo "# not" \'# not\'', 'echo "# not" \'# not\''),
    ('x=`grep -c a f`; echo $x', 'x=`           `; echo $x'),
    ('echo "a `grep -c a f` b"', 'echo "a `           ` b"'),
    ('echo "grep -rn x lib"', 'echo "grep -rn x lib"'),
    ("python3 -c 'import os; os.system(\"grep -rn x lib\")'",
     "python3 -c 'import os; os.system(\"grep -rn x lib\")'"),
    ('cat <<< "grep -rn x lib"', 'cat <<< "grep -rn x lib"'),
    ("echo $'tab\\there'", "echo $'tab\\there'"),
])
def test_comments_and_backtick_bodies_are_blanked_words_are_kept(command, expected):
    assert _view(command) == expected


def test_ranges_cover_commands_and_substitution_bodies_once():
    command = 'n=$(grep -c a f); echo "$(git status)" && (cd lib; ls) # c'
    ranges = command_rewrite_text(command).ranges
    assert [command[start:end] for start, end in ranges] == [
        'n=$(grep -c a f)', 'grep -c a f', 'echo "$(git status)" ', 'git status',
        'cd lib', 'ls']
    assert len(set(ranges)) == len(ranges)


def test_failed_arithmetic_reading_leaves_no_stale_range():
    command = 'x=$( (echo a) | cat ); y=$((1 + 2))'
    ranges = command_rewrite_text(command).ranges
    assert [command[start:end] for start, end in ranges] == [
        'x=$( (echo a) | cat )', 'echo a', 'cat ', 'y=$((1 + 2))']


def test_ansi_c_string_whose_end_differs_between_shells_is_undelimited():
    """dash (POSIX sh, this host's /bin/sh) ends `$'a\\'b'` at the second quote,
    bash at the third; a rewrite cannot know which shell reads it."""
    command = "echo $'a\\'b' 'x; grep -rn x lib'"
    with pytest.raises(ShellStructureUnknown):
        shell_rewrite_text(command)
    assert command_rewrite_text(command) is None
    parse_shell_structure(command)  # the guards keep their bash reading


def test_script_judgement_reads_the_pipeline_neighbour():
    items = parse_shell_structure("cat <<'EOF' | sh\nx\nEOF")
    assert shell_script_heredocs(items, 0) == items[0].heredocs
    assert shell_script_heredocs(items, 1) == ()
    items = parse_shell_structure("cat <<'EOF' | sort\nx\nEOF")
    assert shell_script_heredocs(items, 0) == ()
    items = parse_shell_structure("cat <<'A' <<'B' | sh\na\nA\nb\nB")
    assert shell_script_heredocs(items, 0) == ()
