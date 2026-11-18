"""Requirement admission reads owned metadata, never the growing Turn body."""
import copy
import json
import threading
import time
from concurrent.futures import Future

import pytest

from lib.acceptance import turns
from lib.llm.llm_errors import AbortedError
from lib.tasks_pkg.context_composer import turn_requirements as context
from tests.artifacts.test_turn_review import (
    subject as subject, storage as storage, turn_service as turn_service,
    PRINCIPAL, configure,
)
from tests.memory.test_working_facts_context import quiet_notes as quiet_notes

pytestmark = pytest.mark.unit


def test_current_requirements_do_not_materialize_turn_projection(subject, storage, monkeypatch, tmp_path):
    configure(subject)
    original = storage.client.query
    observations = []

    def measured(operation, payload=None, **kwargs):
        result = original(operation, payload, **kwargs)
        if operation == 'turn.get' and (payload or {}).get('include_projection') is False:
            observations.append(len(json.dumps(result).encode()))
            assert 'projection' not in result, 'Requirement reads must not load the growing Turn body'
        return result

    monkeypatch.setattr(storage.client, 'query', measured)
    measurements = []
    expected = None
    for size in [0, 256 * 1024, 2 * 1024 * 1024]:
        subject.task['content'] = 'x' * size
        subject.service.record_task_event(subject.task, {'type': 'delta', 'content': subject.task['content']})
        # Full default reads still retain the answer; only requirement acquisition
        # omits it. A wall-time measurement is diagnostic, not a timing assertion.
        full = subject.service.get_turn('turn-review', subject.turn_id, user_id=1)
        assert len(full['projection']['content']) == size
        before = len(observations)
        start = time.perf_counter()
        snapshot = turns.read_execution_requirements('turn-review', subject.turn_id,
            subject.task['_attemptId'], subject.task['id'], principal=PRINCIPAL)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert snapshot['objective'] == 'Deliver a checked report'
        expected = snapshot if expected is None else expected
        assert snapshot == expected
        assert len(observations) == before + 1
        measurements.append({'answer_bytes': size, 'elapsed_ms': elapsed_ms,
                             'turn_metadata_json_bytes': observations[-1]})
    assert observations and max(observations) < 4096
    assert max(observations) - min(observations) < 128
    (tmp_path / 'requirement-read-cost.json').write_text(json.dumps(measurements, indent=2))


def test_local_requirement_deadline_waits_as_storage_never_as_a_model_failure(subject, quiet_notes, monkeypatch):
    pending = Future()
    submitted = []
    class Executor:
        def submit(self, *args):
            submitted.append(args)
            return pending
    monkeypatch.setattr(context, 'context_provider_executor', Executor())
    monkeypatch.setattr(context, '_CONTEXT_PROVIDER_DEADLINE_SECONDS', 0)
    subject.task['abort_event'] = threading.Event()
    messages = [{'role': 'user', 'content': 'Continue'}]
    before = copy.deepcopy(messages)
    outcome = {}
    def admit():
        try:
            outcome['value'] = context.refresh_turn_requirements(messages, task=subject.task, model='test-model')
        except BaseException as error:  # the spec inspects the exact refusal
            outcome['error'] = error
    worker = threading.Thread(target=admit, daemon=True)
    worker.start()
    stop = time.monotonic() + 10
    while (subject.task.get('phase') or {}).get('attempt', 0) < 3:
        assert time.monotonic() < stop and worker.is_alive()
        time.sleep(.01)
    # Repeated local deadlines are storage waiting: no task error, no model
    # attribution, no stale prompt and no duplicate read.
    waiting = subject.task['phase']
    assert waiting['detail'] == 'Waiting for storage' and 'model' not in waiting
    assert not outcome and subject.task.get('error') is None and len(submitted) == 1
    subject.task['abort_event'].set()
    worker.join(5)
    assert not worker.is_alive() and isinstance(outcome.get('error'), AbortedError)
    assert subject.task.get('error') is None
    assert pending.cancelled() and messages == before


def test_round_reads_answer_steer_head_and_requirements_from_one_exchange(subject, storage, monkeypatch):
    """The round's boundary reads share one pipelined exchange; each answer
    serves the reads it was issued for, and any later read is live."""
    from lib.storage.port import PrefetchedReads
    from lib.turn_lifecycle import get_turn, turn_read_request
    configure(subject)
    attempt = subject.task['_attemptId']
    live = turns.read_execution_requirements('turn-review', subject.turn_id, attempt,
                                             subject.task['id'], principal=PRINCIPAL)
    lone, exchanges = [], []
    query, query_many = storage.client.query, storage.client.query_many

    def counted_query(operation, payload=None, **kwargs):
        lone.append(operation)
        return query(operation, payload, **kwargs)

    def counted_many(requests, **kwargs):
        exchanges.append([operation for operation, _payload in requests])
        return query_many(requests, **kwargs)

    monkeypatch.setattr(storage.client, 'query', counted_query)
    monkeypatch.setattr(storage.client, 'query_many', counted_many)

    reads = PrefetchedReads(storage.client, [
        turn_read_request('turn-review', subject.turn_id, user_id=1, include_projection=False),
        *turns.execution_requirements_read_requests('turn-review', subject.turn_id, attempt,
                                                    principal=PRINCIPAL)])
    assert exchanges == []
    head = get_turn('turn-review', subject.turn_id, user_id=1, include_projection=False, reads=reads)
    # The first reader sent one exchange; the Turn head both owners read is fetched once.
    assert exchanges == [['turn.get', 'turn.attempt.get', 'record.get']]
    snapshot = turns.read_execution_requirements('turn-review', subject.turn_id, attempt,
                                                 subject.task['id'], principal=PRINCIPAL, reads=reads)
    assert head['currentAttemptId'] == attempt
    assert snapshot == live
    assert lone == []
    again = turns.read_execution_requirements('turn-review', subject.turn_id, attempt,
                                              subject.task['id'], principal=PRINCIPAL, reads=reads)
    assert again == live
    assert sorted(lone) == ['record.get', 'turn.attempt.get', 'turn.get']
