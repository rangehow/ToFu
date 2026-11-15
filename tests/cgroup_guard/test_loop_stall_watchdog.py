"""Tests for the event-loop stall watchdog + /dev/shm fault-dump prune.

Both features live as PURE module-level helpers in ``server.py`` precisely so
they can be exercised without a running event loop or a real stall:

  * ``_loop_stall_decide(age, threshold, already_dumped)`` — the one-dump-per-
    episode / re-arm-on-recovery decision the off-loop watcher thread makes.
  * ``_parse_fault_dump_pid`` / ``_prune_stale_fault_dumps`` — the boot-time
    tmpfs hygiene that deletes ``tofu_faulthandler_<pid>.log`` files whose pid
    is dead (the /dev/shm leak: 5,100 files observed in prod).
``import server`` is import-safe under conftest (serving is __main__-guarded).

NC-bite guidance (per project convention — prove the test FAILS without the
fix, then restore byte-identically):
  * Neuter ``_loop_stall_decide`` to ``return (age > threshold, already_dumped)``
    → ``test_stall_decide_one_dump_per_episode`` FAILS (it would dump every
    poll while stalled instead of once) while the disabled/healthy cases still
    pass — discriminating.
  * Neuter ``_prune_stale_fault_dumps`` to skip the ``pid_alive`` check (delete
    all matches) → ``test_prune_keeps_live_and_self`` FAILS.
  * Derive ``FaultSinks.shm_prefix`` without the data-root namespace (every
    instance shares one unscoped name family again) →
    ``test_booting_one_instance_never_prunes_another_instances_dumps`` and
    ``test_legacy_prune_never_removes_another_instances_dumps`` FAIL.
"""

import fnmatch
import os
import time

import pytest

import server
from lib.logging_pkg.log_policy import STREAM_POLICIES
from lib.server import server_fault_watchdog as faults
from serverctl_pkg.local_placement import workspace_namespace

pytestmark = pytest.mark.unit

# Durable dumps keep the unscoped family name: their directory is the
# instance's own log root.
UNSCOPED = 'tofu_faulthandler_'
# Above Linux PID_MAX_LIMIT (2**22): no process can hold these pids.
DEAD_PIDS = (4194401, 4194402, 4194403)


def test_plain_import_does_not_arm_or_write_crash_sinks():
    """Tools/tests importing helpers must have zero faulthandler file side effects."""
    assert server._fault_log is None
    assert server._fault_shm_log is None


class TestLoopStallDecide:
    def test_disabled_when_threshold_zero(self):
        # threshold<=0 means the watchdog is off — never dump regardless of age.
        assert server._loop_stall_decide(999.0, 0.0, False) == (False, False)
        assert server._loop_stall_decide(999.0, -1.0, True) == (False, True)

    def test_healthy_rearms(self):
        # age within threshold → not a stall, and re-arm (already_dumped→False).
        assert server._loop_stall_decide(1.0, 5.0, False) == (False, False)
        assert server._loop_stall_decide(1.0, 5.0, True) == (False, False)

    def test_stall_first_dump(self):
        # age beyond threshold, not yet dumped → dump once and latch.
        assert server._loop_stall_decide(6.0, 5.0, False) == (True, True)

    def test_stall_decide_one_dump_per_episode(self):
        # A contiguous stall must emit exactly ONE dump: first poll dumps,
        # subsequent polls while still stalled do NOT re-dump.
        should, latched = server._loop_stall_decide(6.0, 5.0, False)
        assert should is True and latched is True
        # still stalled, deeper
        should2, latched2 = server._loop_stall_decide(10.0, 5.0, latched)
        assert should2 is False and latched2 is True
        # recovery re-arms so the NEXT episode dumps again
        should3, latched3 = server._loop_stall_decide(0.5, 5.0, latched2)
        assert should3 is False and latched3 is False
        should4, _ = server._loop_stall_decide(6.0, 5.0, latched3)
        assert should4 is True


