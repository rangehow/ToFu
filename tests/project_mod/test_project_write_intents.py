"""Crash boundary evidence is bounded and never authorizes overwriting user edits."""
import base64
import json
import os
from pathlib import Path
import stat
import uuid

import pytest

from lib.project_mod import write_intents as intents

pytestmark = pytest.mark.unit


def stage(tmp_path, original=b'old\r\n\xff', updated=b'new\n'):
    base = tmp_path / 'project'
    base.mkdir()
    target = base / 'file.txt'
    if original is not None:
        target.write_bytes(original)
    staged = base / ('.tofu_atomic_' + uuid.uuid4().hex)
    with staged.open('wb') as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    return base, target, staged, dict(base_path=str(base), path='file.txt',
        original=original, post_content=updated, stage_name=staged.name,
        conv_id='conversation', task_id='task')


@pytest.mark.parametrize('original', [None, b'', b'old\r\n\xff'])
def test_durable_intent_survives_reopen_and_classifies_rename(tmp_path, original):
    base, target, staged, request = stage(tmp_path, original)
    session = str(tmp_path / 'session')
    with intents.intent_store(session) as store:
        record = store.prepare(**request)
        assert intents.classify(record) == 'unpublished'
    with intents.intent_store(session) as store:
        assert store.read() == [record]
        assert (None if record['original'] is None else base64.b64decode(record['original'])) == original
        os.replace(staged, target)
        assert intents.classify(record) == 'applied'
        assert target.read_bytes() == b'new\n'
        store.acknowledge(record)
        assert store.read() == []


@pytest.mark.parametrize('change', ['same-content-new-inode', 'later-edit', 'symlink', 'root-swap', 'missing-stage'])
def test_uncertain_evidence_is_preserved_without_mutation(tmp_path, change):
    base, target, staged, request = stage(tmp_path)
    with intents.intent_store(str(tmp_path / 'session')) as store:
        record = store.prepare(**request)
        if change == 'missing-stage':
            staged.unlink()
        else:
            os.replace(staged, target)
            if change == 'same-content-new-inode':
                other = base / 'other'
                other.write_bytes(b'new\n')
                os.replace(other, target)
            elif change == 'later-edit':
                target.write_bytes(b'user edit')
            elif change == 'symlink':
                outside = tmp_path / 'outside'
                outside.write_bytes(b'new\n')
                target.unlink()
                target.symlink_to(outside)
            else:
                base.rename(tmp_path / 'moved')
                base.mkdir()
                target.write_bytes(b'new\n')
        before = target.read_bytes()
        assert intents.classify(record) == 'conflict'
        assert store.read() == [record]
        assert target.read_bytes() == before


def test_refuses_changed_original_before_intent_is_published(tmp_path):
    base, target, staged, request = stage(tmp_path)
    target.write_bytes(b'user change')
    with intents.intent_store(str(tmp_path / 'session')) as store:
        with pytest.raises(intents.IntentError, match='Original file changed'):
            store.prepare(**request)
        assert store.read() == []
    assert target.read_bytes() == b'user change'


@pytest.mark.parametrize('source', ['descriptor', 'path'])
def test_late_ctime_of_an_unchanged_inode_is_not_a_change(tmp_path, monkeypatch, source):
    """DolphinFS reports a renamed file's ctime late; content and inode decide."""
    base, target, staged, request = stage(tmp_path)
    real_fstat, real_stat, calls = os.fstat, os.stat, {'fstat': 0}

    def late(metadata):
        values = list(metadata)
        values[stat.ST_CTIME] += 1
        return os.stat_result(values, {'st_ctime_ns': metadata.st_ctime_ns + 10**9,
            'st_mtime_ns': metadata.st_mtime_ns, 'st_atime_ns': metadata.st_atime_ns})

    def fstat(fd):
        calls['fstat'] += 1
        result = real_fstat(fd)
        return late(result) if source == 'descriptor' and calls['fstat'] % 2 == 0 else result

    def path_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        return late(result) if source == 'path' and kwargs.get('dir_fd') is not None else result

    monkeypatch.setattr(intents.os, 'fstat', fstat)
    monkeypatch.setattr(intents.os, 'stat', path_stat)
    with intents.intent_store(str(tmp_path / 'session')) as store:
        record = store.prepare(**request)
    assert intents.classify(record) == 'unpublished'


@pytest.mark.parametrize('field', ['st_size', 'st_mtime_ns', 'st_ino'])
def test_content_or_identity_change_during_read_still_refuses(tmp_path, monkeypatch, field):
    base, target, staged, request = stage(tmp_path)
    real_stat = os.stat

    def path_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if kwargs.get('dir_fd') is None:
            return result
        values = list(result)
        extra = {'st_ctime_ns': result.st_ctime_ns, 'st_mtime_ns': result.st_mtime_ns,
                 'st_atime_ns': result.st_atime_ns}
        if field == 'st_mtime_ns':
            extra['st_mtime_ns'] += 10**9
        else:
            values[{'st_size': stat.ST_SIZE, 'st_ino': stat.ST_INO}[field]] += 1
        return os.stat_result(values, extra)

    monkeypatch.setattr(intents.os, 'stat', path_stat)
    with intents.intent_store(str(tmp_path / 'session')) as store:
        with pytest.raises(intents.IntentError):
            store.prepare(**request)
        assert store.read() == []


