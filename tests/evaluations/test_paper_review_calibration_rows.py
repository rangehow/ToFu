"""Calibration reuse requires exact request, input and response identity."""
import hashlib
import json

import pytest

from evaluations.paper_review_calibration import run
from evaluations.slides_bench import llm_bridge

pytestmark = pytest.mark.unit

ENDPOINT = 'http://127.0.0.1:9/v1/chat/completions'
ROUTE = 'synthetic-transport-route-v1'
SELECTION_RECEIPT = {
    'per_class': 1, 'selector_sha256': 'a' * 64, 'selection_sha256': 'b' * 64,
    'parquet_sha256': {name: 'c' * 64 for name in run._FILES}, 'status': 'completed',
}


@pytest.fixture
def paper():
    return {'id': 'a', 'title': 'A', 'decision': 'reject', 'accepted': False,
            'text': 'An anonymous submission.', 'selection_receipt': dict(SELECTION_RECEIPT)}


def valid_row(paper):
    return {**paper, **run.request_provenance(
        paper, 'fixture', run.review_request(paper)[2], endpoint=ENDPOINT, effective_route=ROUTE),
            'reported_model': 'fixture', 'overall': 5, 'errors': [], 'finish_reason': 'stop',
            'completion_validation': 'compatible_text_choice_v1'}


def reuse(rows, paper):
    return run.reusable_rows([json.dumps(row) for row in rows], run.current_prompt_digest(),
                             papers=[paper], model='fixture', endpoint=ENDPOINT,
                             effective_route=ROUTE)


def test_exact_provenance_is_reusable(paper):
    assert set(reuse([valid_row(paper)], paper)) == {'a'}


@pytest.mark.parametrize('field', ['prompt_digest', 'request_prompt_digest', 'paper_input_digest',
                                   'requested_model', 'reported_model', 'endpoint', 'effective_route',
                                   'selection_receipt', 'completion_validation'])
@pytest.mark.parametrize('replacement', [None, 'stale'])
def test_missing_or_stale_provenance_is_not_reusable(paper, field, replacement):
    row = valid_row(paper)
    if replacement is None:
        row.pop(field)
    else:
        row[field] = replacement
    assert reuse([row], paper) == {}


@pytest.mark.parametrize('field,value', [('text', 'Changed submission'), ('title', 'New title'),
                                         ('accepted', True)])
def test_changed_paper_is_not_reusable(paper, field, value):
    row = valid_row(paper)
    paper[field] = value
    assert reuse([row], paper) == {}


@pytest.mark.parametrize('overall,errors', [(None, ['invalid']), (True, []), (11, []),
                                           (5, ['invalid'])])
def test_failed_or_invalid_review_is_not_reusable(paper, overall, errors):
    row = valid_row(paper)
    row.update(overall=overall, errors=errors)
    assert reuse([row], paper) == {}


@pytest.mark.parametrize('reported', [None, 'other', 'fixture'])
def test_judge_retains_provenance_on_invalid_reply(monkeypatch, paper, reported):
    seen = []
    def request_completion(messages, **kwargs):
        seen.append(messages)
        return {'model': reported, 'choices': [{'finish_reason': 'stop',
                                                'message': {'content': '{broken JSON}'}}],
                'usage': {'prompt_tokens': 7}}
    monkeypatch.setattr(llm_bridge, 'request_completion', request_completion)
    row = run.judge(paper, model='fixture')
    assert row['request_prompt_digest'] == hashlib.sha256(
        json.dumps(seen[0], sort_keys=True).encode()).hexdigest()
    assert row['paper_input_digest'] == valid_row(paper)['paper_input_digest']
    assert row['requested_model'] == 'fixture'
    assert row['reported_model'] == reported
    assert row['usage']['prompt_tokens'] == 7
    assert row['overall'] is None
    assert row['errors']
    if reported != 'fixture':
        assert 'Model identity mismatch' in row['errors'][0]
    assert reuse([row], paper) == {}


def test_actual_prompt_change_invalidates_reuse(monkeypatch, paper):
    row = valid_row(paper)
    from lib.longform.documents import review
    original = review.review_messages
    monkeypatch.setattr(review, 'review_messages', lambda *args, **kwargs:
                        original(*args, **kwargs) + [{'role': 'user', 'content': 'changed'}])
    assert reuse([row], paper) == {}


@pytest.mark.parametrize('reported', [None, 'other', 'fixture'])
def test_identity_is_checked_before_parsing(monkeypatch, paper, reported):
    from lib.longform.documents import review
    parsed = []
    monkeypatch.setattr(llm_bridge, 'request_completion', lambda *args, **kwargs: {
        'model': reported, 'choices': [{'finish_reason': 'stop', 'message': {'content': 'reply'}}]})
    monkeypatch.setattr(review, 'parse_review', lambda *args, **kwargs:
                        (parsed.append(args[0]) or {'overall': 5, 'scores': {}}, []))
    row = run.judge(paper, model='fixture')
    assert parsed == (['reply'] if reported == 'fixture' else [])
    assert not reuse([row], paper)
    assert row['effective_route'] is None


def test_transport_failure_retains_request_provenance(monkeypatch, paper):
    def fail(*args, **kwargs):
        raise RuntimeError('offline failure')
    monkeypatch.setattr(llm_bridge, 'request_completion', fail)
    row = run.judge(paper, model='fixture')
    assert row['request_prompt_digest'] == valid_row(paper)['request_prompt_digest']
    assert row['paper_input_digest'] == valid_row(paper)['paper_input_digest']
    assert row['requested_model'] == 'fixture'
    assert row['reported_model'] is None
    assert row['errors'] == ['RuntimeError: offline failure']