class TestParseFaultDumpPid:
    def test_valid(self):
        assert server._parse_fault_dump_pid('tofu_faulthandler_1234.log') == 1234

    def test_non_numeric_and_foreign(self):
        assert server._parse_fault_dump_pid('tofu_faulthandler_abc.log') is None
        assert server._parse_fault_dump_pid('unrelated.log') is None
        assert server._parse_fault_dump_pid('tofu_faulthandler_.log') is None

    def test_pid_parse_is_scoped_to_one_prefix_and_strictly_decimal(self):
        prefix = 'tofu_faulthandler_0123456789ab_'
        assert server._parse_fault_dump_pid(prefix + '845.log', prefix) == 845
        assert server._parse_fault_dump_pid(
            'tofu_faulthandler_ba9876543210_845.log', prefix) is None
        assert server._parse_fault_dump_pid(prefix + '845.log') is None
        # int() accepts all of these; none of them is a pid this family wrote.
        # A digit-only namespace would otherwise read as one huge dead pid.
        for core in ('1_234', '+12', ' 12', '123456789012_845'):
            assert server._parse_fault_dump_pid(
                'tofu_faulthandler_%s.log' % core) is None


class TestPruneStaleFaultDumps:
    def test_prune_keeps_live_and_self(self, tmp_path):
        # Fake sink dir with: a dead-pid dump, a live-pid dump, our own live
        # sink (keep_basename), and a foreign file. Only the dead one goes.
        dead = tmp_path / 'tofu_faulthandler_999999.log'
        live = tmp_path / 'tofu_faulthandler_4242.log'
        selfsink = tmp_path / 'tofu_faulthandler_777.log'
        foreign = tmp_path / 'something_else.log'
        for p in (dead, live, selfsink, foreign):
            p.write_text('x')

        def fake_alive(pid):
            return pid in (4242, 777)  # 999999 is dead

        removed = server._prune_stale_fault_dumps(
            directory=str(tmp_path),
            keep_basename='tofu_faulthandler_777.log',
            pid_alive=fake_alive, name_prefix=UNSCOPED)

        assert removed == 1
        assert not dead.exists()          # dead pid pruned
        assert live.exists()              # live pid kept
        assert selfsink.exists()          # our own sink kept (keep_basename)
        assert foreign.exists()           # non-matching name untouched

    @pytest.mark.skipif(not os.path.isdir('/proc/self/task'),
                        reason='Linux zombie state')
    def test_default_probe_prunes_the_dump_of_an_unreaped_zombie(self, tmp_path):
        # Signal 0 reaches a zombie, but a zombie never writes its dump again.
        zombie = os.fork()
        if zombie == 0:
            os._exit(0)
        try:
            deadline = time.monotonic() + 5
            while True:
                with open('/proc/%d/stat' % zombie, 'rb') as stat:
                    raw = stat.read()
                if raw[raw.rfind(b')') + 2:].split()[0] == b'Z':
                    break
                assert time.monotonic() < deadline, 'child never exited'
                time.sleep(0.01)
            dead = tmp_path / ('tofu_faulthandler_%d.log' % zombie)
            live = tmp_path / ('tofu_faulthandler_%d.log' % os.getpid())
            for path in (dead, live):
                path.write_text('x')

            removed = server._prune_stale_fault_dumps(
                directory=str(tmp_path), name_prefix=UNSCOPED)

            assert removed == 1
            assert not dead.exists()
            assert live.exists()
        finally:
            os.waitpid(zombie, 0)

    def test_prune_no_matches_is_zero(self, tmp_path):
        (tmp_path / 'readme.txt').write_text('x')
        removed = server._prune_stale_fault_dumps(
            directory=str(tmp_path), keep_basename='', pid_alive=lambda _p: False,
            name_prefix=UNSCOPED)
        assert removed == 0


