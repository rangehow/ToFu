import hashlib

from lib.paper.survey import _survey_working_set
from lib.research.survey_hierarchy import map_reduce, BoundedCalls
from lib.research.entailment import verify_quote, assess_gaps
from lib.research.telemetry import ResearchUsageMeter


def papers(n):
    return [dict(arxiv_id=f'2501.{i:05}', title='Test', content='However a competing solution works. ' * 30,
                 source='parsed_text', paper_hash=str(i), source_version='v1') for i in range(n)]


def fake(phase, payload):
    if phase == 'synthesis':
        return {'survey_md': 'Survey', 'gap_map': {'open_gaps': []}}
    return {'summary': 'summary', 'support': 'support', 'counterevidence': 'competing solution'}


def test_all_65_and_partial_batches():
    seen = []
    def call(phase, payload):
        import json
        assert len(json.dumps(payload)) < 40000
        if phase == 'map':
            seen.extend(c['arxiv_id'] for c in payload['cards'])
        return fake(phase, payload)
    result = map_reduce(papers(65), 'solution', call, _survey_working_set)
    assert len(result[2]) == 65
    assert len(set(seen)) == 65
    assert not result[4]
    count = 0
    def fail(phase, payload):
        nonlocal count
        if phase == 'map':
            count += 1
            if count == 2:
                raise RuntimeError('offline failure')
        return fake(phase, payload)
    partial = map_reduce(papers(65), 'solution', fail, _survey_working_set)
    assert 0 < len(partial[2]) < 65
    assert partial[4]


def test_identity_and_late_counterevidence():
    source = papers(30)
    result = map_reduce(source, 'solution', fake, _survey_working_set)
    cards = result[3]
    sources = {p['arxiv_id']: p for p in source}
    assert verify_quote(cards[0], sources)
    assert not verify_quote(dict(cards[0], quote='forged'), sources)
    assert not verify_quote({k: v for k, v in cards[0].items() if k != 'quote'}, sources)
    late = cards[-1]['card_id']
    def assess(phase, payload):
        return {'assessments': [dict(card_id=c['card_id'],
            status='contradicted' if c['card_id'] == late else 'insufficient')
            for c in payload['cards']]}
    gaps = [{'gap': 'No solution'}]
    assess_gaps(gaps, cards, sources, assess)
    assert gaps[0]['status'] == 'closed'
    assert gaps[0]['assessment'] == 'model_assessed'


def test_checkpoint_resume(tmp_path):
    dispatches = []
    def dispatch(*args, **kwargs):
        dispatches.append(args)
        return ({'role': 'assistant', 'content': '{"summary":"cached"}'}, 'stop', {})
    import json
    def caller():
        return BoundedCalls(dispatch, json.loads, ResearchUsageMeter('survey'),
                            direction='q', user_id=1, workdir=tmp_path, sources=['v1'])
    assert caller()('map', {'x': 1}) == {'summary': 'cached'}
    resumed = caller()
    assert resumed('map', {'x': 1}) == {'summary': 'cached'}
    assert len(dispatches) == 1
    assert resumed.cache_hits == 1
    resumed('map', {'x': 2})
    assert len(dispatches) == 2
