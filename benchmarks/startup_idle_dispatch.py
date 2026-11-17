#!/usr/bin/env python3
"""Cold start, idle cost and submit-to-provider latency of an isolated Tofu.

Entry: CLI (``cold-start``, ``idle``, ``dispatch``). Measures the real
``serverctl.py start`` composition of this checkout: lifecycle manager, API
host (server.py) and resident execution host. Every run uses a scrubbed
environment (nothing inherited from a live Tofu), private manager/API ports
and the checkout's own in-tree ``data/``; it refuses the default ports so it
can never address a live instance. Results are JSON on stdout and are the
evidence recorded in docs/HARNESS_PERFORMANCE.md.

Dependencies: this checkout, a built frontend artifact, a certified storage
engine, and ``tests.tasks_pkg.test_native_production_prices`` for the
loopback provider's model-routing document.
"""
from __future__ import annotations

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
LIVE_PORTS = {15000, 15001}  # The default API and manager ports of a live Tofu.
CLK_TCK = os.sysconf('SC_CLK_TCK')
PAGE = os.sysconf('SC_PAGE_SIZE')


def _free_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        if port not in LIVE_PORTS:
            return port


class Instance:
    """One isolated manager + API + execution host for this checkout."""

    def __init__(self, backend: str) -> None:
        self.api_port = _free_port()
        self.manager_port = _free_port()
        self.scratch = Path(os.environ.get('TMPDIR') or '/tmp') / f'tofu-bench-{uuid.uuid4().hex[:8]}'
        (self.scratch / 'tmp').mkdir(parents=True)
        (self.scratch / 'heartbeat').mkdir()
        tiktoken = Path('/tmp/data-gym-cache')
        if tiktoken.is_dir():
            shutil.copytree(tiktoken, self.scratch / 'tmp' / 'data-gym-cache')
        node = shutil.which('node')
        path = os.pathsep.join(filter(None, [os.path.dirname(node) if node else '',
                                             '/usr/local/bin', '/usr/bin', '/bin']))
        self.env = {
            'PATH': path, 'HOME': os.environ.get('HOME', ''), 'USER': os.environ.get('USER', ''),
            'LANG': 'C.UTF-8', 'TMPDIR': str(self.scratch / 'tmp'),
            'TIKTOKEN_CACHE_DIR': str(self.scratch / 'tmp' / 'data-gym-cache'),
            'PYTHONPATH': str(ROOT), 'TOFU_PROJECT_PATH': str(ROOT),
            # The default heartbeat directory is host-global and shared with a
            # live Tofu's wedge detection; an instance must never write there.
            'TOFU_HEARTBEAT_DIR': str(self.scratch / 'heartbeat'),
            'TOFU_DATA_LAYOUT': 'intree', 'TOFU_AUTH_MODE': 'open',
            'TOFU_PERSONAL_STORAGE_BACKEND': backend,
            'PORT': str(self.api_port), 'TOFU_SUPERVISOR_PORT': str(self.manager_port),
        }
        if node:
            self.env['TOFU_NODE'] = node
        self.python = str(ROOT / '.venv/bin/python3') if (ROOT / '.venv/bin/python3').exists() else sys.executable
        self.base = f'http://127.0.0.1:{self.api_port}'

    # -- lifecycle -------------------------------------------------------
    def _manager(self, action: str, **body) -> dict:
        data = json.dumps({'projectPath': str(ROOT), 'source': 'benchmark', **body}).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{self.manager_port}/{action}', data=data,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())

    def start_cli(self) -> subprocess.Popen:
        return subprocess.Popen([self.python, 'serverctl.py', 'start', '--wait', '600'], cwd=ROOT,
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        self._manager('stop')
        deadline = time.monotonic() + 120
        while self.pids() and time.monotonic() < deadline:
            time.sleep(0.2)

    def close(self) -> None:
        try:
            self.stop()
        except OSError:
            pass
        # The supervisor.sh watchdog respawns supervisor.py: stop it first, then
        # the manager, and wait for both so the next run finds no manager.
        managers = self._manager_pids()
        for pid in sorted(managers, key=lambda pid: not _is_watchdog(pid)):
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 60
        while self._manager_pids() and time.monotonic() < deadline:
            time.sleep(0.2)
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _manager_pids(self) -> list[int]:
        return checkout_manager_pids(self.manager_port)

    # -- observation -----------------------------------------------------
    def health(self) -> dict | None:
        try:
            with urllib.request.urlopen(self.base + '/api/health', timeout=2) as response:
                return json.loads(response.read())
        except (OSError, ValueError, http.client.HTTPException):
            return None  # Not listening yet, or a boot-time listener answering another protocol.

    def execution_record(self) -> dict:
        try:
            return json.loads((ROOT / 'data/execution-service.json').read_text())
        except (OSError, ValueError):
            return {}

    def pids(self) -> dict[str, int]:
        found = {}
        try:
            lock = (ROOT / 'data/.server.lock').read_text().split('@')
            if lock and lock[0].isdigit() and Path(f'/proc/{lock[0]}').exists():
                found['api'] = int(lock[0])
        except OSError:
            pass
        record = self.execution_record()
        if record.get('pid') and Path(f"/proc/{record['pid']}").exists() and record.get('state') != 'stopped':
            found['execution'] = int(record['pid'])
        return found


def checkout_manager_pids(port: int | None = None) -> list[int]:
    """Lifecycle-manager processes (supervisor.sh/.py) of this checkout."""
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
            env = (entry / 'environ').read_bytes()
        except OSError:
            continue
        if (f'{ROOT}/supervisor.' in cmd
                and (port is None or f'TOFU_SUPERVISOR_PORT={port}\0'.encode() in env + b'\0')):
            found.append(int(entry.name))
    return found


def _is_watchdog(pid: int) -> bool:
    try:
        return b'supervisor.sh' in Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return False


def thread_names(pid: int) -> dict[str, str]:
    """OS thread id -> Python thread name, via py-spy when it is installed."""
    spy = shutil.which('py-spy')
    if not spy:
        return {}
    try:
        dump = subprocess.run([spy, 'dump', '--pid', str(pid), '--nonblocking'], capture_output=True,
                              text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    names = {}
    for line in dump.splitlines():
        if line.startswith('Thread ') and '"' in line:
            names[line.split()[1]] = line.split('"')[1]
    return names


def _boot_seconds_since(pid: int) -> float:
    """Wall seconds since ``pid`` started (from /proc/<pid>/stat starttime)."""
    fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
    start_ticks = int(fields[19])
    uptime = float(Path('/proc/uptime').read_text().split()[0])
    return uptime - start_ticks / CLK_TCK


def _proc_sample(pid: int) -> dict:
    fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
    cpu = (int(fields[11]) + int(fields[12])) / CLK_TCK
    rss = int(Path(f'/proc/{pid}/statm').read_text().split()[1]) * PAGE
    switches, threads = 0, {}
    for task in Path(f'/proc/{pid}/task').iterdir():
        try:
            status = (task / 'status').read_text()
            name = (task / 'comm').read_text().strip()
        except OSError:
            continue
        count = sum(int(line.split()[1]) for line in status.splitlines()
                    if line.startswith(('voluntary_ctxt_switches', 'nonvoluntary_ctxt_switches')))
        switches += count
        threads[f'{task.name}:{name}'] = count
    return {'cpu': cpu, 'rss': rss, 'switches': switches, 'threads': threads}


def wait_ready(instance: Instance, t0: float, timeout: float = 600) -> dict:
    api_ready = exec_ready = None
    api_pid = exec_pid = None
    api_age = exec_age = None
    profile = None
    while time.monotonic() - t0 < timeout:
        now = time.monotonic()
        pids = instance.pids()
        if api_ready is None:
            health = instance.health()
            if health and (health.get('boot') or {}).get('status') == 'ready':
                api_ready, api_pid = now - t0, pids.get('api')
                api_age = _boot_seconds_since(api_pid) if api_pid else None
                profile = health.get('bootProfile')
        if exec_ready is None and instance.execution_record().get('state') == 'ready':
            exec_ready, exec_pid = now - t0, pids.get('execution')
            exec_age = _boot_seconds_since(exec_pid) if exec_pid else None
        if api_ready is not None and exec_ready is not None:
            return {'apiReadySeconds': round(api_ready, 3), 'executionReadySeconds': round(exec_ready, 3),
                    'bothReadySeconds': round(max(api_ready, exec_ready), 3),
                    'apiSpawnToReadySeconds': None if api_age is None else round(api_age, 3),
                    'executionSpawnToReadySeconds': None if exec_age is None else round(exec_age, 3),
                    'apiBootProfile': profile}
        time.sleep(0.05)
    raise TimeoutError(f'instance not ready: api={api_ready} execution={exec_ready}')


def _summary(values: list[float]) -> dict:
    return {'median': round(statistics.median(values), 3), 'min': round(min(values), 3),
            'max': round(max(values), 3), 'samples': [round(v, 3) for v in values]}


def cold_start(instance: Instance, samples: int) -> dict:
    # Warm-up: launches the manager and leaves the OS page cache in a steady state.
    cli = instance.start_cli()
    wait_ready(instance, time.monotonic())
    cli.wait(timeout=600)
    instance.stop()
    runs = []
    for _ in range(samples):
        t0 = time.monotonic()
        cli = instance.start_cli()
        runs.append(wait_ready(instance, t0))
        cli.wait(timeout=600)
        instance.stop()
    keys = ('apiReadySeconds', 'executionReadySeconds', 'bothReadySeconds',
            'apiSpawnToReadySeconds', 'executionSpawnToReadySeconds')
    return {'runs': runs, **{key: _summary([run[key] for run in runs if run[key] is not None])
                             for key in keys}}


def idle(instance: Instance, seconds: float, settle: float) -> dict:
    t0 = time.monotonic()
    cli = instance.start_cli()
    ready = wait_ready(instance, t0)
    cli.wait(timeout=600)
    time.sleep(settle)
    pids = instance.pids()
    before = {role: _proc_sample(pid) for role, pid in pids.items()}
    started = time.monotonic()
    peak = {role: sample['rss'] for role, sample in before.items()}
    while time.monotonic() - started < seconds:
        time.sleep(min(10, seconds))
        for role, pid in pids.items():
            peak[role] = max(peak[role], _proc_sample(pid)['rss'])
    elapsed = time.monotonic() - started
    after = {role: _proc_sample(pid) for role, pid in pids.items()}
    result = {'ready': ready, 'windowSeconds': round(elapsed, 1), 'settleSeconds': settle}
    for role, pid in pids.items():
        names = thread_names(pid)
        threads = {f"{name.partition(':')[0]}:{names.get(name.partition(':')[0], name.partition(':')[2])}":
                   count - before[role]['threads'].get(name, 0)
                   for name, count in after[role]['threads'].items()}
        busiest = sorted(threads.items(), key=lambda item: -item[1])[:12]
        result[role] = {
            'cpuPercent': round(100 * (after[role]['cpu'] - before[role]['cpu']) / elapsed, 3),
            'rssMiB': round(after[role]['rss'] / 2**20, 1),
            'peakRssMiB': round(peak[role] / 2**20, 1),
            'wakeupsPerSecond': round((after[role]['switches'] - before[role]['switches']) / elapsed, 2),
            'threads': len(after[role]['threads']),
            'busiestThreadsWakeupsPerSecond': {name: round(count / elapsed, 2) for name, count in busiest},
        }
    instance.stop()
    return result


def hold(instance: Instance) -> dict:
    import signal
    t0 = time.monotonic()
    cli = instance.start_cli()
    ready = wait_ready(instance, t0)
    cli.wait(timeout=600)
    ready.pop('apiBootProfile', None)
    print(json.dumps({'ready': ready, 'pids': instance.pids(), 'api': instance.base,
                      'managerPort': instance.manager_port}), flush=True)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    stop.wait()
    return {'held': True}


class _Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), _ProviderHandler)
        self.first_byte: list[float] = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()


