"""Quote replies on the bot platforms (docs/CHANNEL_PROTOCOL.md P16, docs/CHANNELS.md C12).

Each transport turns the quote facts its platform documents into the core's shapes: which message
is quoted, when the platform names it, and the quoted content the platform carries. The core then
resolves and shows it like any other quote (``tests/channels/test_quotes.py``).

- 企业微信 智能机器人 (document 100719 引用, the same body over the long connection, 101463): ``quote``
  carries the quoted content only. It has no message ID and no sender.
- QQ 官方机器人 (C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE): ``message_type`` 103 is a quote. Its
  ``msg_elements`` hold the quoted content and author, and ``message_scene.ext`` has ``ref_msg_idx``.
- 微信 iLink (openclaw-weixin ``src/api/types.ts`` RefMessage): an item's ``ref_msg`` holds the quoted
  ``message_item``, its ``title`` (摘要) and ``svr_id``, the quoted message's server ID.
"""
import pytest

from lib.channels.bots import ids
from lib.channels.bots.session import BotSession
from tests.channels.bot_fakes import OTHER_ID, OWNER_ID, ilink_message, qq_c2c, qq_group, wecom_text
from tests.channels.conftest import OWNER

pytestmark = pytest.mark.unit


def test_wecom_quote_content_is_the_platforms_copy_without_an_id():
    from lib.channels.wecom.transport import normalize
    message = normalize(wecom_text('m-1', '@RobotA hello robot', quote={'msgtype': 'text',
                                                                        'text': {'content': '这是今日的测试情况'}}))
    assert message.text == '@RobotA hello robot'
    assert message.quote_message_id == '' and message.quoted.text == '这是今日的测试情况'
    assert message.quoted.sender_id == ''  # the platform names no sender
    mixed = normalize(wecom_text('m-2', 'see', quote={'msgtype': 'mixed', 'mixed': {'msg_item': [
        {'msgtype': 'text', 'text': {'content': 'chart'}}, {'msgtype': 'image', 'image': {'url': 'https://x'}}]}}))
    assert mixed.quoted.text == 'chart [image]' and 'https://x' not in repr(mixed.quoted)
    assert normalize(wecom_text('m-3', 'plain')).quoted is None


def test_qq_quote_message_names_the_quoted_index_content_and_author():
    from lib.channels.qq.transport import normalize
    data = {**qq_c2c('ROBOT1.0_q', '这个建议很有帮助'), 'message_type': 103,
            'msg_elements': [{'msg_idx': 'REFIDX_aaa', 'message_type': 0, 'content': '每天坚持阅读半小时',
                              'author': {'id': 'BOTID', 'user_openid': 'BOTID', 'username': 'Tofu', 'bot': True},
                              'attachments': [{'content_type': 'image/png', 'filename': 'a.png', 'url': 'https://q'}]}],
            'message_scene': {'source': 'default', 'ext': ['ref_msg_idx=REFIDX_aaa', 'msg_idx=REFIDX_zzz']}}
    message = normalize('C2C_MESSAGE_CREATE', data)
    assert message.text == '这个建议很有帮助' and message.quote_message_id == 'REFIDX_aaa'
    copy = message.quoted
    assert (copy.text, copy.sender_id, copy.sender_name, copy.sender_kind) == ('每天坚持阅读半小时', 'BOTID', 'Tofu', 'bot')
    assert [(item.kind, item.name) for item in copy.attachments] == [('image', 'a.png')]
    assert 'https://q' not in repr(copy)
    group = normalize('GROUP_AT_MESSAGE_CREATE', {**qq_group('ROBOT1.0_g', ' ok '), 'message_type': 103,
                                                  'msg_elements': [{'content': '=== 消息 1 ===\n[消息内容] 计划'}],
                                                  'message_scene': {'ext': ['ref_msg_idx=TMP_1']}})
    assert group.quote_message_id == 'TMP_1' and group.quoted.text.startswith('=== 消息 1 ===')
    assert normalize('C2C_MESSAGE_CREATE', qq_c2c('ROBOT1.0_p', 'plain')).quoted is None


def test_wechat_ref_msg_names_the_server_id_and_carries_the_quoted_item():
    from lib.channels.wechat.transport import normalize
    message = normalize(ilink_message(7, 'and this?', item_list=[
        {'type': 1, 'text_item': {'text': 'and this?'},
         'ref_msg': {'message_item': {'type': 1, 'create_time_ms': 1_793_934_000_000,
                                      'text_item': {'text': 'the quoted words'}},
                     'title': '摘要', 'svr_id': '9876543210'}}]))
    assert message.text == 'and this?' and message.quote_message_id == '9876543210'
    assert (message.quoted.text, message.quoted.sent_at_ms) == ('the quoted words', 1_793_934_000_000)
    title_only = normalize(ilink_message(8, 'x', item_list=[{'type': 1, 'text_item': {'text': 'x'},
                                                             'ref_msg': {'title': '一段摘要'}}]))
    assert title_only.quote_message_id == '' and title_only.quoted.text == '一段摘要'
    assert normalize(ilink_message(9, 'plain')).quoted is None


def test_the_session_hands_quote_facts_to_ingress(store):
    from lib.channels.bots.platform import Inbound
    from lib.channels.model import QuotedMessage
    session = BotSession(OWNER, 'qq', store=store, transport=object())
    thread = ids.thread_key('qq', 'uid', OWNER_ID)
    copy = QuotedMessage(sender_id=OTHER_ID, text='quoted')
    event = session._event(Inbound(kind='uid', peer=OWNER_ID, sender=OWNER_ID, sender_name='Owner',
                                   message_id='ROBOT1.0_q', text='re', sent_at_s=1_793_934_000,
                                   quote_message_id='REFIDX_aaa', quoted=copy), thread)
    assert (event.reply_to_message_id, event.quoted, event.reply_to_external_id) == ('REFIDX_aaa', copy, '')
