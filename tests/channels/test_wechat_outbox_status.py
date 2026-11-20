"""Owner-visible personal WeChat outbox evidence through an in-memory iLink sender."""
import copy
import json

import pytest
import requests

from lib.channels import service, tools
from lib.channels.model import OutboxItem
from lib.channels.bots import ids
from lib.channels.bots.session import BotSession
from lib.channels.wechat.transport import WeChatTransport
from lib.http_transport.connection_setup import NetworkRoutesExhausted
from tests.channels.test_assistant_authority import owner_turn

pytestmark = pytest.mark.unit
COMMAND = ids.thread_key('wechat', 'uid', 'fixture-owner')
PEER = ids.thread_key('wechat', 'uid', 'fixture-peer')


@pytest.fixture
def sender(store, router, turns, monkeypatch, tmp_path):
    monkeypatch.setattr('lib.runtime.runtime_paths.data_root', lambda: str(tmp_path))
    monkeypatch.setattr('lib.router.watchdog.signals.report_outbox_fault', lambda *args: None)
    store.update_account('wechat', lambda account: {
        **account, 'enabled': True, 'appId': 'fixture-bot', 'commandChat': str(COMMAND),
        'identity': {'uid': 'fixture-owner', 'name': ''}})
    wire, outcomes = [], []

    def http(method, url, *, body, **kwargs):
        assert method == 'POST' and url.endswith('/ilink/bot/sendmessage')
        msg = body['msg']
        if msg['to_user_id'] == 'fixture-peer' and outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, dict):
                wire.append(msg)
                return 200, outcome
            if isinstance(outcome, NetworkRoutesExhausted):
                raise outcome
            wire.append(msg)
            raise outcome
        wire.append(msg)
        return 200, {'ret': 0}

    session = BotSession(store.owner, 'wechat', store=store, transport=WeChatTransport(lambda: 'fixture', http=http))
    session.remember_reply(COMMAND, {'contextToken': 'command-context'})
    session.remember_reply(PEER, {'contextToken': 'peer-context'})
    return session, wire, outcomes


@pytest.mark.parametrize('answer', [{}, {'ret': 0}, {'ret': 0, 'message_id': '123'}])
def test_wechat_draft_approval_and_receipts_do_not_claim_acceptance(store, sender, turns, answer):
    session, wire, outcomes = sender
    task = owner_turn(turns, 'Draft a plain message')
    args = {'thread': str(PEER), 'text': 'Fixture message', 'idempotency_key': 'synthetic-draft'}
    refused = json.loads(tools.execute_json('channel_send', {**args, 'reply_to': f'{PEER}/123'}, task))
    assert refused['code'] == 'reply_to_unsupported' and refused['executed'] is False
    assert 'omit reply_to' in refused['error']
    assert not store.active_outbox(channel='wechat') and not wire

    draft = tools.execute('channel_send', args, task)['item']
    lookup = {'item_id': draft['id']}
    assert draft['state'] == 'draft' and 'deliveryEvidence' not in draft
    assert session.execute_outbox() == 1
    assert [msg['to_user_id'] for msg in wire] == ['fixture-owner']  # approval notice only

    approve = owner_turn(turns, f"Approve {draft['approvalCode']}", name='approved')
    queued = tools.execute('channel_outbox_approve', {**lookup, 'decision': 'approve'}, approve)['item']
    assert queued['state'] == 'queued' and 'deliveryEvidence' not in queued
    assert queued['decision']['by'] == 'owner_message'

    outcomes.extend([NetworkRoutesExhausted('fixture: request not made'),
                     requests.ConnectionError('fixture: response lost'), answer])
    session.execute_outbox()
    [deferred] = service.outbox_page(store.owner, thread=str(PEER), store=store)['items']
    assert (deferred['state'], deferred['error']) == ('queued', 'not_connected')
    assert 'deliveryEvidence' not in deferred and len(wire) == 1

    session.execute_outbox()
    uncertain = tools.execute('channel_outbox', lookup, task)['item']
    assert (uncertain['state'], uncertain['deliveryEvidence']) == ('unknown', 'delivery_unconfirmed')
    assert service.outbox_page(store.owner, thread=str(PEER), store=store)['items'] == [uncertain]

    # Text-only retry semantics stay unchanged; no explicit response proof is retained.
    assert session.execute_outbox() == 1
    accepted = tools.execute('channel_outbox', lookup, task)['item']
    assert (accepted['state'], accepted['deliveryEvidence'], accepted['externalId']) == (
        'sent', 'acceptance_unverified', answer.get('message_id'))
    assert service.audit_page(store.owner, channel='wechat', store=store)['items'] == [accepted]
    assert service.outbox_page(store.owner, thread=str(PEER), store=store)['items'] == []
    assert session.execute_outbox() == 0
    assert len(wire) == 3
    for msg in wire[1:]:
        assert (msg['to_user_id'], msg['context_token'], msg['client_id']) == (
            'fixture-peer', 'peer-context', draft['id'])
        assert msg['item_list'] == [{'type': 1, 'text_item': {'text': draft['text']}}]


@pytest.mark.parametrize('extra', [
    {},
    {'externalId': '123', 'clientId': 'legacy-client'},
    {'trackPartSubmission': True, 'textPart': {'state': 'sent', 'externalId': '123'}},
    {'trackPartSubmission': True, 'textPart': {'state': 'sent'}, 'attachments': [{
        'name': 'fixture.pdf', 'type': 'application/pdf', 'size': 3, 'sha256': '0' * 64,
        'kind': 'file', 'state': 'sent', 'confirmation': 'provider_ack', 'externalId': '124'}]},
])
def test_sent_legacy_rows_do_not_acquire_acceptance_proof_from_projection(store, extra):
    row = {'id': 'ob123456', 'thread': str(PEER), 'text': 'Fixture caption',
           'idempotencyKey': 'legacy', 'state': 'sent', 'origin': {'kind': 'tool'},
           'createdAtMs': 1, 'updatedAtMs': 2, **extra}
    snapshot = copy.deepcopy(row)
    item = OutboxItem.from_record(row)
    store.save_outbox_item(item, 0)
    before = store.outbox_item(item.id)[0].to_record()
    view = service.outbox_item_view(store.owner, item.id, store=store)
    assert (view['state'], view['deliveryEvidence']) == ('sent', 'acceptance_unverified')
    assert row == snapshot
    assert store.outbox_item(item.id)[0].to_record() == before
    if view['attachments']:
        assert view['attachments'][0]['confirmation'] == 'provider_ack'
    item.thread = 'qq:uid:mfrgg'
    assert 'deliveryEvidence' not in item.view()
