"""A tool effect whose completion was never recorded cannot wedge its Worker.

Incident 2026-11-11 (EFFECT-FENCE-1):
- A storage outage swallowed one run_command completion.
- A restart then resumed the same dispatch ledger.
- From then on, every effectful call and every platform read refused
  "Prior tool effect remains unconfirmed": 93 refusals in 14 conversations.

These specs pin the approved repair:
- (a) a continuation settles earlier attempts' intents as interrupted, effect
  unknown, and names them to the model;
- (b) read-only and idempotent calls are never fenced and never fence;
- (c) a failed completion write is kept and retried;
- (d) the fence names its operation and Turn, and inspect_tool_effect
  acknowledges it;
- (e) publication waits only on unconfirmed side effects.
"""
import pytest

from lib.llm.llm_errors import AbortedError
from lib.project_dispatch import admission
from lib.project_dispatch.accounting import tool_operation_id
from lib.project_dispatch.delegation import capture_worker_policy
from lib.project_dispatch.repository import CONTRACT
from tests.project_mod.test_coordinator_continuation import activate, ready, run
from tests.project_mod.test_coordinator_workers import (
    _follow_up_turn, _restart_continuation, _worker_task, base_runtime, context, runtime,
)

__all__ = ('base_runtime', 'context', 'run', 'runtime')
NONCE = '0123456789abcdef0123456789abcdef'
pytestmark = pytest.mark.unit

# Request-owned declarations, shaped like the registry's compiled documents.
DOCUMENTS = {
    'read_files': {'name': 'read_files', 'permission': 'read', 'idempotency': 'idempotent',
                   'effectRecovery': 'idempotent'},
    'poll': {'name': 'poll', 'permission': 'read', 'idempotency': 'idempotent',
             'effectRecovery': 'idempotent'},
    'inspect_tool_effect': {'name': 'inspect_tool_effect', 'permission': 'read',
                            'idempotency': 'read_only', 'effectRecovery': 'read_only'},
    'integration_checkpoint': {'name': 'integration_checkpoint', 'permission': 'read',
                               'idempotency': 'idempotent', 'effectRecovery': 'idempotent'},
    'run_command': {'name': 'run_command', 'permission': 'write', 'idempotency': 'non_idempotent',
                    'effectRecovery': 'reconcile'},
    'write_file': {'name': 'write_file', 'permission': 'write', 'idempotency': 'non_idempotent',
                   'effectRecovery': 'reconcile'},
    # Gateway schemas arrive after assembly and compile a conservative reconcile
    # document (contracts.compile_execution_contract_documents), as in production.
    'execute_tools': {'name': 'execute_tools', 'permission': 'read', 'idempotency': 'read_only',
                      'effectRecovery': 'reconcile'},
    'search_tools': {'name': 'search_tools', 'permission': 'read', 'idempotency': 'read_only',
                     'effectRecovery': 'reconcile'},
    **{name: {'name': name, 'permission': 'read', 'idempotency': 'idempotent',
              'effectRecovery': 'idempotent'}
       for name in ('await_project_workers', 'get_project_workers', 'get_conversation_status')},
    'get_project_messages': {'name': 'get_project_messages', 'permission': 'read',
                             'idempotency': 'read_only', 'effectRecovery': 'read_only'},
    # An MCP tool the server annotates readOnlyHint (hope's watch_job long-poll).
    'mcp__hope__watch_job': {'name': 'mcp__hope__watch_job', 'permission': 'read',
                             'idempotency': 'read_only', 'effectRecovery': 'read_only'},
}


def ledger(r, row):
    return [(item['tool_name'], item['tool_ordinal'], item['phase'])
            for item in r.c.repo.read(row['id']).get('toolAttempts', [])]


def lose_completions(r, monkeypatch):
    """Every later ledger write fails until the returned restore() runs."""
    mutate = r.c.repo.mutate
    monkeypatch.setattr(r.c.repo, 'mutate',
                        lambda *a, **kw: (_ for _ in ()).throw(OSError('storage unavailable')))
    return lambda: monkeypatch.setattr(r.c.repo, 'mutate', mutate)