class TestBoundedFaultDumps:
    def test_budget_keeps_newest_dead_evidence_and_live_file(self, tmp_path):
        old = tmp_path / 'tofu_faulthandler_101.log'
        newest = tmp_path / 'tofu_faulthandler_102.log'
        live = tmp_path / 'tofu_faulthandler_103.log'
        for path in (old, newest, live):
            path.write_bytes(b'x' * 16)
        os.utime(old, ns=(1, 1))
        os.utime(newest, ns=(2, 2))
        os.utime(live, ns=(3, 3))

        removed = server._prune_fault_dump_budget(
            str(tmp_path), name_prefix=UNSCOPED, pid_alive=lambda pid: pid == 103,
            max_dead_files=1, max_dead_bytes=32)

        assert removed == 1
        assert not old.exists()
        assert newest.exists(), 'newest crash evidence must survive next boot'
        assert live.exists(), 'a live process sink must never be retention-pruned'

    def test_live_sink_is_truncated_in_place_only_after_cap(self, tmp_path):
        path = tmp_path / 'tofu_faulthandler_777.log'
        with open(path, 'w+', buffering=1) as sink:
            sink.write('x' * 33)
            fd = sink.fileno()
            assert server._trim_fault_sink_if_oversize(
                sink, 32, header='retained\n') is True
            assert sink.fileno() == fd
            assert path.read_text() == 'retained\n'
            assert server._trim_fault_sink_if_oversize(sink, 32) is False


class TestInstanceScopedFaultDumps:
    """/dev/shm is host-wide: one instance's boot must never evict another's.

    Unscoped ``tofu_faulthandler_<pid>.log`` names let a lane, test or second
    data-root boot prune production's newest stall dump as "over budget".
    """

    @staticmethod
    def _dump(directory, name, mtime_ns):
        path = directory / name
        path.write_bytes(b'dump')
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    @staticmethod
    def _prefix(data_root):
        return faults.FaultSinks.unarmed(
            pid=1, default_log_dir='', data_root=str(data_root)).shm_prefix

    def test_sink_names_carry_the_data_root_namespace(self, tmp_path):
        root_a = tmp_path / 'a' / 'data'
        root_b = tmp_path / 'b' / 'data'
        root_a.mkdir(parents=True)
        root_b.mkdir(parents=True)
        alias = tmp_path / 'alias'
        alias.symlink_to(root_a, target_is_directory=True)
        logs = str(tmp_path / 'logs')

        a = faults.FaultSinks.unarmed(
            pid=4242, default_log_dir=logs, data_root=str(root_a))
        b = faults.FaultSinks.unarmed(
            pid=4242, default_log_dir=logs, data_root=str(root_b))
        a_again = faults.FaultSinks.unarmed(
            pid=4343, default_log_dir=logs, data_root=str(alias))

        assert a.shm_prefix == (
            'tofu_faulthandler_' + workspace_namespace(root_a) + '_')
        assert b.shm_prefix != a.shm_prefix
        assert a_again.shm_prefix == a.shm_prefix
        assert a.shm_path == os.path.join('/dev/shm', a.shm_prefix + '4242.log')
        for sinks in (a, b):
            name = os.path.basename(sinks.shm_path)
            # Existing watchers and the documented family glob still match.
            assert fnmatch.fnmatch(name, 'tofu_faulthandler_*.log')
            assert fnmatch.fnmatch(
                name, STREAM_POLICIES['faulthandler_process'].filename)
        assert os.path.basename(a.durable_path) == 'tofu_faulthandler_4242.log'

    def test_booting_one_instance_never_prunes_another_instances_dumps(
            self, tmp_path, monkeypatch):
        import lib.log
        shm = tmp_path / 'shm'
        shm.mkdir()
        root_a = tmp_path / 'a' / 'data'
        root_b = tmp_path / 'b' / 'data'
        monkeypatch.setattr(faults, '_SHM_DIR', str(shm))
        monkeypatch.setattr(lib.log, 'LOG_DIR', str(tmp_path / 'a' / 'logs'))
        monkeypatch.setattr(faults.faulthandler, 'enable', lambda **_kwargs: None)
        # The existing retention setting: one dead dump per instance.
        monkeypatch.setenv('TOFU_FAULT_DUMP_FILES', '1')
        prefix_a = self._prefix(root_a)
        prefix_b = self._prefix(root_b)
        b_dumps = [self._dump(shm, '%s%d.log' % (prefix_b, pid), mtime)
                   for mtime, pid in enumerate(DEAD_PIDS, start=1)]
        a_old = self._dump(shm, '%s%d.log' % (prefix_a, DEAD_PIDS[0]), 10)
        a_new = self._dump(shm, '%s%d.log' % (prefix_a, DEAD_PIDS[1]), 20)

        sinks = faults.arm_faulthandler_sinks(
            pid=os.getpid(), default_log_dir=str(tmp_path / 'a' / 'logs'),
            data_root=str(root_a))
        try:
            removed = faults.prune_boot_fault_dumps(sinks)
        finally:
            for sink in (sinks.shm_log, sinks.durable_log):
                if sink is not None:
                    sink.close()

        assert all(path.read_bytes() == b'dump' for path in b_dumps), (
            "another instance's dead dumps must survive this boot")
        assert removed == {'tmpfs': 1, 'durable': 0}
        assert not a_old.exists()
        assert a_new.exists(), 'the newest own dead dump stays within budget'
        assert os.path.dirname(sinks.shm_path) == str(shm)
        assert os.path.exists(sinks.shm_path)

    def test_legacy_prune_never_removes_another_instances_dumps(self, tmp_path):
        prefix_a = self._prefix(tmp_path / 'a' / 'data')
        prefix_b = self._prefix(tmp_path / 'b' / 'data')
        own = self._dump(tmp_path, '%s%d.log' % (prefix_a, os.getpid()), 1)
        a_dead = [self._dump(tmp_path, '%s%d.log' % (prefix_a, pid), 1)
                  for pid in DEAD_PIDS]
        b_dead = [self._dump(tmp_path, '%s%d.log' % (prefix_b, pid), 1)
                  for pid in DEAD_PIDS]
        # A pre-upgrade name says nothing about its owner: never this boot's.
        unscoped = self._dump(
            tmp_path, '%s%d.log' % (UNSCOPED, DEAD_PIDS[0]), 1)

        removed = faults._prune_stale_fault_dumps(
            directory=str(tmp_path), keep_basename=own.name,
            name_prefix=prefix_a)

        assert removed == len(DEAD_PIDS)
        assert not any(path.exists() for path in a_dead)
        assert all(path.exists() for path in b_dead + [own, unscoped])


