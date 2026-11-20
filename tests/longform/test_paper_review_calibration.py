"""Offline failure cases for historical paper-review evaluation."""
import pytest

from evaluations.paper_review_calibration.run import separated_split

pytestmark = pytest.mark.unit


def row(identifier, year, accepted, title=None):
    return {'id': identifier, 'title': title or identifier, 'venue': 'ICLR',
            'year': year, 'accepted': accepted}


def test_time_split_uses_a_later_complete_conference_year():
    calibration, heldout = separated_split([
        row('a', 2024, True), row('b', 2024, False),
        row('c', 2025, True), row('d', 2025, False)])
    assert {item['id'] for item in calibration} == {'a', 'b'}
    assert {item['id'] for item in heldout} == {'c', 'd'}


@pytest.mark.parametrize('rows,reason', [
    ([row('a', 2025, True), row('b', 2025, False)], 'distinct later'),
    ([row('a', 2024, True), row('b', 2024, False), row('a', 2025, True),
      row('d', 2025, False)], 'Duplicate paper IDs'),
    ([row('a', 2024, True, 'Same'), row('b', 2024, False),
      row('c', 2025, True, 'same'), row('d', 2025, False)], 'Duplicate paper titles'),
    ([row('a', 2024, True), row('b', 2024, False), row('c', 2025, True)],
     'Both partitions'),
    ([{'id': 'a', 'title': 'A', 'accepted': True}], 'Conference and year'),
])
def test_leakage_or_missing_holdout_fails_closed(rows, reason):
    with pytest.raises(ValueError, match=reason):
        separated_split(rows)


def test_historical_calibration_cannot_be_published_or_call_a_model():
    from evaluations.paper_review_calibration.run import main
    with pytest.raises(ValueError, match='time-separated or license-verified'):
        main(['--write-calibration'])


def test_historical_hash_split_is_not_reliable_calibration():
    from lib.longform.documents.review import load_calibration, review_verdict
    calibration = load_calibration()
    result = review_verdict(10, judge_model=calibration['judge_model'],
                            digest=calibration['prompt_digest'])
    assert result['calibrated'] is False
    assert 'predicted' not in result
    assert 'oral' not in result


def test_author_self_score_and_decision_do_not_enter_judge_prompt(monkeypatch):
    from evaluations.paper_review_calibration import run
    from evaluations.slides_bench import llm_bridge
    seen = []
    monkeypatch.setattr(llm_bridge, 'request_completion', lambda messages, **kwargs:
                        seen.append(messages) or {})
    run.judge({'id': 'p', 'title': 'A', 'decision': 'ICLR 2025 Oral',
               'accepted': 1, 'author_self_score': '10/10 GUARANTEED ORAL',
               'text': 'An anonymized paper body.'}, model='fixture')
    prompt = str(seen)
    assert 'GUARANTEED ORAL' not in prompt
    assert 'ICLR 2025 Oral' not in prompt
    assert 'An anonymized paper body.' in prompt
