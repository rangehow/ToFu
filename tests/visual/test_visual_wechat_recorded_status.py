"""Ordinary app history exposes the actual same-owner recorded-item tool result.

The only outbox row is synthetic and already settled. Real app history restores
its tool result; the invocation recovered from the reloaded card is exercised
through the existing read-only owner tool. The settled-Turn route supplies the
synthetic transcript, not a model-driven composer request. No channel session,
provider credential, send, model or transport is created by this test.
"""
import json
import re
import uuid

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('caption', ['', 'Synthetic recorded original caption'], ids=['file-only', 'file-caption'])
def test_recorded_original_and_caption_status_survives_app_reload(
        browser, live_server, monkeypatch, tmp_path, width, caption):
    from playwright.sync_api import expect

    from lib.channels import tools
    from lib.channels.model import ChannelError, OutboxItem
    from lib.channels.store import ChannelStore
    from tests.channels.test_tools import _task
    from tests.conftest import _dismiss_onboarding_modals

    cid = 'wechat-recorded-status-' + uuid.uuid4().hex
    item_id = 'ob' + uuid.uuid4().hex
    thread = 'wechat:uid:mfrgg'
    # Same legacy-record shape covered by test_wechat_outbox_status; a file ACK
    # must not promote the complete message or caption to platform acceptance.
    store = ChannelStore(1)
    item = OutboxItem.from_record({
        'id': item_id, 'thread': thread, 'text': caption,
        'idempotencyKey': cid, 'state': 'sent', 'origin': {'kind': 'tool'},
        'createdAtMs': 1, 'updatedAtMs': 2, 'trackPartSubmission': True,
        'textPart': {'state': 'sent'} if caption else {}, 'attachments': [{
            'name': 'fixture.pdf', 'type': 'application/pdf', 'size': 3, 'sha256': '0' * 64,
            'kind': 'file', 'state': 'sent', 'confirmation': 'provider_ack', 'externalId': '124'}]})
    store.create_outbox_item(item)
    store.save_outbox_item(item, store.outbox_item(item_id)[1])
    before = store.outbox_item(item_id)[0].to_record()
    with monkeypatch.context() as scope:
        scope.setattr('lib.router.binding.global_router', lambda owner, *, create: {
            'conversationId': 'router-conv', 'availability': 'available', 'revision': 1})
        # This read-only task has no executing Turn; _task's default names a
        # unit-test-only fake Turn absent from the full application authority.
        task = _task(owner=1, turn='')
        queue = tools.execute('channel_outbox', {'thread': thread}, task)
        status = tools.execute('channel_outbox', {'item_id': item_id}, task)
    assert queue == {'items': []}
    assert status['item']['deliveryEvidence'] == 'acceptance_unverified'
    assert status['item']['attachments'][0]['confirmation'] == 'provider_ack'
    assert status['item']['text'] == caption
    assert store.outbox_item(item_id)[0].to_record() == before

    context = browser.new_context(service_workers='block', viewport={'width': width, 'height': 900})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');"
                            "localStorage.setItem('tofu_ui_lang','en');")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    results = [({'thread': thread}, queue), ({'item_id': item_id}, status)]
    rounds = [{'roundNum': index + 1, 'llmRound': index + 1, 'toolCallId': f'status-{index}',
               'toolName': 'channel_outbox', 'toolArgs': args, 'status': 'done',
               'query': 'Read recorded WeChat status', 'toolContent': json.dumps(result)}
              for index, (args, result) in enumerate(results)]
    segments = [{'type': 'tool_use', 'blockId': row['toolCallId'], 'id': row['toolCallId'],
                 'name': row['toolName'], 'input': row['toolArgs'], 'llmRound': row['llmRound'],
                 'result': {'status': 'done', 'content': row['toolContent']}} for row in rounds]
    segments.append({'type': 'text', 'blockId': 'answer', 'text': 'Synthetic status lookup recorded.',
                     'terminal': True, 'deliverable': True})

    def append(command, actor, projection, create=False):
        body = {'commandId': cid + command, 'actor': actor, 'kind': 'text', 'projection': projection}
        if create:
            body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
        response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    try:
        append('-human', 'human', {'content': 'Find the recorded original file and caption status.'}, True)
        append('-assistant', 'assistant', {'content': 'Synthetic status lookup recorded.',
                                          'toolRounds': rounds, 'segments': segments})
        context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId',{json.dumps(cid)});")
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        if width >= 768:
            entry = page.locator(f'.conv-item[data-conv-id="{cid}"]')
            expect(entry).to_have_count(1, timeout=20000)
            entry.click(position={'x': 12, 'y': 12})
        for iteration in range(2):
            chat = page.locator('#chatInner')
            expect(chat).to_contain_text('Synthetic status lookup recorded.', timeout=20000)
            panel = chat.locator('.conversation-tool-panel__toggle')
            if panel.get_attribute('aria-expanded') == 'false':
                panel.click()
            headers = chat.locator('.ptool-result-header')
            expect(headers).to_have_count(2)
            for header in headers.all():
                if not header.locator('..').evaluate('node=>node.open'):
                    header.click()
            bodies = chat.locator('.ptool-result-body')
            expect(bodies.nth(0)).to_contain_text('Items')
            expect(bodies.nth(0)).to_contain_text('None')
            for text in ('fixture.pdf', item.text, item_id, 'acceptance_unverified', 'provider_ack',
                         'active items only', 'text/caption acceptance is unverified',
                         'not handset delivery', 'Handset delivery is unknown'):
                expect(bodies.nth(1)).to_contain_text(text)
            if caption:
                expect(bodies.nth(1)).to_contain_text(caption)
            else:
                expect(bodies.nth(1)).not_to_contain_text('Synthetic recorded original caption')
            if iteration == 1:
                guidance = bodies.nth(1).inner_text().replace('\\"', '"')
                invocation = re.search(r'channel_outbox\((\{"item_id": "[^"]+"\})\)', guidance)
                assert invocation, guidance
                lookup = json.loads(invocation.group(1))
                assert lookup == {'item_id': item_id}
                with monkeypatch.context() as scope:
                    scope.setattr('lib.router.binding.global_router', lambda owner, *, create: {
                        'conversationId': 'router-conv', 'availability': 'available', 'revision': 1})
                    resumed = tools.execute('channel_outbox', lookup, _task(owner=1, turn=''))
                    assert resumed == status
                    assert tools.execute('channel_outbox', {'thread': thread}, task) == {'items': []}
                    with pytest.raises(ChannelError) as refused:
                        tools.execute('channel_outbox', lookup, _task(owner=2, turn=''))
                    assert refused.value.code == 'not_found'
            page.screenshot(path=str(tmp_path / f'wechat-recorded-status-{width}-{iteration}.png'), full_page=True)
            if iteration == 0:
                page.reload(wait_until='domcontentloaded')
                _dismiss_onboarding_modals(page)
        assert not errors
        assert store.outbox_item(item_id)[0].to_record() == before
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
