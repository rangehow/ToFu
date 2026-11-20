"""Small local SQLite pre-send discriminator; synthetic sources, no live inputs."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json
import secrets
import threading
import time
from urllib.parse import quote, quote_plus

import orjson
import pytest

from tests.turn_lifecycle.test_turn_event_carried_task_event import (
    _running_task, storage, turn_service,
)
__all__ = ['storage', 'turn_service']

pytestmark = pytest.mark.unit


class Observation:
    def __init__(self):
        self._caller = threading.local()

    @property
    def row(self):
        return getattr(self._caller, 'row', None)

    @row.setter
    def row(self, value):
        self._caller.row = value

    @contextmanager
    def span(self, label):
        row = self.row
        if row is None or 'stop' in row:
            yield
            return
        start = time.monotonic_ns()
        cpu = time.thread_time_ns()
        try:
            yield
        finally:
            end_cpu = time.thread_time_ns()
            end = time.monotonic_ns()
            if 'stop' in row:
                end, end_cpu = row['stop'], row['stop_cpu']
            row['spans'].append([label, end - start, end_cpu - cpu])

    def wrap(self, monkeypatch, owner, name, label):
        original = getattr(owner, name)

        def measured(*args, **kwargs):
            with self.span(label):
                return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, measured)


class ObservedLock:
    def __init__(self, observer, label, lock):
        self.observer, self.label, self.lock = observer, label, lock
        self.owner = None

    def __enter__(self):
        with self.observer.span(self.label + '_acquire'):
            self.lock.acquire()
        self.owner = threading.get_ident()
        self.hold = self.observer.span(self.label + '_hold')
        self.hold.__enter__()
        return self

    def __exit__(self, *exc):
        assert self.owner == threading.get_ident()
        self.hold.__exit__(*exc)
        row = self.observer.row
        if row is not None:
            row['releases'].append([self.label, 'stop' not in row])
        self.owner = None
        self.lock.release()

    def acquire(self, *args, **kwargs):
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        return self.lock.release()


def _uncached_walk(redactor, value):
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, dict):
        changed = None
        for key, item in value.items():
            new = _uncached_walk(redactor, item)
            if new is not item:
                if changed is None:
                    changed = dict(value)
                changed[key] = new
        return value if changed is None else changed
    if isinstance(value, (list, tuple)):
        items = [_uncached_walk(redactor, item) for item in value]
        if all(new is old for new, old in zip(items, value)):
            return value
        return items if isinstance(value, list) else tuple(items)
    return value


@pytest.mark.parametrize('reuse_strings', [False, True], ids=['previous-walk', 'current'])
def test_small_presend_source_discriminator(tmp_path, monkeypatch, turn_service, storage, reuse_strings):
    from lib import credentials_vault as vault
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary, empty_document
    from lib.tasks_pkg.manager._events import _advance_phase_snapshot
    from cryptography.fernet import Fernet
    from lib.storage import client as sql
    from lib.tool_results import credential_redaction as cr

    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / 'vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    config = tmp_path / 'config.json'
    import lib.config_dir as config_dir
    monkeypatch.setattr(config_dir, 'CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('TOFU_SECRET_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr(cr, '_server_config_path', lambda: str(config))
    repository = ModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    if not reuse_strings:
        monkeypatch.setattr(cr.KnownCredentialRedactor, 'redact_value',
                            lambda self, value: _uncached_walk(self, value) if self.variants else value)
    monkeypatch.setattr(cr, '_cached', {})
    monkeypatch.setattr(cr, '_failing', set())
    observer = Observation()
    monkeypatch.setattr(cr, '_lock', ObservedLock(observer, 'rebuild_lock', threading.Lock()))
    values = {}

    def rotate(source):
        values[source] = secrets.token_urlsafe(24) + '/+= '
        if source == 'vault':
            vault.set_entry('synthetic', values[source].strip())
        elif source == 'config':
            replacement = config.with_suffix('.next')
            replacement.write_text(json.dumps({'providers': [
                {'id': 'synthetic', 'api_key': values[source].strip()}]}))
            replacement.replace(config)
        else:
            repository.put_secret(boundary, values[source].strip(), secret_reference='synthetic')

    for source in ('vault', 'config', 'model'):
        rotate(source)
    document = empty_document()
    document['providers'] = [{'provider_id': 'synthetic', 'name': 'synthetic', 'scope': 'owner'}]
    document['provider_accesses'] = [{'provider_access_id': 'access', 'provider_id': 'synthetic',
                                     'display_name': 'synthetic', 'enabled': True, 'quota_policy': {}}]
    document['credentials'] = [{'credential_id': 'synthetic', 'provider_access_id': 'access',
                                'kind': 'api_key', 'secret_reference': 'synthetic', 'key_hint': '***',
                                'enabled': True, 'authorization': {'connection_ids': [], 'models': []},
                                'quota_policy': {}}]
    repository.compare_and_swap(boundary, document, expected_revision=0)
    rows = []
    original_send = sql.send_frame
    target_requests = []

    def send(sock, request):
        row = observer.row
        if row is not None and 'stop' not in row:
            row['rpc_order'].append(request['operation'])
            if request['operation'] == 'turn.event.record':
                row['stop_cpu'] = time.thread_time_ns()
                row['stop'] = time.monotonic_ns()
                row['request_id'] = request['request_id']
                # Only disposable request objects; no payload/auth enters receipts.
                target_requests.append(request)
        return original_send(sock, request)

    monkeypatch.setattr(sql, 'send_frame', send)
    original_matches = cr.KnownCredentialRedactor._matches

    def matches(redactor, value):
        if observer.row is not None and value is text:
            observer.row['detail_scans'] += 1
        return original_matches(redactor, value)

    monkeypatch.setattr(cr.KnownCredentialRedactor, '_matches', matches)
    original_call = sql.StorageClient._call

    def call(client, kind, operation, *args, **kwargs):
        with observer.span('earlier_rpc' if kind == 'query' else 'command_to_send'):
            return original_call(client, kind, operation, *args, **kwargs)

    monkeypatch.setattr(sql.StorageClient, '_call', call)
    for owner, name, label in (
        (turn_service, '_frame_projection', 'projection'),
        (turn_service, '_frame_projection_patch', 'patch'),
        (turn_service._CommandEncoding, 'frozen', 'freeze'),
        (sql, 'redact_known_credentials', 'redaction'),
        (sql.StorageClient, '_request_frame', 'request'),
        (sql.SessionPool, 'acquire', 'pool'),
        (cr, 'known_credential_redactor', 'lookup'),
        (cr, '_source_identity', 'source_identity'),
        (cr, '_stat_identity', 'source_stat'),
        (cr, '_rebuild', 'rebuild'),
    ):
        observer.wrap(monkeypatch, owner, name, label)

    for name in ('_vault_values', '_server_config_values', '_model_routing_values'):
        original = getattr(cr, name)

        def source(*args, _original=original, _name=name, **kwargs):
            with observer.span(_name.removeprefix('_')):
                yield from _original(*args, **kwargs)

        monkeypatch.setattr(cr, name, source)

    # One cold task, then ordinary warm events with each intended invalidation.
    created, task = _running_task(turn_service, storage, 'sources-conv', 'sources-task')
    task['_turnProjectionStateLock'] = ObservedLock(observer, 'projection_lock', threading.RLock())
    cr._cached.clear()
    expected_rows = []
    text = None
    states = ('cold', 'hit', 'vault', 'config', 'model', 'hit')
    for seq, state in enumerate(states):
        if state in ('vault', 'config', 'model'):
            rotate(state)
        text = ' '.join(form(value.strip()) for value in values.values()
                        for form in (str, lambda s: quote(s, safe=''), quote_plus))
        task['content'] += 'ordinary synthetic line. ' * 16
        wire = {'type': 'phase', 'phase': 'streaming', 'seq': seq, 'detail': text}
        _advance_phase_snapshot(task, wire, observed_at=seq)
        row = {'event_id': seq, 'state': state, 'spans': [], 'rpc_order': [],
               'detail_scans': 0, 'releases': [], 'reuse_strings': reuse_strings}
        row['entry'] = time.monotonic_ns()
        row['entry_cpu'] = time.thread_time_ns()
        observer.row = row
        try:
            result = turn_service.record_task_event(task, wire, task_event={
                'task_id': task['id'], 'sequence': seq, 'event': wire})
        finally:
            observer.row = None
        assert result == 'carried'
        assert row['detail_scans'] == (1 if reuse_strings else 2), 'same immutable phase detail scan count'
        row['wall_ns'] = row.pop('stop') - row.pop('entry')
        row['cpu_ns'] = row.pop('stop_cpu') - row.pop('entry_cpu')
        assert row['request_id'] == target_requests[-1]['request_id']
        # Check actual admitted target frame, after measurement stopped.
        payload = orjson.loads(orjson.dumps(target_requests.pop()['payload']))
        serialized = json.dumps(payload)
        assert not any(form(value.strip()) in serialized for value in values.values()
                       for form in (str, lambda s: quote(s, safe=''), quote_plus))
        expected = ' '.join(f'[redacted credential: {label}]' for label in (
            'SYNTHETIC', 'provider synthetic', 'model-routing synthetic') for _ in range(3))
        assert payload['task_event']['event']['detail'] == expected
        expected_rows.append({**wire, 'detail': expected, 'v': 1})
        assert wire['detail'] == text
        rows.append(row)

    storage.stop()
    storage.start()
    turn = turn_service.get_turn('sources-conv', created['turn']['turnId'], user_id=1)
    assert turn['projection']['content'] == task['content']
    events = storage.client.query('event.list', {
        'task_id': task['id'], 'after_sequence': -1, 'limit': 7})
    assert [event['sequence'] for event in events] == list(range(6))
    assert [event['event'] for event in events] == expected_rows
    assert storage.client.maintenance('system.integrity_check')['ok']
    for row in rows:
        counts = Counter(span[0] for span in row['spans'])
        assert counts['source_identity'] == counts['lookup'] == counts['redaction'] == 1
        assert counts['source_stat'] == 3
        assert counts['rebuild'] == counts['rebuild_lock_acquire'] == int(row['state'] != 'hit')
        assert counts['projection_lock_acquire'] == counts['projection'] == counts['patch'] == counts['freeze'] == 1
        assert ['projection_lock', False] in row['releases']
        assert (['rebuild_lock', True] in row['releases']) is (row['state'] != 'hit')
        prefix = ['turn.attempt.get', 'turn.get'] if row['state'] == 'cold' else []
        if row['state'] != 'hit':
            prefix += ['model_routing.get', 'model_routing.secret.get']
        assert row['rpc_order'] == prefix + ['turn.event.record']
    # Fixed labels, synthetic event/request IDs and clocks only, never values/paths.
    print('PRESEND_SOURCES=' + json.dumps(rows, sort_keys=True))


def test_observation_excludes_concurrent_health_probe(storage, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from lib.storage import client as sql

    observer = Observation()
    row = {'spans': [], 'rpc_order': [], 'releases': []}
    observer.row = row
    entered = threading.Event()
    release = threading.Event()
    health_sends = []
    probe_threads = []
    original_send = sql.send_frame

    def send(sock, request):
        if request['operation'] == 'system.health' and threading.get_ident() in probe_threads:
            health_sends.append(1)
            entered.set()
            assert release.wait(10), 'test must release its own health probe'
        observed = observer.row
        if observed is not None:
            observed['rpc_order'].append(request['operation'])
            observed['stop_cpu'] = time.thread_time_ns()
            observed['stop'] = time.monotonic_ns()
        return original_send(sock, request)

    monkeypatch.setattr(sql, 'send_frame', send)
    for owner, name, label in (
        (sql.StorageClient, '_call', 'rpc'),
        (sql.StorageClient, '_request_frame', 'request'),
        (sql.SessionPool, 'acquire', 'pool'),
    ):
        observer.wrap(monkeypatch, owner, name, label)
    def probe():
        probe_threads.append(threading.get_ident())
        return storage.client.health()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(probe)
        try:
            assert entered.wait(10), 'health probe must reach real transport'
            with observer.span('event_caller'):
                assert observer.row is row
        finally:
            release.set()
        assert future.result(timeout=10)['ready'] is True
    assert health_sends == [1]
    assert [span[0] for span in row['spans']] == ['event_caller']
    assert row['rpc_order'] == []
    assert 'stop' not in row and 'stop_cpu' not in row
    assert observer.row is row
    observer.row = None