class _ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_one_request(self):
        if self.rfile.peek(1):
            self.server.first_byte.append(time.monotonic())
        super().handle_one_request()

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        wire = ('data: ' + json.dumps({'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}],
                                       'usage': {'prompt_tokens': 3, 'completion_tokens': 1,
                                                 'total_tokens': 4}}) + '\n\ndata: [DONE]\n\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)


def _install_provider(client, endpoint: str) -> None:
    """Register the loopback provider's fixture model (creator/alpha via provider-a).

    Unlike the hermetic test helper, the startup catalogue of a real instance may
    hold rows that are not ready; only the fixture model must be callable.
    """
    from tests.tasks_pkg.test_native_production_prices import _full_document, complete_registration
    current = client.get('/api/v4/model-routing').json()['data']
    document = complete_registration(_full_document())
    document['connections'][0]['base_url'] = endpoint
    document['connections'][1]['enabled'] = False
    for collection, identity in [('creators', ('creator_id',)), ('models', ('creator_id', 'model_id'))]:
        supplied = {tuple(row[key] for key in identity) for row in document[collection]}
        document[collection].extend(row for row in current['model_routing'][collection]
                                    if tuple(row[key] for key in identity) not in supplied)
    response = client.put('/api/v4/model-routing', json={
        'expected_revision': current['revision'], 'model_routing': document})
    response.raise_for_status()
    models = response.json()['data']['model_routing']['models']
    assert any(row['creator_id'] == 'creator' and row['model_id'] == 'alpha'
               and row['registration_state'] == 'ready' for row in models), 'fixture model not ready'


def dispatch(instance: Instance, samples: int) -> dict:
    import httpx
    provider = _Provider()
    t0 = time.monotonic()
    cli = instance.start_cli()
    wait_ready(instance, t0)
    cli.wait(timeout=600)
    runs = []
    with httpx.Client(base_url=instance.base, trust_env=False, timeout=60) as client:
        _install_provider(client, f'http://127.0.0.1:{provider.server_port}/v1')
        for index in range(samples + 1):
            conversation = 'dispatch-latency-' + uuid.uuid4().hex
            body = {'commandId': uuid.uuid4().hex, 'inputTurn': {'content': f'latency probe {index}'},
                    'conversation': {'allowCreate': True, 'title': 'latency', 'settings': {}},
                    'config': {'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'},
                               'routing': {'preferred_provider_id': 'provider-a'}, 'maxTokens': 64,
                               'thinkingEnabled': False, 'memoryEnabled': False, 'projectEnabled': False,
                               'searchMode': 'off', 'tools': {'programmaticCalling': 'off'}}}
            count = len(provider.first_byte)
            submitted = time.monotonic()
            response = client.post(f'/api/v4/conversations/{conversation}/turns', json=body)
            admitted = time.monotonic()
            response.raise_for_status()
            turn = response.json()['turn']
            while len(provider.first_byte) == count and time.monotonic() - submitted < 60:
                time.sleep(0.001)
            first = provider.first_byte[count] if len(provider.first_byte) > count else None
            path = f'/api/v4/conversations/{conversation}/turns/' + turn['turnId']
            deadline = time.monotonic() + 60
            while client.get(path).json()['turn']['status'] not in {'completed', 'failed', 'interrupted'}:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            if index == 0:
                # The first turn after ready also pays lazy first-use imports.
                first_turn = {'admissionSeconds': round(admitted - submitted, 3),
                              'submitToFirstProviderByteSeconds':
                                  None if first is None else round(first - submitted, 3)}
                continue
            runs.append({'admissionSeconds': admitted - submitted,
                         'submitToFirstProviderByteSeconds': None if first is None else first - submitted})
    provider.shutdown()
    instance.stop()
    return {'firstTurnAfterReady': first_turn,
            'admission': _summary([run['admissionSeconds'] for run in runs]),
            'submitToFirstProviderByte': _summary([run['submitToFirstProviderByteSeconds'] for run in runs
                                                   if run['submitToFirstProviderByteSeconds'] is not None])}


