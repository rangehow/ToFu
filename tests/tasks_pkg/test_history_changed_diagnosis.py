"""A history_changed refusal names the first Turn whose header changed.

A checkpoint proves its covered history with one digest, so on 2026-11-18 the
tofu lead's restore refused ``history_changed`` (app.log:40464) and rebuilt
3,662,959 tokens without any trace of which Turn had moved. Checkpoints now
carry the headers their proof covers; a refusal compares them with the lane.
"""
import logging

import pytest

from lib.tasks_pkg import model_replay

pytestmark = pytest.mark.unit


def _row(turn_id, parent, *, revision=1, status='completed', attempt=None, actor='assistant'):
    return {'turnId': turn_id, 'parentTurnId': parent, 'status': status,
            'currentAttemptId': attempt or f'attempt-{turn_id}', 'modelContextRevision': revision,
            'actor': actor, 'laneId': 'main', 'projection': {}}


def _lane():
    return [_row('t1', None, actor='human'), _row('t2', 't1', revision=4),
            _row('t3', 't2', actor='human'), _row('target', 't3', status='running')]


def _context(rows, *, covered=0, continuation=None, status='checkpoint_missing'):
    return {'covered': covered, 'continuation': continuation, 'continuationStatus': status,
            'historyProof': {'version': 2, 'count': len(rows) - 1, 'digest': 'a' * 64},
            'proofEligible': True, 'turns': rows}


def _restore(rows, context, config=None):
    config = config if config is not None else {'model': 'claude-opus-5.5'}
    model_replay.restore_prefix(rows, config, conversation_id='conv', user_id=1,
                                validated_context=context)
    return config


def test_a_whole_lane_restore_records_every_covered_header():
    rows = _lane()
    config = _restore(rows, _context(rows))
    assert config['_modelReplayHistoryHeaders'] == [
        ['t1', None, 'completed', 'attempt-t1', 1],
        ['t2', 't1', 'completed', 'attempt-t2', 4],
        ['t3', 't2', 'completed', 'attempt-t3', 1]]


def test_a_restored_prefix_chains_the_checkpoint_headers_and_its_publisher():
    # A covered prefix is not in the page: only the suffix after it arrives.
    rows = _lane()[2:]
    checkpoint = {'historyHeaders': [['t1', None, 'completed', 'attempt-t1', 1]],
                  'publisherHeader': ['t2', None, 'completed', 'attempt-t2', None]}
    context = _context(rows, covered=2, continuation=checkpoint)
    context['historyProof']['count'] = 3
    config = _restore(rows, context)
    assert config['_modelReplayHistoryHeaders'] == [
        ['t1', None, 'completed', 'attempt-t1', 1],
        ['t2', None, 'completed', 'attempt-t2', None],
        ['t3', 't2', 'completed', 'attempt-t3', 1]]


def test_a_count_mismatch_records_nothing():
    rows = _lane()
    context = _context(rows)
    context['historyProof']['count'] = 7
    assert '_modelReplayHistoryHeaders' not in _restore(rows, context)


def test_history_changed_names_the_first_changed_turn(monkeypatch, caplog):
    rows = _lane()
    recorded = [['t1', None, 'completed', 'attempt-t1', 1],
                ['t2', 't1', 'completed', 'attempt-t2', 3],
                ['t3', 't2', 'completed', 'attempt-t3', 1]]
    monkeypatch.setattr(model_replay.ModelContinuationStore, 'get',
                        lambda self, **kwargs: {'historyHeaders': recorded})
    with caplog.at_level(logging.WARNING, logger=model_replay.logger.name):
        config = _restore(rows, _context(rows, status='history_changed'))
    assert config['_modelReplayStatus'] == 'history_changed'
    assert 'first_changed_index=1 turn=t2' in caplog.text
    assert "'attempt-t2', 3]" in caplog.text and "'attempt-t2', 4]" in caplog.text


def test_an_unrecorded_publisher_revision_is_not_reported_as_a_change(monkeypatch, caplog):
    rows = _lane()
    recorded = [['t1', None, 'completed', 'attempt-t1', 1],
                ['t2', None, 'completed', 'attempt-t2', None],
                ['t3', 't2', 'completed', 'attempt-t3', 1]]
    monkeypatch.setattr(model_replay.ModelContinuationStore, 'get',
                        lambda self, **kwargs: {'historyHeaders': recorded})
    with caplog.at_level(logging.WARNING, logger=model_replay.logger.name):
        _restore(rows, _context(rows, status='history_changed'))
    assert 'first_changed=none' in caplog.text


def test_a_legacy_checkpoint_says_its_headers_are_unrecorded(monkeypatch, caplog):
    rows = _lane()
    monkeypatch.setattr(model_replay.ModelContinuationStore, 'get',
                        lambda self, **kwargs: {'messages': []})
    with caplog.at_level(logging.WARNING, logger=model_replay.logger.name):
        _restore(rows, _context(rows, status='history_changed'))
    assert 'first_changed=unrecorded' in caplog.text


def _publishing_task(headers):
    return {'_userId': 1, 'convId': 'conv', '_turnId': 'target', '_attemptId': 'attempt-x',
            'content': 'done', 'model': 'claude-opus-5.5',
            'messages': [{'role': 'user', 'content': 'go'},
                         {'role': 'assistant', 'content': 'done'}],
            'config': {'model': 'claude-opus-5.5', '_modelReplayBase': [],
                       '_modelReplayHistoryProof': {'version': 2, 'count': len(headers),
                                                    'digest': 'a' * 64},
                       '_modelReplayProofEligible': True,
                       '_modelReplayHistoryHeaders': headers}}


def test_publication_carries_the_headers_and_its_own_header():
    headers = [['t1', None, 'completed', 'attempt-t1', 1]]
    candidate = model_replay.prepare_completed_task(_publishing_task(headers))
    projection = candidate['projection']
    assert projection['historyHeaders'] == headers
    assert projection['publisherHeader'] == ['target', None, 'completed', 'attempt-x', None]


def test_the_header_record_never_costs_the_checkpoint(monkeypatch):
    from lib.storage import model_continuation
    real = model_continuation.encode_projection
    monkeypatch.setattr(model_continuation, 'encode_projection',
                        lambda projection: None if 'historyHeaders' in projection else real(projection))
    candidate = model_replay.prepare_completed_task(
        _publishing_task([['t1', None, 'completed', 'attempt-t1', 1]]))
    assert candidate is not None and 'historyHeaders' not in candidate['projection']
