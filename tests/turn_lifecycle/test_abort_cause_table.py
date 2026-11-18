"""Who stopped a Turn: every abort reason the code stamps, at every site that reads it.

``lib.turn_verdict.ABORT_CAUSES`` (docs/contracts/TURN_SETTLEMENT.md, "Abort
causes") is the one table. Four sites read it through ``abort_cause``: the
Turn verdict, the tool-result label, the post-loop finish reason with its error
envelope, and the model-request activity outcome. This file pins each stamped
reason's answer at all four, and requires every reason the code stamps to be
registered: an unregistered stamp would read as a person's Stop.
"""
import ast
import logging
from pathlib import Path
import subprocess

import pytest

import lib.turn_verdict as turn_verdict
from lib.tasks_pkg.manager._stream import _abort_request_attribution
from lib.tasks_pkg.orchestrator._finalize import _settle_post_loop_finish_reason
from lib.turn_verdict import ABORT_CAUSES, abort_cause, derive_task_verdict, system_abort_label

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


class AbortedError(Exception):
    """Named like the agent loop's abort exception, which the activity classifier keys on."""


PERSON = (('interrupted', 'user_abort', 'user_abort'), '', 'aborted', None, ('aborted', None))
RESTART = (('interrupted', 'server_restart', 'system_recovery'), 'server restart', 'aborted', None,
           ('aborted', None))


def _fence(reason):
    return (('failed', 'internal_fence_abort', 'external_authority'),
            f'storage authority fence ({reason})', 'error', 'storage_fence', ('failed', 'storage_fence'))


def _dispatch(reason):
    return (('failed', reason, 'external_authority'), f'project dispatch authority ({reason})', 'error',
            reason, ('failed', reason))


ATTRIBUTION = {
    '': PERSON,
    'send_abort_race': PERSON,
    'superseded_by_new_task': PERSON,
    'conversation_deleted': PERSON,
    'attempt_stopped_by_user': PERSON,
    'human_stop': PERSON,
    'superseded_by_human': PERSON,
    'real_message_preempts_vu': PERSON,
    'user_abort': PERSON,
    'server_shutdown': RESTART,
    'restart_recovery': RESTART,
    # A project lead held its Worker (control_conversation action=hold): a pause like a
    # restart, resumable from its checkpoint, never a person's Stop.
    'lead_hold': (('interrupted', 'lead_hold', 'system_recovery'), 'project lead hold', 'aborted', None,
                  ('aborted', None)),
    'worker_lease_lost': _fence('worker_lease_lost'),
    'turn_attempt_stale_fence': _fence('turn_attempt_stale_fence'),
    'conversation_attempt_stale_fence': _fence('conversation_attempt_stale_fence'),
    'storage_frame_overflow': _fence('storage_frame_overflow'),
    'storage_authority_integrity': _fence('storage_authority_integrity'),
    'dispatch_grant_revoked': _dispatch('dispatch_grant_revoked'),
    'dispatch_authority_lost': _dispatch('dispatch_authority_lost'),
    'stuck_no_progress': (('failed', 'worker_lost', 'generation_error'), 'stuck-task reaper', 'error', None,
                          ('aborted', None)),
    # The unattended tool-loop breaker (tool_dispatch/_parse.py) stopped work
    # that loops on a tool that does not exist; no person did.
    'hallucination_loop': (('failed', 'generation_error', 'generation_error'), 'unattended tool-loop breaker',
                           'error', 'tool_loop', ('failed', 'tool_loop')),
    # The user-action guard (lib/tasks_pkg/user_action_guard.py) stopped a Turn whose
    # next tool call needed an action only the user can take; no person did.
    'awaiting_user_action': (('failed', 'generation_error', 'generation_error'), 'user-action guard',
                             'error', 'tool_call_rejected', ('failed', 'tool_call_rejected')),
}


def _settled(reason):
    task = {'aborted': True, '_abort_reason': reason}
    finish = _settle_post_loop_finish_reason(
        task, 'stop', loop_exit_reason='aborted', abort_detected_phase='model_round',
        model='model', tid='task')
    return task, finish


def _observed(reason):
    task = {'aborted': True, 'status': 'aborted', 'finishReason': 'aborted', '_abort_reason': reason}
    verdict = derive_task_verdict(task)
    settled, finish = _settled(reason)
    status, kind, _detail = _abort_request_attribution({'_abort_reason': reason}, AbortedError())
    return ((verdict.status.value, verdict.cause, verdict.evidence.value), system_abort_label(reason),
            finish, (settled.get('error') or {}).get('kind'), (status, kind))


@pytest.mark.parametrize('reason', sorted(ATTRIBUTION))
def test_each_stamped_abort_reason_is_attributed_the_same_at_every_site(reason):
    assert _observed(reason) == ATTRIBUTION[reason]