def execute(task, name, monkeypatch, *, call_id=None, effect_state=None):
    """Run one admitted call through the executor boundary that opens ledger intents."""
    from lib.tasks_pkg.executor import _execute as executor
    handled = []

    def handler(task_, tc, fn_name, tc_id, fn_args, rn, round_entry, *rest):
        handled.append(fn_name)
        if effect_state is not None:
            round_entry['executionEffect'] = {'operationId': '0' * 32, 'state': effect_state,
                                              'recovery': 'reconcile', 'scope': 'attempt'}
        return tc_id, 'ok', False
    monkeypatch.setattr(executor, '_dispatch_admitted_tool_one', handler)
    result = executor._execute_admitted_tool_one(
        task, {}, name, call_id or 'call-' + name, {}, 1, {}, {}, None, False, None)
    assert handled == [name]
    return result


# (a) ------------------------------------------------------------------------

@pytest.mark.parametrize('successor', ['continue', 'follow_up'])
def test_a_continuation_settles_earlier_intents_as_interrupted_and_admits_new_effects(runtime, successor):
    r = runtime
    task, row = _worker_task(r)
    task['_executionBudget'].begin_tool_attempt(task, 'toolu_lost', 'run_command', 16)
    # The process dies here: no completion or settlement is ever written.
    assert ledger(r, row) == [('run_command', 1, 'intent')]
    resumed = (_restart_continuation(r, task, row) if successor == 'continue'
               else _follow_up_turn(r, task, row))

    admission.bind_account(resumed)

    assert ledger(r, row) == [('run_command', 1, 'interrupted_effect_unknown')]
    stored = r.c.repo.read(row['id'])['toolAttempts'][0]
    [settled] = resumed['_interruptedToolEffects']
    assert settled == {'tool_name': 'run_command', 'tool_call_id': 'toolu_lost', 'tool_ordinal': 1,
                       'operation_id': tool_operation_id(stored)}
    budget = resumed['_executionBudget']
    event = budget.begin_tool_attempt(resumed, 'next', 'write_file', 1)
    budget.finish_tool_attempt(event, phase='executor_returned')
    assert ledger(r, row)[-1] == ('write_file', 2, 'executor_returned')
    assert not resumed.get('aborted')


def test_settling_is_idempotent_and_leaves_terminal_rows_alone(runtime):
    r = runtime
    task, row = _worker_task(r)
    budget = task['_executionBudget']
    done = budget.begin_tool_attempt(task, 'done', 'run_command', 1)
    budget.finish_tool_attempt(done, phase='executor_returned')
    budget.begin_tool_attempt(task, 'lost', 'write_file', 2)
    resumed = _restart_continuation(r, task, row)
    admission.bind_account(resumed)
    settled = ledger(r, row)
    assert settled == [('run_command', 1, 'executor_returned'),
                       ('write_file', 2, 'interrupted_effect_unknown')]
    again = _restart_continuation(r, resumed, r.c.repo.read(row['id']), attempt_id='second-resume')
    admission.bind_account(again)
    assert ledger(r, row) == settled
    assert again['_interruptedToolEffects'] == []


def test_the_settled_interruption_reaches_the_model_naming_the_call():
    from lib.tasks_pkg.orchestrator._resume_state import append_interrupted_effect_note
    task = {'id': 'task', '_interruptedToolEffects': [
        {'tool_name': 'run_command', 'tool_call_id': 'toolu_lost', 'tool_ordinal': 16,
         'operation_id': 'a' * 32}]}
    messages = [{'role': 'user', 'content': 'go'}]
    append_interrupted_effect_note(task, messages)
    note = messages[-1]
    assert note['role'] == 'user' and len(messages) == 2
    text = note['content'] if isinstance(note['content'], str) else str(note['content'])
    assert 'run_command' in text and 'toolu_lost' in text and 'a' * 32 in text
    assert 'effect is unknown' in text and 'before repeating' in text
    # Nothing to say: nothing is appended.
    untouched = [{'role': 'user', 'content': 'go'}]
    append_interrupted_effect_note({'id': 'task'}, untouched)
    assert untouched == [{'role': 'user', 'content': 'go'}]