def test_cached_labels_and_metadata_come_from_corpus_without_rewriting(paper):
    row = valid_row(paper)
    row.update(accepted=True, decision='oral', title='Tampered title')
    serialized = json.dumps(row)
    result = reuse([row], paper)['a']
    assert {key: result[key] for key in ('accepted', 'decision', 'title')} == {
        key: paper[key] for key in ('accepted', 'decision', 'title')}
    assert json.dumps(row) == serialized


@pytest.mark.parametrize('endpoint,route', [(None, ROUTE), (ENDPOINT, None),
                                           ('http://127.0.0.1:8/completions', ROUTE),
                                           (ENDPOINT, 'upgraded-route')])
def test_endpoint_or_effective_route_change_blocks_reuse(paper, endpoint, route):
    assert run.reusable_rows([json.dumps(valid_row(paper))], run.current_prompt_digest(),
                             papers=[paper], model='fixture', endpoint=endpoint,
                             effective_route=route) == {}


@pytest.mark.parametrize('rows', [[], [{'overall': 4, 'accepted': True}],
                                 [{'overall': 4, 'accepted': False}]])
@pytest.mark.parametrize('metric', [run.auc, run.best_threshold, run.bootstrap_auc,
                                   lambda rows: run.at_threshold(rows, 5)])
def test_class_empty_metrics_refuse_insufficient_evidence(rows, metric):
    with pytest.raises(ValueError, match='Insufficient evidence'):
        metric(rows)


@pytest.mark.parametrize('missing', ['calibration', 'heldout', 'all', None])
def test_summary_uses_corpus_and_replaces_stale_result(monkeypatch, tmp_path, missing):
    papers = []
    for parity in (0, 1):
        for accepted in (True, False):
            identifier = next(str(i) for i in range(len(papers) * 100, 10000)
                              if int(run._key(str(i))[-1], 16) % 2 == parity)
            papers.append(dict(id=identifier, title=identifier, accepted=accepted,
                               decision='accept' if accepted else 'reject', text='Synthetic',
                               selection_receipt=dict(SELECTION_RECEIPT)))
    monkeypatch.setattr(run, 'select_papers', lambda *args: papers)
    def judge(paper, **kwargs):
        parity = int(run._key(paper['id'])[-1], 16) % 2
        failed = missing == 'all' or (missing is not None and not paper['accepted'] and
                 parity == (0 if missing == 'calibration' else 1))
        return {**paper, 'accepted': not paper['accepted'], 'decision': 'tampered',
                'title': 'tampered', 'reported_model': 'fixture', 'finish_reason': 'stop',
                'completion_validation': 'compatible_text_choice_v1',
                'overall': None if failed else 8,
                'errors': ['synthetic failure'] if failed else []}
    monkeypatch.setattr(run, 'judge', judge)
    summary_path = tmp_path / 'summary-fixture.json'
    summary_path.write_text('{"heldout_auc": 1.0, "threshold": 8}')
    assert run.main(['--out', str(tmp_path), '--judge-model', 'fixture',
                     '--endpoint', ENDPOINT]) == 0
    summary = json.loads(summary_path.read_text())
    if missing is None:
        assert summary['status'] == 'diagnostic'
        assert summary['threshold'] == 8
        assert summary['heldout_auc'] == summary['calibration_auc'] == 0.5
        assert summary['decision_counts'] == {'accept': 2, 'reject': 2}
        assert summary['heldout_accepted'] == 1
    else:
        assert summary['status'] == 'insufficient_evidence'
        assert summary['reason']
        assert summary['threshold'] is None
        assert summary['heldout_auc'] is None
        assert summary['calibration_auc'] is None
    raw_rows = [json.loads(line) for line in (tmp_path / 'rows-fixture.jsonl').read_text().splitlines()]
    assert all(row['decision'] == 'tampered' for row in raw_rows)
    assert summary['selection_receipt'] == SELECTION_RECEIPT
    assert all(row['selection_receipt'] == SELECTION_RECEIPT for row in raw_rows)
    assert summary['effective_route'] is None
    assert summary['cache_reuse_eligible'] is False


@pytest.mark.parametrize('finish_reason', [None, '', 'unknown', 'tool_calls',
                                          'content_filter', 'length', 'stop', 'end_turn'])
def test_reuse_requires_normal_terminal_completion(paper, finish_reason):
    row = valid_row(paper)
    row['finish_reason'] = finish_reason
    assert bool(reuse([row], paper)) is (finish_reason in ('stop', 'end_turn'))


@pytest.mark.parametrize('field', ['selection_sha256', 'selector_sha256', 'parquet_sha256',
                                  'per_class', 'status'])
def test_changed_selection_receipt_blocks_detached_scores(paper, field):
    row = valid_row(paper)
    paper['selection_receipt'] = {**paper['selection_receipt'], field: 'changed'}
    assert reuse([row], paper) == {}


def test_missing_selection_receipt_blocks_detached_scores(paper):
    row = valid_row(paper)
    row.pop('selection_receipt')
    assert reuse([row], paper) == {}


@pytest.mark.parametrize('status', [None, 'incomplete', 'failed'])
def test_judge_without_completed_selection_cannot_score(monkeypatch, paper, status):
    if status is None:
        paper.pop('selection_receipt')
    else:
        paper['selection_receipt']['status'] = status
    from lib.longform.documents import review
    monkeypatch.setattr(llm_bridge, 'request_completion', lambda *args, **kwargs: {
        'model': 'fixture', 'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})
    monkeypatch.setattr(review, 'parse_review', lambda *args, **kwargs:
                        ({'overall': 8, 'scores': {}}, []))
    row = run.judge(paper, model='fixture')
    assert row['overall'] is None
    assert 'completed selection receipt' in row['errors'][0]
