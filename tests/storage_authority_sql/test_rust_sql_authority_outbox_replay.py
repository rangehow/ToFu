"""Cross-language encrypted outbox and passive SQL replay acceptance.

The Python decoder, authenticated shadow reader and replay target consume
records published by the real Rust process; framing-only success cannot
certify encryption or mutation capture. Each authority and replay target
owns an independent scratch data directory.
"""
from __future__ import annotations

from dataclasses import replace
import json
import time

from cryptography.fernet import Fernet
import pytest

from lib.secret_envelope import reset_secret_envelope_for_test
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
from lib.storage_authority_sql.logical_outbox import decode_logical_payload
from lib.storage_authority_sql.logical_replay import BackendReplayTarget, replay_records
from lib.storage_authority_sql.logical_shadow import LogicalCommitShadow
from tests.storage.test_storage_logical_outbox import _config
from tests.storage_authority_sql.test_rust_sql_authority_differential import (
    Authority, PYTHON_AUTHORITY_COMMAND, rust_authority_binary,
)
from tests.support.sql_authority_identity import (
    assert_child_identity, assert_process_generation, bind_candidate_identity, child_identity,
)

pytestmark = pytest.mark.unit


def _observe_child(authority, implementation, record_property, phase):
    identity = child_identity(authority.supervisor, authority.command)
    if implementation == 'rust':
        bind_candidate_identity(identity)
    record_property('sql_authority_child_' + phase, json.dumps(identity, sort_keys=True))
    assert_child_identity(identity)
    return identity


@pytest.mark.parametrize('implementation', ['python', 'rust'])
def test_published_record_decrypts_and_replays_on_the_python_authority(
        tmp_path, monkeypatch, implementation, record_property):
    reset_secret_envelope_for_test()
    monkeypatch.setenv('TOFU_SECRET_ENCRYPTION_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SHADOW', 'required')
    sink = tmp_path / 'logical-commits'
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SHADOW_DIR', str(sink))
    command = (str(rust_authority_binary()), 'serve') if implementation == 'rust' else PYTHON_AUTHORITY_COMMAND
    authority = Authority(tmp_path / 'source', command)
    try:
        identity = _observe_child(authority, implementation, record_property, 'started')
        payload = {'namespace': 'replay-proof', 'key': 'value', 'value': {'text': 'é', 'list': [1, True, None]}}
        first = authority.supervisor.client.command('record.put', payload, 'outbox-replay-proof')
        assert authority.supervisor.client.command('record.put', payload, 'outbox-replay-proof') == first
        deadline = time.monotonic() + 10
        while True:
            status = authority.supervisor.client.metrics()['logical_outbox']
            if status['published_sequence'] == 1 and status['pending_records'] == 0:
                break
            assert time.monotonic() < deadline, status
            time.sleep(0.02)
        stream_id = status['stream_id']
        executed = _observe_child(authority, implementation, record_property, 'published')
        assert_process_generation(implementation, identity, executed)
    finally:
        authority.stop()
    try:
        with LogicalCommitShadow(sink, stream_id=stream_id) as reader:
            records = reader.read_records()
        assert len(records) == 1
        clear = decode_logical_payload(records[0])
        assert clear['request'] == payload
        assert clear['response'] == first
        assert len(clear['mutations']) == 1
        assert clear['mutations'][0]['rowcount'] == 1
        assert clear['mutations'][0]['sql'].startswith('INSERT INTO storage_records')
        target = SQLiteBackend(replace(_config(tmp_path / 'target'), logical_shadow_mode='off'))
        target.start()
        try:
            replay = BackendReplayTarget(target, target_name='cross-language-proof', stream_id=stream_id)
            result = replay_records(records, replay)
            assert result.applied_records == 1
            assert replay.checkpoint().last_sequence == 1
            row = target.query('verify.replay', lambda session: session.fetch_one(
                'SELECT value_json, version FROM storage_records WHERE namespace=? AND record_key=?',
                ('replay-proof', 'value')), time.monotonic() + 5)
            assert json.loads(row['value_json']) == payload['value']
            assert row['version'] == 1
        finally:
            target.close()
    finally:
        reset_secret_envelope_for_test()