# (b) ------------------------------------------------------------------------

@pytest.mark.parametrize('tool', ['read_files', 'poll', 'inspect_tool_effect', 'integration_checkpoint',
                                  'mcp__hope__watch_job'])
def test_a_read_only_or_idempotent_call_after_an_unconfirmed_effect_runs_without_an_intent(
        runtime, monkeypatch, tool):
    """Opening no intent is also why a read-only long-poll that genuinely times
    out (ndp 2026-11-12, mcp__hope__watch_job) can never fence its Worker."""
    from tests.project_mod.test_coordinator_request_budget import bind
    r = runtime
    bind(r)
    task, row = _worker_task(r)
    task['_toolContractDocumentsByName'] = DOCUMENTS
    _stranded(r, row, task)

    assert execute(task, tool, monkeypatch)[1] == 'ok'

    assert ledger(r, row) == [('run_command', 7, 'intent')]
    # A side effect is still fenced by the same unconfirmed occurrence.
    with pytest.raises(AbortedError, match='Prior tool effect remains unconfirmed'):
        execute(task, 'run_command', monkeypatch)


def test_a_read_call_writes_a_retained_completion_on_its_way(runtime, monkeypatch):
    r = runtime
    task, row = _worker_task(r)
    task['_toolContractDocumentsByName'] = DOCUMENTS
    budget = task['_executionBudget']
    event = budget.begin_tool_attempt(task, 'toolu_lost', 'run_command', 1)
    restore = lose_completions(r, monkeypatch)
    budget.finish_tool_attempt(event, phase='executor_returned')
    # Still unwritable: the read neither waits on it nor fails.
    assert execute(task, 'read_files', monkeypatch)[1] == 'ok'
    restore()
    assert ledger(r, row) == [('run_command', 1, 'intent')]
    assert execute(task, 'poll', monkeypatch)[1] == 'ok'
    assert ledger(r, row) == [('run_command', 1, 'executor_returned')]


def test_an_effectful_call_still_opens_an_intent(runtime, monkeypatch):
    r = runtime
    task, row = _worker_task(r)
    task['_toolContractDocumentsByName'] = DOCUMENTS
    execute(task, 'run_command', monkeypatch)
    assert ledger(r, row) == [('run_command', 1, 'executor_returned')]


def test_the_fence_class_is_the_registered_declaration_then_the_reviewed_contract(runtime, monkeypatch):
    """A built-in's registry declaration decides; a dynamic tool's reviewed request
    contract decides; with neither, the call is an effect."""
    r = runtime
    task, row = _worker_task(r)
    budget = task['_executionBudget']
    begin, seen = budget.begin_tool_attempt, {}

    def spy(task_, call_id, name, round_num, **kwargs):
        seen[name] = kwargs['effect_recovery']
        return begin(task_, call_id, name, round_num, **kwargs)
    monkeypatch.setattr(budget, 'begin_tool_attempt', spy)
    task['_toolContractDocumentsByName'] = {**DOCUMENTS, 'mcp__docs__read': {
        'name': 'mcp__docs__read', 'permission': 'read', 'idempotency': 'read_only',
        'effectRecovery': 'read_only'}}
    assert execute(task, 'read_files', monkeypatch)[1] == 'ok'
    assert execute(task, 'mcp__docs__read', monkeypatch)[1] == 'ok'
    # Observed as an effect, so this grant's ledger refuses the ungranted tool.
    with pytest.raises(AbortedError, match="not in this Worker's grant"):
        execute(task, 'mcp__docs__mutate', monkeypatch)
    assert seen == {'read_files': 'idempotent', 'mcp__docs__read': 'read_only',
                    'mcp__docs__mutate': 'reconcile'}
    assert ledger(r, row) == []


def test_a_pre_start_refusal_settles_not_entered_and_fences_nothing(runtime, monkeypatch):
    r = runtime
    task, row = _worker_task(r)
    task['_toolContractDocumentsByName'] = DOCUMENTS
    execute(task, 'run_command', monkeypatch, effect_state='not_started')
    assert ledger(r, row) == [('run_command', 1, 'not_entered')]
    execute(task, 'run_command', monkeypatch, call_id='after-refusal')
    assert ledger(r, row)[-1] == ('run_command', 2, 'executor_returned')


