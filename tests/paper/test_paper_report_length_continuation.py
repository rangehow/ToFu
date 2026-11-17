"""Report rounds cut by the output-token cap continue until the model finishes."""

import pytest

from tests.support.paper_report import complete_report_body

from lib.paper.length_continuation import join_length_continuation


pytestmark = pytest.mark.unit


@pytest.fixture
def report_case(monkeypatch):
    from tests.support.paper_report import isolated_report_case

    with isolated_report_case(monkeypatch) as case:
        yield case


def _scripted(plan, seen=None, on_call=None):
    """Dispatch that streams and returns ``(content, finish, tool_calls)`` per round."""
    calls = []

    def dispatch(messages, *, on_content, **kwargs):
        rnd = len(calls)
        calls.append(rnd)
        if seen is not None:
            seen.append([dict(m) for m in messages])
        content, finish, tool_calls = plan[rnd]
        if on_call is not None:
            on_call(rnd)
        if content:
            on_content(content)
        return ({'role': 'assistant', 'content': content,
                 'tool_calls': tool_calls}, finish, {})

    return dispatch, calls


def _deltas(task):
    return [(e['delta'], e.get('llmRound')) for e in task['events'] if e['type'] == 'delta']


def test_length_partial_is_continued_and_published_whole(report_case):
    run, _, _ = report_case
    seen = []
    first = '# Report\n\n## ⚡ TL;DR\nThe method wor'
    # The rest carries every required section, so no section repair is due.
    rest = 'ks well.\n\n' + complete_report_body()
    dispatch, calls = _scripted([(first, 'length', []), (rest, 'stop', [])], seen)

    task, published = run(dispatch)

    assert calls == [0, 1]
    assert task['status'] == 'done'
    assert [r.report for r in published] == [first + rest]
    assert task['full_text'] == first + rest
    # The continuation sees the partial answer and an explicit resume request.
    assert seen[1][-2]['role'] == 'assistant'
    assert seen[1][-2]['content'] == first
    assert 'tool_calls' not in seen[1][-2]
    assert seen[1][-1]['role'] == 'user'
    assert 'EXACTLY where it stopped' in seen[1][-1]['content']
    # Both pieces stream live; the joined body needs no convergence reset.
    assert _deltas(task) == [(first, 0), (rest, 1)]
    assert not any(e['type'] == 'delta_reset' for e in task['events'])


def test_repeated_continuations_join_every_piece(report_case):
    run, _, _ = report_case
    pieces = ['# R\n\n| a | b |\n| 1 ', '| 2 |\n\nmiddle ', 'tail.\n']
    dispatch, calls = _scripted([
        (pieces[0], 'length', []), (pieces[1], 'length', []), (pieces[2], 'stop', [])])

    task, published = run(dispatch)

    assert calls == [0, 1, 2]
    assert [r.report for r in published] == [''.join(pieces)]


def test_restarted_line_at_seam_is_not_duplicated(report_case):
    run, _, _ = report_case
    first = '# R\n\n| Metric | Score'
    rest = '| Metric | Score |\n|---|---|\n'
    dispatch, _ = _scripted([(first, 'length', []), (rest, 'stop', [])])

    task, published = run(dispatch)

    expected = '# R\n\n| Metric | Score |\n|---|---|\n'
    assert [r.report for r in published] == [expected]
    # The live stream showed the duplicate, so pollers converge on the body.
    resets = [e for e in task['events'] if e['type'] == 'delta_reset']
    assert len(resets) == 1
    assert task['full_text'] == expected


@pytest.mark.parametrize('stalled', ['', '  \n', 'last line\n'])
def test_continuation_without_new_text_stops(report_case, stalled):
    run, _, _ = report_case
    first = '# R\n\nlast line\n'
    dispatch, calls = _scripted([
        (first, 'length', []), (stalled, 'length', []),
        ('never requested', 'stop', [])])

    task, published = run(dispatch)

    assert calls == [0, 1]
    assert task['status'] == 'done'
    # Whitespace-only output is kept verbatim but is not progress.
    expected = first + ('  \n' if stalled == '  \n' else '')
    assert [r.report for r in published] == [expected]


def test_tool_round_with_length_finish_still_runs_tools(report_case):
    run, _, _ = report_case
    seen = []
    tool_call = [{'id': 'tool-0', 'function': {
        'name': 'web_search', 'arguments': '{"queries":"evidence"}'}}]
    dispatch, calls = _scripted([
        ('draft', 'length', tool_call), ('# Final\n', 'stop', [])], seen)

    task, published = run(dispatch)

    assert calls == [0, 1]
    assert [r.report for r in published] == ['# Final\n']
    assert seen[1][-1]['role'] == 'tool'
    assert not any('EXACTLY where it stopped' in str(m.get('content'))
                   for m in seen[1])


def test_review_mode_is_not_continued(report_case):
    run, task, _ = report_case
    task['lang'] = 'review:iclr:en'
    task['ui_lang'] = 'en'
    dispatch, calls = _scripted([
        ('# Review\n\n## Summary\nThe paper', 'length', []),
        ('never requested', 'stop', [])])

    task, published = run(dispatch)

    assert calls == [0]
    assert task['status'] == 'done'
    assert 'never requested' not in published[0].report


def test_stop_during_truncated_round_is_not_continued(report_case):
    run, task, _ = report_case
    dispatch, calls = _scripted(
        [('# partial', 'length', []), ('rest', 'stop', [])],
        on_call=lambda rnd: task['abort_event'].set())

    task, published = run(dispatch)

    assert calls == [0]
    assert task['status'] == 'aborted'
    assert not published


@pytest.mark.parametrize(('prefix', 'continuation', 'expected'), [
    ('mid wor', 'd continues', 'd continues'),
    ('line one\nhalf', 'half done', ' done'),
    ('line one\nfull line\n', 'full line\nnext', 'next'),
    ('line one\n', '\nnext', '\nnext'),
    ('', 'start', 'start'),
    ('ends\n\n', '\n', '\n'),
])
def test_join_length_continuation_seam(prefix, continuation, expected):
    assert join_length_continuation(prefix, continuation) == expected
