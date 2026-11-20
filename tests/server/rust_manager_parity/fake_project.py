"""A disposable Tofu project whose processes are stdlib-only test doubles.

The lifecycle manager under test (Python ``supervisor.py`` or the Rust
``tofu-manager manager``) runs real processes in this directory:

- ``server.py``: an API worker holding ``data/.server.lock`` (``pid@host``)
  and answering ``/api/ready`` and ``/api/health`` (with its pid) on ``PORT``.
- ``serverctl.py prepare-frontend``: artifact preparation whose outcome the
  test selects with files in the project (see ``SERVERCTL``).
- optional native storage: ``contracts/native_storage_connection_v2.json``
  enables the manager's resident storage host, and ``serverctl_pkg`` in the
  project shadows the real host module (children run
  ``python -m serverctl_pkg.native_storage_host`` with cwd=project).

Every double appends one line per lifecycle event to ``events.log`` so a test
can assert ordering across processes (build before stop, host replacement
after the old worker exited).
"""
from __future__ import annotations

from pathlib import Path

WORKER = r'''import fcntl, json, os, signal, socket, sys, time
ENV_KEYS = ('TOFU_PROCESS_ROLE', 'TOFU_STORAGE_CONNECTION_FILE', 'TOFUDB_TRANSPORT',
            'PYTHONPYCACHEPREFIX', 'TOFU_PARITY_INTERPRETER', 'PYTHONPATH', 'TOFU_DATA_DIR',
            'TOFU_AGENT_QUEUE_CAPACITY')
from http.server import BaseHTTPRequestHandler, HTTPServer
# Like a real worker's data_root(): TOFU_DATA_DIR wins over the project's data/.
explicit = os.environ.get('TOFU_DATA_DIR')
data = ((explicit if os.path.basename(explicit) == 'data' else os.path.join(explicit, 'data'))
        if explicit else os.path.join(os.getcwd(), 'data'))
os.makedirs(data, exist_ok=True)
events = os.path.join(os.getcwd(), 'events.log')
def event(text):
    with open(events, 'a', encoding='utf-8') as stream:
        stream.write(text + '\n')
crash_once = os.path.join(os.getcwd(), 'worker-crash-once')
if os.path.exists(os.path.join(os.getcwd(), 'worker-crash-always')):
    event('worker-crash %d' % os.getpid())
    raise SystemExit(3)
if os.path.exists(crash_once):
    os.unlink(crash_once)
    event('worker-crash %d' % os.getpid())
    raise SystemExit(3)
lock_path = os.path.join(data, '.server.lock')
lock = open(lock_path, 'w+')
fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
lock.write('%d@%s\n' % (os.getpid(), socket.gethostname()))
lock.flush()
def readiness():
    # worker-ready-follows-storage: like lib readiness, storage gates /api/ready.
    if not os.path.exists(os.path.join(os.getcwd(), 'worker-ready-follows-storage')):
        return {'ok': True, 'ready': True, 'state': 'ready',
                'storage': {'ready': True, 'state': 'ready'}}
    try:
        with open(os.path.join(data, 'native-storage-service.json'), encoding='utf-8') as stream:
            state = json.load(stream)['state']
    except Exception:
        state = 'unknown'
    ready = state == 'ready'
    return {'ok': ready, 'ready': ready, 'state': 'ready' if ready else 'starting',
            'pid': os.getpid(), 'storage': {'ready': ready, 'state': state}}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a): pass
    def do_GET(self):
        payload = (
            readiness()
            if self.path == '/api/ready' else
            {'ok': True, 'pid': os.getpid(), 'bootId': str(os.getpid())}
        )
        body = json.dumps(payload).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)
def handle_sigterm(*_args):
    event('worker-exit %d' % os.getpid())
    # quota-after-stop arms parity_shim.c once: the volume fills as this worker stops.
    if os.path.exists('quota-after-stop'):
        os.replace('quota-after-stop', 'quota-full')
    if os.environ.get('WORKER_CLEAN_SIGNAL') == '1':
        marker_path = os.path.join(data, '.server_shutdown.json')
        temporary_path = marker_path + '.tmp'
        with open(temporary_path, 'w', encoding='utf-8') as stream:
            json.dump({
                'state': 'clean',
                'pid': os.getpid(),
                'host': socket.gethostname(),
                'clean_ts': time.time(),
                'reason': 'signal',
            }, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, marker_path)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, handle_sigterm)
with open('env-worker.json', 'w', encoding='utf-8') as stream:
    json.dump({key: os.environ.get(key) for key in ENV_KEYS}, stream)
event('worker-start %d' % os.getpid())
server = HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler)
# worker-tls holds a certificate and key path: serve HTTPS and record it like server.py.
tls = os.path.join(os.getcwd(), 'worker-tls')
if os.path.exists(tls):
    import ssl
    certfile, keyfile = open(tls, encoding='utf-8').read().split()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    with open(os.path.join(data, '.last_serve_mode'), 'w', encoding='utf-8') as stream:
        stream.write('https')
server.serve_forever()
'''