def test_prepare_requires_durable_record_before_caller_can_publish(tmp_path, monkeypatch):
    from lib.project_mod import modifications
    base, target, staged, request = stage(tmp_path)
    with intents.intent_store(str(tmp_path / 'session')) as store:
        def fail(*args):
            raise OSError('injected journal flush failure')
        monkeypatch.setattr(modifications, '_atomic_json_write', fail)
        with pytest.raises(OSError, match='journal flush'):
            store.prepare(**request)
        assert target.read_bytes() == request['original']
        assert staged.exists()
        assert store.read() == []


@pytest.mark.parametrize('field', ['original', 'post_content'])
def test_complete_byte_evidence_is_persisted_and_classified(tmp_path, field):
    base,target,staged,request=stage(tmp_path)
    request[field]=b'x'*(8*1024*1024)
    if field=='original':target.write_bytes(request[field])
    else:staged.write_bytes(request[field])
    with intents.intent_store(str(tmp_path/'session')) as store:
        record=store.prepare(**request)
        assert store.read()==[record]
        assert intents.classify(record)=='unpublished'


def test_complete_pending_set_survives_both_retired_count_limits(tmp_path):
    base=tmp_path/'project';base.mkdir()
    session=str(tmp_path/'session')
    with intents.intent_store(session) as store:
        for number in range(180):
            (Path(store.directory)/f'aux-{number}').write_text('owned auxiliary data')
        records=[]
        for number in range(137):
            original=b'old';updated=f'new-{number}'.encode()
            target=base/f'file-{number}.txt';target.write_bytes(original)
            staged=base/('.tofu_atomic_'+uuid.uuid4().hex)
            with staged.open('wb') as output:
                output.write(updated);output.flush();os.fsync(output.fileno())
            records.append(store.prepare(base_path=str(base),path=target.name,
                original=original,post_content=updated,stage_name=staged.name))
        assert {row['changeId'] for row in store.read()}=={row['changeId'] for row in records}
    for number,record in enumerate(records):
        if number%2==0:os.replace(base/record['stageName'],base/record['path'])
    (base/records[-1]['path']).write_bytes(b'later user edit')
    with intents.intent_store(session) as store:
        reopened=store.read()
        assert len(reopened)==137
        for record in reopened:
            number=int(record['path'].removeprefix('file-').removesuffix('.txt'))
            expected='conflict' if number==136 else 'applied' if number%2==0 else 'unpublished'
            assert intents.classify(record)==expected


@pytest.mark.parametrize('malformed', ['invalid-json', 'symlink', 'invalid-path', 'identity-mismatch'])
def test_corrupt_pending_evidence_fails_closed(tmp_path, malformed):
    base, target, staged, request = stage(tmp_path)
    with intents.intent_store(str(tmp_path / 'session')) as store:
        record = store.prepare(**request)
        path = Path(store.directory) / (record['changeId'] + '.json')
        if malformed == 'invalid-json':
            path.write_bytes(b'x' * (8 * 1024 * 1024))
        elif malformed == 'symlink':
            outside = tmp_path / 'outside.json'
            path.rename(outside)
            path.symlink_to(outside)
        else:
            changed = dict(record)
            changed['path' if malformed == 'invalid-path' else 'changeId'] = ('../outside' if malformed == 'invalid-path' else 'f' * 32)
            path.write_text(json.dumps(changed))
        with pytest.raises(OSError):
            store.read()
        assert path.exists()
        assert target.read_bytes() == request['original']


def test_file_lock_contention_is_bounded_per_file_and_releases_after_failure(tmp_path, monkeypatch):
    from lib.project_mod import journal_locks
    session = str(tmp_path / 'session')
    os.makedirs(session)
    real_limit = intents._limit
    monkeypatch.setattr(intents, '_limit', lambda key: 0 if key == 'lockWaitSeconds' else real_limit(key))
    import concurrent.futures

    def competing_thread():
        with pytest.raises(journal_locks.JournalLockBusy, match='file file.txt is busy'):
            with journal_locks.path_locks(session, ['file.txt']):
                pytest.fail('other thread must not bypass the file lock')
        with journal_locks.path_locks(session, ['other.txt']):
            pass  # A different file never contends.
        with journal_locks.try_path_lock(session, 'file.txt') as free:
            assert free is False

    with pytest.raises(RuntimeError, match='body failed'):
        with journal_locks.path_locks(session, ['file.txt']):
            # Same-thread journal publication reenters the same file lock.
            with journal_locks.path_locks(session, ['file.txt']):
                with intents.intent_store(session) as nested:
                    assert nested.read() == []
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(competing_thread).result(timeout=5)
            raise RuntimeError('body failed')
    with journal_locks.try_path_lock(session, 'file.txt') as free:
        assert free is True
    assert not journal_locks._active_descriptors


