"""The offline restore CLI must bind a receipt to the actual retained bytes."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib.storage.v2_client import StorageV2Client
from scripts.tofudb_perf_compare import _read_readiness
from tests.support.tofudb_binary import ensure_engine_binary

pytestmark = [pytest.mark.unit, pytest.mark.serial]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tool(tmp_path):
    binary = ensure_engine_binary(ROOT)
    if binary is None:
        pytest.skip('Native TofuDB CLI required')
    env = {'PATH': os.defpath, 'TMPDIR': str(tmp_path)}

    def call(command, *args, code=0):
        result = subprocess.run([binary, command, *args], env=env, check=False,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == code, result.stderr
        return result

    return binary, env, call


def _put_synthetic_record(binary, env, source, key):
    secret = b'exact-restore-synthetic-test-secret'
    with subprocess.Popen(
        [binary, 'serve', '--data-dir', source, '--owner-id', '11', '--tenant-id', '7',
         '--durability', 'strict'],
        env={**env, 'TOFU_STORAGE_TOKEN': secret.decode('ascii')},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as child:
        try:
            readiness = json.loads(_read_readiness(child.stdout.fileno()))
            client = StorageV2Client('127.0.0.1', int(readiness['port']), secret,
                                     owner_id=11, tenant_id=7, timeout=20)
            try:
                client.command('record.put', {'namespace': 'restore-test', 'key': key,
                                              'value': {'synthetic': True}}, f'exact-restore-put-{key}')
            finally:
                client.close()
        finally:
            child.stdin.close()
            try:
                assert child.wait(timeout=30) == 0
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
                raise


def _fields(receipt):
    return dict(pair.split('=', 1) for pair in receipt.splitlines()[0].split())


def test_exact_restore_selects_receipt_and_never_falls_back_after_prune(tool, tmp_path):
    binary, env, call = tool
    source, backup, target = (tmp_path / name for name in ('source', 'backup', 'target'))
    call('init', '--data-dir', source)
    _put_synthetic_record(binary, env, source, 'older')
    old = _fields(call('backup', '--data-dir', source, '--backup-dir', backup).stdout)
    old_path, = (backup / 'snapshots').glob('*.manifest')
    old_bytes = old_path.read_bytes()
    assert 'manifest_sha256' not in old and 'source_control_generation' not in old
    digest = hashlib.sha256(old_bytes).hexdigest()
    # Independent custody receipt, retained before selector extraction; not a graph copy.
    receipt_path = tmp_path / 'custody-receipt.json'
    receipt_path.write_text(json.dumps({'sha256': digest}))
    preserved = tmp_path / 'preserved.manifest'
    preserved.write_bytes(old_bytes)

    def selectors(receipt):
        result = subprocess.run(
            [sys.executable, '-m', 'serverctl_pkg.tofudb_restore_selectors',
             '--manifest', preserved,
             '--manifest-sha256', receipt['sha256']],
            env=env, capture_output=True, text=True, check=False, timeout=30)
        assert result.returncode == 0, result.stderr
        return tuple(result.stdout.split())

    selector = selectors(json.loads(receipt_path.read_text()))
    generation = selector[3]
    # Existing online receipt's SHA-256 suffices without duplicated provenance fields.
    online_receipt = {'sha256': digest, 'durable_sequence': int(old['durable_sequence'])}
    assert selectors(online_receipt) == selector
    _put_synthetic_record(binary, env, source, 'newer')
    newer = _fields(call('backup', '--data-dir', source, '--backup-dir', backup).stdout)
    assert int(newer['durable_sequence']) > int(old['durable_sequence'])
    args = ('--backup-dir', backup, '--data-dir', target)

    for wrong_selector in (selector[:2], ('--manifest', old_path),
                           ('--manifest-sha256', 'bad', *selector[2:])):
        call('restore', *args, *wrong_selector, code=2)
        assert not target.exists()

    call('restore', *args, *selector[:3], str(int(generation) + 1), code=1)
    assert not target.exists()
    old_path.write_bytes(old_bytes[:-1] + bytes([old_bytes[-1] ^ 1]))
    call('restore', *args, *selector, code=1)
    assert not target.exists()
    old_path.write_bytes(old_bytes)

    with (backup / 'BACKUP_LOCK').open('rb') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        call('restore', *args, *selector, code=1)
        assert not target.exists()

    manifest_only = tmp_path / 'manifest-only'
    manifest_only.mkdir()
    for directory in ('blocks', 'snapshots', 'pins', 'gc-runs'):
        (manifest_only / directory).mkdir()
    (manifest_only / 'snapshots' / old_path.name).write_bytes(old_bytes)
    unavailable = tmp_path / 'unavailable-target'
    refused = call('restore', '--backup-dir', manifest_only, '--data-dir', unavailable,
                   *selector, code=1)
    assert 'missing' in refused.stderr and not (unavailable / 'CONTROL').exists()

    restored = _fields(call('restore', *args, *selector).stdout)
    assert restored['durable_sequence'] == old['durable_sequence']
    assert restored['source_control_generation'] == generation
    assert restored['manifest_sha256'] == digest
    verified = _fields(call('verify', '--data-dir', target).stdout)
    assert (verified['verdict'], verified['durable_sequence']) == ('ok', old['durable_sequence'])
    assert int(verified['history_blocks_verified']) > 0 and verified['missing_blocks'] == '0'
    # Simulate interruption after CONTROL publication but before marker removal.
    (target / 'RESTORE').write_bytes(old_bytes)
    assert _fields(call('restore', *args, *selector).stdout) == restored
    assert not (target / 'RESTORE').exists()

    latest = tmp_path / 'latest'
    latest_args = ('--backup-dir', backup, '--data-dir', latest)
    assert _fields(call('restore', *latest_args).stdout)['durable_sequence'] == newer['durable_sequence']
    call('restore', *latest_args, *selector, code=1)
    assert _fields(call('verify', '--data-dir', latest).stdout)['durable_sequence'] == newer['durable_sequence']

    resume = tmp_path / 'resume'
    resume.mkdir()
    marker = resume / 'RESTORE'
    newer_path, = set((backup / 'snapshots').glob('*.manifest')) - {old_path}
    newer_bytes = newer_path.read_bytes()
    marker.write_bytes(newer_bytes)
    resume_args = ('--backup-dir', backup, '--data-dir', resume)
    call('restore', *resume_args, *selector, code=1)
    assert marker.read_bytes() == newer_bytes and not (resume / 'CONTROL').exists()
    marker.write_bytes(old_bytes)
    call('prune-backup', '--backup-dir', backup, '--retain-generations', '1')
    assert not old_path.exists() and newer_path.read_bytes() == newer_bytes
    call('restore', *resume_args, *selector, code=1)
    assert marker.read_bytes() == old_bytes and not (resume / 'CONTROL').exists()
    call('restore', *args, *selector, code=1)  # Even an already completed target cannot mask absence.

    ordinary = _fields(call('restore', *resume_args).stdout)
    assert ordinary['durable_sequence'] == newer['durable_sequence']
    assert set(ordinary) == {'format', 'authority_uuid', 'durable_sequence'}
    assert not marker.exists()
    verified = _fields(call('verify', '--data-dir', resume).stdout)
    assert (verified['verdict'], verified['durable_sequence']) == ('ok', newer['durable_sequence'])
