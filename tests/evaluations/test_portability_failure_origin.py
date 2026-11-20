"""Offline tests for classification by failure origin, not error wording."""
import errno
import http.client
import io
import json
import urllib.error

import pytest

from evaluations.portability import models, report, run
from evaluations.portability.scenario import Scenario

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('origin', 'status'), [
    ('arm', 401), ('arm', 503), ('transport', 400), ('transport', 401), ('transport', 503),
])
def test_http_failure_classification_depends_on_origin(monkeypatch, tmp_path, origin, status):
    calls = []

    def request(path, body):
        calls.append(path)
        raise urllib.error.HTTPError('http://fixture.invalid', status, 'offline', {}, io.BytesIO(b'unavailable'))

    def arm(task, subject, scratch):
        if origin == 'arm':
            raise OSError(errno.ENOSPC, f'cannot write captured HTTP {status} response')
        return subject.ask('offline fixture')

    monkeypatch.setattr(models, '_request', request)
    waits = []
    monkeypatch.setattr(models.time, 'sleep', waits.append)
    monkeypatch.setattr(run, 'actual_environment', lambda: {'host': 'offline'})
    scenario = Scenario('origin', '1', 'score', ({'id': 'case'},), arm, arm, lambda *_: 1)
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    record = json.loads(path.read_text())
    info = report.summarise(report.load([tmp_path]))['origin']
    assert f'HTTP {status}' in record['error']
    if origin == 'arm' or status != 503:
        assert waits == []
    assert 'artifact_path' not in record
    if origin == 'arm':
        assert calls == []
        assert record['error_kind'] == 'agent' and record['score'] == 0.0
        assert info['infrastructure_failures'] == 0
        assert info['models'][0]['baseline'] == 0.0
        assert info['models'][0]['errors']['baseline'] == 1
    else:
        assert len(calls) == (models.TRANSIENT_ATTEMPTS if status == 503 else 1)
        assert record['error_kind'] == 'infrastructure' and record['score'] is None
        assert info['infrastructure_failures'] == 1
        assert info['models'][0]['baseline'] is None
        assert info['models'][0]['n_baseline'] == 0


@pytest.mark.parametrize('body', [b'{', b'\xff'])
def test_arm_local_decode_failure_remains_an_agent_zero(monkeypatch, tmp_path, body):
    def arm(*_):
        return json.loads(body.decode('utf-8'))

    monkeypatch.setattr(run, 'actual_environment', lambda: {'host': 'offline'})
    scenario = Scenario('local-decode', '1', 'score', ({'id': 'case'},), arm, arm, lambda *_: 1)
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    record = json.loads(path.read_text())
    info = report.summarise(report.load([tmp_path]))['local-decode']
    assert record['error_kind'] == 'agent' and record['score'] == 0.0
    assert info['infrastructure_failures'] == 0
    assert info['models'][0]['n_baseline'] == 1


