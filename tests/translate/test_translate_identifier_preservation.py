"""A translation that drops a source identifier is refused; callers keep the original.

The identifier rule is structural (``lib/translate/engine/_quality_checks.py``):
URLs, words containing an underscore, and words whose letters and digits
interleave. Quantities, ordinals, names with a number, plain words and numbers
may be re-expressed by a faithful translation and are never refused.
"""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from lib.translate.engine import _engine
from lib.translate.engine._quality_checks import (
    _targets_chinese,
    translation_candidate_verdict,
)

pytestmark = pytest.mark.unit

ID_SOURCE = 'mw91ei1sivrnyc 这个对话为什么暂时无法获取进展？正在重新连接，啥情况？'
ID_DROPPED = ("Why is this conversation temporarily unable to retrieve progress? "
              "It's reconnecting, what's going on?")
ID_KEPT = ("mw91ei1sivrnyc Why is this conversation temporarily unable to retrieve "
           "progress? It's reconnecting, what's going on?")
SHA_SOURCE = 'Fixed in commit b22e38bd26; the slides task slides_8c32fe7d7c494e45 now finishes.'
SHA_DROPPED = '已在提交中修复；幻灯片任务现在可以完成了。'
SHA_KEPT = '已在提交 b22e38bd26 中修复；幻灯片任务 slides_8c32fe7d7c494e45 现在可以完成了。'


def _verdict(source, candidate, *, target, finish='stop'):
    return translation_candidate_verdict(
        candidate, source, clen=len(source), target=target,
        target_is_chinese=_targets_chinese(target), finish=finish, model='m',
        overgeneration_verdict=_engine._translation_overgeneration_verdict)


def test_reported_input_loses_its_conversation_id():
    verdict, reason = _verdict(ID_SOURCE, ID_DROPPED, target='English')
    assert verdict == 'dropped_identifier'
    assert 'mw91ei1sivrnyc' in reason
    assert _verdict(ID_SOURCE, ID_KEPT, target='English') == ('ok', '')


def test_output_translation_loses_a_commit_sha_and_task_id():
    verdict, reason = _verdict(SHA_SOURCE, SHA_DROPPED, target='Chinese')
    assert verdict == 'dropped_identifier'
    assert 'b22e38bd26' in reason and 'slides_8c32fe7d7c494e45' in reason
    partial = SHA_KEPT.replace('slides_8c32fe7d7c494e45', '')
    verdict, reason = _verdict(SHA_SOURCE, partial, target='Chinese')
    assert verdict == 'dropped_identifier'
    assert 'slides_8c32fe7d7c494e45' in reason and 'b22e38bd26' not in reason
    assert _verdict(SHA_SOURCE, SHA_KEPT, target='Chinese') == ('ok', '')


@pytest.mark.parametrize(('source', 'dropped', 'kept', 'token'), [
    ('打开 https://example.com/docs/guide?lang=zh 看看',
     'Take a look at the docs page',
     'Take a look at https://example.com/docs/guide?lang=zh',
     'https://example.com/docs/guide'),
    ('改一下 lib/translate/engine/_quality_checks.py 第42行',
     'Change line 42 of the quality check file',
     'Change line 42 of lib/translate/engine/_quality_checks.py',
     'quality_checks'),
    ('会话 ID:mt18xr3wfs0rbq 卡住了',
     'The conversation is stuck',
     'Conversation ID: mt18xr3wfs0rbq is stuck',
     'mt18xr3wfs0rbq'),
    ('请求 550e8400-e29b-41d4-a716-446655440000 失败',
     'The request failed',
     'Request 550e8400-e29b-41d4-a716-446655440000 failed',
     '550e8400'),
])
def test_urls_paths_ids_and_uuids_are_identifiers(source, dropped, kept, token):
    verdict, reason = _verdict(source, dropped, target='English')
    assert verdict == 'dropped_identifier'
    assert token in reason
    assert _verdict(source, kept, target='English') == ('ok', '')


@pytest.mark.parametrize(('source', 'candidate', 'target'), [
    ('第3页有什么问题？', 'What is wrong on page 3?', 'English'),
    ('会议定在2026年11月9日。', 'The meeting is on November 9, 2026.', 'English'),
    ('用python调用这个API', 'Call this api from Python', 'English'),
    ('一共有1000条记录', 'There are 1,000 records in total', 'English'),
    ('GPT-4o 在 5G 网络和 H100 上的表现', 'How GPT-4o performs on 5G networks and H100',
     'English'),
    ('The request took 100ms, 2x faster than the 3rd attempt in Q3.',
     '该请求耗时100毫秒，比第三季度的第3次尝试快2倍。', 'Chinese'),
    ('See Fig.1 for the 24x7 setup on 1920x1080 screens.',
     '7×24 配置见图1，适用于 1920×1080 屏幕。', 'Chinese'),
    ('Run the e2e suite for slides_8c32fe7d7c494e45.',
     '为 slides\\_8c32fe7d7c494e45 运行 E2E 测试套件。', 'Chinese'),
    ('请保留 ⟦NT_0⟧ 并解释它', 'Please keep it and explain it', 'English'),
    ('H2O 和 Fe3O4 的反应很快', 'H₂O reacts quickly with Fe₃O₄', 'English'),
])
def test_ordinary_translations_are_not_refused(source, candidate, target):
    assert _verdict(source, candidate, target=target) == ('ok', '')


