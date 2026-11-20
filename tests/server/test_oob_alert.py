"""Out-of-band outage alert watcher (serverctl_pkg/oob_alert.py).

Executable specification of docs/STORAGE_OPERATIONS.md "Out-of-band outage
alert": probe classification, the exact log-line formats of the storage
runtime and the manager, one alert and one recovery notice per incident,
delivery retry/unknown semantics, the read-only vault reader, the robot
sender against a fake open platform, and the real run/stop lifecycle. Nothing
here contacts a real endpoint or the production application.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from serverctl_pkg import oob_alert
from serverctl_pkg.oob_alert import Config, Probe, Signal
from tests.support.production_api_guard import ProductionConnectionRefused, fail_on_attempts

pytestmark = pytest.mark.unit


def test_no_test_can_reach_the_production_tofu(request):
    # 2026-11-13 11:44:11: a test inherited Config's default api_url and reached the live server.
    attempts = request.getfixturevalue('production_api_guard')
    for address in (('127.0.0.1', 15000), ('localhost', 15001), ('::ffff:127.0.0.1', 15000), ('0.0.0.0', 15001)):
        with pytest.raises(ProductionConnectionRefused, match='production Tofu'):
            socket.create_connection(address, timeout=1)
    # Code that swallows the refusal still has its attempt recorded, and the guard then fails that test.
    status, body, error = oob_alert.http_get_json('http://127.0.0.1:15000/api/v4/model-routing', 1.0)
    assert (status, body) == (None, None) and error.startswith('ProductionConnectionRefused')
    assert ('127.0.0.1', 15000) in attempts
    with pytest.raises(pytest.fail.Exception, match='connections to the production Tofu were attempted'):
        fail_on_attempts(list(attempts))
    attempts.clear()  # this spec consumed its own attempts
    with socket.socket() as server, socket.socket() as client:  # every other port is untouched
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        client.connect(server.getsockname())

REPO = Path(__file__).resolve().parents[2]
START = time.mktime(time.strptime('2026-11-12 06:00:00', '%Y-%m-%d %H:%M:%S'))

# Verbatim production lines from 2026-11-12 (logs/server-console.log and the
# manager's formats in server_manager.py).
LIVENESS_KILL = ('2026-11-12 06:13:21,094 CRITICAL Storage sidecar pid=119238 failed 49 consecutive liveness '
                 'probes; terminating so the exit path can fence and restart storage (unresponsive for 120s)')
BREAKER_TRIP = ('2026-11-12 06:13:39,928 CRITICAL Storage restart circuit breaker tripped: 6 storage '
                'crashes/failed starts within 600s (limit 5); last restart error: RuntimeError: storage sidecar '
                'closed its readiness channel before startup (exit 1). Restarts are stopped and storage stays '
                'fenced (degraded); leaving this state requires a process restart')
DEGRADED_REMINDER = ('2026-11-12 06:14:39,928 WARNING Storage runtime remains degraded (fenced): 6 storage '
                     'crashes/failed starts within 600s (limit 5); last restart error: RuntimeError: storage '
                     'sidecar closed its readiness channel before startup (exit 1)')
RECOVERY_PAUSED = ('2026-11-12 06:20:00,001 ERROR [server_manager] worker recovery paused after 5 failures in '
                   '120s; explicit start/restart is required to reset the crash-loop budget')
DECOYS = (
    # A command echoed into a log quotes the phrase but not the emitting format.
    '2026-11-12 09:47:39 [INFO] lib.project_mod.run_command [x]: run_command: $ rg "Storage restart circuit '
    'breaker tripped"',
    '2026-11-12 06:13:31,099 WARNING Storage writes fenced after sidecar exit code=-1; scheduling restart',
    '2026-11-12 06:13:20,000 WARNING Storage sidecar liveness probe failed (1/3) pid=119238',
)


def _config(tmp_path: Path, **overrides) -> Config:
    (tmp_path / 'tofu' / 'logs').mkdir(parents=True, exist_ok=True)
    return Config(tofu_root=tmp_path / 'tofu', **overrides)


@pytest.mark.parametrize('name', sorted(oob_alert.SCENARIOS))
def test_dry_run_scenarios_alert_once_and_recover_once(tmp_path, name):
    timeline, options, expected = oob_alert.SCENARIOS[name]
    delivered = oob_alert.run_scenario(timeline, root=tmp_path, start=START, **options)

    assert [(item['role'], item['minute']) for item in delivered] == expected
    prefixes = {'alert': '【Tofu 停机告警】', 'recovery': '【Tofu 已恢复】', 'heartbeat_alert': '【Tofu 巡检停止】',
                'heartbeat_recovery': '【Tofu 巡检恢复】', 'overrun': '【Tofu 计划重启超时】'}
    for item in delivered:
        assert item['text'].startswith(prefixes[item['role']])
    if options.get('fail_writes'):
        assert not (tmp_path / 'state' / 'events.jsonl').exists()  # alerted with nothing written
        return
    log = tmp_path / 'state' / 'events.jsonl'  # absent when nothing happened (a slow-only timeline)
    events = [json.loads(line)['event'] for line in log.read_text().splitlines()] if log.exists() else []
    assert events.count('alert_sent') == sum(role == 'alert' for role, _ in expected)
    assert events.count('recovery_sent') == sum(role == 'recovery' for role, _ in expected)


def test_simulate_command_reports_every_scenario_passing(capsys):
    assert oob_alert.main(['simulate']) == 0
    output = capsys.readouterr().out
    assert output.count('PASS ') == len(oob_alert.SCENARIOS) and 'FAIL' not in output


def _fake_http(answers):
    calls = []

    def http(url, timeout):
        calls.append((url, timeout))
        for prefix, answer in answers.items():
            if prefix in url:
                return answer
        return None, None, 'ConnectionRefusedError: [Errno 111] Connection refused'
    return http, calls


def _storage(state, *, degraded=False, last_error=''):
    return {'backend': 'tofudb', 'degraded': degraded, 'last_error': last_error, 'ready': state == 'ready',
            'restarting': state == 'restarting', 'state': state}


def test_probe_separates_ready_storage_down_breaker_lifecycle_and_unanswered(tmp_path):
    config = _config(tmp_path)
    cases = {
        'ready': ({'/api/ready': (200, {'ok': True, 'ready': True, 'state': 'ready',
                                         'storage': _storage('ready')}, '')}, ('ready', True, '')),
        'storage': ({'/api/ready': (503, {'ok': False, 'ready': False, 'state': 'ready',
                                           'storage': _storage('restarting')}, '')},
                    ('storage_unavailable', False, '')),
        'breaker': ({'/api/ready': (503, {'ok': False, 'ready': False, 'state': 'ready', 'storage': _storage(
            'degraded', degraded=True, last_error='6 storage crashes/failed starts within 600s (limit 5)')}, '')},
                    ('storage_unavailable', False, '6 storage crashes/failed starts within 600s (limit 5)')),
        'lifecycle': ({'/api/ready': (503, {'ok': False, 'ready': False, 'state': 'starting',
                                             'storage': _storage('ready')}, '')}, ('not_ready', False, '')),
        'crashloop': ({'/status?': (200, {'observed': 'crashloop', 'lastFailureReason': 'worker failed 5 times',
                                           'nativeStorageService': {'state': 'ready'},
                                           'executionService': {'state': 'ready'}}, '')},
                      ('api_unanswered', False, 'manager recovery paused: worker failed 5 times')),
        'everything-down': ({}, ('api_unanswered', False, '')),
    }
    for name, (answers, (kind, available, latched)) in cases.items():
        http, calls = _fake_http(answers)
        observed = oob_alert.probe(config, now=lambda: START, http=http)
        assert (observed.kind, observed.available, observed.latched) == (kind, available, latched), name
        assert calls[0] == (config.api_url + '/api/ready', oob_alert.READINESS_PROBE_DEADLINE_SECONDS)
        # The manager is asked only when the API host did not answer.
        assert (len(calls) == 2) == (kind == 'api_unanswered'), name
    assert 'manager status unavailable' in observed.detail


def test_each_incident_is_published_as_its_out_of_band_claim_for_the_r7_adoption(tmp_path):
    # 1ece0ae3: R7's `<groupId>:<episode>` can't exist while storage is down. The watcher names the outage
    # provisionally by its own incident id and leaves logs/self-repair/oob-<id>.incident.json for
    # report_storage_fence to adopt as an alias, so the rollup sees the owner was told once and the outcome
    # once. It is not the repair claim (oob-<id>.claim), which would keep the O2 session from starting.
    timeline = ['ready'] + ['storage_unavailable'] * 11 + ['ready', 'ready']
    oob_alert.run_scenario(timeline, root=tmp_path, start=START)
    handoff = tmp_path / 'tofu' / 'logs' / 'self-repair'
    claims = sorted(handoff.glob('oob-*.incident.json'))

    assert [path.name for path in claims] == [f'oob-inc-{int(START) + 60}.incident.json']
    assert not list(handoff.glob('*.claim'))
    claim = json.loads(claims[0].read_text())
    assert claim == {'version': 1, 'key': f'inc-{int(START) + 60}', 'layer': 'out-of-band',
                     'kind': 'storage_unavailable', 'openedAt': START + 60, 'closedAt': START + 720,
                     'planned': False, 'owner': {'alert': 'sent', 'overrun': None, 'recovery': 'sent'}}
    assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', claim['key'])  # oob_repair._KEY


def test_a_later_incident_never_rewrites_an_earlier_records_delivered_outcome(tmp_path):
    timeline = ['ready'] + ['storage_unavailable'] * 11 + ['ready'] + ['storage_unavailable'] * 2 + ['ready']
    oob_alert.run_scenario(timeline, root=tmp_path, start=START)
    handoff = tmp_path / 'tofu' / 'logs' / 'self-repair'

    first = json.loads((handoff / f'oob-inc-{int(START) + 60}.incident.json').read_text())
    second = json.loads((handoff / f'oob-inc-{int(START) + 780}.incident.json').read_text())
    assert first['owner'] == {'alert': 'sent', 'overrun': None, 'recovery': 'sent'}
    assert (second['closedAt'], second['owner']) == (START + 900, {'alert': None, 'overrun': None, 'recovery': None})


def test_a_planned_restart_claim_says_planned_and_a_silent_one_is_still_published(tmp_path):
    timeline = ['ready'] + ['planned_down'] * 2 + ['planned_starting'] * 3 + ['ready']
    assert oob_alert.run_scenario(timeline, root=tmp_path, start=START, restart=PLANNED) == []
    (claim_path,) = (tmp_path / 'tofu' / 'logs' / 'self-repair').glob('oob-*.incident.json')
    claim = json.loads(claim_path.read_text())
    assert (claim['planned'], claim['closedAt'], claim['owner']) == (
        True, START + 360, {'alert': None, 'overrun': None, 'recovery': None})


class _StubRepairs:
    """Records the repair sessions the watcher starts (oob_repair.Repairs' trigger surface)."""

    def __init__(self):
        self.started: list[str] = []

    def start(self, key, detail):
        if key in self.started:
            return None
        self.started.append(key)
        return {'key': key}

    def escalations(self):
        return []

    def poll(self, open_keys):
        return []


# The 2026-11-13 02:07 recovery, verbatim (logs/server-console.log): the engine exited, the runtime
# started attempt 1 and spawned pid 107379, which opened for 627 s and became ready at 02:18:05.
RECOVERY_0207 = (
    '2026-11-13 02:07:35,827 CRITICAL Storage sidecar exited unexpectedly exit_code=1 uptime=17633.6s\n'
    '2026-11-13 02:07:35,827 WARNING Storage writes fenced after sidecar exit code=1; scheduling restart '
    '(crash 1/5 in 600s window)\n'
    '2026-11-13 02:07:35,927 INFO Storage sidecar restart attempt 1 starting (backoff reached 0.1s)\n'
    '2026-11-13 02:07:38,090 INFO Storage sidecar spawned pid={pid} backend=tofudb transport=daemon\n')


@pytest.mark.parametrize('engine', ['alive', 'gone'])
def test_a_repair_never_races_a_recovery_its_own_signals_show_progressing(tmp_path, engine):
    # 2f7f2839, O7 leg (e): at the 02:18:00 alert the runtime's attempt-1 engine was alive and opening; a
    # repair session then would have raced a recovery that finished 5 s later. Recovering = the latest
    # attempt's spawned engine is alive with no failure since; stuck = it is gone (or failed, or latched).
    clock = oob_alert.FakeClock(INCIDENT_0207)
    config = _config(tmp_path)
    console = config.logs_dir / 'server-console.log'
    console.write_text('2026-11-13 02:06:00,000 INFO scratch\n')
    sleeper = subprocess.Popen(['tofu-db', '-c', 'import time; time.sleep(600)'], executable=sys.executable)
    pid = sleeper.pid if engine == 'alive' else 999_999_999
    kinds = iter(['ready'] + ['storage_unavailable'] * 11 + ['ready'])
    watcher = oob_alert.Watcher(config, tmp_path / 'state', sender=_ScriptedSender([]), clock=clock,
                                probe_fn=lambda: oob_alert.injected_probe(next(kinds), clock()))
    watcher.repairs = _StubRepairs()
    try:
        for minute in range(13):
            if minute == 1:
                with console.open('a') as stream:
                    stream.write(RECOVERY_0207.format(pid=pid))
            watcher.tick()
            clock.now += oob_alert.PROBE_INTERVAL_S
    finally:
        sleeper.kill()
        sleeper.wait()

    events = [json.loads(line) for line in (tmp_path / 'state' / 'events.jsonl').read_text().splitlines()]
    assert [event['event'] for event in events if event['event'].startswith('alert_')] == ['alert_sent']
    if engine == 'alive':
        assert watcher.repairs.started == []
        (held,) = [event for event in events if event['event'] == 'repair_held_recovering']
        assert held['pid'] == pid
    else:
        assert watcher.repairs.started == [f'inc-{int(INCIDENT_0207) + 60}']


TIMED_OUT = (None, None, 'TimeoutError: timed out')
REFUSED = (None, None, 'ConnectionRefusedError: [Errno 111] Connection refused')


def _probe_run(config, answers, *, start=START):
    """One probe per watcher interval, each carrying the previous one, as ``Watcher.tick`` does."""
    previous, observed = None, []
    for index, answer in enumerate(answers):
        at = start + index * oob_alert.PROBE_INTERVAL_S
        result = oob_alert.probe(config, now=lambda at=at: at, http=_fake_http({'/api/ready': answer})[0],
                                 previous=previous)
        previous = dataclasses.asdict(result)
        observed.append(result)
    return observed


def test_timeouts_shorter_than_the_contract_run_are_slow_not_down(tmp_path):
    # 2026-11-13 00:22-00:35 and 2026-11-18 15:26-15:29: answers slower than the old 2 s deadline read as outages.
    config = _config(tmp_path)
    short = [TIMED_OUT] * (oob_alert.READINESS_TIMEOUT_RUN - 1)
    observed = _probe_run(config, [*short, (200, {'ready': True}, ''), *short])

    assert [(item.kind, item.available) for item in observed] == (
        [('slow', True)] * len(short) + [('ready', True)] + [('slow', True)] * len(short))
    assert [item.timeouts for item in observed] == [*range(1, len(short) + 1), 0, *range(1, len(short) + 1)]
    assert all(item.latency is not None and item.latency >= 0 for item in observed)


def test_the_contract_run_of_timeouts_is_down_from_the_first_timeout(tmp_path):
    config = _config(tmp_path)
    run = oob_alert.READINESS_TIMEOUT_RUN
    observed = _probe_run(config, [(200, {'ready': True}, '')] + [TIMED_OUT] * run)
    last = observed[-1]

    assert (last.kind, last.available, last.timeouts) == ('api_unanswered', False, run)
    assert last.since == START + oob_alert.PROBE_INTERVAL_S  # the run's first timeout
    assert f'no answer within {oob_alert.READINESS_PROBE_DEADLINE_SECONDS:g} s on {run} consecutive' in last.detail
    state: dict = {}
    oob_alert.step(state, last, [])
    assert state['incident']['openedAt'] == last.since


def test_a_refused_port_is_down_at_once(tmp_path):
    observed = _probe_run(_config(tmp_path), [REFUSED])
    assert (observed[0].kind, observed[0].available, observed[0].timeouts) == ('api_unanswered', False, 0)


def test_watcher_records_latency_and_timeout_run_per_probe(tmp_path):
    config = _config(tmp_path)
    answers = iter([TIMED_OUT, TIMED_OUT, (200, {'ready': True}, '')])
    clock = {'now': START}

    def scripted():
        return oob_alert.probe(config, now=lambda: clock['now'], http=_fake_http({'/api/ready': next(answers)})[0],
                               previous=watcher.state.get('lastProbe'))
    watcher = oob_alert.Watcher(config, tmp_path / 'state', sender=oob_alert.DryRunSender(),
                                clock=lambda: clock['now'], probe_fn=scripted)
    for _ in range(3):
        watcher.tick()
        clock['now'] += oob_alert.PROBE_INTERVAL_S
    records = [json.loads(line) for line in (tmp_path / 'state' / 'probes.jsonl').read_text().splitlines()]

    assert [(r['kind'], r['consecutiveTimeouts']) for r in records] == [('slow', 1), ('slow', 2), ('ready', 0)]
    assert all(isinstance(r['latencySeconds'], float) for r in records)


class _ReadyHandler(BaseHTTPRequestHandler):
    status = 503
    body = b''

    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(self.status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_):
        pass


def test_probe_reads_a_real_loopback_503_without_any_proxy(tmp_path, monkeypatch):
    # A configured proxy must never carry a loopback probe.
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:9')
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:9')
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setenv('NO_PROXY', '')
    # The watcher-lifecycle specs set the class attribute to 200 for good; under xdist one may run first.
    monkeypatch.setattr(_ReadyHandler, 'status', 503)
    _ReadyHandler.body = json.dumps({'ok': False, 'ready': False, 'state': 'ready', 'storage': _storage(
        'degraded', degraded=True, last_error='6 storage crashes/failed starts within 600s (limit 5)')}).encode()
    server = ThreadingHTTPServer(('127.0.0.1', 0), _ReadyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = _config(tmp_path, api_url=f'http://127.0.0.1:{server.server_address[1]}')
        observed = oob_alert.probe(config)
    finally:
        server.shutdown()
        server.server_close()
    assert observed.kind == 'storage_unavailable'
    assert observed.latched == '6 storage crashes/failed starts within 600s (limit 5)'
    assert observed.detail.startswith('/api/ready 503: storage degraded')


def test_tail_signals_matches_only_the_emitting_formats_and_follows_rotation(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    console, manager = logs / oob_alert.CONSOLE_LOG, logs / oob_alert.MANAGER_LOG
    console.write_text(BREAKER_TRIP + '\n')  # history before the first read is not an incident
    manager.write_text('')
    offsets: dict = {}
    assert oob_alert.tail_signals(logs, offsets) == []

    with console.open('a') as stream:
        stream.write('\n'.join([*DECOYS, LIVENESS_KILL, BREAKER_TRIP, DEGRADED_REMINDER]) + '\n')
        stream.write(BREAKER_TRIP[:40])  # a partial line waits for its newline
    with manager.open('a') as stream:
        stream.write(RECOVERY_PAUSED + '\n')
    found = oob_alert.tail_signals(logs, offsets)
    assert [item.kind for item in found] == ['liveness_kill', 'breaker_trip', 'breaker_latched', 'recovery_paused']
    assert found[0].at == time.mktime(time.strptime('2026-11-12 06:13:21', '%Y-%m-%d %H:%M:%S'))
    assert found[1].detail.startswith('6 storage crashes/failed starts within 600s (limit 5)')
    assert found[3].detail == 'worker recovery paused after 5 failures in 120s'

    with console.open('a') as stream:
        stream.write(BREAKER_TRIP[40:] + '\n')
    assert [item.kind for item in oob_alert.tail_signals(logs, offsets)] == ['breaker_trip']
    assert oob_alert.tail_signals(logs, offsets) == []

    replacement = logs / 'next.log'  # a rotated log is read from its start
    replacement.write_text(LIVENESS_KILL + '\n')
    os.replace(replacement, console)
    assert [item.kind for item in oob_alert.tail_signals(logs, offsets)] == ['liveness_kill']


def test_incident_is_backdated_to_the_liveness_kill_and_a_breaker_line_latches_it():
    state: dict = {'version': 1}
    kill_at = START - 100
    observed = Probe(START, False, 'storage_unavailable', '/api/ready 503: storage restarting')
    due = oob_alert.step(state, observed, [Signal('liveness_kill', kill_at, 'Storage sidecar pid=1 failed 49 '
                                                                           'consecutive liveness probes')])
    assert due == [] and state['incident']['openedAt'] == kill_at
    due = oob_alert.step(state, Probe(START + 60, False, 'storage_unavailable', 'x'),
                         [Signal('breaker_trip', START + 50, '6 storage crashes/failed starts')])
    assert [item['role'] for item in due] == ['alert']
    assert state['incident']['latched'] == '6 storage crashes/failed starts'
    assert '重启熔断已触发' in due[0]['text'] and '自 05:58 起' in due[0]['text']
    # The evidence stays in the incident record (events.jsonl), not in the short alert.
    assert set(state['incident']['evidence']) == {'liveness_kill', 'breaker_trip'}


# 2026-11-13 02:07:35 the engine exited; storage was ready again at 02:18:05 (628 s). The live watcher's
# probes: ready 02:07:00, '/api/ready 503: storage restarting' 02:08:00-02:18:00, ready 02:19:00. It never
# alerted: its last unready probe came exactly 600 s after the first and the bound was strict.
INCIDENT_0207 = time.mktime(time.strptime('2026-11-13 02:07:00', '%Y-%m-%d %H:%M:%S'))


def test_the_0207_engine_crash_alerts_at_its_tenth_minute(tmp_path):
    timeline = ['ready'] + ['storage_unavailable'] * 11 + ['ready']
    delivered = oob_alert.run_scenario(timeline, root=tmp_path, start=INCIDENT_0207)

    assert [(item['role'], item['minute']) for item in delivered] == [('alert', 11), ('recovery', 12)]
    assert delivered[0]['text'].startswith('【Tofu 停机告警】存储不可用已 10 分钟（自 02:08 起，仍未恢复）。')
    assert delivered[1]['text'] == '【Tofu 已恢复】02:19 起服务恢复正常，本次停机约 11 分钟（02:08–02:19）。'


@pytest.mark.parametrize('unready, alerts', [(10, False), (11, True)], ids=['540s-observed', '600s-observed'])
def test_an_outage_alerts_once_observed_for_ten_minutes_or_more(tmp_path, unready, alerts):
    timeline = ['ready'] + ['storage_unavailable'] * unready + ['ready']
    delivered = oob_alert.run_scenario(timeline, root=tmp_path, start=START)

    assert [item['role'] for item in delivered] == (['alert', 'recovery'] if alerts else [])


def test_the_outage_bound_includes_exactly_ten_minutes_from_the_first_failed_observation():
    state: dict = {'version': 1}
    assert oob_alert.step(state, Probe(START, False, 'storage_unavailable', 'x'), []) == []
    assert oob_alert.step(state, Probe(START + oob_alert.ALERT_AFTER_S - 1, False, 'storage_unavailable', 'x'),
                          []) == []
    due = oob_alert.step(state, Probe(START + oob_alert.ALERT_AFTER_S, False, 'storage_unavailable', 'x'), [])
    assert [item['role'] for item in due] == ['alert'] and state['incident']['openedAt'] == START


# A planned restart (GATE-R3, 2026-11-13): preflight from 05:59, the old worker exits at 06:00:30; the
# replacement serves only after a 690 s WAL replay at reopen.
PLANNED = {'startedAt': -60, 'exitAt': 30}


def test_a_planned_restart_that_overruns_sends_one_notice_and_never_a_storage_outage(tmp_path):
    timeline = ['ready'] + ['planned_down'] * 2 + ['planned_starting'] * 10 + ['ready', 'ready']
    delivered = oob_alert.run_scenario(timeline, root=tmp_path, start=START, restart=PLANNED)

    assert [(item['role'], item['minute']) for item in delivered] == [('overrun', 11), ('recovery', 13)]
    notice, recovery = (item['text'] for item in delivered)
    assert notice.splitlines()[0].startswith('【Tofu 计划重启超时】计划重启已停机 10 分钟（自 06:01 起），仍未完成')
    assert '停机告警】' not in notice and '存储不可用' not in notice and 'storage_recover' not in notice
    assert recovery == '【Tofu 已恢复】06:13 起服务恢复正常，本次计划重启停机约 12 分钟（06:01–06:13）。'


def test_a_planned_restart_inside_ten_minutes_is_silent(tmp_path):
    timeline = ['ready'] + ['planned_down'] * 2 + ['planned_starting'] * 8 + ['ready']
    assert oob_alert.run_scenario(timeline, root=tmp_path, start=START, restart=PLANNED) == []


def test_a_planned_restart_that_fails_alerts_as_an_outage_at_once(tmp_path):
    timeline = ['ready', 'planned_down', 'planned_down', 'planned_paused', 'planned_paused', 'ready']
    delivered = oob_alert.run_scenario(timeline, root=tmp_path, start=START, restart=PLANNED)

    assert [(item['role'], item['minute']) for item in delivered] == [('alert', 3), ('recovery', 5)]
    assert delivered[0]['text'].startswith('【Tofu 停机告警】')


@pytest.mark.parametrize('restart', [
    {'startedAt': 200, 'exitAt': 230},      # a recovery restart begun after the outage opened
    {'startedAt': -4000, 'exitAt': -3900},  # an earlier restart that finished before the last ready probe
], ids=['restart-during-outage', 'finished-restart'])
def test_an_outage_the_restart_did_not_cause_keeps_the_outage_alert(tmp_path, restart):
    timeline = ['ready'] + ['storage_unavailable'] * 3 + ['planned_down'] * 10 + ['ready']
    delivered = oob_alert.run_scenario(timeline, root=tmp_path, start=START, restart=restart)

    assert [item['role'] for item in delivered] == ['alert', 'recovery']
    assert delivered[0]['text'].startswith('【Tofu 停机告警】')


def _manager_state(tmp_path: Path, **fields) -> Path:
    root = tmp_path / 'tofu'
    (root / 'data').mkdir(parents=True, exist_ok=True)
    (root / 'data' / 'server-manager-state.json').write_text(json.dumps(fields), encoding='utf-8')
    return root


def test_the_planned_restart_comes_from_the_managers_own_restart_record(tmp_path):
    exit_fields = {'lastWorkerExitKind': 'planned', 'lastWorkerExitReason': 'restart', 'lastWorkerExitAt': 1000.0}
    started = {'source': 'application-restart', 'outcome': 'started', 'startedAt': 900.0, 'requestedAt': 899.0}
    spawned = {**started, 'outcome': 'spawned', 'settledAt': 1100.0, 'workerPid': 7}
    cases = {
        'in-progress': ({**exit_fields, 'lastRestart': started}, 'started'),
        'replacement-booting': ({**exit_fields, 'lastRestart': spawned, 'workerReadyLoggedAt': 0.0}, 'spawned'),
        'replacement-ready': ({**exit_fields, 'lastRestart': spawned, 'workerReadyLoggedAt': 1200.0}, None),
        'paused': ({**exit_fields, 'observed': 'crashloop',
                    'lastRestart': {**started, 'outcome': 'paused', 'settledAt': 1100.0}}, 'paused'),
        'paused-then-recovered': ({**exit_fields, 'observed': 'running',
                                   'lastRestart': {**started, 'outcome': 'paused', 'settledAt': 1100.0}}, None),
        'refused': ({**exit_fields, 'lastRestart': {**started, 'outcome': 'refused'}}, None),
        'crash-exit': ({**exit_fields, 'lastWorkerExitKind': 'failure', 'lastRestart': started}, None),
        'exit-before-the-restart': ({**exit_fields, 'lastWorkerExitAt': 800.0, 'lastRestart': started}, None),
    }
    for name, (fields, outcome) in cases.items():
        record = oob_alert.planned_restart(_manager_state(tmp_path / name, **fields))
        assert (record.get('outcome') if record else None) == outcome, name
        if record:
            assert (record['startedAt'], record['exitAt']) == (900.0, 1000.0), name
    assert oob_alert.planned_restart(tmp_path / 'no-manager') == {}
    root = _manager_state(tmp_path / 'corrupt')
    (root / 'data' / 'server-manager-state.json').write_text('{', encoding='utf-8')
    assert oob_alert.planned_restart(root) == {}


def test_an_unavailable_probe_carries_the_managers_planned_restart(tmp_path):
    root = _manager_state(tmp_path, lastWorkerExitKind='planned', lastWorkerExitAt=START - 30, lastRestart={
        'source': 'serverctl', 'outcome': 'started', 'startedAt': START - 90})
    config = Config(tofu_root=root)
    (root / 'logs').mkdir()
    for answers, kind in (({}, 'api_unanswered'),
                          ({'/api/ready': (503, {'ready': False, 'state': 'starting',
                                                 'storage': _storage('not_started')}, '')}, 'storage_unavailable')):
        observed = oob_alert.probe(config, now=lambda: START, http=_fake_http(answers)[0])
        assert observed.kind == kind and observed.restart['outcome'] == 'started'
    ready = oob_alert.probe(config, now=lambda: START, http=_fake_http(
        {'/api/ready': (200, {'ready': True, 'storage': _storage('ready')}, '')})[0])
    assert ready.available and ready.restart == {}


class _ScriptedSender:
    name = 'scripted'

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.texts: list[str] = []

    def send(self, text):
        self.texts.append(text)
        return self.outcomes.pop(0) if self.outcomes else ('sent', 'ok')


def _ticks(tmp_path, sender, timeline):
    clock = oob_alert.FakeClock(START)
    kinds = iter(timeline)
    watcher = oob_alert.Watcher(_config(tmp_path), tmp_path / 'state', sender=sender, clock=clock,
                                probe_fn=lambda: oob_alert.injected_probe(next(kinds), clock()))
    for _ in timeline:
        watcher.tick()
        clock.now += oob_alert.PROBE_INTERVAL_S
    return watcher


def test_a_refused_send_is_retried_and_an_unknown_outcome_is_never_resent(tmp_path):
    sender = _ScriptedSender([('not_sent', 'token_rejected'), ('unknown', 'robot_unknown: no response')])
    watcher = _ticks(tmp_path / 'a', sender, ['breaker'] * 5)
    assert len(sender.texts) == 2  # refused once, then an unknown outcome closes the alert
    assert watcher.state['incident']['alert']['state'] == 'unknown'

    sender = _ScriptedSender([('not_sent', 'token_rejected')])
    watcher = _ticks(tmp_path / 'b', sender, ['breaker', 'breaker', 'breaker', 'ready', 'ready'])
    assert [text.split('】')[0] + '】' for text in sender.texts] == ['【Tofu 停机告警】', '【Tofu 停机告警】',
                                                                  '【Tofu 已恢复】']
    assert watcher.state['recovery']['state'] == 'sent'


def test_without_an_enabled_path_the_alert_is_recorded_once_as_undeliverable(tmp_path):
    watcher = _ticks(tmp_path, oob_alert.NoSender(), ['breaker'] * 4 + ['ready'])
    events = [json.loads(line) for line in (tmp_path / 'state' / 'events.jsonl').read_text().splitlines()]
    assert [event['event'] for event in events] == ['incident_opened', 'alert_disabled', 'incident_closed']
    assert watcher.state.get('recovery') is None  # nothing was delivered, so nothing recovers
    probes = (tmp_path / 'state' / 'probes.jsonl').read_text().splitlines()
    assert len(probes) == 5 and json.loads(probes[-1])['kind'] == 'ready'


def test_vault_reader_decrypts_what_the_vault_module_stores_and_never_mints(tmp_path, monkeypatch):
    from lib import credentials_vault

    vault = tmp_path / 'config'
    vault.mkdir()
    monkeypatch.setattr(credentials_vault, '_STORE_PATH', vault / 'credentials_vault.json')
    monkeypatch.setattr(credentials_vault, '_KEY_PATH', vault / '.credentials_vault.key')
    monkeypatch.setattr(credentials_vault, '_fernet', None)
    credentials_vault.set_entry('channel.u1.dx.robot', 'robot-secret-value')

    assert oob_alert.read_vault_entry(vault, 'channel.u1.dx.robot') == 'robot-secret-value'
    assert oob_alert.read_vault_entry(vault, 'channel.u1.wechat.token') is None

    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        oob_alert.read_vault_entry(empty, 'channel.u1.dx.robot')
    assert list(empty.iterdir()) == []


class _FakeOpenPlatform:
    """Token endpoint and group send of the 大象 open platform, in memory."""

    def __init__(self, *, token_status=200, post=None):
        self.requests: list[tuple[str, dict, bytes]] = []
        self.token_status = token_status
        self.post = post

    def __call__(self, method, url, headers, body):
        self.requests.append((url, headers, body))
        if url.endswith('/token'):
            if self.token_status != 200:
                return self.token_status, b'{"error": "invalid_client"}'
            return 200, json.dumps({'access_token': 'access', 'token_type': 'Bearer', 'expires_in': 3600}).encode()
        if self.post is not None:
            return self.post(url, body)
        gid = str(json.loads(body)['gid'])
        return 200, json.dumps({'status': {'code': 0}, 'data': {'messageMap': {gid: 'msg-1'}}}).encode()


def _robot_config(tmp_path):
    return _config(tmp_path, dx_robot={'enabled': True, 'clientId': 'client-1', 'gid': '71158539432',
                                       'credentialName': 'channel.u1.dx.robot'})


def test_robot_sender_posts_the_text_to_the_bound_group(tmp_path):
    platform = _FakeOpenPlatform()
    sender = oob_alert.DxRobotSender(_robot_config(tmp_path), transport=platform, secret=lambda: 'secret')
    outcome, detail = sender.send('告警文本')
    assert outcome == 'sent' and detail.startswith('gid 71158539432 message msg-1; HTTP 200 ')
    assert '"messageMap": {"71158539432": "msg-1"}' in detail
    assert 'access' not in detail  # the token endpoint's answer is never kept
    token_url, _, token_form = platform.requests[0]
    assert token_url == 'https://ssosv.internal.example.com/sson/auth/oidc/v1/token' and b'client_id=client-1' in token_form
    send_url, headers, body = platform.requests[1]
    assert send_url == 'https://xopen.internal.example.com/open-apis/dx-msg/sendGroupMsgByRobot'
    assert headers['Authorization'] == 'Bearer access'
    sent = json.loads(body)
    assert sent['gid'] == 71158539432 and json.loads(sent['sendMsgInfo']['body']) == {'text': '告警文本'}


def test_robot_sender_maps_refusal_to_retry_and_a_lost_answer_to_unknown(tmp_path):
    refused = oob_alert.DxRobotSender(_robot_config(tmp_path), transport=_FakeOpenPlatform(token_status=401),
                                      secret=lambda: 'secret')
    assert refused.send('x')[0] == 'not_sent'

    def lost(url, body):
        raise TimeoutError('read timed out')
    unknown = oob_alert.DxRobotSender(_robot_config(tmp_path), transport=_FakeOpenPlatform(post=lost),
                                      secret=lambda: 'secret')
    assert unknown.send('x')[0] == 'unknown'

    def missing_secret():
        raise LookupError('vault entry channel.u1.dx.robot is empty')
    no_secret = oob_alert.DxRobotSender(_robot_config(tmp_path), transport=_FakeOpenPlatform(),
                                        secret=missing_secret)
    assert no_secret.send('x')[0] == 'not_sent'


def test_the_watcher_and_its_robot_path_never_import_the_storage_client(tmp_path):
    script = f"""
import json, sys
sys.path.insert(0, {str(REPO)!r})
from pathlib import Path
from serverctl_pkg import oob_alert
config = oob_alert.Config(tofu_root=Path({str(tmp_path)!r}), api_url='http://127.0.0.1:9',
                          manager_url='http://127.0.0.1:9',
                          dx_robot={{'enabled': True, 'clientId': 'c', 'gid': '1', 'credentialName': 'n'}})
def transport(method, url, headers, body):
    if url.endswith('/token'):
        return 200, b'{{"access_token": "a", "token_type": "Bearer", "expires_in": 60}}'
    return 200, b'{{"status": {{"code": 0}}, "data": {{"messageMap": {{"1": "m"}}}}}}'
sender = oob_alert.DxRobotSender(config, transport=transport, secret=lambda: 's')
watcher = oob_alert.Watcher(config, Path({str(tmp_path / 'state')!r}), sender=sender)
watcher.tick()
print(json.dumps({{'outcome': sender.send('x')[0],
                  'storage': sorted(m for m in sys.modules if m.startswith('lib.storage'))}}))
"""
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, env=env, timeout=120,
                            check=True)
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    assert answer == {'outcome': 'sent', 'storage': []}


def test_named_constants_match_their_owning_sources():
    import server_manager
    from lib.http_transport import client

    from serverctl_pkg import readiness_liveness

    readiness = inspect.signature(server_manager.probe_application_readiness).parameters['timeout'].default
    assert oob_alert.READINESS_PROBE_DEADLINE_SECONDS == readiness == readiness_liveness.READINESS_PROBE_DEADLINE_SECONDS
    assert oob_alert.READINESS_TIMEOUT_RUN == readiness_liveness.READINESS_TIMEOUT_RUN
    assert oob_alert.MANAGER_STATE_FILE == Path('data') / server_manager.MANAGER_STATE_FILE
    assert oob_alert.RESTART_IN_PROGRESS == server_manager.RESTART_IN_PROGRESS
    assert oob_alert.ROBOT_REQUEST_TIMEOUT_S == client._DEFAULT_TIMEOUT


RECOVER = '/ops/storage_recover.sh'


def test_the_alert_says_what_is_down_since_when_and_what_to_do():
    incident = {'openedAt': START, 'latched': 'storm'}
    text = oob_alert.alert_text(incident, Probe(START + 660, False, 'storage_unavailable', 'x'),
                                oob_alert.action_line(Config(tofu_root=REPO, recover_command=RECOVER)))
    assert text.splitlines() == [
        '【Tofu 停机告警】存储不可用已 11 分钟（自 06:00 起，重启熔断已触发，仍未恢复）。',
        f'处理：在终端运行 {RECOVER}（编译引擎前会问 [y/N]，默认 N）。',
        '恢复后会再通知（外部告警，不经过 TofuDB）。']
    assert oob_alert.recovery_text(incident, START + 1560) == (
        '【Tofu 已恢复】06:26 起服务恢复正常，本次停机约 26 分钟（06:00–06:26）。')


def test_the_test_message_goes_out_at_most_once_and_never_touches_dedup_state(tmp_path, monkeypatch, capsys):
    sender = _ScriptedSender([])
    monkeypatch.setattr(oob_alert, 'configured_sender', lambda config: sender)
    state = tmp_path / 'state'
    state.mkdir()
    open_incident = {'version': 1, 'incident': {'id': 'inc-1', 'openedAt': START, 'alert': {'state': 'pending'}}}
    (state / 'state.json').write_text(json.dumps(open_incident))
    config = _config(tmp_path, recover_command=RECOVER)
    assert oob_alert.command_test_message(config, state) == 0
    assert oob_alert.command_test_message(config, state) == 1
    [text] = sender.texts
    lines = text.splitlines()
    assert lines[0].startswith('测试') and lines[1].startswith('【Tofu 停机告警】存储不可用已 11 分钟（自 ')
    assert lines[2] == oob_alert.action_line(config) and RECOVER in lines[2]
    assert json.loads((state / 'state.json').read_text()) == open_incident  # dedup state untouched
    marker = json.loads((state / 'test-message.json').read_text())
    assert marker['outcome'] == 'sent' and marker['text'] == text

    monkeypatch.setattr(oob_alert, 'configured_sender', lambda config: oob_alert.NoSender())
    other = tmp_path / 'other'
    other.mkdir()
    assert oob_alert.command_test_message(_config(tmp_path), other) == 1
    assert not (other / 'test-message.json').exists()


def _wait_for(predicate, deadline_s=60.0):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_run_status_and_stop_manage_one_real_watcher_process(tmp_path):
    _ReadyHandler.status = 200
    _ReadyHandler.body = json.dumps({'ok': True, 'ready': True, 'state': 'ready', 'storage': _storage('ready')}
                                    ).encode()
    server = ThreadingHTTPServer(('127.0.0.1', 0), _ReadyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = tmp_path / 'state'
    state.mkdir()
    (tmp_path / 'tofu' / 'logs').mkdir(parents=True)
    (state / 'config.json').write_text(json.dumps({'tofuRoot': str(tmp_path / 'tofu'),
                                                   'apiUrl': f'http://127.0.0.1:{server.server_address[1]}'}))
    command = [sys.executable, str(REPO / 'serverctl_pkg' / 'oob_alert.py'), '--state-dir', str(state)]
    watcher = subprocess.Popen([*command, 'run'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert _wait_for(lambda: (state / 'probes.jsonl').exists() and (state / 'watcher.pid').exists())
        second = subprocess.run([*command, 'run'], capture_output=True, text=True, timeout=60)
        assert second.returncode == 1 and 'another watcher holds' in second.stderr
        status = subprocess.run([*command, 'status'], capture_output=True, text=True, timeout=60)
        assert status.returncode == 0 and f'running, pid {watcher.pid}' in status.stdout
        assert 'delivery path: none' in status.stdout and 'ready: ready' in status.stdout
        stop = subprocess.run([*command, 'stop'], capture_output=True, text=True, timeout=60)
        assert stop.returncode == 0 and 'stopped' in stop.stdout
        assert watcher.wait(timeout=30) == 0
    finally:
        if watcher.poll() is None:
            watcher.kill()
        server.shutdown()
        server.server_close()
    assert not (state / 'watcher.pid').exists()
    events = [json.loads(line)['event'] for line in (state / 'events.jsonl').read_text().splitlines()]
    assert events == ['watcher_started', 'watcher_stopped']
    stopped = subprocess.run([*command, 'status'], capture_output=True, text=True, timeout=60)
    assert stopped.returncode == 3 and 'NOT running' in stopped.stdout


def _installed_with_repairs(tmp_path: Path, package: Path) -> tuple[Path, Path]:
    """An installed watcher copy (as the ops doc installs it) whose config enables the repair agent."""
    state, tofu = tmp_path / 'state', tmp_path / 'tofu'
    state.mkdir()
    (tofu / 'logs').mkdir(parents=True)
    (tofu / 'serverctl_pkg').symlink_to(package, target_is_directory=True)
    copy = state / 'oob_alert.py'
    copy.write_bytes((REPO / 'serverctl_pkg' / 'oob_alert.py').read_bytes())
    (state / 'config.json').write_text(json.dumps({
        'tofuRoot': str(tofu), 'apiUrl': 'http://127.0.0.1:9',
        'repairAgent': {'enabled': True, 'harness': ['true'], 'runner': 'direct'}}))
    return copy, state


def test_an_installed_copy_run_isolated_loads_the_repair_layer_from_the_checkout(tmp_path):
    copy, state = _installed_with_repairs(tmp_path, REPO / 'serverctl_pkg')
    done = subprocess.run([sys.executable, '-I', str(copy), '--state-dir', str(state), 'once', '--inject', 'ready'],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert 'probe: ready: ready' in done.stdout


def test_a_broken_repair_layer_in_the_checkout_never_stops_the_alert_path(tmp_path):
    # The checkout may carry the very bug being repaired: the watcher alerts without its repair layer.
    broken = tmp_path / 'broken_pkg'
    broken.mkdir()
    for module in (REPO / 'serverctl_pkg').glob('*.py'):
        (broken / module.name).write_bytes(module.read_bytes())
    (broken / 'oob_repair.py').write_text('def broken(:\n')
    copy, state = _installed_with_repairs(tmp_path, broken)
    done = subprocess.run([sys.executable, '-I', str(copy), '--state-dir', str(state), 'once',
                           '--inject', 'storage_unavailable'], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert 'probe: storage_unavailable' in done.stdout and 'repair layer unavailable: SyntaxError' in done.stdout


class _RevealApi(BaseHTTPRequestHandler):
    """The audited loopback reveal endpoints (oob_relay.RevealKeySource) of a scratch Tofu: a GET
    listing and a POST-only reveal, as in production (a GET reveal is refused with 405)."""
    calls: list[str] = []
    secret = 'sk-scratch-project-credential-199c'

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 - http.server API
        _RevealApi.calls.append(self.path)
        if self.path == '/api/v4/model-routing':
            self._json(200, {'data': {'model_routing': {
                'credentials': [{'credential_id': 'c1', 'provider_access_id': 'pa1', 'kind': 'api_key', 'enabled': True}],
                'provider_accesses': [{'provider_access_id': 'pa1', 'provider_id': 'example-corp'}]}}})
        else:
            self._json(405, {'error': 'method not allowed'})

    def do_POST(self):  # noqa: N802 - http.server API
        _RevealApi.calls.append(self.path)
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if self.path == '/api/v4/model-routing/credentials/c1/secret/reveal' and _RevealApi.secret:
            self._json(200, {'data': {'secret': json.dumps({'api_key': _RevealApi.secret})}})
        else:
            self._json(404, {'error': 'not found'})

    def log_message(self, *_):
        pass


def test_the_watcher_owns_the_relay_reveals_once_at_start_and_again_after_a_verified_recovery(tmp_path):
    _RevealApi.calls, _RevealApi.secret = [], 'sk-scratch-project-credential-199c'
    api = ThreadingHTTPServer(('127.0.0.1', 0), _RevealApi)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    (tmp_path / 'tofu' / 'logs').mkdir(parents=True)
    config = Config(tofu_root=tmp_path / 'tofu', api_url=f'http://127.0.0.1:{api.server_address[1]}',
                    repair_agent={'enabled': True, 'harness': ['true']})
    kinds = iter(['storage_unavailable', 'ready'])
    clock = oob_alert.FakeClock(START)
    watcher = oob_alert.Watcher(config, tmp_path / 'state', sender=_ScriptedSender([]), clock=clock,
                                probe_fn=lambda: oob_alert.injected_probe(next(kinds), clock()))
    try:
        assert watcher.start_relay() == '…199c'
        assert (tmp_path / 'state' / 'relay.sock').is_socket()
        assert watcher.repairs.runner.keywords == {'relay_socket': tmp_path / 'state' / 'relay.sock'}
        assert len(_RevealApi.calls) == 2  # eager, while Tofu is up: one listing, one audited reveal
        watcher.tick()
        clock.now += oob_alert.PROBE_INTERVAL_S
        watcher.tick()  # the incident closed: a verified recovery reveals again
        assert _RevealApi.calls.count('/api/v4/model-routing/credentials/c1/secret/reveal') == 2
    finally:
        watcher.close()
        api.shutdown()
        api.server_close()
    assert not (tmp_path / 'state' / 'relay.sock').exists()
    secret = _RevealApi.secret
    assert all(secret not in path.read_text(errors='replace') for path in tmp_path.rglob('*') if path.is_file())


def test_no_tick_ever_asks_the_api_for_a_credential(tmp_path):
    # 11:44:11: a tick resolved the relay's key lazily against the configured API. Only start_relay()
    # (the watcher's run) and a verified recovery after it may reveal.
    _RevealApi.calls, _RevealApi.secret = [], 'sk-scratch-project-credential-199c'
    api = ThreadingHTTPServer(('127.0.0.1', 0), _RevealApi)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    (tmp_path / 'tofu' / 'logs').mkdir(parents=True)
    config = Config(tofu_root=tmp_path / 'tofu', api_url=f'http://127.0.0.1:{api.server_address[1]}',
                    repair_agent={'enabled': True, 'harness': ['true']})
    clock = oob_alert.FakeClock(START)
    kinds = iter(['breaker', 'breaker', 'ready'])
    watcher = oob_alert.Watcher(config, tmp_path / 'state', sender=_ScriptedSender([]), clock=clock,
                                probe_fn=lambda: oob_alert.injected_probe(next(kinds), clock()))
    try:
        for _ in range(3):
            watcher.tick()
            clock.now += oob_alert.PROBE_INTERVAL_S
    finally:
        watcher.close()
        api.shutdown()
        api.server_close()
    assert _RevealApi.calls == []
    assert not (tmp_path / 'state' / 'repair').exists()


def test_without_a_model_credential_an_incident_is_alert_only(tmp_path):
    _RevealApi.calls, _RevealApi.secret = [], ''
    (tmp_path / 'tofu' / 'logs').mkdir(parents=True)
    config = Config(tofu_root=tmp_path / 'tofu', api_url='http://127.0.0.1:9',
                    repair_agent={'enabled': True, 'harness': ['true']})
    clock = oob_alert.FakeClock(START)
    watcher = oob_alert.Watcher(config, tmp_path / 'state', sender=_ScriptedSender([]), clock=clock,
                                probe_fn=lambda: oob_alert.injected_probe('breaker', clock()))
    watcher.repairs = _StubRepairs()
    try:
        assert watcher.start_relay() == ''
        watcher.tick()  # a latched breaker alerts at once; with no credential no session starts
    finally:
        watcher.close()
    events = [json.loads(line) for line in (tmp_path / 'state' / 'events.jsonl').read_text().splitlines()]
    assert watcher.repairs.started == []
    assert [event['event'] for event in events if event['event'].startswith(('alert_', 'repair_'))] == [
        'repair_unavailable', 'alert_sent']


def test_the_once_dry_run_never_starts_a_repair_session(tmp_path):
    copy, state = _installed_with_repairs(tmp_path, REPO / 'serverctl_pkg')
    done = subprocess.run([sys.executable, '-I', str(copy), '--state-dir', str(state), 'once', '--inject', 'breaker'],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert 'alert -> sent (dry run: recorded, not sent)' in done.stdout
    assert 'repair layer: not exercised by a dry run' in done.stdout
    assert not (tmp_path / 'tofu' / 'logs' / 'self-repair').exists()


def test_start_returns_once_the_watcher_logged_its_start_never_after_a_fixed_sleep(tmp_path):
    # 2026-11-13 10:08: a deploy check that slept 6 s judged a slow (7 s) start as failed and rolled back.
    _ReadyHandler.status = 200
    _ReadyHandler.body = json.dumps({'ok': True, 'ready': True, 'state': 'ready', 'storage': _storage('ready')}
                                    ).encode()
    server = ThreadingHTTPServer(('127.0.0.1', 0), _ReadyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = tmp_path / 'state'
    state.mkdir()
    (tmp_path / 'tofu' / 'logs').mkdir(parents=True)
    (state / 'config.json').write_text(json.dumps({'tofuRoot': str(tmp_path / 'tofu'),
                                                   'apiUrl': f'http://127.0.0.1:{server.server_address[1]}'}))
    command = [sys.executable, str(REPO / 'serverctl_pkg' / 'oob_alert.py'), '--state-dir', str(state)]
    digest = hashlib.sha256((REPO / 'serverctl_pkg' / 'oob_alert.py').read_bytes()).hexdigest()[:16]
    try:
        started = subprocess.run([*command, 'start'], capture_output=True, text=True, timeout=120)
        assert started.returncode == 0, started.stderr
        event = json.loads((state / 'events.jsonl').read_text().splitlines()[-1])
        assert (event['event'], event['sha256']) == ('watcher_started', digest)  # logged before start returned
        assert started.stdout.strip() == f'watcher started: pid {event["pid"]}, source sha256 {digest}'
        assert (state / 'watcher.pid').read_text().strip() == str(event['pid'])
        again = subprocess.run([*command, 'start'], capture_output=True, text=True, timeout=120)
        assert again.returncode == 1 and 'another watcher holds' in again.stderr
    finally:
        subprocess.run([*command, 'stop'], capture_output=True, text=True, timeout=60)
        server.shutdown()
        server.server_close()
    assert [json.loads(line)['event'] for line in (state / 'events.jsonl').read_text().splitlines()] == [
        'watcher_started', 'watcher_stopped']


# ── Part 2: direct breaker-trip notification and the Watchdog dead-man ──

POST_TRIP_BREAKER = ('2026-11-12 06:13:39,928 CRITICAL Storage restart circuit breaker tripped: 6 storage '
                     'crashes/failed starts within 600s (limit 5); last restart error: RuntimeError: x. Storage '
                     'stays fenced (degraded); the start is retried after 60s, backing off to one attempt per '
                     '600s breaker window, and a process restart retries at once')


def test_both_breaker_trip_wordings_are_read(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    console = logs / oob_alert.CONSOLE_LOG
    console.write_text('')
    offsets: dict = {}
    oob_alert.tail_signals(logs, offsets)
    console.write_text(BREAKER_TRIP + '\n' + POST_TRIP_BREAKER + '\n')
    found = oob_alert.tail_signals(logs, offsets)
    assert [item.kind for item in found] == ['breaker_trip', 'breaker_trip']
    assert found[1].detail == ('6 storage crashes/failed starts within 600s (limit 5); last restart error: '
                               'RuntimeError: x')


def _notify_config(tmp_path):
    (tmp_path / 'tofu' / 'data').mkdir(parents=True, exist_ok=True)
    return _config(tmp_path)


def test_a_trip_notification_for_another_authority_sends_nothing(tmp_path, monkeypatch):
    sender = _ScriptedSender([])
    monkeypatch.setattr(oob_alert, 'configured_sender', lambda config: sender)
    state = tmp_path / 'state'
    state.mkdir()
    assert oob_alert.command_notify(_notify_config(tmp_path), state, reason='storm',
                                    data_dir=tmp_path / 'pytest-store' / 'data') == 0
    assert sender.texts == [] and not (state / 'trip-alert.json').exists()


def test_a_trip_notification_alerts_once_for_the_whole_outage(tmp_path, monkeypatch):
    sender = _ScriptedSender([])
    monkeypatch.setattr(oob_alert, 'configured_sender', lambda config: sender)
    config = _notify_config(tmp_path)
    state = tmp_path / 'state'
    state.mkdir()
    reason = '6 storage crashes/failed starts within 600s (limit 5)'
    assert oob_alert.command_notify(config, state, reason=reason, data_dir=tmp_path / 'tofu' / 'data') == 0
    assert len(sender.texts) == 1 and '重启熔断已触发' in sender.texts[0]
    assert json.loads((state / 'trip-alert.json').read_text())['outcome'] == 'sent'

    # The watcher's own latched alert for the same outage is the notification already sent.
    watcher = oob_alert.Watcher(config, state, sender=sender,
                                probe_fn=lambda: oob_alert.injected_probe('breaker', time.time()))
    [outcome] = watcher.tick()
    assert outcome['role'] == 'alert' and outcome['outcome'] == 'sent'
    assert 'breaker-trip notification' in outcome['detail'] and len(sender.texts) == 1

    # A second trip in the same alerted outage is skipped.
    assert oob_alert.command_notify(config, state, reason=reason, data_dir=tmp_path / 'tofu' / 'data') == 0
    events = [json.loads(line)['event'] for line in (state / 'events.jsonl').read_text().splitlines()]
    assert events == ['trip_alert_sent', 'incident_opened', 'alert_sent', 'trip_alert_skipped']
    assert len(sender.texts) == 1


def test_notify_trip_starts_a_detached_child_that_records_the_trip(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (tmp_path / 'tofu' / 'data').mkdir(parents=True)
    (state / 'config.json').write_text(json.dumps({'tofuRoot': str(tmp_path / 'tofu')}))
    assert oob_alert.notify_trip('storm', data_dir=tmp_path / 'other' / 'data', state_dir=state)
    assert oob_alert.notify_trip('storm', data_dir=tmp_path / 'tofu' / 'data', state_dir=state)
    assert _wait_for(lambda: (state / 'trip-alert.json').exists())
    record = json.loads((state / 'trip-alert.json').read_text())
    assert record['outcome'] == 'disabled' and record['reason'] == 'storm'  # no path enabled: recorded only
    assert not oob_alert.notify_trip('storm', data_dir=tmp_path / 'tofu' / 'data', state_dir=tmp_path / 'none')


def test_a_stale_heartbeat_alerts_once_per_review_and_waits_for_an_open_outage():
    state: dict = {'version': 1}
    beat = {'version': 1, 'owner': 1, 'at': START, 'schedule': '7,37 * * * *', 'enabled': True,
            'staleAfter': START + 100}
    assert oob_alert.heartbeat_step(state, [beat], START + 50, outage_open=False) == []
    assert oob_alert.heartbeat_step(state, [beat], START + 150, outage_open=True) == []
    [due] = oob_alert.heartbeat_step(state, [beat], START + 160, outage_open=False)
    assert due['role'] == 'heartbeat_alert' and '巡检停止' in due['text']
    oob_alert.record_delivery(state, due, 'sent', 'ok', START + 160)
    assert oob_alert.heartbeat_step(state, [beat], START + 220, outage_open=False) == []
    disabled = {**beat, 'at': START + 300, 'enabled': False, 'staleAfter': None}
    [notice] = oob_alert.heartbeat_step(state, [disabled], START + 300, outage_open=False)
    assert notice['role'] == 'heartbeat_recovery' and '关闭' in notice['text']
    oob_alert.record_delivery(state, notice, 'sent', 'ok', START + 300)
    assert oob_alert.heartbeat_step(state, [disabled], START + 360, outage_open=False) == []
    assert state['heartbeats'] == {}


class _FakeILink:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests: list[tuple[str, dict, dict]] = []

    def __call__(self, method, url, headers, body):
        self.requests.append((url, headers, json.loads(body)))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _wechat_config(tmp_path, monkeypatch, *, reply=True):
    from lib import credentials_vault
    vault = tmp_path / 'tofu' / 'data' / 'config'
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(credentials_vault, '_STORE_PATH', vault / 'credentials_vault.json')
    monkeypatch.setattr(credentials_vault, '_KEY_PATH', vault / '.credentials_vault.key')
    monkeypatch.setattr(credentials_vault, '_fernet', None)
    credentials_vault.set_entry('channel.u1.wechat.token', 'bot-token-value')
    if reply:
        credentials_vault.set_entry('channel.u1.wechat.reply',
                                    json.dumps({'to': 'owner@im.wechat', 'contextToken': 'ctx-1'}))
    return _config(tmp_path, wechat={'enabled': True, 'credentialName': 'channel.u1.wechat.token',
                                     'replyEntry': 'channel.u1.wechat.reply'})


def test_wechat_sender_replies_into_the_command_chat_with_the_mirrored_context(tmp_path, monkeypatch):
    ilink = _FakeILink([(200, b'{"ret": 0, "message_id": "m-1"}')])
    sender = oob_alert.WeChatSender(_wechat_config(tmp_path, monkeypatch), transport=ilink)
    assert sender.send('告警') == ('sent', '微信 message m-1')
    url, headers, body = ilink.requests[0]
    assert url == 'https://ilinkai.weixin.qq.com/ilink/bot/sendmessage'
    assert headers['Authorization'] == 'Bearer bot-token-value' and headers['AuthorizationType'] == 'ilink_bot_token'
    assert body['msg']['to_user_id'] == 'owner@im.wechat' and body['msg']['context_token'] == 'ctx-1'
    assert body['msg']['item_list'] == [{'type': 1, 'text_item': {'text': '告警'}}]


def test_wechat_sender_refusals_are_definite_and_a_lost_answer_is_unknown(tmp_path, monkeypatch):
    import urllib.error
    config = _wechat_config(tmp_path, monkeypatch)
    expired = oob_alert.WeChatSender(config, transport=_FakeILink([(200, b'{"ret": -2}')])).send('x')
    assert expired[0] == 'not_sent' and 'context token expired' in expired[1]
    refused = oob_alert.WeChatSender(config, transport=_FakeILink([urllib.error.URLError(ConnectionRefusedError())]))
    assert refused.send('x')[0] == 'not_sent'
    lost = oob_alert.WeChatSender(config, transport=_FakeILink([TimeoutError('read timed out')]))
    assert lost.send('x')[0] == 'unknown'
    no_reply = oob_alert.WeChatSender(_wechat_config(tmp_path / 'b', monkeypatch, reply=False),
                                      transport=_FakeILink([]))
    assert no_reply.send('x') == ('not_sent', 'no 微信 bot token or command-chat reply context in the vault')


def test_the_robot_goes_first_and_wechat_is_the_best_effort_second_path(tmp_path, monkeypatch):
    first = _ScriptedSender([('not_sent', 'token_rejected')])
    second = _ScriptedSender([('sent', 'ok')])
    first.name, second.name = 'dx-robot', 'wechat'
    chain = oob_alert.ChainSender([first, second])
    assert chain.send('x') == ('sent', 'dx-robot: token_rejected; wechat: ok')
    assert chain.name == 'dx-robot+wechat'
    config = _wechat_config(tmp_path, monkeypatch)
    both = Config(tofu_root=config.tofu_root, wechat=config.wechat,
                  dx_robot={'enabled': True, 'clientId': 'c', 'gid': '1', 'credentialName': 'n'})
    assert oob_alert.configured_sender(both).name == 'dx-robot+wechat'
    assert oob_alert.configured_sender(config).name == 'wechat'
    assert isinstance(oob_alert.configured_sender(Config(tofu_root=config.tofu_root)), oob_alert.NoSender)


# ── acceptance case (5): the out-of-band path must not depend on the disk that is filling ──

def test_a_full_disk_never_stops_the_alert_or_its_recovery_notice(tmp_path):
    """2026-11-12 22:13: the root overlay was at 98% and filling. Every local
    write failing with ENOSPC must still yield exactly one alert and one
    recovery notice; dedup holds in memory and the failures are counted."""
    sender = _ScriptedSender([])
    clock = oob_alert.FakeClock(START)
    kinds = iter(['breaker', 'breaker', 'breaker', 'ready', 'ready'])
    watcher = oob_alert.Watcher(_config(tmp_path), tmp_path / 'state', sender=sender, clock=clock,
                                probe_fn=lambda: oob_alert.injected_probe(next(kinds), clock()),
                                fail_writes=True)
    for _ in range(5):
        watcher.tick()
        clock.now += oob_alert.PROBE_INTERVAL_S
    assert [text.split('】')[0] + '】' for text in sender.texts] == ['【Tofu 停机告警】', '【Tofu 已恢复】']
    assert watcher.state['writeFailures']['count'] >= 10
    assert 'No space left on device' in watcher.state['writeFailures']['last']
    assert not (tmp_path / 'state' / 'state.json').exists()  # nothing could be written


def test_the_run_loop_outlives_a_failing_tick_on_a_full_disk(tmp_path):
    stop = threading.Event()
    calls = []

    def probe_fn():
        calls.append(1)
        if len(calls) >= 2:
            stop.set()
        raise RuntimeError('probe exploded')
    watcher = oob_alert.Watcher(_config(tmp_path), tmp_path / 'state', sender=_ScriptedSender([]),
                                probe_fn=probe_fn, fail_writes=True)
    original = oob_alert.PROBE_INTERVAL_S
    oob_alert.PROBE_INTERVAL_S = 0.01
    try:
        watcher.run(stop)  # returns instead of dying on the tick_failed log write
    finally:
        oob_alert.PROBE_INTERVAL_S = original
    assert len(calls) == 2


def test_the_default_state_dir_is_off_the_root_overlay_and_private(tmp_path):
    assert oob_alert.DEFAULT_STATE_DIR == Path('/tmp') / f'tofu-oob-alert-{os.getuid()}'
    target = tmp_path / 'elsewhere'
    target.mkdir()
    link = tmp_path / 'state'
    link.symlink_to(target)
    with pytest.raises(PermissionError, match='not a private directory'):
        oob_alert.Watcher(_config(tmp_path), link, sender=_ScriptedSender([]))
    loose = tmp_path / 'loose'
    loose.mkdir(mode=0o777)
    os.chmod(loose, 0o777)
    oob_alert.Watcher(_config(tmp_path), loose, sender=_ScriptedSender([]))
    assert (os.stat(loose).st_mode & 0o777) == 0o700


def test_preflight_proves_the_robot_credentials_without_posting(tmp_path, capsys):
    platform = _FakeOpenPlatform()
    sender = oob_alert.DxRobotSender(_robot_config(tmp_path), transport=platform, secret=lambda: 'robot-secret-abcd')
    passed, detail = sender.preflight()
    assert passed and 'token minted' in detail and detail.endswith('…abcd; group 71158539432')
    assert 'robot-secret' not in detail
    assert [url for url, _, _ in platform.requests] == ['https://ssosv.internal.example.com/sson/auth/oidc/v1/token']
    refused = oob_alert.DxRobotSender(_robot_config(tmp_path), transport=_FakeOpenPlatform(token_status=401),
                                      secret=lambda: 'robot-secret-abcd')
    passed, detail = refused.preflight()
    assert not passed and detail.startswith('token_rejected')
    assert oob_alert.NoSender().preflight() == (False, 'no delivery path is enabled in config.json')


def test_status_reports_write_failures(tmp_path, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'config.json').write_text(json.dumps({'tofuRoot': str(tmp_path / 'tofu')}))
    (state / 'state.json').write_text(json.dumps({'version': 1, 'writeFailures': {
        'count': 3, 'last': 'events.jsonl: OSError: [Errno 28] No space left on device', 'at': START}}))
    assert oob_alert.main(['--state-dir', str(state), 'status']) == 3
    assert 'local writes failing: 3' in capsys.readouterr().out