@pytest.fixture
def offline_wire(monkeypatch, tmp_path):
    responses, requests, waits, scored = [], [], [], []

    def urlopen(request, *, timeout):
        requests.append(request.selector)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response) if isinstance(response, bytes) else response

    def arm(task, subject, scratch):
        return subject.ask('offline fixture')

    def score(task, artifact, judge):
        scored.append(artifact)
        return 0.0

    monkeypatch.setattr(models, 'ENDPOINT', 'http://fixture.invalid')
    monkeypatch.setattr(models.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(models.time, 'sleep', waits.append)
    monkeypatch.setattr(run, 'actual_environment', lambda: {'host': 'offline'})
    scenario = Scenario('wire', '1', 'score', ({'id': 'case'},), arm, arm, score)

    def execute(*wire_responses):
        responses.extend(wire_responses)
        path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
        assert not responses
        return json.loads(path.read_text()), report.summarise(report.load([tmp_path]))['wire']

    return execute, requests, waits, scored


@pytest.mark.parametrize('wire', [
    b'{', b'\xff', b'[]',
    *(b'{"model":"subject","choices":[{"finish_reason":"stop","message":{"content":""}}],'
      b'"unused":{"nested":[' + constant + b']}}' for constant in (b'NaN', b'Infinity', b'-Infinity')),
    b'{"model":"subject","choices":[null]}',
    b'{"model":"subject","choices":{"0":{}}}',
    b'{"model":"subject","choices":[{"finish_reason":"stop"}]}',
    b'{"model":"subject","choices":[{"finish_reason":"stop","message":[] }]}',
    b'{"model":"subject","choices":[{"finish_reason":"stop","message":{"content":false}}]}',
    b'{"model":"subject","choices":[{"finish_reason":"stop","message":{"content":""}}],"usage":[]}',
    b'{"model":"subject","choices":[{"finish_reason":"stop","message":{"content":""}}],'
    b'"usage":{"prompt_tokens":10,"completion_tokens":"invalid"}}',
])
def test_malformed_wire_completion_is_unscored(offline_wire, wire):
    execute, requests, waits, scored = offline_wire
    record, info = execute(wire)
    assert requests == ['/v1/chat/completions']
    assert waits == scored == []
    assert record['error_kind'] == 'infrastructure' and record['score'] is None
    assert record['usage'] == {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
    assert 'artifact_path' not in record
    assert info['infrastructure_failures'] == 1
    assert info['models'][0]['baseline'] is None
    assert info['models'][0]['n_baseline'] == 0


class TruncatedResponse(io.BytesIO):
    def read(self, *args):
        raise http.client.IncompleteRead(b'synthetic partial body', 1)


@pytest.mark.parametrize('status', [None, 401, 503])
def test_truncated_wire_response_is_unscored(offline_wire, status):
    execute, requests, waits, scored = offline_wire
    attempts = models.TRANSIENT_ATTEMPTS if status == 503 else 1
    responses = [TruncatedResponse() for _ in range(attempts)]
    if status is not None:
        responses = [urllib.error.HTTPError('http://fixture.invalid', status, 'offline', {}, body)
                     for body in responses]
    record, info = execute(*responses)
    assert requests == ['/v1/chat/completions'] * attempts
    assert bool(waits) == (status == 503)
    assert scored == []
    assert record['error_kind'] == 'infrastructure' and record['score'] is None
    assert record['usage'] == {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
    assert 'artifact_path' not in record
    assert info['infrastructure_failures'] == 1
    assert info['models'][0]['n_baseline'] == 0


@pytest.mark.parametrize('failure', [ConnectionResetError, TimeoutError])
@pytest.mark.parametrize('status', [200, 401, 503])
def test_response_read_transport_failure_preserves_policy(offline_wire, status, failure):
    execute, requests, waits, scored = offline_wire
    attempts = 1 if status == 401 else models.TRANSIENT_ATTEMPTS

    class BrokenBody(io.BytesIO):
        def read(self, *args):
            raise failure('synthetic response read failure')

    responses = [BrokenBody() for _ in range(attempts)]
    if status != 200:
        responses = [urllib.error.HTTPError('http://fixture.invalid', status, 'offline', {}, body)
                     for body in responses]
    record, info = execute(*responses)

    assert record['error_kind'] == 'infrastructure' and record['score'] is None
    assert requests == ['/v1/chat/completions'] * attempts
    assert bool(waits) == (status != 401)
    assert scored == []
    assert record['usage'] == {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
    assert 'artifact_path' not in record
    assert info['infrastructure_failures'] == 1
    assert info['models'][0]['n_baseline'] == 0
    if status != 200:
        assert f'HTTP {status}' in record['error']


@pytest.mark.parametrize('transient', [None, 429, 503, 'connection', 'error-envelope'])
def test_valid_wire_completion_preserves_measured_zero(offline_wire, transient):
    execute, requests, waits, scored = offline_wire
    if isinstance(transient, int):
        initial = [urllib.error.HTTPError('http://fixture.invalid', transient, 'offline', {}, io.BytesIO(b''))]
    elif transient == 'connection':
        initial = [urllib.error.URLError('offline')]
    elif transient == 'error-envelope':
        initial = [b'{"error":{"message":"busy"}}']
    else:
        initial = []
    record, info = execute(*initial, b'{"model":"subject","choices":[{"finish_reason":"stop",'
                           b'"message":{"content":""}}],"usage":{"prompt_tokens":3,"completion_tokens":0}}')
    assert requests == ['/v1/chat/completions'] * (len(initial) + 1)
    assert waits == ([1] if initial else [])
    assert scored == ['']
    assert record['score'] == 0.0 and record['error_kind'] is None
    assert record['usage'] == {'calls': 1, 'prompt_tokens': 3, 'completion_tokens': 0}
    assert record['artifact_path']
    assert info['infrastructure_failures'] == 0
    assert info['models'][0]['baseline'] == 0.0
    assert info['models'][0]['n_baseline'] == 1