def test_every_registered_abort_reason_has_its_attribution_pinned():
    # The retired Autopilot carrier is not a stop; its own test follows.
    assert set(ATTRIBUTION) - {''} == set(ABORT_CAUSES) - {'legacy_autopilot_retired'}


def test_the_tool_loop_breaker_fails_the_turn_without_inviting_a_retry():
    """The same unattended Turn would call the same invented tool again."""
    task, finish = _settled('hallucination_loop')
    assert finish == 'error'
    assert (task['error']['kind'], task['error']['retryable']) == ('tool_loop', False)
    assert 'unattended tool-loop breaker' in task['error']['detail']


def test_a_restart_carrier_settles_as_a_restart_without_the_abort_flag():
    """Restart recovery settles carriers that never set ``aborted`` (worker_recovery, SQL restart)."""
    verdict = derive_task_verdict({'status': 'aborted', 'finishReason': 'aborted',
                                   '_abort_reason': 'server_shutdown'})
    assert (verdict.status.value, verdict.cause) == ('interrupted', 'server_restart')


def test_a_reason_on_a_task_that_was_not_stopped_changes_nothing():
    """The retired Autopilot carrier records its reason beside an error; it is not a stop."""
    verdict = derive_task_verdict({'status': 'error', 'finishReason': 'error',
                                   'error': {'kind': 'bad_request', 'message': 'retired'},
                                   '_abort_reason': 'legacy_autopilot_retired'})
    assert (verdict.status.value, verdict.cause) == ('failed', 'generation_error')


def test_an_unregistered_reason_reads_as_a_person_and_is_reported_once(monkeypatch, caplog):
    monkeypatch.setattr(turn_verdict, '_UNREGISTERED_REPORTED', set())
    with caplog.at_level(logging.ERROR, logger=turn_verdict.__name__):
        for _ in range(3):
            assert abort_cause('a_stop_without_a_row') is abort_cause('')
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert "'a_stop_without_a_row'" in caplog.records[0].getMessage()


# What the completeness scan below follows.
_SCANNED_ROOTS = ('lib', 'routes', 'tofu_agent')
# A function that writes its caller's reason into ``_abort_reason``, by the
# name of that parameter: its callers' literals and the parameter's default
# are stamps.
_PASS_THROUGH = {
    'abort_owned': 'reason',
    'cancel_task': 'reason',
    'abort_running_tasks_for_conv': 'reason',
    'quiesce_running_tasks': 'reason',
    '_mark_shutdown_quiesced': 'reason',
    'encode_cancellation': 'reason',
    'abort_attempt': 'abort_reason',
    'stale_attempt_abort_reason': 'fallback',
}
# A function whose returned value is stamped.
_RETURNS_A_REASON = frozenset({'stale_attempt_abort_reason', 'authority_stop_reason'})
# Writes whose value a caller or a durable record supplies. Each one passes
# through a stamp that the scan finds at its source.
_PASS_THROUGH_WRITES = frozenset({
    ('lib/agent_core/task_runtime.py', 'TaskRuntime.abort_owned'),
    ('lib/conversation_sync/worker_execution.py', 'cancel_claimed_attempt'),
    ('lib/conversation_sync/worker_recovery.py', 'recover_exited_attempt'),
    ('lib/tasks_pkg/manager/_persist.py', 'build_result_meta'),
    ('lib/tasks_pkg/manager/_registry.py', '_mark_shutdown_quiesced'),
    ('lib/tasks_pkg/manager/_registry.py', 'abort_running_tasks_for_conv._mark_aborted'),
    ('lib/turn_lifecycle.py', 'abort_attempt'),
})


def _name_of(expression):
    if isinstance(expression, ast.Name):
        return expression.id
    return expression.attr if isinstance(expression, ast.Attribute) else ''


def _called(call):
    return _name_of(call.func)


# Calls that run their first argument with the rest (``asyncio.to_thread``).
_FORWARDING_CALLS = frozenset({'to_thread', 'partial'})


def _is_reason_key(node):
    return isinstance(node, ast.Constant) and node.value == '_abort_reason'


def _parameters(function):
    names = [argument.arg for argument in (*function.args.posonlyargs, *function.args.args)]
    return names[1:] if names[:1] in (['self'], ['cls']) else names


def _parameter_default(function, name):
    positional = [*function.args.posonlyargs, *function.args.args]
    for argument, default in zip(positional[len(positional) - len(function.args.defaults):],
                                 function.args.defaults):
        if argument.arg == name:
            return default
    for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
        if argument.arg == name:
            return default
    return None