# Behaviour files (all optional, in the project root):
#   prep-frontend-fail     frontend part fails; content is the reason
#   prep-engine-fail       storage-engine part fails; content is the reason
#   prep-engine-decision   JSON decision reported as ``TOFU_ENGINE_DECISION``;
#                          decision ``refused`` exits 1, any other exits 0
SERVERCTL = r'''import argparse, json, os, sys
from pathlib import Path

def event(text):
    with open('events.log', 'a', encoding='utf-8') as stream:
        stream.write(text + '\n')

def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare-frontend')
    prepare.add_argument('--operation', default='')
    prepare.add_argument('--parts', choices=('all', 'frontend', 'storage-engine'), default='all')
    prepare.add_argument('--uncertified-engine-override')
    args = parser.parse_args()
    with open('prep-calls.log', 'a', encoding='utf-8') as stream:
        stream.write(args.parts + '\n')
    event('prep ' + args.parts)
    if args.parts in ('all', 'storage-engine'):
        decision_file = Path('prep-engine-decision')
        if decision_file.exists():
            decision = json.loads(decision_file.read_text(encoding='utf-8'))
            print('storage engine certification decision follows', file=sys.stderr)
            print('TOFU_ENGINE_DECISION ' + json.dumps(decision, sort_keys=True), file=sys.stderr)
            if decision.get('decision') == 'refused':
                return 1
        failure = Path('prep-engine-fail')
        if failure.exists():
            print('Storage engine preflight failed: ' + failure.read_text(encoding='utf-8').strip(),
                  file=sys.stderr)
            return 1
    if args.parts in ('all', 'frontend'):
        failure = Path('prep-frontend-fail')
        if failure.exists():
            print('Frontend preflight failed: ' + failure.read_text(encoding='utf-8').strip(),
                  file=sys.stderr)
            return 1
    return 0

raise SystemExit(main())
'''