_HOLD_LANDING = r'''
import sys, time
from lib.runtime.protected_steps import protected_step
with protected_step('git_land', detail=sys.argv[1],
                    on_wait=lambda message: print('waits', time.monotonic(), flush=True)):
    print('runs', time.monotonic(), flush=True)
    sys.stdin.readline()
    if len(sys.argv) > 2:  # land a source change, as a real landing does
        with open(sys.argv[2], 'a', encoding='utf-8') as handle:
            handle.write('# landed during the restart benchmark\n')
print('done', time.monotonic(), flush=True)
'''


def _scripted_turn(client, index: int) -> dict:
    conversation = 'restart-e2e-' + uuid.uuid4().hex
    body = {'commandId': uuid.uuid4().hex, 'inputTurn': {'content': f'restart probe {index}'},
            'conversation': {'allowCreate': True, 'title': 'restart', 'settings': {}},
            'config': {'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'},
                       'routing': {'preferred_provider_id': 'provider-a'}, 'maxTokens': 64,
                       'thinkingEnabled': False, 'memoryEnabled': False, 'projectEnabled': False,
                       'searchMode': 'off', 'tools': {'programmaticCalling': 'off'}}}
    response = client.post(f'/api/v4/conversations/{conversation}/turns', json=body)
    response.raise_for_status()
    path = f'/api/v4/conversations/{conversation}/turns/' + response.json()['turn']['turnId']
    deadline = time.monotonic() + 120
    while True:
        turn = client.get(path).json()['turn']
        if turn['status'] in {'completed', 'failed', 'interrupted'}:
            return {'conversation': conversation, 'status': turn['status']}
        if time.monotonic() > deadline:
            raise TimeoutError(f'turn {path} stayed {turn["status"]}')
        time.sleep(0.05)


