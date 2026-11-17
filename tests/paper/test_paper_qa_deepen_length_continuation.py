"""Q&A and Deepen answers cut by the output-token cap continue until finished.

Both engines reuse ``lib.paper.length_continuation`` (the report's policy):
a no-tool ``length`` round is resumed, the published answer is the joined
pieces, and a continuation that adds no visible text ends the loop.
"""

import pytest

import lib.paper.deepen_engine as de
import lib.paper.qa_engine as qe
from lib.paper.length_continuation import OUTPUT_TOKEN_CEILING
from lib.paper.qa_runtime import _new_qa_task

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def _scripted(plan):
    """Dispatch streaming and returning ``(content, finish)`` per no-tool round."""
    seen = []

    def dispatch(messages, *, on_content=None, **kwargs):
        seen.append({'messages': [dict(m) for m in messages],
                     'max_tokens': kwargs.get('max_tokens')})
        content, finish = plan[len(seen) - 1]
        if content and on_content:
            on_content(content)
        return ({'role': 'assistant', 'content': content, 'tool_calls': None},
                finish, {})

    return dispatch, seen


def _run_qa(monkeypatch, plan):
    dispatch, seen = _scripted(plan)
    monkeypatch.setattr(qe, 'dispatch_stream', dispatch)
    task = _new_qa_task(
        f'qa_len_{len(plan)}_{id(plan)}', 'abcdef0000000000000000000000cc01',
        'en', None, question='q', user_id=TEST_OWNER_USER_ID)
    qe._run_qa_task(task, [{'role': 'system', 'content': 's'},
                           {'role': 'user', 'content': 'q'}])
    done = [e for e in task['events'] if e.get('type') == 'done']
    return task, done, seen


def _run_deepen(monkeypatch, plan):
    dispatch, seen = _scripted(plan)
    monkeypatch.setattr(de, 'dispatch_stream', dispatch)
    cached = []
    monkeypatch.setattr(de, '_write_deepen_cache',
                        lambda *args, **kwargs: cached.append(args[5]))
    monkeypatch.setattr(de, '_accumulate_deepen_cost', lambda *a, **k: None)
    task = de._new_deepen_task(
        f'deepen_len_{id(plan)}', 'abcdef0000000000000000000000cc02', 'en',
        None, section_idx=0, mode='deeper', section_heading='H',
        user_id=TEST_OWNER_USER_ID)
    de._run_deepen_task(task, [{'role': 'user', 'content': 'x'}],
                        paper_hash='abcdef0000000000000000000000cc02',
                        section={'heading': 'H', 'hash': 'h'}, ui_lang='en')
    done = [e for e in task['events'] if e.get('type') == 'done']
    return task, done, seen, cached


def test_qa_truncated_answer_is_continued_and_published_whole(monkeypatch):
    first, rest = 'The answer is wor', 'th two parts.\n'
    task, done, seen = _run_qa(monkeypatch, [(first, 'length'), (rest, 'stop')])

    assert len(seen) == 2
    assert task['status'] == 'done'
    assert done[-1]['answer'] == first + rest
    assert task['full_text'] == first + rest
    assert seen[1]['messages'][-2] == {'role': 'assistant', 'content': first}
    assert 'EXACTLY where it stopped' in seen[1]['messages'][-1]['content']
    assert {call['max_tokens'] for call in seen} == {OUTPUT_TOKEN_CEILING}


def test_qa_restarted_seam_line_converges_live_text(monkeypatch):
    first, rest = 'intro\n| a | b', '| a | b |\n'
    task, done, _ = _run_qa(monkeypatch, [(first, 'length'), (rest, 'stop')])

    assert done[-1]['answer'] == 'intro\n| a | b |\n'
    assert task['full_text'] == 'intro\n| a | b |\n'
    assert [e['type'] for e in task['events']
            if e['type'] in ('delta_reset', 'delta')][-2:] == ['delta_reset', 'delta']


@pytest.mark.parametrize('stalled', ['', '  \n', 'last line\n'])
def test_qa_continuation_without_new_text_stops(monkeypatch, stalled):
    first = 'start\nlast line\n'
    task, done, seen = _run_qa(monkeypatch, [
        (first, 'length'), (stalled, 'length'), ('never requested', 'stop')])

    assert len(seen) == 2
    assert task['status'] == 'done'
    assert 'never requested' not in done[-1]['answer']
    assert done[-1]['answer'].startswith(first)


def test_deepen_truncated_answer_is_continued_cached_and_published(monkeypatch):
    first, rest = '## Deeper\nStep one, step', ' two.\n'
    task, done, seen, cached = _run_deepen(
        monkeypatch, [(first, 'length'), (rest, 'stop')])

    assert len(seen) == 2
    assert task['status'] == 'done'
    assert done[-1]['content'] == first + rest
    assert cached == [first + rest]
    assert 'EXACTLY where it stopped' in seen[1]['messages'][-1]['content']
    assert {call['max_tokens'] for call in seen} == {OUTPUT_TOKEN_CEILING}


def test_deepen_continuation_without_new_text_stops(monkeypatch):
    first = '## Deeper\nlast line\n'
    task, done, seen, cached = _run_deepen(monkeypatch, [
        (first, 'length'), ('last line\n', 'length'), ('never requested', 'stop')])

    assert len(seen) == 2
    assert task['status'] == 'done'
    assert done[-1]['content'] == first
    assert cached == [first]
