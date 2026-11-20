"""Liveness consumers agree that an exited, unreaped child is dead.

Signal 0 still reaches a process that has exited but whose parent has not
waited for it yet. Each consumer below used to read such a zombie as alive: a
dead worker's fault dump stayed protected as a live sink, a zombie Web lock
holder blocked storage cutover, and the sidecar crash benchmark timed its
child's reaper instead of its death.
``runtime_guards.pid_is_alive`` is the one shared rule; a running child stays
alive under it.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.logging_pkg import log_retention
from lib.storage import storage_boundary

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not sys.platform.startswith('linux'), reason='Linux /proc process state')]


def _state(pid):
    try:
        raw = Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError:
        return None
    return raw[raw.rfind(b')') + 2:].split()[0].decode()


@pytest.fixture()
def zombie():
    # Its own session: a stale "alive" verdict can only ever signal this child.
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    deadline = time.monotonic() + 10
    while _state(child.pid) != 'Z':
        assert time.monotonic() < deadline, 'child never exited'
        time.sleep(0.01)
    yield child.pid
    child.wait(timeout=5)


@pytest.fixture()
def live(tmp_path):
    # A server.py command line also satisfies lock-holder identity checks.
    (tmp_path / 'server.py').write_text('import time\ntime.sleep(60)\n')
    child = subprocess.Popen(
        [sys.executable, 'server.py'], cwd=tmp_path, start_new_session=True)
    yield child.pid
    child.kill()
    child.wait(timeout=5)


def test_storage_cutover_does_not_count_a_zombie_web_lock_holder(
        tmp_path, zombie, live):
    lock = tmp_path / 'data' / '.server.lock'
    lock.parent.mkdir()
    for holder, active in ((zombie, False), (live, True)):
        lock.write_text(f'{holder}@{socket.gethostname()}\n')
        report = storage_boundary.cutover_report(tmp_path)
        web = next(owner for owner in report['ownership']['owners']
                   if owner['kind'] == 'web')
        assert web['pid'] == holder
        assert web['active'] is active
        assert report['ownership']['quiescent'] is not active


def test_fault_dump_of_an_exited_unreaped_writer_is_pruned_as_dead(
        tmp_path, zombie, live):
    for pid in (zombie, live):
        (tmp_path / f'tofu_faulthandler_{pid}.log').write_text('dump\n')
    result = log_retention.maintain_logs(
        tmp_path, dry_run=True, now=time.time() + 400 * 86_400)
    assert result['faulthandler_process']['live_files'] == 1
    removed = {Path(item['path']).name for item in result['removed']}
    assert f'tofu_faulthandler_{zombie}.log' in removed
    assert f'tofu_faulthandler_{live}.log' not in removed


def test_sidecar_reopen_observes_the_exited_child_without_its_reaper(
        monkeypatch, zombie):
    from scripts import tofudb_perf_compare as bench

    engine = bench.LegacyEngine('sqlite')
    engine._runtime = SimpleNamespace(stop=lambda: None)
    engine._child_pid = zombie
    monkeypatch.setattr(engine, '_launch_runtime', lambda: None)
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(bench, 'time', SimpleNamespace(
        perf_counter_ns=time.perf_counter_ns,
        monotonic=lambda: clock[0], sleep=sleep))
    engine.reopen_after_sigkill()
    assert sleeps == []


@pytest.mark.parametrize('case', ['missing', 'already-exited', 'signal-error', 'wrong-exit'])
def test_benchmark_crash_refuses_to_relaunch_without_verified_signal_exit(monkeypatch, case):
    from scripts import tofudb_perf_compare as bench

    def kill():
        if case == 'signal-error':
            raise ProcessLookupError('gone before signal')

    child = (None if case == 'missing' else SimpleNamespace(
        poll=lambda: 0 if case == 'already-exited' else None,
        kill=kill, wait=lambda **_: 0))
    engine = bench.LegacyEngine('sqlite')
    engine._runtime = SimpleNamespace(_supervisor=SimpleNamespace(_process=child))
    monkeypatch.setattr(engine, '_relaunch_after_kill', lambda _: pytest.fail('reopened without SIGKILL'))
    with pytest.raises((bench.BenchError, ProcessLookupError)):
        engine.crash_cycle()
    assert engine.last_crash_receipt is None


def test_verified_kill_survives_failed_reopen(tmp_path, monkeypatch):
    from scripts import tofudb_perf_compare as bench

    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) as child:
        engine = bench.LegacyEngine('sqlite')
        engine._project_root = tmp_path
        engine._runtime = SimpleNamespace(_supervisor=SimpleNamespace(_process=child))
        def fail_reopen(_):
            raise RuntimeError('fixture reopen failed')
        monkeypatch.setattr(engine, '_relaunch_after_kill', fail_reopen)
        with pytest.raises(RuntimeError, match='fixture reopen failed'):
            engine.crash_cycle()
        assert engine.last_crash_receipt == {
            'killed_pid': child.pid, 'exit_code': -9, 'authority_directory': str(tmp_path)}
