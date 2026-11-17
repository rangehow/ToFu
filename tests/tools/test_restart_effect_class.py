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


def _row(call_id, tool='read_files', *, llm_round=3, attempt='a1', content='ok', **extra):
    row = {'toolCallId': call_id, 'toolName': tool, 'toolArgs': '{}', 'llmRound': llm_round,
           'attemptId': attempt}
    if content is not None:
        row['toolContent'] = content
    row.update(extra)
    return row


def test_a_cut_batch_of_repeatable_calls_is_dropped_whole():
    """The model re-issues the batch itself and reads no cut receipt (docs/RUN_CONTINUITY.md)."""
    from lib.tools.restart_effect_class import repeatable_restart_cut_batch
    rows = [_row('a', llm_round=1), {'type': 'progress'},
            _row('b', 'poll', _restartCut=True), _row('c', 'read_files')]
    assert repeatable_restart_cut_batch(rows, worktree=None) == (2, 3)


def test_a_batch_the_restart_did_not_cut_is_kept():
    """The model already read it: dropping it would rerun finished work."""
    from lib.tools.restart_effect_class import repeatable_restart_cut_batch
    assert repeatable_restart_cut_batch([_row('a'), _row('b')], worktree=None) == ()
    assert repeatable_restart_cut_batch([], worktree=None) == ()


def test_a_dead_process_cut_counts_as_cut():
    from lib.tools.restart_effect_class import repeatable_restart_cut_batch
    rows = [_row('a', llm_round=1), _row('b', 'read_files', content=None)]
    assert repeatable_restart_cut_batch(rows, worktree=None) == (1,)


def test_one_unrepeatable_call_keeps_the_whole_batch():
    from lib.tools.restart_effect_class import repeatable_restart_cut_batch
    write = _row('w', 'write_file', executionEffect={'state': 'returned', 'recovery': 'reconcile',
                                                     'operationId': 'op'})
    rows = [_row('r', 'read_files', _restartCut=True), write]
    assert repeatable_restart_cut_batch(rows, worktree=None) == ()
    never_started = {**write, 'executionEffect': {'state': 'not_started', 'recovery': 'reconcile'}}
    assert repeatable_restart_cut_batch([rows[0], never_started], worktree=None) == (0, 1)


@pytest.mark.parametrize('command, repeatable', [
    ('git status', True),
    ('.venv/bin/python3 -m pytest -q tests/a.py', True),
    ('make test TESTS="tests/a.py"', True),
    ('git reset --hard HEAD~1', False),
    ('mv a b', False),
    ('git push origin HEAD', False),
])
def test_only_reads_and_confined_test_runs_repeat(command, repeatable):
    """A verifiable Git or file write changes state again when it runs twice."""
    from lib.tools.restart_effect_class import repeatable_cut_call
    row = {'toolName': 'run_command', 'toolArgs': {'command': command},
           'executionEffect': {'state': 'entered', 'recovery': 'reconcile'}}
    assert repeatable_cut_call(row, worktree=WORKTREE) is repeatable


def test_a_test_run_outside_an_isolated_worktree_does_not_repeat():
    from lib.tools.restart_effect_class import repeatable_cut_call
    row = {'toolName': 'run_command', 'toolArgs': {'command': 'pytest -q'},
           'executionEffect': {'state': 'entered', 'recovery': 'reconcile'}}
    assert repeatable_cut_call(row, worktree=None) is False