# Resident host double for both resident services: the manager's resident
# protocol of serverctl_pkg.native_storage_host / serverctl_pkg.execution_host,
# stdlib only (the project package shadows the real serverctl_pkg, so the real
# record helpers are not importable). ``{KIND}`` selects the service:
#
# storage (events ``host-*``):
#   probe-stop-mode     clean (default) | stale | timeout | hang, as
#                       tests/support/resident_storage_probe.py
#   storage-engine      path of an engine binary: the plan names it with its
#                       sha256 (a managed engine, as native_engine plans)
#   storage-plan-fixed  the plan stays the same, so a restart keeps the host
#   bad-engines         engine digests whose first start fails like the
#                       2026-11-17 21:49 host (published ``failed``)
# execution (events ``exec-*``):
#   exec-stop-mode      clean | stale | timeout | hang
#   exec-plan-fail      preparation fails with this text (exit 1)
#   exec-digest         the plan's digest (default ``1``); changing it
#                       replaces the host on the next restart
# Every invocation appends ``<kind> <argv[1]> <TOFU_PARITY_INTERPRETER>`` to
# interpreter-calls.log so a test sees which interpreter ran it.
RESIDENT_HOST = r"""import argparse, hashlib, json, os, select, signal, stat, sys, threading, time
from pathlib import Path

KIND = '{KIND}'
RECORD_FORMAT = {FORMAT!r}
EVENT = 'host' if KIND == 'storage' else 'exec'
STOP_FILE = 'probe-stop-mode' if KIND == 'storage' else 'exec-stop-mode'
FAILED_START = ('RuntimeError: sidecar health preflight did not pass '
                '(probe_error=timeout; unhealthy=)')
ENV_KEYS = {ENV_KEYS!r}

def event(text):
    with open('events.log', 'a', encoding='utf-8') as stream:
        stream.write(text + '\n')

def start_ticks(pid):
    try:
        raw = Path(f'/proc/{{pid}}/stat').read_text()
        return int(raw[raw.rfind(')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None

def read_record(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {{}}
    return record if isinstance(record, dict) and record.get('format') == RECORD_FORMAT else {{}}

def write_record(path, record):
    temporary = path.with_name(f'.{{path.name}}.{{os.getpid()}}.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)

def control_matches(record, descriptor):
    import fcntl
    control = record.get('managerControl')
    if not isinstance(control, dict):
        return False
    try:
        info = os.fstat(descriptor)
        mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
    except OSError:
        return False
    return (stat.S_ISFIFO(info.st_mode) and info.st_dev == control.get('device')
            and info.st_ino == control.get('inode') and mode == os.O_RDONLY)

def storage_plan():
    engine = Path('storage-engine')
    if engine.exists():
        binary = Path(engine.read_text().strip()).resolve()
        return {{'backend': 'parity-probe', 'binary': str(binary),
                'binarySha256': hashlib.sha256(binary.read_bytes()).hexdigest()}}
    counter = Path('probe-generation')
    generation = int(counter.read_text().strip() or 0) if counter.exists() else 0
    if not (Path('storage-plan-fixed').exists() and generation):
        generation += 1
    counter.write_text(str(generation))
    return {{'backend': 'parity-probe', 'generation': generation}}

def execution_plan():
    failure = Path('exec-plan-fail')
    if failure.exists():
        print(failure.read_text(encoding='utf-8').strip(), file=sys.stderr)
        raise SystemExit(1)
    digest = Path('exec-digest')
    plan = {{'backend': 'exec-probe', 'digest': digest.read_text().strip() if digest.exists() else '1'}}
    interpreter = os.environ.get('TOFU_PARITY_INTERPRETER')
    if interpreter:
        plan['python'] = interpreter
    return plan

def plan(output):
    value = storage_plan() if KIND == 'storage' else execution_plan()
    Path(output).write_text(json.dumps(value))
    event(f'{{EVENT}}-plan {{value.get("generation") or value.get("digest") or value.get("binarySha256", "")[:12]}}')
    return 0

def serve(args):
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    state = Path(args.state_file)
    deadline = time.monotonic() + 10.0
    record = {{}}
    while not stopped.is_set() and time.monotonic() < deadline:
        if start_ticks(args.manager_pid) != args.manager_start:
            return 1
        candidate = read_record(state)
        if candidate.get('serviceId') == args.service_id and candidate.get('pid') == os.getpid():
            if (candidate.get('managerPid') == args.manager_pid
                    and candidate.get('managerStartTime') == args.manager_start):
                record = candidate
            break
        stopped.wait(0.05)
    if not record or not control_matches(record, args.manager_fd):
        return 1
    Path(f'env-{{KIND}}.json').write_text(json.dumps({{key: os.environ.get(key) for key in ENV_KEYS}}))

    def owner_gone():
        return bool(select.select([args.manager_fd], [], [], 0)[0])

    bad = Path('bad-engines')
    engine = str((record.get('plan') or {{}}).get('binarySha256') or '')
    failing = KIND == 'storage' and bool(engine) and bad.exists() and engine in bad.read_text().split()
    record['state'] = 'failed' if failing else 'ready'
    record['error'] = FAILED_START if failing else ''
    write_record(state, record)
    event(f'{{EVENT}}-{{"failed" if failing else "ready"}} {{args.service_id}}')
    while not stopped.wait(0.05):
        if owner_gone():
            event(f'{{EVENT}}-owner-gone {{args.service_id}}')
            return 0
    mode_file = Path(STOP_FILE)
    mode = mode_file.read_text().strip() if mode_file.exists() else 'clean'
    event(f'{{EVENT}}-stop {{args.service_id}} {{mode}}')
    if mode == 'hang':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while not owner_gone():
            threading.Event().wait(0.05)
        return 0
    if mode == 'stale':
        return 0
    record['state'] = 'failed' if mode == 'timeout' else 'stopped'
    record['error'] = ('TimeoutError: native daemon drain did not finish within its budget'
                       if mode == 'timeout' else '')
    write_record(state, record)
    return 0

def main():
    with open('interpreter-calls.log', 'a', encoding='utf-8') as stream:
        stream.write(f'{{KIND}} {{sys.argv[1] if len(sys.argv) > 1 else ""}} '
                     f'{{os.environ.get("TOFU_PARITY_INTERPRETER", "-")}}\n')
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--plan-output')
    parser.add_argument('--service-id')
    parser.add_argument('--state-file')
    for name in ('--manager-pid', '--manager-start', '--manager-fd', '--lease-fd', '--report-fd'):
        parser.add_argument(name, type=int)
    args, _unknown = parser.parse_known_args()
    if args.plan:
        return plan(args.plan_output)
    return serve(args)

raise SystemExit(main())
"""
# Launch environment the doubles record (env-<kind>.json, env-worker.json).
ENV_KEYS = ('TOFU_PROCESS_ROLE', 'TOFU_STORAGE_CONNECTION_FILE', 'TOFUDB_TRANSPORT',
            'PYTHONPYCACHEPREFIX', 'TOFU_PARITY_INTERPRETER', 'TOFU_DATA_DIR')
