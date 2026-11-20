"""Sensitive completions retain attribution, never payloads or replay authority."""
import hashlib
import json
import os

import pytest

from lib.desktop_agent._journal import CallJournal


pytestmark = pytest.mark.unit

TYPES = ('codex_login_read', 'egress_http', 'egress_http_stream')
ERROR = ('device_unavailable: sensitive result was not retained; this call completed and was '
         'not run again. Use a new call id for a fresh authorized request.')


def record_path(directory, call_id='call'):
    return directory / (hashlib.sha256(call_id.encode()).hexdigest() + '.json')


@pytest.mark.parametrize('kind', TYPES)
@pytest.mark.parametrize('failed', (False, True))
def test_sensitive_completion_and_retries(tmp_path, kind, failed):
    directory = tmp_path / 'calls'
    journal = CallJournal(str(directory))
    command = {'id': 'call', 'type': kind}
    assert journal.begin(command) is None
    journal.complete(kind, {}, {'id': 'call', 'result': None if failed else 'secret-body',
                                'error': 'secret-error' if failed else None})
    record = json.loads(record_path(directory).read_text())
    assert set(record) == {'id', 'type', 'state', 'at'}
    assert record['state'] == 'sensitive_done'
    expected = {'id': 'call', 'result': None, 'error': ERROR}
    assert journal.begin(command) == expected
    assert CallJournal(str(directory)).begin(command) == expected
    # Changing the incoming command to a read must not bypass stored policy.
    assert journal.begin({'id': 'call', 'type': 'screenshot'}) == expected


@pytest.mark.parametrize('kind', TYPES)
def test_legacy_migration_preserves_retention_and_other_files(tmp_path, kind):
    directory = tmp_path / 'calls'
    directory.mkdir(mode=0o777)
    path = record_path(directory)
    path.write_text(json.dumps({'id': 'call', 'type': kind, 'state': 'done', 'at': 10,
                                'result': 'secret-body', 'error': 'secret-error'}))
    os.chmod(path, 0o666)
    stamp = 1_000_000_000_123_456_789
    os.utime(path, ns=(stamp, stamp))
    transcript = directory / 'transcript.json'
    transcript.write_text('secret-body')
    journal = CallJournal(str(directory), clock=lambda: stamp / 1e9 + 1)
    assert path.stat().st_mtime_ns == stamp
    assert set(json.loads(path.read_text())) == {'id', 'type', 'state', 'at'}
    assert 'secret' not in path.read_text()
    assert transcript.read_text() == 'secret-body'
    if os.name == 'posix':
        assert directory.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
    assert journal.begin({'id': 'call', 'type': kind})['error'] == ERROR
    CallJournal(str(directory), clock=lambda: stamp / 1e9 + journal.retention + 1)
    assert not path.exists()


@pytest.mark.parametrize('record', [None, [], 7, {}, {'id': 'wrong', 'state': 'done'},
                                  {'id': 'call', 'state': 'bogus'}, 'malformed'])
def test_invalid_record_never_authorizes_execution(tmp_path, record):
    journal = CallJournal(str(tmp_path))
    path = record_path(tmp_path)
    path.write_text('{' if record == 'malformed' else json.dumps(record))
    for kind in ('egress_http', 'screenshot'):
        reply = journal.begin({'id': 'call', 'type': kind})
        assert reply is not None
        assert reply['error'].startswith('device_unavailable:')


def test_incoming_sensitive_type_does_not_replay_ordinary_result(tmp_path):
    journal = CallJournal(str(tmp_path))
    record_path(tmp_path).write_text(json.dumps(
        {'id': 'call', 'type': 'exec', 'state': 'done', 'at': 10, 'result': 'secret'}))
    assert journal.begin({'id': 'call', 'type': 'egress_http'})['error'] == ERROR


def test_completion_write_failure_retains_started_refusal(tmp_path, monkeypatch):
    journal = CallJournal(str(tmp_path))
    command = {'id': 'call', 'type': 'egress_http'}
    assert journal.begin(command) is None

    def fail(*args, **kwargs):
        raise OSError('synthetic write failure')

    monkeypatch.setattr(os, 'replace', fail)
    journal.complete('egress_http', {}, {'id': 'call', 'result': 'secret', 'error': 'secret'})
    assert json.loads(record_path(tmp_path).read_text())['state'] == 'started'
    assert journal.begin(command)['error'].startswith('device_unavailable:')
    assert len(list(tmp_path.iterdir())) == 1


def test_begin_write_failure_does_not_authorize_execution(tmp_path, monkeypatch):
    journal = CallJournal(str(tmp_path))

    def fail(*args, **kwargs):
        raise OSError('synthetic write failure')

    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        journal.begin({'id': 'call', 'type': 'codex_login_read'})
    assert not list(tmp_path.iterdir())


def test_migration_write_failure_fails_opening_without_destroying_record(tmp_path, monkeypatch):
    path = record_path(tmp_path)
    original = json.dumps({'id': 'call', 'type': 'egress_http', 'state': 'done',
                           'at': 10, 'result': 'secret'})
    path.write_text(original)
    stamp = path.stat().st_mtime_ns

    def fail(*args, **kwargs):
        raise OSError('synthetic write failure')

    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        CallJournal(str(tmp_path))
    assert path.read_text() == original
    assert path.stat().st_mtime_ns == stamp
    assert len(list(tmp_path.iterdir())) == 1


def test_migration_redacts_wrong_id_but_keeps_refusal(tmp_path):
    path = record_path(tmp_path)
    path.write_text(json.dumps({'id': 'wrong', 'type': 'egress_http', 'state': 'done',
                                'at': 10, 'result': 'secret', 'error': 'secret'}))
    journal = CallJournal(str(tmp_path))
    assert 'secret' not in path.read_text()
    reply = journal.begin({'id': 'call', 'type': 'egress_http'})
    assert reply is not None
    assert reply['error'].startswith('device_unavailable:')


def test_completion_cannot_downgrade_stored_sensitive_type(tmp_path):
    from lib.desktop_agent._journal import CallJournal
    import json
    journal = CallJournal(str(tmp_path / 'calls'))
    assert journal.begin({'id': 'same', 'type': 'codex_login_read', 'params': {}}) is None
    journal.complete('desktop_read_file', {}, {'id': 'same', 'result': 'synthetic-secret', 'error': 'synthetic-secret'})
    record = json.loads(next((tmp_path / 'calls').glob('*.json')).read_text())
    assert record['type'] == 'codex_login_read'
    assert set(record) == {'id', 'type', 'state', 'at'}
    assert 'not run again' in journal.begin({'id': 'same', 'type': 'desktop_read_file', 'params': {}})['error']