class _StampScan(ast.NodeVisitor):
    """Collect the literals one module stamps into ``_abort_reason`` and its dynamic writes."""

    def __init__(self, path, positions):
        self.path, self.positions = path, positions
        self.scopes, self.names = [], []
        self.literals, self.dynamic_writes = set(), set()

    def visit_ClassDef(self, node):
        self.names.append(node.name)
        self.generic_visit(node)
        self.names.pop()

    def visit_FunctionDef(self, node):
        self.names.append(node.name)
        self.scopes.append(node)
        parameter = _PASS_THROUGH.get(node.name)
        default = _parameter_default(node, parameter) if parameter else None
        if default is not None:
            self.literals |= self._resolve(default)[0]
        if node.name in _RETURNS_A_REASON:
            for item in ast.walk(node):
                if isinstance(item, ast.Return) and item.value is not None:
                    self.literals |= self._resolve(item.value)[0]
        self.generic_visit(node)
        self.scopes.pop()
        self.names.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        if any(isinstance(target, ast.Subscript) and _is_reason_key(target.slice) for target in node.targets):
            self._stamp(node.value)
        self.generic_visit(node)

    def visit_Dict(self, node):
        for key, value in zip(node.keys, node.values):
            if key is not None and _is_reason_key(key):
                self._stamp(value)
        self.generic_visit(node)

    def visit_Call(self, node):
        for keyword in node.keywords:
            if keyword.arg == '_abort_reason':
                self._stamp(keyword.value)
        name, arguments = _called(node), node.args
        if name in _FORWARDING_CALLS and arguments and _name_of(arguments[0]) in _PASS_THROUGH:
            name, arguments = _name_of(arguments[0]), arguments[1:]
        if name in _PASS_THROUGH:
            value = next((keyword.value for keyword in node.keywords if keyword.arg == _PASS_THROUGH[name]), None)
            index = self.positions.get(name)
            if value is None and index is not None and index < len(arguments):
                value = arguments[index]
            if value is not None:
                self.literals |= self._resolve(value)[0]
        self.generic_visit(node)

    def _stamp(self, value):
        literals, dynamic = self._resolve(value)
        self.literals |= literals
        if dynamic:
            self.dynamic_writes.add((self.path, '.'.join(self.names) or '<module>'))

    def _resolve(self, node, seen=frozenset()):
        """``(literals, dynamic)``: the strings ``node`` can be, and whether a caller decides it."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}, False
        if isinstance(node, ast.IfExp):
            return self._union(self._resolve(node.body, seen), self._resolve(node.orelse, seen))
        if isinstance(node, ast.BoolOp):
            return self._union(*(self._resolve(value, seen) for value in node.values))
        if isinstance(node, ast.Call) and _called(node) == 'str' and len(node.args) == 1:
            return self._resolve(node.args[0], seen)
        if isinstance(node, ast.Call) and _called(node) in _RETURNS_A_REASON:
            return set(), False  # Its returns are collected where it is defined.
        if isinstance(node, ast.Name):
            constant = getattr(turn_verdict, node.id, None)
            if node.id.isupper() and isinstance(constant, str):
                return {constant}, False
            assigned = [] if node.id in seen else [
                statement.value for scope in self.scopes for statement in ast.walk(scope)
                if isinstance(statement, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == node.id for target in statement.targets)]
            if assigned:
                return self._union(*(self._resolve(value, seen | {node.id}) for value in assigned))
        return set(), True

    @staticmethod
    def _union(*parts):
        return set().union(*(literals for literals, _ in parts)), any(dynamic for _, dynamic in parts)


def _stamped_abort_reasons():
    listed = subprocess.run(['git', 'ls-files', '--', *(f'{root}/*.py' for root in _SCANNED_ROOTS)],
                            cwd=ROOT, check=True, capture_output=True, text=True, timeout=60).stdout.split()
    markers = ('_abort_reason', *_PASS_THROUGH, *_RETURNS_A_REASON)
    modules = []
    for relative in listed:
        text = (ROOT / relative).read_text(encoding='utf-8')
        if any(marker in text for marker in markers):
            modules.append((relative, ast.parse(text, relative)))
    positions = {}
    for _relative, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _PASS_THROUGH:
                parameters = _parameters(node)
                if _PASS_THROUGH[node.name] in parameters:
                    positions[node.name] = parameters.index(_PASS_THROUGH[node.name])
    literals, dynamic_writes = set(), set()
    for relative, tree in modules:
        scan = _StampScan(relative, positions)
        scan.visit(tree)
        literals |= scan.literals
        dynamic_writes |= scan.dynamic_writes
    return literals - {''}, dynamic_writes


def test_every_abort_reason_the_code_stamps_is_registered_and_every_registered_one_is_stamped():
    stamped, dynamic_writes = _stamped_abort_reasons()
    assert stamped - set(ABORT_CAUSES) == set(), (
        'Register each stamped reason in lib.turn_verdict.ABORT_CAUSES with who stopped the work; '
        'unregistered, it reads as a person\'s Stop.')
    assert set(ABORT_CAUSES) - stamped == set(), 'Remove table rows no code stamps any longer.'
    assert dynamic_writes == _PASS_THROUGH_WRITES, (
        'A write whose reason the scan cannot read: stamp a literal, or pass the reason through a '
        '_PASS_THROUGH function and list the write here.')
