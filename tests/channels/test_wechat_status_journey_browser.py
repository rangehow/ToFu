"""Chromium reads exact tool results from the synthetic command-chat FILE journey.

The existing in-memory channel fixtures exercise normal tools and the real
outbox executor; only iLink/CDN IO is fake. The browser hosts the production
native tool-result presenter, not the Settings active-list interception.
"""
import json

import pytest
import requests

from lib.channels import tools
from lib.channels.bots.session import BotSession
from lib.channels.model import ChannelError
from lib.channels.wechat.transport import WeChatTransport
from tests._runtime_sections import ROOT, native_module_path
from tests.channels.conftest import OWNER
from tests.channels.test_tools import _task
from tests.channels.test_wechat_files import (
    CHAT, ORIGINAL, _response, binding as binding, fixture_wire,
)
from tests.support.frontend_visual import (
    frontend_visual_origin as frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('cut', ['ok', 'caption_empty', 'lost_ack', 'oversize'])
def test_command_chat_original_file_status_guidance_in_chromium(
        binding, frontend_visual_page, tmp_path, cut):
    wire = []

    def http(method, url, *, body, **kwargs):
        assert method == 'POST'
        if url.endswith('/getuploadurl'):
            return 200, {'upload_param': 'fixture-upload'}
        assert url.endswith('/sendmessage')
        wire.append(body['msg'])
        if body['msg']['item_list'][0]['type'] == 4 and cut == 'lost_ack':
            raise requests.ConnectionError('synthetic lost ACK')
        if body['msg']['item_list'][0]['type'] == 1 and cut == 'caption_empty':
            return 200, {}
        return 200, {'ret': 0}

    def cdn(method, url, **kwargs):
        assert method == 'POST'
        assert 'Authorization' not in kwargs['headers']
        return _response(413 if cut == 'oversize' else 200,
                         headers={'x-encrypted-param': 'fixture-reference'})

    transport = WeChatTransport(lambda: 'synthetic-token', http=fixture_wire(http), cdn=fixture_wire(cdn))
    session = BotSession(OWNER, 'wechat', store=binding, transport=transport)
    session.remember_reply(CHAT, {'contextToken': 'owner-context'})
    task = _task()
    chats = tools.execute('channel_threads', {'channel': 'wechat', 'mode': 'command'}, task)
    [command] = chats['threads']
    assert command['isCommandChat'] and command['key'] == str(CHAT)
    args = {'thread': command['key'], 'text': 'Synthetic original-file caption',
            'idempotency_key': 'browser-status', 'attachments': [{'path': 'proof.pdf'}]}
    sent = tools.execute('channel_send', args, task)
    lookup = {'item_id': sent['item']['id']}
    queued = tools.execute('channel_outbox', {'thread': command['key']}, task)
    assert [item['state'] for item in queued['items']] == ['queued']
    assert 'deliveryEvidence' not in queued['items'][0]
    session.execute_outbox()
    before = binding.outbox_item(lookup['item_id'])[0].to_record()
    active = tools.execute('channel_outbox', {'thread': command['key']}, task)
    status = tools.execute('channel_outbox', lookup, task)
    assert binding.outbox_item(lookup['item_id'])[0].to_record() == before
    assert status['item']['text'] == args['text']
    [file] = status['item']['attachments']
    assert (file['name'], file['size'], file['kind'], file['renditionOf']) == (
        'proof.pdf', len(ORIGINAL), 'file', None)
    expected = {'ok': 'sent', 'caption_empty': 'sent', 'lost_ack': 'unknown', 'oversize': 'failed'}[cut]
    assert status['item']['state'] == expected
    if expected == 'sent':
        assert not active['items']
        assert status['item']['deliveryEvidence'] == 'acceptance_unverified'
        assert file['confirmation'] == 'provider_ack'
    elif expected == 'unknown':
        assert status['item']['deliveryEvidence'] == 'delivery_unconfirmed'
        assert 'confirmation' not in file
    else:
        assert file['state'] == 'failed' and file['error'] == 'attachment_rejected'
    assert len([msg for msg in wire if msg['item_list'][0]['type'] == 4]) == (cut != 'oversize')
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_outbox', lookup, _task(owner=OWNER + 1))
    assert refused.value.code == 'not_found'

    page = frontend_visual_page
    page.set_viewport_size({'width': 390, 'height': 900})
    page.add_script_tag(path=native_module_path(
        '.native/wechat-status-result.js', ROOT / 'frontend/src/conversation/presentation/tool-result-presentation.ts'))
    results = [('channel_send', args, sent), ('channel_outbox', {'thread': command['key']}, active),
               ('channel_outbox', lookup, status)]
    page.evaluate('''results => {
      document.body.innerHTML='<main id="fixture" class="conversation-surface"></main>';
      const presenter=createToolResultPresentation({translate:key=>key,
        writeGateRefusal:{resolveRefusal:()=>null,renderNoticeHtml:()=>''}});
      document.querySelector('#fixture').innerHTML=results.map(([name,args,result],index)=>
        presenter.renderGenericResultHtml({toolName:name,toolCallId:'status-'+index,roundNum:index+1,
          status:'done',toolArgs:args,toolContent:JSON.stringify(result,null,2)}, {},
          {iconHtml:'',queryHtml:name,rootPillHtml:'',badgeHtml:''})).join('');
    }''', results)
    page.add_style_tag(content='body{display:block;overflow:auto}#fixture{max-width:100%;padding:16px}')
    headers = page.locator('.ptool-result-header')
    assert headers.count() == 3
    for header in headers.all():
        header.click()
    bodies = page.locator('.ptool-result-body')
    assert f'channel_outbox({json.dumps(lookup)})' in bodies.nth(0).inner_text().replace('\\"', '"')
    text = bodies.nth(2).inner_text()
    for value in ('proof.pdf', args['text'], expected, 'active items only',
                  'text/caption acceptance is unverified', 'not handset delivery', 'Handset delivery is unknown'):
        assert value in text
    if expected == 'sent':
        assert 'provider_ack' in text and 'acceptance_unverified' in text
        assert '"items": []' in bodies.nth(1).inner_text()
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path / f'wechat-status-{cut}.png'), full_page=True)