def _landing(instance: Instance, name: str, edits: Path | None = None) -> tuple[subprocess.Popen, list]:
    """A real protected step (git_land) in the instance's data root, held until released."""
    process = subprocess.Popen([instance.python, '-c', _HOLD_LANDING, name, *([str(edits)] if edits else [])],
                               cwd=ROOT, env=instance.env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    events: list = []
    threading.Thread(target=lambda: [events.append(line.split()) for line in process.stdout],
                     daemon=True).start()
    return process, events


def _until(predicate, what: str, seconds: float = 120) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(what)
        time.sleep(0.05)


def restart(instance: Instance, land_source: bool = False) -> dict:
    """A real ``serverctl.py restart`` of a running instance while landings run.

    A landing runs when the restart starts; a second starts while the restart
    is pending. The restart must wait for the first, keep the second waiting
    until it finished, replace both hosts and serve a scripted Turn after.
    With ``land_source`` the running landing changes application source (only
    in an exported copy, never a Git checkout), so the execution host must be
    replaced too; without it an unchanged execution host is kept by design.
    """
    if land_source and (ROOT / '.git').exists():
        raise SystemExit('--land-source edits application source; run it from an exported copy, not a checkout')
    import httpx
    provider = _Provider()
    t0 = time.monotonic()
    cli = instance.start_cli()
    wait_ready(instance, t0)
    cli.wait(timeout=600)
    before = instance.pids()
    with httpx.Client(base_url=instance.base, trust_env=False, timeout=60) as client:
        _install_provider(client, f'http://127.0.0.1:{provider.server_port}/v1')
        first_turn = _scripted_turn(client, 0)
    running, running_events = _landing(
        instance, 'running-landing', ROOT / 'lib/runtime/protected_steps.py' if land_source else None)
    _until(lambda: any(event[0] == 'runs' for event in running_events), 'first landing never ran')
    output: list[str] = []
    started = time.monotonic()
    # TOFU_RESTART_GATE_PASSED is the operator handoff a gated caller uses (serverctl).
    command = subprocess.Popen([instance.python, 'serverctl.py', 'restart', '--wait', '600'], cwd=ROOT,
                               env={**instance.env, 'TOFU_RESTART_GATE_PASSED': '1'},
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True)
    threading.Thread(target=lambda: output.extend(command.stdout), daemon=True).start()
    _until(lambda: any('Waiting for running protected steps' in line for line in output),
           'restart never waited for the running landing')
    late, late_events = _landing(instance, 'late-landing')
    _until(lambda: any(event[0] == 'waits' for event in late_events), 'late landing did not wait')
    running.stdin.write('\n')
    running.stdin.flush()
    running.wait(timeout=60)
    returncode = command.wait(timeout=900)
    restarted = time.monotonic()
    _until(lambda: any(event[0] == 'runs' for event in late_events), 'late landing never resumed', 300)
    late_ran = float(next(event[1] for event in late_events if event[0] == 'runs'))
    late.stdin.write('\n')
    late.stdin.flush()
    late.wait(timeout=60)
    after = instance.pids()
    health = instance.health() or {}
    with httpx.Client(base_url=instance.base, trust_env=False, timeout=60) as client:
        second_turn = _scripted_turn(client, 1)
    provider.shutdown()
    result = {
        'restartExitCode': returncode, 'restartSeconds': round(restarted - started, 1),
        'restartOutput': [line.rstrip() for line in output][-40:],
        'pidsBefore': before, 'pidsAfter': after,
        'apiReplaced': bool(after.get('api')) and after.get('api') != before.get('api'),
        'executionReplaced': bool(after.get('execution')) and after.get('execution') != before.get('execution'),
        'executionRecord': {key: instance.execution_record().get(key) for key in ('state', 'pid', 'plan')},
        'healthBoot': health.get('boot'),
        'lateLandingWaitedForRestart': late_ran >= restarted - 1.0 or late_ran > started,
        'lateLandingRanAfterRestartSeconds': round(late_ran - started, 1),
        'turnBefore': first_turn, 'turnAfter': second_turn,
    }
    result['landedSource'] = land_source
    result['ok'] = (returncode == 0 and result['apiReplaced']
                    and result['executionReplaced'] == land_source
                    and (health.get('boot') or {}).get('status') == 'ready'
                    and first_turn['status'] == second_turn['status'] == 'completed')
    instance.stop()
    return result


def main() -> int:
    # The provider fixture lives in tests/; a clean environment has no PYTHONPATH.
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('sqlite', 'tofudb'), default='sqlite')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('cold-start').add_argument('--samples', type=int, default=5)
    idle_parser = sub.add_parser('idle')
    idle_parser.add_argument('--seconds', type=float, default=600)
    idle_parser.add_argument('--settle', type=float, default=60)
    sub.add_parser('dispatch').add_argument('--samples', type=int, default=10)
    sub.add_parser('hold', help='start one instance, print its identity, keep it until SIGTERM/SIGINT')
    sub.add_parser('restart', help='serverctl restart of a running instance while landings run, then a Turn'
                   ).add_argument('--land-source', action='store_true',
                                  help='the landing changes source (exported copies only)')
    args = parser.parse_args()
    if checkout_manager_pids():
        parser.error(f'a lifecycle manager of {ROOT} is already running (pids {checkout_manager_pids()}); '
                     'stop it first: a second one never takes over the checkout')
    if (ROOT / 'data/.server.lock').is_file():
        lock = (ROOT / 'data/.server.lock').read_text().split('@')
        if lock and lock[0].isdigit() and Path(f'/proc/{lock[0]}').exists():
            parser.error(f'{ROOT} already runs a server (pid {lock[0]}); refusing to measure a live instance')
    instance = Instance(args.backend)
    try:
        if args.command == 'cold-start':
            result = cold_start(instance, args.samples)
        elif args.command == 'idle':
            result = idle(instance, args.seconds, args.settle)
        elif args.command == 'hold':
            result = hold(instance)
        elif args.command == 'restart':
            result = restart(instance, args.land_source)
        else:
            result = dispatch(instance, args.samples)
    finally:
        instance.close()
    print(json.dumps(result, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
