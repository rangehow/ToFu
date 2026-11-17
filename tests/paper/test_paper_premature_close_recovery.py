"""A stream closed before the provider finish frame does not fail paper tasks.

Live evidence (kimi-k3 / gemini-3.6-flash report probes): the dispatcher
returned a typed ``premature_close`` result and the agent-loop chassis raised
``UnverifiedProviderStreamError`` ("agent-loop round: provider stream is not
verified complete (state=premature_close)"), failing the whole report. Report,
Q&A and Deepen now recover through the chassis ``retry_bonus`` hook: partial
answer text is continued like an output-cap cut, anything else resends the
round, and the chassis' retry cap still fails a stream that never finishes.
"""

import pytest

import lib.paper.deepen_engine as de
import lib.paper.qa_engine as qe
from lib.agent_loop.agent_loop import DEFAULT_MAX_RETRY_BONUS, detect_premature_close
from lib.llm.stream_result import ProviderStreamResult
from lib.paper.qa_runtime import _new_qa_task
from tests.support.paper_report import complete_report_body

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def _typed_premature_close(content, tool_calls=None):
    """The dispatcher's shape: typed result, no finish frame, stamped state."""
    return ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': content, 'tool_calls': tool_calls},
        None, {'_missing_finish_reason': True, '_missing_done': True})


def _round(shape, content, tool_calls=None):
    msg = {'role': 'assistant', 'content': content, 'tool_calls': tool_calls}
    if shape == 'typed':
        return _typed_premature_close(content, tool_calls)
    if shape == 'stop':
        return msg, 'stop', {}
    # Short legacy results whose finish is absent or an error marker.
    return msg, shape, {}


def _scripted(plan, seen):
    def dispatch(messages, *, on_content=None, **kwargs):
        seen.append([dict(m) for m in messages])
        shape, content, tool_calls = plan[len(seen) - 1]
        if content and on_content:
            on_content(content)
        return _round(shape, content, tool_calls)

    return dispatch


@pytest.mark.parametrize(('finish', 'usage', 'expected'), [
    (None, {'_stream_state': 'premature_close'}, True),
    ('stop', {'_stream_state': 'premature_close'}, True),
    (None, {}, True),
    ('error', {}, True),
    ('stop', {'_stream_state': 'provider_finished'}, False),
    ('stop', {}, False),
    ('aborted', {}, False),
])
def test_detect_premature_close_follows_typed_state(finish, usage, expected):
    msg = {'role': 'assistant', 'content': 'x'}
    assert detect_premature_close(0, msg, finish, usage) is expected


@pytest.fixture
def report_case(monkeypatch):
    from tests.support.paper_report import isolated_report_case

    with isolated_report_case(monkeypatch) as case:
        yield case


def test_report_partial_text_is_continued_after_premature_close(report_case):
    run, _, _ = report_case
    seen = []
    first = '# Report\n\n## ⚡ TL;DR\nThe method wor'
    rest = 'ks well.\n\n' + complete_report_body()
    task, published = run(_scripted(
        [('typed', first, None), ('stop', rest, None)], seen))

    assert len(seen) == 2
    assert task['status'] == 'done'
    assert [r.report for r in published] == [first + rest]
    # Resumed, not regenerated: the partial answer is kept and continued.
    assert seen[1][-2] == {'role': 'assistant', 'content': first}
    assert 'EXACTLY where it stopped' in seen[1][-1]['content']


@pytest.mark.parametrize('shape', ['typed', None, 'error'])
def test_report_empty_premature_close_resends_unchanged_round(report_case, shape):
    run, _, _ = report_case
    seen = []
    body = '# Report\n\n' + complete_report_body()
    task, published = run(_scripted([(shape, '', None), ('stop', body, None)], seen))

    assert len(seen) == 2
    assert seen[1] == seen[0]
    assert task['status'] == 'done'
    assert [r.report for r in published] == [body]


def test_report_cut_tool_round_is_resent_without_running_tools(report_case):
    run, _, _ = report_case
    seen = []
    cut_call = [{'id': 't0', 'function': {
        'name': 'web_search', 'arguments': '{"queries":"evid'}}]
    body = '# Report\n\n' + complete_report_body()
    task, published = run(_scripted(
        [('typed', 'draft', cut_call), ('stop', body, None)], seen))

    assert len(seen) == 2
    assert seen[1] == seen[0]
    assert not any(e['type'] == 'tool_start' for e in task['events'])
    assert any(e['type'] == 'delta_reset' for e in task['events'])
    assert [r.report for r in published] == [body]


def test_report_still_fails_when_stream_never_finishes(report_case):
    run, _, _ = report_case
    seen = []
    plan = [('typed', '', None)] * (DEFAULT_MAX_RETRY_BONUS + 1)
    task, published = run(_scripted(plan, seen))

    assert len(seen) == DEFAULT_MAX_RETRY_BONUS + 1
    assert task['status'] == 'error'
    assert not published
    assert 'premature_close' in str(task.get('error'))


def test_qa_partial_answer_is_continued_after_premature_close(monkeypatch):
    seen = []
    first, rest = 'The answer is wor', 'th two parts.\n'
    monkeypatch.setattr(qe, 'dispatch_stream', _scripted(
        [('typed', first, None), ('stop', rest, None)], seen))
    task = _new_qa_task(
        'qa_premature_close', 'abcdef0000000000000000000000cc11',
        'en', None, question='q', user_id=TEST_OWNER_USER_ID)
    qe._run_qa_task(task, [{'role': 'system', 'content': 's'},
                           {'role': 'user', 'content': 'q'}])

    done = [e for e in task['events'] if e.get('type') == 'done']
    assert task['status'] == 'done'
    assert done[-1]['answer'] == first + rest
    assert seen[1][-2] == {'role': 'assistant', 'content': first}


def test_deepen_empty_premature_close_resends_round(monkeypatch):
    seen = []
    answer = '## Deeper\nStep one, step two.\n'
    monkeypatch.setattr(de, 'dispatch_stream', _scripted(
        [('error', '', None), ('stop', answer, None)], seen))
    cached = []
    monkeypatch.setattr(de, '_write_deepen_cache',
                        lambda *args, **kwargs: cached.append(args[5]))
    monkeypatch.setattr(de, '_accumulate_deepen_cost', lambda *a, **k: None)
    task = de._new_deepen_task(
        'deepen_premature_close', 'abcdef0000000000000000000000cc12', 'en',
        None, section_idx=0, mode='deeper', section_heading='H',
        user_id=TEST_OWNER_USER_ID)
    de._run_deepen_task(task, [{'role': 'user', 'content': 'x'}],
                        paper_hash='abcdef0000000000000000000000cc12',
                        section={'heading': 'H', 'hash': 'h'}, ui_lang='en')

    assert task['status'] == 'done'
    assert seen[1] == seen[0]
    assert cached == [answer]
