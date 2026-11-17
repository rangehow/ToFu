"""Consumed mailbox carriers are history: later sends neither rescan nor re-authorize them.

Acknowledgement semantics stay exact: a carrier still pending acknowledgement
is scanned and validated on every send, a dropped carrier is never confirmed,
and carried history still fails closed when the task's mailbox scope changes.
"""
import pytest

from lib.llm.stream_result import ProviderStreamResult
from lib.tasks_pkg import message_input_receipts as receipts

pytestmark = pytest.mark.unit

KEY = 'project-key'


def complete():
    return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': 'Read'}, 'stop', {})


@pytest.fixture
def lead(monkeypatch):
    from lib.conversations import project_messages as project
    state = {'finds': [], 'senders': [], 'identity': (1, 'recipient', 'project'),
             'rows': {}}

    def identity(task):
        if state['identity'] is None:
            raise project.ProjectMessageError('The running task project no longer matches its conversation')
        return state['identity']

    class Mailbox:
        def __init__(self, owner, project_path, recipient):
            self.key = KEY if (owner, recipient, project_path) == (1, 'recipient', 'project') else 'other'

        def find(self, ids):
            state['finds'].append(sorted(ids))
            return {i: state['rows'][i] for i in ids if i in state['rows']}

    monkeypatch.setattr(project, 'identity', identity)
    monkeypatch.setattr(project, 'ProjectMailbox', Mailbox)
    monkeypatch.setattr(project, 'validate_sender',
                        lambda owner, sender, path, *, recipient: state['senders'].append(sender))
    return state


def carrier(text, ids, mailbox=KEY):
    return {'role': 'user', 'content': text, '_projectMessageIds': list(ids), '_projectMailbox': mailbox}


def send(task, messages, pending=()):
    """One round: acquisition remembers carriers, then a verified dispatch."""
    task['_projectMessageAttempt'] = {'owner': 1, 'recipient': 'recipient', 'project': 'project',
                                      'ids': list(pending)}
    receipts.remember(task, messages)
    receipts.stream_with_receipts(lambda *a, **kw: complete(), task, {'messages': messages})
    return task['_verifiedMessageInputIds']['project']


def test_consumed_history_is_not_rescanned_and_new_carriers_share_one_find(lead):
    lead['rows'] = {i: {'id': i, 'sender': 'lead-a' if i != 'c' else 'lead-b'} for i in 'abcd'}
    task, history = {}, [carrier('first from lead-a', ['a'])]
    assert send(task, history, pending=['a']) == ['a']
    assert lead['finds'] == [['a']] and lead['senders'] == ['lead-a']

    # Delivered and acknowledged: the next rounds carry it without a durable read.
    lead['finds'].clear(), lead['senders'].clear()
    history.append({'role': 'assistant', 'content': 'noted'})
    assert send(task, history) == []
    assert lead['finds'] == [] and lead['senders'] == []

    # Three new carriers, two senders: one find, one check per distinct sender.
    history += [carrier('b from lead-a', ['b']), carrier('c from lead-b', ['c']),
                carrier('d from lead-a', ['d'])]
    assert send(task, history, pending=['b', 'c', 'd']) == ['b', 'c', 'd']
    assert lead['finds'] == [['b', 'c', 'd']]
    assert sorted(lead['senders']) == ['lead-a', 'lead-b']


def test_a_pending_carrier_is_revalidated_every_send_and_a_dropped_one_never_confirms(lead):
    lead['rows'] = {'a': {'id': 'a', 'sender': 'lead-a'}}
    task, history = {}, [carrier('pending from lead-a', ['a'])]
    assert send(task, history, pending=['a']) == ['a']
    # Its acknowledgement failed: still pending, so it is scanned and checked again.
    lead['finds'].clear(), lead['senders'].clear()
    assert send(task, history, pending=['a']) == ['a']
    assert lead['finds'] == [['a']] and lead['senders'] == ['lead-a']
    # A summarized carrier is not in the request: nothing to acknowledge.
    task['_projectMessageAttempt']['ids'] = ['a']
    receipts.stream_with_receipts(lambda *a, **kw: complete(), task,
                                  {'messages': [{'role': 'user', 'content': 'summary'}]})
    assert task['_verifiedMessageInputIds']['project'] == []


def test_carried_history_still_fails_closed_when_the_task_scope_changes(lead):
    lead['rows'] = {'a': {'id': 'a', 'sender': 'lead-a'}}
    task, history = {}, [carrier('first from lead-a', ['a'])]
    send(task, history, pending=['a'])
    lead['identity'] = None
    called = []
    with pytest.raises(receipts.MessageInputScopeError):
        task['_projectMessageAttempt'] = {'ids': []}
        receipts.remember(task, history)
        receipts.stream_with_receipts(lambda *a, **kw: called.append(1) or complete(), task,
                                      {'messages': history})
    assert called == []


def test_a_carrier_from_another_mailbox_is_never_carried_as_history(lead):
    lead['rows'] = {'a': {'id': 'a', 'sender': 'lead-a'}}
    task = {}
    send(task, [carrier('first from lead-a', ['a'])], pending=['a'])
    foreign = [carrier('first from lead-a', ['a']), carrier('foreign', ['z'], mailbox='other')]
    with pytest.raises(receipts.MessageInputScopeError):
        send(task, foreign)


def test_an_unverified_response_records_no_consumed_history(lead):
    lead['rows'] = {'a': {'id': 'a', 'sender': 'lead-a'}}
    task, history = {}, [carrier('first from lead-a', ['a'])]
    task['_projectMessageAttempt'] = {'ids': []}
    receipts.remember(task, history)
    with pytest.raises(RuntimeError):
        receipts.stream_with_receipts(lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('lost')),
                                      task, {'messages': history})
    lead['finds'].clear()
    send(task, history)
    assert lead['finds'] == [['a']]
