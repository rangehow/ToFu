"""Candidate quality preserves the public retry, feedback and acceptance boundary."""
from types import SimpleNamespace

import pytest

from lib.llm.diagnostic_timing import DiagnosticTiming, bind_timing
from lib.translate.engine import _engine
from lib.translate.errors import TranslationContentRefused

pytestmark = pytest.mark.unit
SOURCE = 'A faithful translation keeps all the meaning of the original source document.'
TRANSLATED = '忠实的翻译应当保留原始文档表达的全部含义。'
# The reported input: auto-translation dropped the leading conversation id.
ID_SOURCE = 'mw91ei1sivrnyc 这个对话为什么暂时无法获取进展？正在重新连接，啥情况？'
ID_DROPPED = ("Why is this conversation temporarily unable to retrieve progress? "
              "It's reconnecting, what's going on?")
SHA_SOURCE = 'Fixed in commit b22e38bd26; rerun the translation suite before landing.'
SHA_DROPPED = '已在提交中修复；合并前请重新运行翻译测试套件。'
SHA_KEPT = '已在提交 b22e38bd26 中修复；合并前请重新运行翻译测试套件。'


@pytest.fixture
def run_quality(monkeypatch):
    calls, penalties, cache, refusals, statuses, audits = [], [], [], [], [], []
    now = [0.0]
    monkeypatch.setattr(_engine, '_try_translation_fast_paths', lambda *_a, **_k: None)
    monkeypatch.setattr(_engine, '_MAX_CONTENT_RETRIES', 3)
    dispatcher = SimpleNamespace(record_truncation=lambda *a, **k: penalties.append((a, k)))
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(_engine.translate_cache, 'put', lambda *a, **k: cache.append((a, k)))
    monkeypatch.setattr(_engine.translate_refusal, 'put', lambda *a, **k: refusals.append((a, k)))
    monkeypatch.setattr('lib.log.audit_log', lambda *a, **k: audits.append((a, k)))
    timing = DiagnosticTiming(kind='quality-spec', clock=lambda: now[0], emit=lambda _record: None)

    def run(replies, *, chunk=SOURCE, source='English', target='Chinese', **options):
        def dispatch(*_args, **kwargs):
            index = len(calls)
            assert index < len(replies), 'unexpected additional paid attempt'
            calls.append(set(kwargs['excluded_models']))
            now[0] += 1
            body, finish = replies[index]
            return body, {'finish_reason': finish,
                '_dispatch': {'key': f'key-{index}', 'model': f'model-{index}'}}
        monkeypatch.setattr(_engine, '_dispatch_translation_candidate', dispatch)
        with bind_timing(timing):
            return _engine._translate_one_chunk(
                chunk, 'Translate faithfully.', source=source, target=target,
                use_cache=False, status_cb=lambda status: statuses.append(status['kind']),
                **options)

    return SimpleNamespace(run=run, calls=calls, penalties=penalties, cache=cache,
        refusals=refusals, statuses=statuses, audits=audits, timing=timing,
        dispatcher=dispatcher)


@pytest.mark.parametrize('body', [
    TRANSLATED,
    [{'type': 'text', 'text': TRANSLATED}, {'type': 'image', 'text': 'ignored'}],
    {'content': TRANSLATED},
    '<think>private reasoning</think><translate>' + TRANSLATED + '</translate>',
])
def test_normalization_preserves_accepted_text_and_model_provenance(run_quality, body):
    f = run_quality
    text, usage = f.run([(body, 'stop')])
    assert text == TRANSLATED
    assert usage['_dispatch'] == {'key': 'key-0', 'model': 'model-0'}
    assert usage['_translate_trace']['content_fails'] == 0
    assert f.cache == [((SOURCE, 'English', 'Chinese', TRANSLATED), {'model': 'model-0'})]
    assert f.penalties == f.refusals == []
    assert f.statuses == ['started']
    assert f.timing.snapshot()['active_spans'] == []


def test_different_quality_failures_share_one_budget_and_exclusion_set(run_quality):
    f = run_quality
    with pytest.raises(TranslationContentRefused) as caught:
        f.run([('', 'stop'), (TRANSLATED, 'length'), (SOURCE, 'stop')])
    assert caught.value.verdict == 'noop'
    assert caught.value.content_fails == 3
    assert caught.value.attempts == 3
    assert f.calls == [set(), {'model-0'}, {'model-0', 'model-1'}]
    assert len(f.penalties) == 3
    assert f.cache == []
    assert len(f.refusals) == 1
    assert f.refusals[0][1]['verdict'] == 'noop'
    assert f.refusals[0][1]['content_fails'] == 3
    assert f.statuses == ['started', 'empty_output', 'truncated', 'noop_final']
    assert f.timing.snapshot()['active_spans'] == []
    assert f.timing.snapshot()['stages']['quality_validation']['count'] == 3


