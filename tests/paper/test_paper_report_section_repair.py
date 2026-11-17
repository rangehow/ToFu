"""Required report sections: prompt-derived list and the one-round repair pass."""

import pytest

from lib.llm.llm_errors import AbortedError
from lib.paper.prompts import _REPORT_PROMPT_EN, _REPORT_PROMPT_ZH
from lib.paper.report_engine._sections import (
    find_missing_sections,
    insert_repaired_sections,
    required_sections,
    section_headings_from_prompt,
)


pytestmark = pytest.mark.unit

EN = [s.heading for s in required_sections('en')]


def _report(indices, lang='en'):
    headings = [s.heading for s in required_sections(lang)]
    return ''.join(f'## {headings[i]}\nBody {i}.\n\n' for i in indices)


def test_section_list_is_derived_from_both_prompts():
    en = section_headings_from_prompt(_REPORT_PROMPT_EN)
    zh = section_headings_from_prompt(_REPORT_PROMPT_ZH)
    assert len(en) == len(zh) == 10
    assert [s.emoji for s in required_sections('en')] == [
        s.emoji for s in required_sections('zh')]
    assert EN[0] == '⚡ TL;DR' and EN[-1] == '📝 Technical Reference'
    assert EN[4] == '💡 Method — How It Works'
    assert [s.heading for s in required_sections('zh')][8] == '🗺️ 研究全景与影响'
    # Quality-bar / formatting headings before the first --- are not sections.
    assert not any('Quality bar' in h or '质量底线' in h for h in en + zh)


def test_heading_matching_tolerates_level_bold_emoji_and_title_drift():
    report = (
        '# ⚡ TL;DR\nx\n'
        '### **📋 Paper Card**\nx\n'
        '## 🔑 Core Terminology (read this first)\nx\n'
        '## Problem & Motivation\nx\n'
        '## 💡 Method\nx\n'
        '## 📊 实验分析\nx\n'
        '## ✅ Strengths\nx\n'
        '## ⚠ Weaknesses and Limitations\nx\n'
        '```\n## 🗺️ Research Landscape & Impact\n```\n'
    )
    missing = find_missing_sections(report, 'en')
    assert [s.heading for s in missing] == [EN[8], EN[9]]


def test_insertion_is_canonical_and_drops_repeated_sections():
    report = _report([0, 1, 2, 4, 5, 6, 7]) + '### trailing sub of 7\nkeep\n'
    missing = find_missing_sections(report, 'en')
    assert [s.index for s in missing] == [3, 8, 9]
    reply = ('Sure, here they are.\n# 📝 Technical Reference\nRef body.\n'
             '## ⚡ TL;DR\nDuplicate must be dropped.\n'
             '## 🎯 Problem & Motivation\nProblem body.\n'
             '### 🗺️ Research Landscape & Impact\n### Predecessors\nLand body.\n')
    out, repaired = insert_repaired_sections(report, reply, missing, 'en')
    assert [s.index for s in repaired] == [3, 8, 9]
    assert find_missing_sections(out, 'en') == []
    order = [out.index(f'## {h}\n') for h in EN]
    assert order == sorted(order)
    assert out.count('⚡ TL;DR') == 1 and 'Duplicate' not in out
    assert out.index('keep') < out.index(EN[8])
    assert '### Predecessors\nLand body.' in out
    assert 'Sure, here' not in out


def test_insertion_keeps_thematic_break_between_sections():
    report = '\n---\n\n'.join(f'## {EN[i]}\nBody {i}.\n' for i in (0, 1, 3))
    missing = find_missing_sections(report, 'en')
    reply = f'## {EN[2]}\nTerms body.\n'
    out, repaired = insert_repaired_sections(report, reply, missing[:1], 'en')
    assert [s.index for s in repaired] == [2]
    between = out[out.index(EN[2]):out.index(EN[3])]
    assert '\n---\n' in between
    assert out.count('\n---\n') == 3


@pytest.fixture
def report_case(monkeypatch):
    from tests.support.paper_report import isolated_report_case

    with isolated_report_case(monkeypatch) as case:
        yield case


