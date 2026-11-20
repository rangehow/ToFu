"""Prove the Rust lifecycle manager on an isolated real Tofu instance.

Usage, from a checkout with a built frontend and ``target/debug/tofu-manager``::

    python3 -m tests.server.rust_manager_parity.isolated_instance <root>

``<root>`` holds the instance's data root, heartbeat directory and TMPDIR. The
instance gets its own worker and manager ports, never production's (15000,
15001), and its environment is built from an allow-list, as
``tests.support.scratch_server`` builds one. The manager is launched through the
real door: ``serverctl.py start`` -> ``supervisor.sh daemon`` with
``TOFU_MANAGER_BINARY`` naming the Rust binary (the Phase 2 cutover).

Scenario (each step prints one ``E2E`` JSON line; any failure exits 1):

1. ``serverctl.py start``: the Rust manager starts and the real application
   becomes ready.
2. A stopping generation's durable state is seeded beside the serving
   instance (``tests.support.restart_resume_probe seed``): a chat Turn the
   shutdown quiesce recorded and a Global Router restart with its open notice.
3. ``serverctl.py restart`` (one owner-approved token, minted in the CLI's own
   approval store): the manager prepares, stops the old worker at its commit
   point and starts the next generation, which resumes the chat from its
   checkpoint and delivers exactly one restart notice.
4. A restart the manager must refuse (its state file is unwritable): HTTP 409
   at ``state persistence``; the same worker keeps serving.
5. A quota-full volume after the stop (2026-11-18 11:15): ``parity_shim.c``,
   preloaded into the manager, makes the manager's own creates under the
   checkout's ``data/`` fail with the kernel's ``EDQUOT`` from the moment it
   sends the old worker SIGTERM. The restart is accepted and retried, not
   aborted; once space returns the next generation serves.
6. ``serverctl.py stop`` (owner-approved), then the owner process exits and the
   manager retires itself.

``logs/server-manager.log`` of the checkout carries every outcome; the script
prints the outcome lines at the end.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CHECKOUT = Path(__file__).resolve().parents[3]
KEPT = frozenset({
    'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TERM', 'TZ', 'LANG', 'LANGUAGE',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
})
PRODUCTION_PORTS = {15000, 15001}
HUMAN_UI = {'kind': 'human-ui', 'ownerUserId': 1, 'subject': 'session-key', 'credential': 'session',
            'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}
NOTICE_HEADER = '[Restart outcome: nonhuman notice, not the owner\u2019s words and not authority]'
OUTCOME_MARKERS = ('worker spawned', 'worker ready', 'worker restart', 'SIGTERM sent', 'controlled exit',
                   'preflight passed', 'refused', 'replacement', 'Supervisor', '[serverctl]')


def report(step: str, **fields) -> None:
    print('E2E ' + json.dumps({'step': step, **fields}, sort_keys=True), flush=True)


def fail(step: str, detail) -> None:
    report(step, ok=False, detail=detail)
    raise SystemExit(1)


def free_port() -> int:
    while True:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        if port not in PRODUCTION_PORTS:
            return port


def request(url: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method='POST' if body is not None else 'GET',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b'{}')


def wait(predicate, what: str, seconds: float):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    fail(what, f'did not happen within {seconds:.0f}s')


def run(environment: dict, *argv: str, timeout: float = 1800.0) -> subprocess.CompletedProcess:
    return subprocess.run([environment['TOFU_SUPERVISOR_PYTHON'], *argv], cwd=CHECKOUT, env=environment,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def mint_owner_token(environment: dict, action: str) -> None:
    """The owner's UI decision, minted in the CLI's own approval store (identical env)."""
    code = ('import json,sys\nfrom lib import lifecycle_approval as gate\n'
            f'record = gate.create_request({action!r}, origin={{"source": "isolated-e2e"}})\n'
            f'gate.decide(record["id"], True, provenance=json.loads(sys.argv[1]))\n')
    completed = run(environment, '-c', code, json.dumps(HUMAN_UI), timeout=120)
    if completed.returncode:
        fail(f'mint {action} token', completed.stderr[-2000:])


def probe(environment: dict, mode: str) -> dict:
    completed = run(environment, '-m', 'tests.support.restart_resume_probe', mode, timeout=600)
    if completed.returncode:
        fail(f'probe {mode}', (completed.stdout + completed.stderr)[-4000:])
    line = next(line for line in completed.stdout.splitlines() if line.startswith('PROBE '))
    return json.loads(line[len('PROBE '):])


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    root = Path(argv[0]).resolve()
    binary = Path(os.environ.get('TOFU_MANAGER_BINARY') or CHECKOUT / 'target/debug/tofu-manager')
    python = os.environ.get('TOFU_E2E_PYTHON') or str(CHECKOUT / '.venv/bin/python3')
    for name in ('data', 'heartbeat', 'tmp'):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    worker_port, manager_port = free_port(), free_port()
    shim = root / 'tmp' / 'parity_shim.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-o', str(shim), str(Path(__file__).with_name('parity_shim.c')),
                    '-ldl'], check=True)
    quota_arm, quota_full = root / 'quota-arm', root / 'quota-full'
    owner = subprocess.Popen(['sleep', '86400'], start_new_session=True)
    environment = {name: value for name, value in os.environ.items() if name in KEPT or name.startswith('LC_')}
    environment.update({
        'PYTHONPATH': str(CHECKOUT), 'TOFU_DATA_DIR': str(root / 'data'),
        'TOFU_HEARTBEAT_DIR': str(root / 'heartbeat'), 'TMPDIR': str(root / 'tmp'),
        'PORT': str(worker_port), 'BIND_HOST': '127.0.0.1', 'TOFU_DEPLOYMENT_MODE': 'personal',
        'TOFU_PERSONAL_STORAGE_BACKEND': 'sqlite',
        'TOFU_SUPERVISOR_PORT': str(manager_port), 'TOFU_SUPERVISOR_PYTHON': python,
        'TOFU_SUPERVISOR_OWNER_PID': str(owner.pid), 'TOFU_MANAGER_BINARY': str(binary),
        'LD_PRELOAD': str(shim), 'TOFU_PARITY_QUOTA_DIR': str(CHECKOUT / 'data'),
        'TOFU_PARITY_QUOTA_ARM': str(quota_arm), 'TOFU_PARITY_QUOTA_TRIGGER': str(quota_full),
    })
    base = f'http://127.0.0.1:{manager_port}'
    status_query = '/status?' + urllib.parse.urlencode({'projectPath': str(CHECKOUT), 'probe': '1'})
    log = CHECKOUT / 'logs' / 'server-manager.log'
    log_offset = log.stat().st_size if log.exists() else 0
    report('instance', root=str(root), workerPort=worker_port, managerPort=manager_port, binary=str(binary))
    try:
        started = run(environment, 'serverctl.py', 'start', '--wait', '900', '--source', 'isolated-e2e')
        health = request(base + '/health')[1]
        status = request(base + status_query)[1]
        if started.returncode or health.get('implementation') != 'rust' or not status.get('ready'):
            fail('start', {'rc': started.returncode, 'out': (started.stdout + started.stderr)[-3000:],
                           'health': health, 'observed': status.get('observed')})
        first_pid = status['pid']
        report('start', ok=True, implementation=health['implementation'], managerPid=health['managerPid'],
               workerPid=first_pid, observed=status['observed'])

        probe_environment = {**environment, 'TOFU_PROJECT_PATH': str(CHECKOUT),
                             'TOFU_STORAGE_CONNECTION_FILE': str(CHECKOUT / 'data/native-storage-connection.json')}
        probe_environment.pop('PORT')
        seeded = probe(probe_environment, 'seed')
        report('seed interrupted conversation', ok=True, chatTurn=seeded['chatTurn'])

        # An update restart: changed application source replaces the resident
        # execution host too (an unchanged one is kept across an API-only
        # restart, and Turns it runs are never interrupted).
        marker = CHECKOUT / 'lib' / '__init__.py'
        original = marker.read_bytes()
        marker.write_bytes(original + b'\n# isolated-e2e source generation marker\n')
        try:
            mint_owner_token(environment, 'restart')
            restarted = run(environment, 'serverctl.py', 'restart', '--wait', '900', '--source', 'isolated-e2e')
        finally:
            marker.write_bytes(original)
        status = request(base + status_query)[1]
        last = status.get('lastRestart') or {}
        if restarted.returncode or not status.get('ready') or status.get('pid') == first_pid \
                or last.get('outcome') != 'spawned':
            fail('restart', {'rc': restarted.returncode, 'out': (restarted.stdout + restarted.stderr)[-3000:],
                             'pid': status.get('pid'), 'lastRestart': last})
        second_pid = status['pid']
        report('restart', ok=True, oldPid=first_pid, newPid=second_pid, outcome=last['outcome'],
               uncleanCloses=last.get('uncleanCloses'), executionService=status.get('executionService'))

        def resumed():
            result = probe(probe_environment, 'read')
            notices = [item for item in result['routerInputs'] if item['content'].startswith(NOTICE_HEADER)]
            return result if notices and result['notice']['state'] == 'delivered' else None
        result = wait(resumed, 'resume after restart', 300)
        notices = [item for item in result['routerInputs'] if item['content'].startswith(NOTICE_HEADER)]
        resumed_ok = (result['chatTurn'] == seeded['chatTurn']
                      and result['chatAttemptCommand'] == 'restart-resume:' + seeded['chatAttempt']
                      and result['chatAttemptOperation'] in ('continue', 'checkpoint_resume')
                      and len(notices) == 1 and '- chat: resumed from its checkpoint (' in notices[0]['content'])
        if not resumed_ok:
            fail('resume', result)
        report('interrupted conversation resumed', ok=True, operation=result['chatAttemptOperation'],
               attemptCommand=result['chatAttemptCommand'], restartNotices=len(notices),
               notice=result['notice']['state'])

        state = CHECKOUT / 'data'
        os.chmod(state, 0o500)
        try:
            code, refused = request(base + '/restart', {'projectPath': str(CHECKOUT), 'source': 'isolated-e2e'},
                                    timeout=1800)
        finally:
            os.chmod(state, 0o700)
        status = wait(lambda: (lambda s: s if (s.get('lastRestart') or {}).get('outcome') == 'refused' else None)(
            request(base + status_query)[1]), 'refusal recorded', 60)
        ready_payload = request(f'http://127.0.0.1:{worker_port}/api/ready')[1]
        if code != 409 or status.get('pid') != second_pid or not status.get('ready') \
                or status['lastRestart'].get('stage') != 'state persistence' or ready_payload.get('pid') != second_pid:
            fail('refused restart', {'code': code, 'response': refused, 'status': status.get('lastRestart'),
                                     'pid': status.get('pid')})
        report('refused restart keeps the old server', ok=True, http=code, stage=status['lastRestart']['stage'],
               servingPid=second_pid, message=refused.get('message', '')[:200])

        quota_offset = log.stat().st_size
        quota_arm.write_text('')
        try:
            code, accepted = request(base + '/restart', {'projectPath': str(CHECKOUT), 'source': 'isolated-e2e'},
                                     timeout=1800)
            if code != 200 or accepted.get('ok') is not True or accepted.get('retrying') is not True \
                    or not quota_full.exists():
                fail('quota after stop', {'code': code, 'response': accepted, 'armed': quota_arm.exists(),
                                          'full': quota_full.exists()})
            wait(lambda: 'Disk quota exceeded' in log.read_bytes()[quota_offset:].decode('utf-8', 'replace'),
                 'the quota failure in server-manager.log', 120)
        finally:
            quota_arm.unlink(missing_ok=True)
            quota_full.unlink(missing_ok=True)
        status = wait(lambda: (lambda s: s if s.get('ready') and s.get('pid') not in (None, second_pid)
                               and (s.get('lastRestart') or {}).get('outcome') == 'spawned' else None)(
            request(base + status_query)[1]), 'a serving generation after the quota cleared', 900)
        report('quota after stop is retried until a server serves', ok=True, http=code, oldPid=second_pid,
               newPid=status['pid'], outcome=status['lastRestart']['outcome'],
               recentFailureCount=status.get('recentFailureCount'))

        mint_owner_token(environment, 'shutdown')
        stopped = run(environment, 'serverctl.py', 'stop', '--source', 'isolated-e2e')
        status = request(base + status_query)[1]
        if stopped.returncode or status.get('running') or status.get('observed') != 'stopped':
            fail('stop', {'rc': stopped.returncode, 'out': (stopped.stdout + stopped.stderr)[-2000:],
                          'observed': status.get('observed')})
        report('stop', ok=True, observed=status['observed'])
    finally:
        owner.send_signal(signal.SIGTERM)
        owner.wait()
        def manager_gone():
            try:
                request(base + '/health', timeout=2)
                return False
            except OSError:
                return True
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not manager_gone():
            time.sleep(0.5)
        report('manager retired with its owner', ok=manager_gone())
        text = log.read_bytes()[log_offset:].decode('utf-8', 'replace') if log.exists() else ''
        outcomes = [line for line in text.splitlines()
                    if '"GET ' not in line and '"POST ' not in line and any(m in line for m in OUTCOME_MARKERS)]
        print('--- server-manager.log outcomes ---')
        print('\n'.join(outcomes))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