def test_dropped_identifier_shares_the_content_budget_and_refuses_without_caching(run_quality):
    f = run_quality
    with pytest.raises(TranslationContentRefused) as caught:
        f.run([(ID_DROPPED, 'length'), (ID_DROPPED, 'stop'), (ID_DROPPED, 'stop')],
              chunk=ID_SOURCE, source='', target='English')
    assert caught.value.verdict == 'dropped_identifier'
    assert 'mw91ei1sivrnyc' in caught.value.reason
    assert caught.value.content_fails == caught.value.attempts == 3
    assert f.calls == [set(), {'model-0'}, {'model-0', 'model-1'}]
    assert len(f.penalties) == 3
    assert f.cache == []
    assert len(f.refusals) == 1
    assert f.refusals[0][1]['verdict'] == 'dropped_identifier'
    assert 'mw91ei1sivrnyc' in f.refusals[0][1]['reason']
    assert f.statuses == ['started', 'truncated', 'dropped_identifier',
                          'dropped_identifier_final']
    assert f.timing.snapshot()['active_spans'] == []
    assert f.timing.snapshot()['stages']['quality_validation']['count'] == 3


@pytest.mark.parametrize('strict_model', [False, True])
def test_identifier_retry_commits_only_the_candidate_that_keeps_it(run_quality, strict_model):
    f = run_quality
    text, usage = f.run([(SHA_DROPPED, 'stop'), (SHA_KEPT, 'stop')],
                        chunk=SHA_SOURCE, strict_model=strict_model)
    assert text == SHA_KEPT
    assert usage['_translate_trace']['content_fails'] == 1
    assert f.calls == [set(), set() if strict_model else {'model-0'}]
    assert len(f.penalties) == 1
    assert 'b22e38bd26' in f.penalties[0][1]['error']
    assert f.cache == [((SHA_SOURCE, 'English', 'Chinese', SHA_KEPT), {'model': 'model-1'})]
    assert f.refusals == []
    assert f.statuses == ['started', 'dropped_identifier']


def test_strict_model_feedback_does_not_change_the_explicit_model_selection(run_quality):
    f = run_quality
    text, usage = f.run([('', 'stop'), (SOURCE, 'stop'), (TRANSLATED, 'stop')], strict_model=True)
    assert text == TRANSLATED
    assert f.calls == [set(), set(), set()]
    assert len(f.penalties) == 2
    assert usage['_translate_trace']['content_fails'] == 2
    assert len(f.cache) == 1


def test_empty_exhaustion_never_commits_or_creates_a_content_refusal_marker(run_quality):
    f = run_quality
    with pytest.raises(ValueError, match='3 content fails') as caught:
        f.run([('', 'stop')] * 3)
    assert not isinstance(caught.value, TranslationContentRefused)
    assert f.cache == f.refusals == []
    assert f.statuses[-1] == 'empty_final'
    assert f.timing.snapshot()['active_spans'] == []


@pytest.mark.parametrize('accept_partial', [False, True])
def test_partial_acceptance_is_explicit_and_never_cacheable(run_quality, accept_partial):
    f = run_quality
    if accept_partial:
        text, usage = f.run([(TRANSLATED, 'length')] * 3, accept_truncated=True)
        assert text == TRANSLATED
        assert usage['_translate_trace']['verdict'] == 'truncated'
    else:
        with pytest.raises(TranslationContentRefused) as caught:
            f.run([(TRANSLATED, 'length')] * 3, accept_truncated=False)
        assert caught.value.verdict == 'truncated'
    assert f.cache == f.refusals == []
    assert f.statuses == ['started', 'truncated', 'truncated', 'truncated_final']
    assert len(f.audits) == 1
    assert f.audits[0][1]['content_fails'] == 3
    assert f.timing.snapshot()['active_spans'] == []


@pytest.mark.parametrize('port', ['feedback', 'refusal_cache'])
def test_required_quality_side_effect_failure_preserves_the_original_error(run_quality, monkeypatch, port):
    f = run_quality
    failure = RuntimeError(f'injected {port} failure')
    def fail(*_a, **_k):
        raise failure
    if port == 'feedback':
        monkeypatch.setattr(f.dispatcher, 'record_truncation', fail)
    else:
        monkeypatch.setattr(_engine.translate_refusal, 'put', fail)
    with pytest.raises(RuntimeError) as caught:
        f.run([(SOURCE, 'stop')] * 3)
    assert caught.value is failure
    assert len(f.calls) == (1 if port == 'feedback' else 3)
    assert f.cache == []
    assert f.timing.snapshot()['active_spans'] == []


def test_best_effort_audit_failure_preserves_partial_acceptance(run_quality, monkeypatch):
    f = run_quality
    def fail(*_a, **_k):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr('lib.log.audit_log', fail)
    text, usage = f.run([(TRANSLATED, 'length')] * 3)
    assert text == TRANSLATED
    assert usage['_translate_trace']['verdict'] == 'truncated'
    assert f.cache == []
    assert f.timing.snapshot()['active_spans'] == []