class TestPidAlive:
    def test_self_is_alive(self):
        assert server._pid_alive(os.getpid()) is True

    def test_bogus_pids(self):
        assert server._pid_alive(0) is False
        assert server._pid_alive(-5) is False
        # An almost-certainly-dead high pid.
        assert server._pid_alive(4000000000) is False


class TestStallPressureContext:
    """The stall line must carry the host-pressure reading (2026-08-05 audit:
    classifying a stall meant hand-correlating error.log against
    cgroup_pressure.log — 7/19 stalls within 120s of a pressure event, the
    rest host-quiet. With the reading inline, the classification is free)."""

    def test_real_loadavg_present(self):
        out = server._stall_pressure_context()
        assert 'load1=' in out  # /proc/loadavg is always readable on Linux

    def test_cgroup_part_when_pressure_readable(self, monkeypatch):
        import lib.cgroup_guard as cg
        monkeypatch.setattr(cg, 'pressure',
                            lambda: {'limit': 1, 'usage': 1, 'pct': 91.42,
                                     'swap': None})
        assert 'cgmem=91.4%' in server._stall_pressure_context()

    def test_cgroup_part_absent_when_unreadable(self, monkeypatch):
        import lib.cgroup_guard as cg
        monkeypatch.setattr(cg, 'pressure', lambda: None)
        out = server._stall_pressure_context()
        assert 'cgmem' not in out and 'load1=' in out

    def test_stall_line_carries_pressure_suffix(self):
        """Source pin: the extracted owner still appends pressure context."""
        import inspect
        from lib.server.server_loop_watchdog import LoopWatchdog

        source = inspect.getsource(LoopWatchdog._stall_watch)
        assert 'pressure = self.hooks._stall_pressure_context()' in source
        assert "(' [' + pressure + ']') if pressure else ''" in source
        assert 'pressure=pressure' in source
