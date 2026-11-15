"""Auto-research reuses the project's Agent event/UI contracts end to end."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('raw,expected', [
    ('2502.00299v5', '2502.00299'),
    ('arXiv:2502.00299v5', '2502.00299'),
    ('https://arxiv.org/abs/2502.00299v5', '2502.00299'),
    ({'arxiv_id': 'hep-th/0601001v2'}, 'hep-th/0601001'),
])
def test_shared_arxiv_normalizer_handles_model_citation_shapes(raw, expected):
    from lib.paper.arxiv import normalize_arxiv_id
    assert normalize_arxiv_id(raw) == expected


def test_recipe_threads_tool_events_through_survey_and_ideate(monkeypatch):
    import lib.research.recipe as rc

    seen = []
    gaps = {'open_gaps': [{'id': 'g1'}]}

    def survey(*args, on_tool_event=None, **kwargs):
        assert callable(on_tool_event)
        on_tool_event({'type': 'tool_start', 'toolName': 'web_search'})
        return {'ok': True, 'open_gaps': gaps, 'survey_md': '# S',
                'inputs_used': 3, 'citation_audit': None,
                'usage': {'calls': 2}}

    def ideate(*args, on_tool_event=None, **kwargs):
        assert callable(on_tool_event)
        on_tool_event({'type': 'tool_done', 'toolName': 'web_search'})
        return {'ok': True, 'accepted': [],
                'rejected': [{'reject_stage': 'rubric'}], 'threshold': 4.0,
                'gate_reached': 'rubric', 'usage': {'calls': 3}}

    monkeypatch.setattr(rc, '_build_survey', survey)
    monkeypatch.setattr(rc, '_generate_ideas', ideate)
    monkeypatch.setattr(rc, '_persist_survey', lambda *a, **k: True)
    monkeypatch.setattr(rc, '_persist_ideate', lambda *a, **k: True)
    ctx = {'direction': 'd', 'lang': 'en', 'user_id': 1,
           'abort': lambda: False, 'n_ideas': 2, 'emit': seen.append,
           'artifacts': {'harvest': {'arxiv_ids': ['1', '2', '3'],
                                     'folder_id': 'f'}}}
    survey_art = rc._run_survey(ctx)
    ctx['artifacts']['survey'] = survey_art
    ideate_art = rc._run_ideate(ctx)

    assert [event['type'] for event in seen] == ['tool_start', 'tool_done']
    assert survey_art['usage']['calls'] == 2
    assert ideate_art['usage']['calls'] == 3


def test_survey_grounds_against_loaded_corpus_not_mutable_folder(monkeypatch):
    """A paper can be shared by multiple research runs but has one folder_id.
    Folder drift must not downgrade a paper the current survey actually read."""
    import lib.paper.survey as survey

    monkeypatch.setattr(survey, '_load_paper_inputs', lambda *a, **k: [{
        'arxiv_id': '2502.00299', 'paper_hash': 'h', 'title': 'ChunkKV',
        'source': 'parsed_text', 'content': 'paper text'}])
    from tests.paper.test_paper_survey import _hierarchy_dispatch
    monkeypatch.setattr(survey, 'dispatch_stream', _hierarchy_dispatch(
        '# Survey arXiv:2502.00299',
        {'schema_version': 1, 'clusters': [], 'method_matrix': [],
         'open_gaps': [{'id': 'g1', 'gap': 'x',
                        'evidence': ['2502.00299']}]}))
    monkeypatch.setattr(survey, '_audit_citations', lambda *a, **k: None)
    # If build_survey falls back to folder membership, this empty set makes the
    # evidence grounded/low-confidence (or stripped) instead of library-grade.
    monkeypatch.setattr(survey, '_library_id_set', lambda *a, **k: set())

    got = survey.build_survey(
        'direction', ['2502.00299'], user_id=1, folder_id='raced-away')
    gap = got['open_gaps']['open_gaps'][0]
    assert gap['evidence_tiers']['2502.00299'] == 'library'
    assert gap['library_evidence_count'] == 1
    assert gap['low_confidence'] is True  # loaded identity is not scientific entailment
    assert gap['reference_verification']['support'] == 'insufficient'
    assert gap['evidence_assessments']
    assert all(row['status'] == 'insufficient' for row in gap['evidence_assessments'])
    assert got['open_gaps']['surveyed_arxiv_ids'] == ['2502.00299']


def test_survey_checkpoint_restores_all_usage_without_double_charging(tmp_path, monkeypatch):
    from lib.research.survey_hierarchy import BoundedCalls
    from lib.research.telemetry import ResearchUsageMeter
    import lib.research.telemetry as telemetry

    monkeypatch.setattr(telemetry, 'compute_cost', lambda *a, **k: {
        'costUsd': 0.01, 'costCny': 0.07, 'pricingSource': 'default_estimate'})
    attempts = []

    def dispatch(*args, **kwargs):
        attempts.append(1)
        return ({'content': '{"summary":"saved"}'}, 'stop', {
            'prompt_tokens': 100, 'completion_tokens': 20,
            'model': 'fixture-model'})

    def caller():
        return BoundedCalls(dispatch, json.loads, ResearchUsageMeter('survey'),
                            direction='resume', user_id=1, workdir=tmp_path)

    original = caller()
    original('map', {'x': 1})
    before = original.meter.snapshot()
    assert before['calls'] == before['priced_calls'] == 1
    assert before['prompt_tokens'] == 100
    assert before['completion_tokens'] == 20
    assert before['cost_usd'] == 0.01
    resumed = caller()
    assert resumed.meter.snapshot() == before
    for _ in range(2):
        resumed('map', {'x': 1})
    assert resumed.meter.snapshot() == before
    resumed('map', {'x': 2})
    assert resumed.meter.calls == len(attempts) == 2
    assert resumed.meter.agent_tokens == 240
    assert caller().meter.snapshot() == resumed.meter.snapshot()


def test_survey_checkpoint_concurrent_instances_share_admission(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from lib.research.survey_hierarchy import BoundedCalls
    from lib.research.telemetry import ResearchUsageMeter

    attempts = []

    def dispatch(*args, **kwargs):
        attempts.append(1)
        time.sleep(0.03)
        return ({'content': '{"summary":"saved"}'}, 'stop', {})

    def caller():
        return BoundedCalls(dispatch, json.loads,
                            ResearchUsageMeter('survey', dispatch_budget=2),
                            direction='concurrent', user_id=1, workdir=tmp_path)

    callers = [caller() for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda call: call('map', {'x': 1}), callers))
    assert results == [{'summary': 'saved'}] * 4
    assert len(attempts) == 1
    assert sum(call.cache_hits for call in callers) == 3
    callers[0]('map', {'x': 2})
    with pytest.raises(RuntimeError, match='budget exhausted'):
        callers[1]('map', {'x': 3})
    assert len(attempts) == 2
    assert caller().meter.calls == 2


def test_survey_checkpoint_accounts_failed_responses_on_resume(tmp_path):
    from lib.research.survey_hierarchy import BoundedCalls
    from lib.research.telemetry import ResearchUsageMeter

    def dispatch(*args, **kwargs):
        raise RuntimeError('provider unavailable')

    def caller():
        return BoundedCalls(dispatch, json.loads, ResearchUsageMeter('survey'),
                            direction='failure', user_id=1, workdir=tmp_path)

    original = caller()
    with pytest.raises(RuntimeError, match='provider unavailable'):
        original('map', {})
    resumed = caller()
    assert resumed.meter.calls == resumed.meter.agent_dispatches == 1
    assert resumed.meter.snapshot() == original.meter.snapshot()
    assert resumed.reserved_tokens == original.reserved_tokens > 0