NATIVE_STORAGE_HOST = RESIDENT_HOST.format(KIND='storage', FORMAT='tofu.native-storage-service/v1',
                                           ENV_KEYS=ENV_KEYS)
EXECUTION_HOST = RESIDENT_HOST.format(KIND='execution', FORMAT='tofu.execution-service/v1',
                                      ENV_KEYS=ENV_KEYS)


def build_project(root: Path, *, native_storage: bool = False, execution: bool = False) -> Path:
    """Create the project under ``root``; return its realpath.

    ``execution`` adds ``contracts/worker_job_lifecycle_v1.json``, which turns on
    the manager-owned resident execution host (``ExecutionService.enabled``).
    """
    project = root / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'logs').mkdir()
    (project / 'server.py').write_text(WORKER)
    (project / 'stop.sh').write_text('#!/bin/sh\nexit 0\n')
    (project / 'serverctl.py').write_text(SERVERCTL)
    if native_storage or execution:
        (project / 'contracts').mkdir()
        package = project / 'serverctl_pkg'
        package.mkdir()
        (package / '__init__.py').write_text('')
    if native_storage:
        (project / 'contracts/native_storage_connection_v2.json').write_text('{}\n')
        (package / 'native_storage_host.py').write_text(NATIVE_STORAGE_HOST)
    if execution:
        (project / 'contracts/worker_job_lifecycle_v1.json').write_text('{}\n')
        (package / 'execution_host.py').write_text(EXECUTION_HOST)
    return project.resolve()