# (c) ------------------------------------------------------------------------

def test_a_lost_completion_is_written_before_the_next_effect(runtime, monkeypatch):
    r = runtime
    task, row = _worker_task(r)
    budget = task['_executionBudget']
    event = budget.begin_tool_attempt(task, 'toolu_lost', 'run_command', 1)
    restore = lose_completions(r, monkeypatch)
    budget.finish_tool_attempt(event, phase='executor_returned')
    restore()
    assert ledger(r, row) == [('run_command', 1, 'intent')]

    budget.begin_tool_attempt(task, 'next', 'write_file', 2)

    assert ledger(r, row) == [('run_command', 1, 'executor_returned'), ('write_file', 2, 'intent')]


def test_an_unwritable_completion_refuses_the_next_effect_and_settles_on_resume(runtime, monkeypatch):
    r = runtime
    task, row = _worker_task(r)
    budget = task['_executionBudget']
    event = budget.begin_tool_attempt(task, 'toolu_lost', 'run_command', 1)
    restore = lose_completions(r, monkeypatch)
    budget.finish_tool_attempt(event, phase='executor_returned')
    with pytest.raises(AbortedError) as refused:
        budget.begin_tool_attempt(task, 'next', 'write_file', 2)
    message = str(refused.value)
    assert 'run_command #1 toolu_lost phase=executor_returned' in message
    assert 'storage unavailable' in message
    restore()
    assert ledger(r, row) == [('run_command', 1, 'intent')]
    # The process dies with the completion still unwritten: resume settles it.
    resumed = _restart_continuation(r, task, row)
    admission.bind_account(resumed)
    assert ledger(r, row) == [('run_command', 1, 'interrupted_effect_unknown')]


def test_settlement_writes_a_lost_completion(runtime, monkeypatch):
    from lib.agent_core.execution_budget import close_execution_budget
    r = runtime
    task, row = _worker_task(r)
    budget = task['_executionBudget']
    event = budget.begin_tool_attempt(task, 'toolu_lost', 'run_command', 1)
    restore = lose_completions(r, monkeypatch)
    budget.finish_tool_attempt(event, phase='executor_exception')
    restore()
    close_execution_budget(task)
    assert ledger(r, row) == [('run_command', 1, 'executor_exception')]


# (d) ------------------------------------------------------------------------

def _stranded(r, row, task, *, ordinal=7, tool='run_command', call='toolu_old'):
    """An intent no live attempt owns, as an earlier unsettled attempt leaves it."""
    from lib.tasks_pkg.workspace_delivery import WORKSPACE_WORK_ID_KEY
    stored = r.c.repo.read(row['id'])
    nonce = (stored.get('toolReservations') or {}).get('accountNonce') or NONCE
    r.attempts['old-attempt'] = {'conversationId': task['convId'], 'turnId': 'old-turn',
                                 'attemptId': 'old-attempt', 'taskId': 'conversation-attempt:old-attempt',
                                 'status': 'interrupted'}
    intent = {'root_task_id': task[WORKSPACE_WORK_ID_KEY], 'tool_ordinal': ordinal,
              'task_id': 'conversation-attempt:old-attempt', 'tool_call_id': call,
              'tool_name': tool, 'phase': 'intent', 'account_nonce': nonce}
    r.c.repo.mutate(row['id'], lambda value: value.setdefault('toolAttempts', []).append(intent))
    return tool_operation_id(intent)