def test_directory_scan_does_not_apply_an_auxiliary_entry_quota(tmp_path):
    with intents.intent_store(str(tmp_path/'session')) as store:
        for number in range(160):
            (Path(store.directory)/f'aux-{number}').write_bytes(b'auxiliary')
        assert store.read()==[]


@pytest.mark.parametrize('publish', [False, True])
def test_process_exit_releases_lock_and_preserves_recovery_evidence(tmp_path, publish):
    import subprocess
    import sys
    base, target, staged, request = stage(tmp_path)
    session = str(tmp_path / 'session')
    program = '''
import os, sys
from lib.project_mod import journal_locks
from lib.project_mod.write_intents import intent_store
with journal_locks.path_locks(sys.argv[1], ['file.txt']), intent_store(sys.argv[1]) as store:
    store.prepare(base_path=sys.argv[2], path='file.txt', original=b'old\\r\\n\\xff',
                  post_content=b'new\\n', stage_name=sys.argv[3], conv_id='c', task_id='t')
    if sys.argv[4] == 'publish':
        os.replace(os.path.join(sys.argv[2], sys.argv[3]), os.path.join(sys.argv[2], 'file.txt'))
    os._exit(23)
'''
    result = subprocess.run([sys.executable, '-c', program, session, str(base), staged.name,
                             'publish' if publish else 'prepare'], capture_output=True, timeout=90)
    assert result.returncode == 23, result.stderr.decode(errors='replace')
    from lib.project_mod import journal_locks
    with journal_locks.try_path_lock(session, 'file.txt') as free, \
            intents.intent_store(session) as store:
        assert free is True
        records = store.read()
        assert len(records) == 1
        assert base64.b64decode(records[0]['original']) == request['original']
        assert intents.classify(records[0]) == ('applied' if publish else 'unpublished')
    assert target.read_bytes() == (b'new\n' if publish else request['original'])


def test_aborted_cleanup_can_resume_after_stage_removed_before_ack(tmp_path, monkeypatch):
    base, target, staged, request = stage(tmp_path)
    session = str(tmp_path / 'session')
    with intents.intent_store(session) as store:
        record = store.abort(store.prepare(**request))
        def interrupted_ack(*args):
            raise OSError('injected exit after stage cleanup')
        monkeypatch.setattr(store, 'acknowledge', interrupted_ack)
        with pytest.raises(OSError, match='after stage cleanup'):
            store.discard_aborted(record)
        assert not staged.exists()
    with intents.intent_store(session) as store:
        assert store.read() == [record]
        store.discard_aborted(record)
        assert store.read() == []
    assert target.read_bytes() == request['original']


def test_auxiliary_entry_does_not_refuse_new_intent(tmp_path):
    base,target,staged,request=stage(tmp_path)
    with intents.intent_store(str(tmp_path/'session')) as store:
        (Path(store.directory)/'abandoned.tmp').write_bytes(b'')
        assert store.read()==[]
        record=store.prepare(**request)
        assert store.read()==[record]
        assert len(list(Path(store.directory).iterdir()))==2
    assert target.read_bytes()==request['original']


@pytest.mark.parametrize('owner_action', ['acknowledge', 'abort', 'abort-then-acknowledge', 'in-place-corruption'])
def test_listing_survives_other_owner_settling_its_intent_mid_read(tmp_path, monkeypatch, owner_action):
    """Another path's writer may unlink or replace its own intent while a reader holds it open.

    Only a change of a stable intent (same inode) is corruption and fails loudly.
    """
    base, target, staged, request = stage(tmp_path)
    with intents.intent_store(str(tmp_path / 'session')) as store:
        record = store.prepare(**request)
        path = Path(store.directory) / (record['changeId'] + '.json')
        identity = os.stat(path).st_ino
        fstat = os.fstat
        injected = []

        def settling_fstat(descriptor):
            result = fstat(descriptor)
            if result.st_ino == identity and not injected:
                # Between the reader's open/first fstat and its second fstat.
                injected.append(owner_action)
                if owner_action == 'acknowledge':
                    store.acknowledge(record)
                elif owner_action.startswith('abort'):
                    store.abort(record)
                else:
                    with open(path, 'ab') as handle:
                        handle.write(b' ')
            return result

        monkeypatch.setattr(os, 'fstat', settling_fstat)
        if owner_action == 'in-place-corruption':
            with pytest.raises(intents.IntentError, match='changed while reading'):
                store.read()
            return
        if owner_action == 'abort-then-acknowledge':
            replaced = []

            def acknowledging_fstat(descriptor):
                result = settling_fstat(descriptor)
                if injected and result.st_ino != identity and not replaced:
                    replaced.append(True)
                    store.acknowledge(record)
                return result
            monkeypatch.setattr(os, 'fstat', acknowledging_fstat)
        listed = store.read()
        assert injected == [owner_action]
        if owner_action == 'abort':
            assert [entry['state'] for entry in listed] == ['aborted']
        else:
            assert listed == []