def _scripted(replies):
    calls = []

    def dispatch(messages, *, on_content, **kwargs):
        calls.append([dict(m) for m in messages])
        reply = replies[len(calls) - 1]
        if isinstance(reply, BaseException):
            raise reply
        if len(calls) == 1:
            on_content(reply)
        return ({'role': 'assistant', 'content': reply, 'tool_calls': []}, 'stop',
                {'prompt_tokens': 100, 'completion_tokens': 7 * len(calls)})

    return dispatch, calls


def test_missing_sections_repaired_in_one_round(report_case, monkeypatch):
    run, _, worker = report_case
    captured = {}
    monkeypatch.setattr(worker, '_build_report_meta',
                        lambda model, provider, usage, rounds, elapsed:
                        captured.update(usage=dict(usage), rounds=rounds) or {})
    body = _report(range(8))
    repair = f'## {EN[9]}\nRef.\n\n## {EN[8]}\nLand.\n'
    dispatch, calls = _scripted([body, repair])
    task, published = run(dispatch)

    assert task['status'] == 'done'
    assert len(calls) == 2
    assert calls[1][-2] == {'role': 'assistant', 'content': body}
    request = calls[1][-1]['content']
    assert calls[1][-1]['role'] == 'user'
    assert f'## {EN[8]}' in request and f'## {EN[9]}' in request
    assert f'## {EN[7]}' not in request
    report = published[0].report
    assert report.index(EN[7]) < report.index(EN[8]) < report.index(EN[9])
    assert report == task['full_text']
    assert published[0].meta['sectionRepair'] == {
        'missing': [EN[8], EN[9]], 'repaired': [EN[8], EN[9]]}
    assert captured == {'usage': {
        'prompt_tokens': 200, 'completion_tokens': 21, 'cache_read_tokens': 0,
        'cache_write_tokens': 0, 'reasoning_tokens': 0}, 'rounds': 2}
    assert published[0].meta['agentUsageV1']['agent_dispatches'] == 2
    events = [{k: e[k] for k in ('type', 'delta', 'llmRound') if k in e}
              for e in task['events'] if e['type'] in ('delta', 'delta_reset')]
    assert events[-2:] == [{'type': 'delta_reset'}, {'type': 'delta', 'delta': report}]


def test_complete_report_triggers_no_extra_dispatch(report_case):
    run, _, _ = report_case
    body = _report(range(10))
    dispatch, calls = _scripted([body])
    task, published = run(dispatch)
    assert task['status'] == 'done'
    assert len(calls) == 1
    assert published[0].report == body
    assert 'sectionRepair' not in published[0].meta


def test_review_family_is_untouched(report_case):
    run, task, _ = report_case
    task['lang'] = 'review:iclr:en'
    task['ui_lang'] = 'en'
    dispatch, calls = _scripted(['# Review\n\n## Summary\nX.\n\n## Strengths\n- A.\n'])
    task, published = run(dispatch)
    assert task['status'] == 'done'
    assert len(calls) == 1
    assert 'sectionRepair' not in published[0].meta


def test_repair_failure_publishes_original(report_case):
    run, _, _ = report_case
    body = _report(range(9))
    dispatch, calls = _scripted([body, RuntimeError('provider down')])
    task, published = run(dispatch)
    assert task['status'] == 'done'
    assert len(calls) == 2
    assert published[0].report == body
    assert published[0].meta['sectionRepair'] == {'missing': [EN[9]], 'repaired': []}


def test_abort_during_repair_is_honored(report_case):
    run, _, _ = report_case
    body = _report(range(9))
    dispatch, _ = _scripted([body, AbortedError('stop')])
    task, published = run(dispatch)
    assert task['status'] != 'done'
    assert not published
    assert any(e['type'] == 'aborted' for e in task['events'])


def test_repair_reply_tool_calls_do_not_start_another_round(report_case):
    run, _, _ = report_case
    body = _report(range(9))
    calls = []

    def dispatch(messages, *, on_content, **kwargs):
        calls.append(len(messages))
        if len(calls) == 1:
            on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': []}, 'stop', {})
        return ({'role': 'assistant', 'content': f'## {EN[9]}\nRef.\n',
                 'tool_calls': [{'id': 't', 'function': {
                     'name': 'web_search', 'arguments': '{}'}}]}, 'tool_calls', {})

    task, published = run(dispatch)
    assert task['status'] == 'done'
    assert len(calls) == 2
    assert published[0].report.rstrip().endswith(f'## {EN[9]}\n\nRef.')