@pytest.mark.parametrize(('source', 'candidate', 'target', 'finish', 'expected'), [
    (ID_SOURCE, ID_DROPPED, 'English', 'length', 'truncated'),
    ('mw91ei1sivrnyc ' + '这个对话为什么暂时无法获取进展，正在重新连接。' * 8,
     'The conversation cannot fetch progress and keeps reconnecting. ' * 4,
     'Chinese', 'stop', 'wrong_language'),
    (ID_SOURCE, ID_DROPPED * 12, 'English', 'stop', 'over_generated'),
])
def test_identifier_check_runs_only_after_existing_checks(
        source, candidate, target, finish, expected):
    assert _verdict(source, candidate, target=target, finish=finish)[0] == expected


@pytest.fixture
def lossy_engine(monkeypatch):
    """Run the real engine with a provider that always drops identifiers."""
    cache, refusals, dispatched = [], [], []
    replies = {ID_SOURCE: ID_DROPPED, SHA_SOURCE: SHA_DROPPED}

    def dispatch(messages, **_kwargs):
        index = len(dispatched)
        source = next(text for text in replies if text in messages[-1]['content'])
        dispatched.append(source)
        return replies[source], {
            'finish_reason': 'stop',
            '_dispatch': {'key': f'key-{index}', 'model': f'model-{index}'}}

    monkeypatch.setattr(_engine, '_try_translation_fast_paths', lambda *_a, **_k: None)
    monkeypatch.setattr(_engine, '_MAX_CONTENT_RETRIES', 3)
    monkeypatch.setattr(_engine, '_dispatch_translation_candidate', dispatch)
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher',
                        lambda: SimpleNamespace(record_truncation=lambda *a, **k: None))
    monkeypatch.setattr(_engine.translate_cache, 'put', lambda *a, **k: cache.append(a))
    monkeypatch.setattr(_engine.translate_refusal, 'get', lambda *_a, **_k: None)
    monkeypatch.setattr(_engine.translate_refusal, 'put',
                        lambda *a, **k: refusals.append(k['verdict']))
    return SimpleNamespace(cache=cache, refusals=refusals, dispatched=dispatched)


def test_input_translation_sends_the_original_text(lossy_engine, monkeypatch):
    from lib.chat import turn_builder
    from lib.translate import execution

    def run_inline(_job_id, *, owner_user_id, function):
        future = Future()
        try:
            future.set_result(function())
        except Exception as exc:
            future.set_exception(exc)
        return future

    monkeypatch.setattr(turn_builder, '_should_translate_input', lambda *_a: True)
    monkeypatch.setattr(execution, 'submit_attended_translation', run_inline)
    config = {'autoTranslate': True}

    # Synchronous steer/queued path.
    message = turn_builder.build_user_msg_from_payload(
        {'text': ID_SOURCE}, config, user_id=7, conv_id='conv-1')
    assert message['content'] == ID_SOURCE
    assert message['_translateFailed'] == 'failed'
    assert 'originalContent' not in message
    # Headless API path.
    assert turn_builder.translate_user_text_to_english(ID_SOURCE, config) == (
        ID_SOURCE, None, None, 'failed')
    # Accepted attempt: the worker's model view keeps the authored text.
    # lib.translate.commit binds get_turn when first imported; a first import
    # under this stub would keep it after monkeypatch restores lifecycle.
    import lib.translate.commit  # noqa: F401
    import lib.turn_lifecycle as lifecycle
    turns = {'turn-1': {'parentTurnId': 'input-1'},
             'input-1': {'projectionRevision': 4, 'projection': {'content': ID_SOURCE}}}
    monkeypatch.setattr(lifecycle, 'get_turn',
                        lambda _conv, turn_id, *, user_id: turns[turn_id])
    deferred = turn_builder._translate_accepted_input('conv-1', 'turn-1', config, user_id=7)
    assert (deferred.original, deferred.translated) == (ID_SOURCE, None)
    assert (deferred.failure, deferred.recorded) == ('failed', False)

    assert len(lossy_engine.dispatched) == 9
    assert lossy_engine.cache == []
    assert lossy_engine.refusals == ['dropped_identifier'] * 3


def test_assistant_translation_keeps_the_original_turn(lossy_engine, monkeypatch):
    import lib.translate.runtime._worker as worker
    from lib.translate.runtime import _translate_runtime

    commits, marked, enrichment = [], [], []
    monkeypatch.setattr(worker, 'commit_translation_to_turn',
                        lambda *a, **k: commits.append(a))
    monkeypatch.setattr(worker, '_schedule_segment_enrichment',
                        lambda *a, **k: enrichment.append(a))
    monkeypatch.setattr('lib.translate.commit.mark_turn_translation_failed',
                        lambda conv, turn, *, user_id, reason, error: marked.append(
                            (conv, turn, user_id, reason, error)))
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    task = _translate_runtime.create(user_id=7, task_id='identifier-refusal')
    _translate_runtime.mark_running('identifier-refusal', fields={'model': None})
    try:
        worker._do_translate('identifier-refusal', SHA_SOURCE, 'Chinese', 'English',
                             'conv-1', 'turn-1', 'translatedContent', user_id=7)
    finally:
        _translate_runtime.discard('identifier-refusal')

    assert commits == []
    assert len(marked) == 1
    assert marked[0][:4] == ('conv-1', 'turn-1', 7, 'worker_error')
    assert 'verdict=dropped_identifier' in marked[0][4]
    assert task['status'] == 'error'
    assert len(lossy_engine.dispatched) == 3
    assert lossy_engine.cache == []
    assert lossy_engine.refusals == ['dropped_identifier']