@pytest.mark.parametrize('tool', ['run_command', 'execute_tools'])
def test_the_fence_names_operation_and_turn_and_inspection_acknowledges_it(runtime, monkeypatch, tool):
    from tests.project_mod.test_coordinator_request_budget import bind
    r = runtime
    bind(r)
    task, row = _worker_task(r)
    operation = _stranded(r, row, task, tool=tool)
    budget = task['_executionBudget']
    with pytest.raises(AbortedError) as refused:
        budget.begin_tool_attempt(task, 'next', 'write_file', 2)
    message = str(refused.value)
    assert 'not a call budget' in message and f'{tool} #7 toolu_old phase=intent' in message
    assert f'operation_id={operation}' in message and 'turn_id=old-turn' in message
    assert 'inspect_tool_effect' in message

    # Allowed while fenced: the inspection is a read and opens no intent.
    task['_toolContractDocumentsByName'] = DOCUMENTS
    from lib.tasks_pkg.handlers.tool_effect_inspection import inspect_for_task
    result = inspect_for_task(task, {'operation_id': operation, 'turn_id': 'old-turn'})
    assert result == {'ok': True, 'contract': 'tofu.conversation-sync.tool-effect-inspection/v1',
                      'conversationId': task['convId'], 'turnId': 'old-turn',
                      'operationId': operation, 'outcome': 'unknown', 'reason': 'acknowledged'}
    assert ledger(r, row) == [(tool, 7, 'effect_unknown_acknowledged')]
    # Acknowledging twice is a replay, never a second settlement.
    assert inspect_for_task(task, {'operation_id': operation, 'turn_id': 'old-turn'}) == result
    event = budget.begin_tool_attempt(task, 'next', 'write_file', 2)
    budget.finish_tool_attempt(event, phase='executor_returned')
    assert ledger(r, row)[-1][::2] == ('write_file', 'executor_returned')


def test_a_late_real_completion_replaces_an_unknown_settlement(runtime, monkeypatch):
    r = runtime
    task, row = _worker_task(r)
    budget = task['_executionBudget']
    event = budget.begin_tool_attempt(task, 'toolu_slow', 'run_command', 1)
    stored = r.c.repo.read(row['id'])['toolAttempts'][0]
    r.c.repo.mutate(row['id'], lambda value: value['toolAttempts'][0].update(
        phase='effect_unknown_acknowledged'))
    budget.finish_tool_attempt(event, phase='executor_returned')
    assert ledger(r, row) == [('run_command', stored['tool_ordinal'], 'executor_returned')]


# The two shapes after the 00:30 restart (2026-11-12) ----------------------------

def test_a_stranded_coordinator_wait_never_fences_its_reads(run, monkeypatch):
    """Coordinator 1a390889: await_project_workers #255 stayed an intent and refused
    get_project_workers, get_conversation_status and get_project_messages."""
    r = run
    # Its continuation policy was frozen before poll absorbed await_project_workers;
    # only such a policy names, and so can strand, the retired wait
    # (docs/PROJECT_COORDINATION.md, decision 2026-11-12).
    frozen = ['await_project_workers' if name == 'poll' else name for name in CONTRACT['continuationTools']]
    with monkeypatch.context() as before_poll:
        before_poll.setitem(CONTRACT, 'continuationTools', frozen)
        capture_worker_policy(r.task, r.task['config'], [{'function': {'name': name}} for name in
                              frozen + ['run_command', 'read_files', 'write_file']],
                              'model-a', worker_model='model-a')
    ready(r)
    row = activate(r)
    task = r.task
    task['_toolContractDocumentsByName'] = DOCUMENTS
    _stranded(r, row, task, ordinal=255, tool='await_project_workers',
              call='toolu_01B1mAQucaBsczxJkKk5k945')
    # Its next wait is poll: name repair routes a fresh call of the retired name there.
    for name in ('get_project_workers', 'get_conversation_status', 'get_project_messages', 'poll'):
        assert execute(task, name, monkeypatch)[1] == 'ok'
    assert ledger(r, row) == [('await_project_workers', 255, 'intent')]


def test_a_gateway_call_wrapping_a_checkpoint_opens_no_intent_of_its_own(runtime, monkeypatch):
    """FE-COORD-VIEW-R2: execute_tools #126 wrapping integration_checkpoint fenced the Worker.
    Each inner call is fenced at its own entry; neither of these is an effect to fence."""
    r = runtime
    task, row = _worker_task(r)
    task['_toolContractDocumentsByName'] = DOCUMENTS
    for name in ('execute_tools', 'integration_checkpoint', 'search_tools'):
        assert execute(task, name, monkeypatch)[1] == 'ok'
    assert ledger(r, row) == []
