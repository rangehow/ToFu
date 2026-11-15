"""A restart-cut call's effect class decides whether it resumes by itself (docs/RUN_CONTINUITY.md)."""
import pytest

from lib.tools.restart_effect_class import cut_call_effect_class, run_command_effect_class

pytestmark = pytest.mark.unit

WORKTREE = '/worktrees/worker'


@pytest.mark.parametrize('command, expected', [
    ('git status', 'read_only'),
    ('git ls-remote origin', 'read_only'),
    ('git log --oneline -3 && git ls-remote origin', 'read_only'),
    ('ls -la | head', 'read_only'),
    ('grep -rn needle lib | wc -l', 'read_only'),
    ('cd /worktrees/worker && PYTHONPATH=$PWD .venv/bin/python3 -m pytest -q tests/a.py', 'verifiable'),
    ('timeout 600 .venv/bin/python3 -m pytest -q tests/a.py', 'verifiable'),
    ('make test TESTS="tests/a.py"', 'verifiable'),
    ('.venv/bin/python3 scripts/verify.py run tests/a.py', 'verifiable'),
    ('npm test', 'verifiable'),
    ('git add -A && git commit -m wip', 'verifiable'),
    ('git merge main', 'verifiable'),
    ('sed -i s/a/b/ lib/x.py', 'verifiable'),
    ('rm -rf build', 'verifiable'),
    ('git push origin HEAD', 'unknown'),
    ('git fetch origin', 'unknown'),
    ('git branch -D other', 'unknown'),
    ('npm publish', 'unknown'),
    ('pip install requests', 'unknown'),
    ('curl -X POST http://example.invalid', 'unknown'),
    ('scp a host:b', 'unknown'),
    ('python3 deploy.py', 'unknown'),
    ('rm -rf /tmp/elsewhere', 'unknown'),
    ('echo hi > /etc/motd', 'unknown'),
    ('cd /elsewhere && pytest', 'unknown'),
    ('cat $(ls)', 'unknown'),
    ('', 'unknown'),
])
def test_a_run_command_is_classified_by_what_it_can_change(command, expected):
    assert run_command_effect_class(command, WORKTREE, WORKTREE) == expected


@pytest.mark.parametrize('declared', ['read_only', 'idempotent'])
def test_a_tool_declaration_decides_first(declared):
    assert cut_call_effect_class('create_memory', '{}', declared=declared, worktree=None) == declared


def test_a_reconciling_tool_other_than_run_command_needs_a_decision():
    assert cut_call_effect_class('write_file', '{"path": "a"}', declared='reconcile',
                                 worktree=WORKTREE) == 'unknown'


def test_a_working_directory_outside_the_worktree_needs_a_decision():
    arguments = {'command': 'pytest -q', 'working_dir': '/elsewhere'}
    assert cut_call_effect_class('run_command', arguments, declared='reconcile', worktree=WORKTREE) == 'unknown'
    inside = {'command': 'pytest -q', 'working_dir': 'sub'}
    assert cut_call_effect_class('run_command', inside, declared='reconcile', worktree=WORKTREE) == 'verifiable'


def test_without_an_isolated_worktree_only_a_reading_command_resumes():
    """A task bound to a shared checkout has no confined state the resumed Worker can re-check."""
    def classify(command):
        return cut_call_effect_class('run_command', {'command': command}, declared='reconcile', worktree=None)
    assert classify('git ls-remote origin') == 'read_only'
    assert classify('python3 -m pytest -q') == 'unknown'
    assert classify('rm -rf build') == 'unknown'
    assert classify('git commit -am wip') == 'unknown'


def test_unreadable_arguments_need_a_decision():
    assert cut_call_effect_class('run_command', 'not json', declared='reconcile', worktree=WORKTREE) == 'unknown'
    assert cut_call_effect_class('run_command', '[]', declared='reconcile', worktree=WORKTREE) == 'unknown'
