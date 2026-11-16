"""Local executor death requires kernel evidence, including unreaped exits."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import lib.process_owner as owners

pytestmark = pytest.mark.unit


def test_unreaped_process_exit_is_dead_and_cold_research_task_is_interrupted(monkeypatch):
    if not hasattr(os, 'WNOWAIT') or owners.process_owner() is None:
        pytest.skip('local Linux process incarnation evidence unavailable')
    code = ('import json,sys; from lib.process_owner import process_owner; '
            'print(json.dumps(process_owner()),flush=True); sys.stdin.readline()')
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        stamp = json.loads(child.stdout.readline())
        assert owners.owner_state(stamp) == 'alive'
        from tests.research.test_research_execution_owner import repository
        from tests.research.test_research_action_observations import MemoryRecords
        from lib.research.action_terminal import load_public_action_task
        client = MemoryRecords()
        monkeypatch.setattr('lib.research.execution_owner.process_owner', lambda: stamp)
        repository(client).claim_initial()
        monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
        assert load_public_action_task('research_action_observed', user_id=1) is None
        child.stdin.write('\n'); child.stdin.flush()
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        # The PID still exists, but this process can never execute again.
        assert Path(f'/proc/{child.pid}/stat').exists()
        assert owners.owner_state(stamp) == 'dead'
        task = load_public_action_task('research_action_observed', user_id=1)
        assert task['status'] == 'interrupted' and task['replay_allowed'] is False
        child.wait(timeout=5)
        assert owners.owner_state(stamp) == 'dead'
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)


@pytest.mark.parametrize('state,expected', [('R','alive'), ('S','alive'), ('D','alive'),
    ('T','alive'), ('t','alive'), ('I','alive'), ('K','alive'), ('W','alive'), ('P','alive'),
    ('Z','dead'), ('X','dead'), ('x','dead'), ('?','unknown'), ('ZZ','unknown')])
def test_kernel_state_with_same_start_time(state, expected, monkeypatch):
    stamp = {'host':'local','boot':'boot','pid_namespace':'namespace','pid':31,'started':'555'}
    monkeypatch.setattr(owners, 'process_owner', lambda: stamp)
    fields = [state] + ['0'] * 18 + ['555']; fields[17] = '1'
    stat = '31 (worker ) name) ' + ' '.join(fields)
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *args, **kwargs:
        stat if str(path) == '/proc/31/stat' else original(path, *args, **kwargs))
    assert owners.owner_state(stamp) == expected


def test_exited_leader_with_live_worker_retains_execution_authority():
    if owners.process_owner() is None:
        pytest.skip('local Linux process incarnation evidence unavailable')
    code = ('import ctypes,json,time,threading; from lib.process_owner import process_owner; '
            'threading.Thread(target=lambda: time.sleep(60)).start(); '
            'print(json.dumps(process_owner()),flush=True); ctypes.CDLL(None).pthread_exit(None)')
    child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True)
    try:
        stamp = json.loads(child.stdout.readline())
        deadline = time.monotonic() + 5
        while True:
            fields = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z' or time.monotonic() >= deadline: break
            time.sleep(.01)
        assert fields[0] == 'Z' and int(fields[17]) > 1
        assert owners.owner_state(stamp) == 'alive'
    finally:
        child.kill(); child.wait(timeout=5); child.stdout.close()


@pytest.mark.parametrize('state,threads,expected', [('Z','2','alive'), ('X','2','alive'),
    ('x','2','alive'), ('R','0','unknown'), ('Z','0','unknown'), ('Z','-1','unknown'),
    ('R','invalid','unknown')])
def test_thread_count_fences_whole_process_exit(state, threads, expected, monkeypatch):
    stamp = {'host':'local','boot':'boot','pid_namespace':'namespace','pid':31,'started':'555'}
    monkeypatch.setattr(owners, 'process_owner', lambda: stamp)
    fields = [state] + ['0'] * 18 + ['555']; fields[17] = threads
    stat = '31 (worker) ' + ' '.join(fields)
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *args, **kwargs:
        stat if str(path) == '/proc/31/stat' else original(path, *args, **kwargs))
    assert owners.owner_state(stamp) == expected


def test_recorded_exit_proves_death_only_where_the_kernel_cannot_and_is_never_laundered(
        tmp_path, monkeypatch):
    if owners.process_owner() is None:
        pytest.skip('local Linux process incarnation evidence unavailable')
    code = ('import json,sys; from lib.process_owner import process_owner; '
            'print(json.dumps(process_owner()),flush=True); sys.stdin.readline()')
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        stamp = json.loads(child.stdout.readline())
        with pytest.raises(ValueError, match='not proved'):
            owners.record_exit(stamp, tmp_path)
        child.stdin.write('\n'); child.stdin.flush()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)
    here = owners.process_owner()
    monkeypatch.setattr(owners, 'exit_evidence_dir', lambda: str(tmp_path))
    recorded = Path(owners.record_exit(stamp, tmp_path))
    assert json.loads(recorded.read_text())['owner'] == stamp
    successor = {**here, 'host': 'successor-host'}
    monkeypatch.setattr(owners, 'process_owner', lambda: successor)
    # A host move: the kernel cannot see the old incarnation, the record can.
    assert owners.owner_state(stamp) == 'dead'
    other = {**stamp, 'started': str(int(stamp['started']) + 1)}
    assert owners.owner_state(other) == 'unknown'
    # Only kernel evidence admits a record, so a successor cannot re-certify one.
    with pytest.raises(ValueError, match='not proved'):
        owners.record_exit(stamp, tmp_path / 'copy')
    recorded.write_text(json.dumps({'format': owners.EXIT_RECORD_FORMAT, 'owner': other}))
    assert owners.owner_state(stamp) == 'unknown'


def test_host_retirement_covers_only_other_hosts(tmp_path, monkeypatch):
    here = owners.process_owner()
    if here is None:
        pytest.skip('local Linux process incarnation evidence unavailable')
    monkeypatch.setattr(owners, 'exit_evidence_dir', lambda: str(tmp_path))
    with pytest.raises(ValueError, match='another host'):
        owners.record_host_retired(here['host'], tmp_path, evidence={})
    owners.record_host_retired('retired-host', tmp_path, evidence={'lock': '1@retired-host'})
    assert owners.owner_state({**here, 'host': 'retired-host'}) == 'dead'
    # This host's own processes keep kernel evidence; a retirement never reaches them.
    assert owners.owner_state(here) == 'alive'
    assert owners.owner_state({**here, 'boot': 'other-boot'}) == 'unknown'


def test_era_retirement_covers_only_that_era_never_the_readers(tmp_path, monkeypatch):
    # A container restarted under the same hostname gets a new PID namespace;
    # a host retirement never reaches it, so only an era record can.
    here = owners.process_owner()
    if here is None:
        pytest.skip('local Linux process incarnation evidence unavailable')
    monkeypatch.setattr(owners, 'exit_evidence_dir', lambda: str(tmp_path))
    with pytest.raises(ValueError, match='another serving era'):
        owners.record_era_retired(here, tmp_path, evidence={})
    old = {**here, 'pid_namespace': '4026530001'}
    owners.record_era_retired(old, tmp_path, evidence={'lock': 'old era'})
    assert owners.owner_state({**old, 'pid': 7, 'started': '1'}) == 'dead'
    assert owners.owner_state({**here, 'pid_namespace': '4026530002'}) == 'unknown'
    assert owners.owner_state({**here, 'boot': 'other-boot'}) == 'unknown'
    assert owners.owner_state(here) == 'alive'
