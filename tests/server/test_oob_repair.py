"""Out-of-band headless repair sessions (serverctl_pkg/oob_repair.py).

Executable specification of designs/headless-recovery-agent/DESIGN.md, piece
O2 (the session core): one sandboxed session per incident, started on the
watcher's own alert decision or an in-platform escalation, never while the
in-platform layer holds a live lease or the owner's recovery run holds its
lock; stopped when its incident closes; its outcome recorded. The harness is a
fake script; one test runs the real kernel sandbox. Nothing here contacts a
model, the production application or the owner.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from serverctl_pkg import oob_alert, oob_relay, oob_repair
from serverctl_pkg.oob_alert import Config
from tests.support.production_api_guard import production_api_guard  # noqa: F401 - autouse guard

pytestmark = pytest.mark.unit

START = time.mktime(time.strptime('2026-11-12 06:00:00', '%Y-%m-%d %H:%M:%S'))

FAKE_HARNESS = textwrap.dedent('''
    import json, os, socket, sys, time
    args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    report = {'pid': os.getpid(), 'env': sorted(os.environ), 'cwd': os.getcwd(), 'args': args,
              'prompt': open(args['--prompt'], encoding='utf-8').read()}
    probe_pid = int(args.get('--probe-pid') or 0)
    if probe_pid:
        try:
            os.kill(probe_pid, 0)
            report['probe_pid'] = 'visible'
        except ProcessLookupError:
            report['probe_pid'] = 'invisible'
        except PermissionError:
            report['probe_pid'] = 'visible'
    probe_port = int(args.get('--probe-port') or 0)
    if probe_port:
        s = socket.socket()
        s.settimeout(2)
        try:
            s.connect(('127.0.0.1', probe_port))
            report['probe_port'] = 'reachable'
        except OSError as error:
            report['probe_port'] = f'blocked:{error.errno}'
        finally:
            s.close()
    time.sleep(float(args.get('--sleep') or 0))
    with open(args['--output'], 'w', encoding='utf-8') as stream:
        json.dump({'ok': True, 'status': 'done', 'content': 'diagnosed', 'report': report}, stream)
''')


def _harness(tmp_path: Path, *extra: str) -> list[str]:
    script = tmp_path / 'fake_harness.py'
    script.write_text(FAKE_HARNESS, encoding='utf-8')
    return [sys.executable, '-I', str(script), '--output', '{output}', '--prompt', '{prompt}', *extra]


def _repairs(tmp_path: Path, *extra: str, runner=None, owner_lock: Path | None = None, clock=None) -> oob_repair.Repairs:
    tofu = tmp_path / 'tofu'
    (tofu / 'logs').mkdir(parents=True, exist_ok=True)
    config = oob_repair.RepairConfig(enabled=True, harness=_harness(tmp_path, *extra))
    return oob_repair.Repairs(config, tmp_path / 'state', tofu,
                              workspace=lambda key: (tmp_path / 'lanes' / key).mkdir(parents=True) or tmp_path / 'lanes' / key,
                              runner=runner or oob_repair.direct_runner, owner_lock=owner_lock,
                              clock=clock or time.time)


def _finish(repairs: oob_repair.Repairs, open_keys: set[str]) -> list[dict]:
    deadline = time.monotonic() + 60
    outcomes: list[dict] = []
    while time.monotonic() < deadline and not outcomes:
        outcomes = repairs.poll(open_keys)
        time.sleep(0.05)
    return outcomes


def test_one_session_per_incident_and_its_outcome_is_recorded(tmp_path):
    repairs = _repairs(tmp_path)
    incident = {'id': 'inc-1', 'kind': 'storage_unavailable', 'openedAt': START, 'latched': 'storm'}
    first = repairs.start('inc-1', {'trigger': 'outage', 'incident': incident})
    again = repairs.start('inc-1', {'trigger': 'outage', 'incident': incident})
    assert first is not None and again == first  # the claim holds: no second session
    [outcome] = _finish(repairs, {'inc-1'})
    assert outcome['key'] == 'inc-1' and outcome['status'] == 'finished' and outcome['exitCode'] == 0
    result = json.loads((tmp_path / 'state' / 'repair' / 'inc-1' / 'result.json').read_text())
    assert result['ok'] is True and 'storage_unavailable' in result['report']['prompt']
    assert repairs.start('inc-1', {'trigger': 'outage', 'incident': incident})['status'] == 'finished'
    assert json.loads((tmp_path / 'state' / 'repair' / 'inc-1' / 'outcome.json').read_text())['status'] == 'finished'


def test_each_session_gets_its_key_less_route_through_the_sandbox_bridge(tmp_path):
    repairs = _repairs(tmp_path, '--routing', '{routing}')
    started = repairs.start('inc-2', {'trigger': 'outage', 'incident': {'id': 'inc-2'}})
    _finish(repairs, {'inc-2'})
    session = Path(started['directory'])
    envelope = json.loads((session / 'routing.json').read_text())
    [connection] = envelope['model_routing']['connections']
    assert connection['base_url'] == oob_relay.SANDBOX_BASE_URL
    assert set(envelope['credential_secrets'].values()) == {'relay-held'}  # the relay holds the key
    report = json.loads((session / 'result.json').read_text())['report']
    assert report['args']['--routing'] == str(session / 'routing.json')


def test_a_live_in_platform_lease_keeps_the_session_off(tmp_path):
    repairs = _repairs(tmp_path, clock=lambda: START)
    leases = tmp_path / 'tofu' / 'logs' / 'self-repair'
    leases.mkdir(parents=True)
    (leases / 'turn-failed.conv-1.lease').write_text(json.dumps({'fingerprint': 'turn-failed.conv-1',
                                                                 'staleAfter': START + 60}))
    assert repairs.start('turn-failed.conv-1', {'trigger': 'escalation'}) is None
    assert repairs.refusals['turn-failed.conv-1'] == 'in-platform lease live until 06:01'
    stale = _repairs(tmp_path / 'later', clock=lambda: START + 61)
    (tmp_path / 'later' / 'tofu' / 'logs' / 'self-repair').mkdir(parents=True)
    (tmp_path / 'later' / 'tofu' / 'logs' / 'self-repair' / 'turn-failed.conv-1.lease').write_text(
        json.dumps({'fingerprint': 'turn-failed.conv-1', 'staleAfter': START + 60}))
    assert stale.start('turn-failed.conv-1', {'trigger': 'escalation'}) is not None


def test_an_escalation_starts_a_repair_and_the_claim_is_mirrored_for_tofu(tmp_path):
    repairs = _repairs(tmp_path)
    escalations = tmp_path / 'tofu' / 'logs' / 'self-repair'
    escalations.mkdir(parents=True)
    (escalations / 'turn-failed.conv-1.escalate.json').write_text(json.dumps({
        'fingerprint': 'turn-failed.conv-1', 'cause': 'compaction archive above the engine cap',
        'subject': 'coordinator-1', 'evidence': ['11 failed attempts 16:01-21:00']}))
    (escalations / 'bad name!.escalate.json').write_text('{}')
    [escalation] = repairs.pending_escalations()
    assert escalation['fingerprint'] == 'turn-failed.conv-1'
    assert repairs.start(escalation['fingerprint'], {'trigger': 'escalation', 'escalation': escalation})
    assert (escalations / 'oob-turn-failed.conv-1.claim').exists()
    assert repairs.pending_escalations() == []  # claimed once
    # The escalation file stays open while it exists; withdrawal closes the session.
    [outcome] = _finish(repairs, {'turn-failed.conv-1'})
    assert outcome['status'] == 'finished'


def test_a_withdrawn_escalation_stops_its_session(tmp_path):
    repairs = _repairs(tmp_path, '--sleep', '120')
    escalations = tmp_path / 'tofu' / 'logs' / 'self-repair'
    escalations.mkdir(parents=True)
    (escalations / 'turn-failed.conv-1.escalate.json').write_text(json.dumps({'fingerprint': 'turn-failed.conv-1'}))
    assert repairs.start('turn-failed.conv-1', {'trigger': 'escalation'})
    (escalations / 'turn-failed.conv-1.escalate.json').unlink()
    [outcome] = _finish(repairs, set())
    assert outcome['status'] == 'stopped'


def test_the_owners_recovery_run_wins(tmp_path):
    lock = tmp_path / '.storage_recover.lock'
    holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent(f'''
        import fcntl, sys, time
        handle = open({str(lock)!r}, 'a')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        print('held', flush=True)
        time.sleep(60)
    ''')], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        repairs = _repairs(tmp_path, owner_lock=lock)
        assert repairs.start('inc-1', {'trigger': 'outage'}) is None
        assert repairs.refusals['inc-1'] == 'the owner\'s storage_recover.sh run holds its lock'
    finally:
        holder.kill()
        holder.wait()
    with open(lock, 'a') as handle:  # free again: the lock never blocks a start
        assert fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB) is None
    assert _repairs(tmp_path / 'again', owner_lock=lock).start('inc-1', {'trigger': 'outage'}) is not None


def test_the_session_is_stopped_when_its_incident_closes(tmp_path):
    repairs = _repairs(tmp_path, '--sleep', '120')
    session = repairs.start('inc-1', {'trigger': 'outage'})
    assert session['status'] == 'running'
    assert repairs.poll({'inc-1'}) == []  # still open, still running
    outcomes = _finish(repairs, set())  # the incident closed
    assert [(item['key'], item['status']) for item in outcomes] == [('inc-1', 'stopped')]
    assert outcomes[0]['reason'] == 'incident closed'
    assert repairs.running() == {}


def test_the_session_environment_carries_no_credentials(tmp_path, monkeypatch):
    for name in ('GITHUB_TOKEN', 'DEV_SANKUAI_TOKEN', 'SSH_AUTH_SOCK', 'TOFU_AGENT_MODEL_ROUTING', 'https_proxy'):
        monkeypatch.setenv(name, 'secret-value')
    repairs = _repairs(tmp_path)
    repairs.start('inc-1', {'trigger': 'outage'})
    _finish(repairs, {'inc-1'})
    report = json.loads((tmp_path / 'state' / 'repair' / 'inc-1' / 'result.json').read_text())['report']
    assert not {'GITHUB_TOKEN', 'DEV_SANKUAI_TOKEN', 'SSH_AUTH_SOCK', 'TOFU_AGENT_MODEL_ROUTING',
                'https_proxy'} & set(report['env'])
    assert {'HOME', 'PATH', 'TMPDIR', 'GIT_SSH_COMMAND'} <= set(report['env'])
    assert report['cwd'] == str(tmp_path / 'lanes' / 'inc-1')


def _namespaces_available() -> bool:
    """A bounded probe: on a loaded host the namespace clone itself can stall."""
    try:
        probe = subprocess.Popen(['unshare', '-Urnp', '--fork', 'true'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        return probe.wait(timeout=15) == 0
    except subprocess.TimeoutExpired:
        probe.kill()
        probe.wait(timeout=5)
        return False


@pytest.mark.skipif(not _namespaces_available(), reason='user, network and PID namespaces are not permitted here')
def test_the_sandbox_hides_tofu_processes_and_the_network(tmp_path):
    """Kernel-enforced: the session cannot signal a host process or reach a loopback port."""
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        repairs = _repairs(tmp_path, '--probe-pid', str(os.getpid()), '--probe-port', str(port),
                           runner=oob_repair.sandboxed_runner)
        assert repairs.start('inc-1', {'trigger': 'outage'}) is not None
        [outcome] = _finish(repairs, {'inc-1'})
    finally:
        server.close()
    assert outcome['status'] == 'finished', outcome
    report = json.loads((tmp_path / 'state' / 'repair' / 'inc-1' / 'result.json').read_text())['report']
    assert report['probe_pid'] == 'invisible'
    assert report['probe_port'].startswith('blocked')
    assert report['pid'] < 10  # a fresh PID namespace


def test_production_sessions_run_in_the_sandbox():
    assert oob_repair.RepairConfig.from_mapping({'enabled': True, 'harness': ['x']}).runner is oob_repair.sandboxed_runner
    argv = oob_repair.sandbox_argv(['tofu-agent', 'run'])
    assert argv[:argv.index('--')] == ['unshare', '--map-root-user', '--net', '--pid', '--kill-child']
    assert oob_repair.RepairConfig.from_mapping({}).enabled is False


def _watcher(tmp_path, repairs_mapping: dict, kinds: list[str]):
    sender_texts: list[str] = []

    class Sender:
        name = 'scripted'

        def send(self, text):
            sender_texts.append(text)
            return 'sent', 'ok'

    tofu = tmp_path / 'tofu'
    (tofu / 'logs').mkdir(parents=True, exist_ok=True)
    config = Config(tofu_root=tofu, repair_agent=repairs_mapping)
    clock = oob_alert.FakeClock(START)
    sequence = iter(kinds)
    watcher = oob_alert.Watcher(config, tmp_path / 'state', sender=Sender(), clock=clock,
                                probe_fn=lambda: oob_alert.injected_probe(next(sequence), clock()))
    watcher.repairs = _repairs(tmp_path) if repairs_mapping.get('enabled') else None
    return watcher, clock, sender_texts


def test_the_alert_names_the_running_repair_and_a_disabled_agent_changes_nothing(tmp_path):
    watcher, clock, texts = _watcher(tmp_path / 'on', {'enabled': True, 'runner': 'direct'}, ['breaker'])
    watcher.tick()
    assert '已自动启动修复' in texts[0]
    assert set(watcher.repairs.running()) == {watcher.state['incident']['id']}
    off, _, off_texts = _watcher(tmp_path / 'off', {}, ['breaker'])
    off.tick()
    assert off.repairs is None and '已自动启动修复' not in off_texts[0]


UNCLEAN_CLOSE = ('2026-11-12 15:52:43,895 WARNING [server_manager] explicit restart replaced an execution host '
                 'that exited after an unclean close: RuntimeError: Process background services have not '
                 'quiesced: bot channel sessions (still running)')


def test_an_unclean_close_starts_a_repair_while_storage_is_ready(tmp_path):
    """Acceptance case 3: the 15:50 restart did not quiesce the bot channel sessions."""
    tofu = tmp_path / 'tofu'
    logs = tofu / 'logs'
    logs.mkdir(parents=True)
    (logs / oob_alert.MANAGER_LOG).write_text('')
    config = Config(tofu_root=tofu, repair_agent={'enabled': True, 'runner': 'direct'})
    watcher = oob_alert.Watcher(config, tmp_path / 'state', sender=oob_alert.DryRunSender(),
                                probe_fn=lambda: oob_alert.injected_probe('ready', START))
    watcher.repairs = _repairs(tmp_path)
    watcher.tick()  # baseline: the tail starts at the log's end
    (logs / oob_alert.MANAGER_LOG).open('a').write(UNCLEAN_CLOSE + '\n')
    watcher.tick()
    expected_key = f'unclean_close.{int(time.mktime(time.strptime("2026-11-12 15:52:43", "%Y-%m-%d %H:%M:%S")))}'
    assert set(watcher.repairs.running()) == {expected_key}
    session = json.loads((tmp_path / 'state' / 'repair' / expected_key / 'session.json').read_text())
    assert session['workspace'] == str(tmp_path / 'lanes' / expected_key)
