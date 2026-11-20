"""Opt-in managed Detect journey: python -m tests.support.managed_manual_detect.

A committed source snapshot gets its own real execution registry, SQLite sidecar,
manager owner pipe and forked shard. Only Anthropic wire responses are synthetic.
The test controller in the primary closes a verified shard owner writer on
request, without replacing service composition, placement, control or workers.
"""
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4


OWNER = 1
MODEL = 'detect-fixture'
WAIT = 90


def until(read):
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError(f'timed out: {read!r}')


class Provider:
    def __init__(self):
        self.holding = threading.Event()
        self.release = threading.Event()
        self.schedule = None
        self.probes = []
        self.results = {}
        self.tool_calls = Counter()
        self.probe_entered = threading.Event()
        self.probe_release = threading.Event()
        self.hold_probe = False
        self.probe_peer = None

    def handle(self, body, peer):
        messages = body.get('messages') or []
        flat = json.dumps(messages)
        if not body.get('stream'):
            from lib.provider.provider_probe import _CHAT_PROBE_PROMPT
            if messages == [{'role': 'user', 'content': _CHAT_PROBE_PROMPT}]:
                self.probes.append(body['model'])
                if self.hold_probe:
                    self.probe_peer = peer
                    self.probe_entered.set()
                    assert self.probe_release.wait(WAIT)
            return {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': MODEL,
                    'content': [{'type': 'text', 'text': 'Probe OK'}],
                    'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        if 'hold-primary-fixture' in flat:
            self.holding.set()
            assert self.release.wait(WAIT)
        results = [block for message in messages if isinstance(message['content'], list)
                   for block in message['content'] if block.get('type') == 'tool_result']
        case = next((case for case in ('withdraw-', 'refuse-', '')
                     if f'{case}run-detect-fixture' in flat), None)
        if case is not None and not results:
            names = {tool['name'] for tool in body.get('tools', [])}
            assert 'schedule_manage' in names, sorted(names)
            self.tool_calls[case] += 1
            return {'type': 'tool_use', 'id': 'toolu_detect_fixture', 'name': 'schedule_manage',
                    'input': {'action': 'run', 'task_id': self.schedule}}
        if case is not None and results:
            self.results.setdefault(case, results)
        return {'type': 'text', 'text': 'The disposable fixture has finished the requested verification.'}


def stream(block):
    reason = 'tool_use' if block['type'] == 'tool_use' else 'end_turn'
    if reason == 'tool_use':
        delta = {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
        start = {**block, 'input': {}}
    else:
        delta = {'type': 'text_delta', 'text': block['text']}
        start = {**block, 'text': ''}
    events = [('message_start', {'message': {'id': 'msg_fixture', 'type': 'message',
               'role': 'assistant', 'model': MODEL, 'content': [], 'stop_reason': None,
               'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
              ('content_block_start', {'index': 0, 'content_block': start}),
              ('content_block_delta', {'index': 0, 'delta': delta}),
              ('content_block_stop', {'index': 0}),
              ('message_delta', {'delta': {'stop_reason': reason}, 'usage': {'output_tokens': 1}}),
              ('message_stop', {})]
    return ''.join(f'event: {kind}\ndata: {json.dumps(dict(payload, type=kind))}\n\n'
                   for kind, payload in events).encode()


@contextmanager
def fake_provider():
    fixture = Provider()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data": []}')

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            assert self.path == '/v1/messages', self.path
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            result = fixture.handle(body, self.client_address)
            streaming = body.get('stream')
            payload = stream(result) if streaming else json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if streaming else 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            fixture.address = server.server_address
            yield fixture, server.server_port
        finally:
            fixture.release.set()
            fixture.probe_release.set()
            server.shutdown()
            thread.join()


def seed_model(port):
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary
    ref = {'creator_id': 'detect-fixture', 'model_id': MODEL}
    caps = ['text', 'tools']
    document = {
        'contract_version': 'tofu.model-routing/v2', 'revision': 1,
        'creators': [{'creator_id': 'detect-fixture', 'name': 'Fixture'}],
        'models': [{**ref, 'display_name': 'Fixture', 'capabilities': caps, 'context_window': 100_000,
                    'quality_rank': 1, 'registration_metadata': {'release_date': '2026-11-12',
                    'checked_at': '2026-11-12', 'source_urls': ['https://example.invalid/detect-fixture']},
                    'list_pricing': {'input': 0, 'output': 0, 'currency': 'USD', 'unit': 'per_million_tokens'},
                    'inference_profile': {'reasoning': {'support': 'unsupported'}}}],
        'providers': [{'provider_id': 'fixture', 'name': 'Fixture', 'scope': 'owner'}],
        'provider_accesses': [{'provider_access_id': 'access', 'provider_id': 'fixture', 'enabled': True,
                               'quota_policy': {}}],
        'connections': [{'connection_id': 'conn', 'provider_access_id': 'access',
                         'base_url': f'http://127.0.0.1:{port}', 'protocol': 'anthropic', 'enabled': True}],
        'credentials': [{'credential_id': 'identity', 'provider_access_id': 'access', 'kind': 'local_identity',
                         'enabled': True, 'quota_policy': {}, 'secret_reference': '', 'key_hint': '',
                         'authorization': {'connection_ids': ['conn'], 'models': [ref]}}],
        'offerings': [{'offering_id': 'offering', 'provider_access_id': 'access', 'identity_state': 'confirmed',
                       'model': ref, 'enabled': True, 'capabilities': caps, 'context_window': 100_000}],
        'deployments': [{'deployment_id': 'deployment', 'offering_id': 'offering', 'connection_id': 'conn',
                         'wire_model_id': MODEL, 'identity_confidence': 'high', 'probe_status': 'passed'}],
    }
    ModelRoutingRepository().compare_and_swap(OwnerBoundary.create(OWNER), document, expected_revision=0)


def submit(text, *, scoped):
    from lib.api_keys._context import AuthContext
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.identity import principal_from_auth_context
    scopes = {'conversations'} | ({'agents:scheduler'} if scoped else set())
    principal = principal_from_auth_context(AuthContext(key_id='fixture', owner_user_id=OWNER,
        scopes=frozenset(scopes)), allow_personal_owner=False)
    conv_id = uuid4().hex
    return conversation_turn_commands.create_turn(conv_id, OWNER, {
        'commandId': uuid4().hex, 'message': {'text': text},
        'conversation': {'allowCreate': True, 'title': text, 'settings': {}},
        'config': {'model': MODEL, 'chatMode': 'chat', 'autoApply': True, 'schedulerEnabled': True,
                   'fetchEnabled': True, 'memoryEnabled': False, 'searchMode': 'off',
                   'tools': {'executionScope': 'available', 'programmaticCalling': 'off', 'toolSearch': 'off'}},
    }, request_started_at=time.time(), principal=principal).value['attempt']['attemptId']


def attempt(attempt_id):
    from lib.turn_lifecycle import get_attempt
    return get_attempt(attempt_id, user_id=OWNER, include_execution_config=True)


def worker_pid(row):
    from lib.storage.generated_worker_job_contract import WORKER_EXECUTION_CONFIG_KEY
    return row['config'][WORKER_EXECUTION_CONFIG_KEY]['process']['pid']


def ready(execution):
    record = execution.record()
    assert record.get('state') != 'failed', record
    return record if record.get('state') == 'ready' and execution.host_matches(record) else None


def shard_record_for(execution, pid):
    from serverctl_pkg.execution_shards import ExecutionShardService, RECORD_FORMAT
    from serverctl_pkg.resident_service import read_record

    for path in execution.state_path.parent.glob('execution-shards/*/execution-shard.json'):
        record = read_record(path, record_format=RECORD_FORMAT)
        if record.get('pid') == pid:
            shard = ExecutionShardService(execution.project, sys.executable, path.parent)
            assert shard.host_matches(record)
            return shard, record
    raise AssertionError(f'no ready shard record for worker {pid}')


def tcp_connection(pid, remote_port, local_port=None, *, states=('01',)):
    """Observe a loopback TCP socket belonging to this exact PID."""
    fds = set()
    for fd in Path(f'/proc/{pid}/fd').glob('*'):
        try:
            fds.add(os.readlink(fd))
        except (FileNotFoundError, ProcessLookupError):
            continue
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        _, local, remote, state, *_, inode = line.split()[:10]
        if (state in states and remote == f'0100007F:{remote_port:04X}'
                and (local_port is None or local == f'0100007F:{local_port:04X}')
                and f'socket:[{inode}]' in fds):
            return int(local.rsplit(':', 1)[1], 16)
    return None


def exercise(execution, fixture):
    from lib.scheduler.manager import ScheduledTaskManager
    from serverctl_pkg.resident_service import close_manager_control, manager_control_matches

    record = until(lambda: ready(execution))
    manager = ScheduledTaskManager()
    row = until(lambda: next((task for task in manager.list_tasks(user_id=OWNER)
                              if task.get('system_key') == 'daily-provider-probe'), None))
    fixture.schedule = row['id']
    hold_id = submit('hold-primary-fixture', scoped=False)
    assert fixture.holding.wait(WAIT), attempt(hold_id)
    assert worker_pid(attempt(hold_id)) == record['pid']
    detect_id = submit('run-detect-fixture', scoped=True)
    result = terminal_attempt(detect_id)
    shard_pid = worker_pid(result)
    assert shard_pid != record['pid'], result['status']
    shard, shard_record = shard_record_for(execution, shard_pid)
    assert shard_record['managerPid'] == record['pid']
    assert shard_record['commandControl']['serviceId'] != record['serviceId']
    assert result['status'] == 'completed'
    success_result, = fixture.results['']
    assert '[OK] Task executed' in json.dumps(success_result), success_result
    assert fixture.probes == [MODEL], fixture.probes
    outcome = manager.get_task(fixture.schedule, user_id=OWNER)
    assert json.loads(outcome['last_result']) == {'ok': True, 'providers': 1, 'cells': 1, 'grant_changes': 0}
    refusal = terminal_attempt(submit('refuse-run-detect-fixture', scoped=False))
    assert worker_pid(refusal) == shard_pid and refusal['status'] == 'completed'
    refusal_result, = fixture.results['refuse-']
    assert 'not found' in json.dumps(refusal_result).lower(), refusal_result
    assert fixture.probes == [MODEL] and manager.get_task(fixture.schedule, user_id=OWNER) == outcome
    withdrawal = withdraw(execution, fixture, register=record, shard=shard, before=outcome, manager=manager)
    fixture.release.set()
    until(lambda: attempt(hold_id)['status'] == 'completed')
    assert manager_control_matches(record, record['managerControl']['fd'], write=True)
    close_manager_control(record)  # No SIGTERM sent to the primary: real owner EOF.
    assert execution._process.wait(timeout=WAIT) == 0
    assert execution.record()['state'] == 'stopped' and not shard.host_matches(shard_record)
    print(json.dumps({'primary': record['pid'], 'shard': shard_pid, 'attempt': detect_id,
                      'schedule': fixture.schedule, 'probe_requests': fixture.probes,
                      'tool_result': success_result['content'], 'scheduler_result': outcome['last_result'],
                      'refusal': refusal_result['content'], 'withdrawal': withdrawal,
                      'owner_eof': 'primary exited 0, stopped record, shard joined'}), flush=True)


def terminal_attempt(attempt_id):
    terminal = {'completed', 'failed', 'interrupted', 'truncated'}
    return until(lambda: row if (row := attempt(attempt_id))['status'] in terminal else None)


def withdraw(execution, fixture, *, register, shard, before, manager):
    from lib.tasks_pkg.command_control_transport import call

    fixture.hold_probe = True
    attempt_id = submit('withdraw-run-detect-fixture', scoped=True)
    assert fixture.probe_entered.wait(WAIT), attempt(attempt_id)['status']
    active = attempt(attempt_id)
    pid = worker_pid(active)
    assert active['status'] == 'running' and pid != register['pid']
    _, shard_record = shard_record_for(execution, pid)
    _, provider_port = fixture.address
    assert tcp_connection(register['pid'], provider_port, fixture.probe_peer[1])
    control_port = register['commandControl']['port']
    shard_control_port = tcp_connection(pid, control_port)
    assert shard_control_port, 'entered shard has no connection to the primary control'
    copied = call(register['commandControl'], 'manual_detect', active['taskId'], user_id=OWNER,
        params={'conversation_id': active['conversationId'], 'turn_id': uuid4().hex,
                'attempt_id': attempt_id, 'schedule_id': fixture.schedule})
    assert copied == {'success': None, 'result': 'Task not found'}, copied
    assert fixture.probes == [MODEL, MODEL]
    marker = execution.state_path.parent / 'withdraw-shard-owner'
    staged = marker.with_suffix('.staged')
    staged.write_text(str(pid))
    staged.replace(marker)
    until(lambda: marker.with_suffix('.closed').exists())
    until(lambda: not tcp_connection(pid, control_port))
    until(lambda: tcp_connection(register['pid'], shard_control_port, control_port, states=('08',)))
    assert ready(execution) and not fixture.probe_release.is_set()
    assert tcp_connection(register['pid'], provider_port, fixture.probe_peer[1])
    assert manager.get_task(fixture.schedule, user_id=OWNER) == before
    fixture.probe_release.set()
    until(lambda: not tcp_connection(register['pid'], shard_control_port, control_port, states=('08',)))
    until(lambda: not shard.host_matches(shard_record))
    settled = terminal_attempt(attempt_id)
    assert settled['status'] == 'interrupted', settled['status']
    assert manager.get_task(fixture.schedule, user_id=OWNER) == before
    assert fixture.probes == [MODEL, MODEL], fixture.probes
    assert fixture.tool_calls == {'': 1, 'refuse-': 1, 'withdraw-': 1}, fixture.tool_calls
    return {'attempt': attempt_id, 'shard': pid, 'provider_pid': register['pid'],
            'attempt_status': settled['status'], 'scheduler_result_unchanged': True,
            'primary_copied_turn_refusal': copied,
            'primary_control_peer_closed_while_probe_held': True,
            'primary_relay_joined_provider_before_close': True}


def close_shard_on_request(directory, stop):
    # Test controller runs in the real pipe owner's process. It can close only
    # a verified, currently published shard writer, never a relay socket.
    from serverctl_pkg.execution_shards import RECORD_FORMAT
    from serverctl_pkg.resident_service import close_manager_control, manager_control_matches, read_record
    marker = directory / 'withdraw-shard-owner'
    while not stop.wait(.02):
        if not marker.is_file():
            continue
        wanted = int(marker.read_text() or '0')
        for path in directory.glob('execution-shards/*/execution-shard.json'):
            record = read_record(path, record_format=RECORD_FORMAT)
            if record.get('pid') == wanted and record.get('managerPid') == os.getpid():
                fd = record['managerControl']['fd']
                assert manager_control_matches(record, fd, write=True)
                close_manager_control(record)
                assert not manager_control_matches(record, fd, write=True)
                marker.with_suffix('.closed').touch()
                return


def primary():
    from serverctl_pkg.execution_host import main as host_main
    directory = Path(sys.argv[sys.argv.index('--state-file') + 1]).parent
    stop = threading.Event()
    owner = threading.Thread(target=close_shard_on_request, args=(directory, stop))
    owner.start()
    try:
        return host_main()
    finally:
        stop.set()
        owner.join()


def child(root):
    assert root.name.startswith('managed-detect-') and root.stat().st_mode & 0o077 == 0
    assert Path.cwd() == root / 'checkout' == Path(os.environ['TOFU_PROJECT_PATH'])
    assert Path(os.environ['TOFU_DATA_DIR']) == root / 'runtime'
    assert Path(os.environ['HOME']) == root / 'home'
    assert not os.environ.get('TOFU_STORAGE_CONNECTION_FILE')
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.connection_file import write_connection_document
    from lib.storage.service import install_runtime_for_test
    from serverctl_pkg.execution_service import ExecutionService

    supervisor = StorageSupervisor(connection_file='')
    runtime = StorageRuntime(supervisor, auto_restart=False)
    install_runtime_for_test(runtime)
    class FixtureExecutionService(ExecutionService):
        def launch_command(self, plan):
            return [str(Path(__file__).resolve()), '--primary', self.host_module]

    execution = FixtureExecutionService(os.getcwd(), sys.executable)
    try:
        runtime.start()
        connection = root / 'storage.json'
        write_connection_document(connection, supervisor.connection_document(service_id=uuid4().hex))
        env = {**os.environ, 'TOFU_STORAGE_CONNECTION_FILE': str(connection)}
        with fake_provider() as (fixture, port):
            seed_model(port)
            execution.ensure(execution.prepare(env), env)
            exercise(execution, fixture)
    finally:
        try:
            execution.stop(timeout=WAIT, retire_failed_close=True)
        finally:
            runtime.stop()


def main():
    source = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    print(f'Managed Detect source revision: {revision}', flush=True)
    with tempfile.TemporaryDirectory(prefix='managed-detect-') as name:
        root = Path(name)
        checkout = root / 'checkout'
        checkout.mkdir()
        with tempfile.TemporaryFile() as archive:
            subprocess.run(['git', 'archive', revision], stdout=archive, cwd=source, check=True)
            archive.seek(0)
            subprocess.run(['tar', '-xf', '-'], stdin=archive, cwd=checkout, check=True)
        for name in ('home', 'tmp', 'runtime'):
            (root / name).mkdir()
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ') if key in os.environ}
        env.update(HOME=str(root / 'home'), TMPDIR=str(root / 'tmp'), PYTHONPATH=str(checkout),
                   PYTHONUNBUFFERED='1', PYTHONNOUSERSITE='1', TOFU_PROJECT_PATH=str(checkout),
                   TOFU_DATA_DIR=str(root / 'runtime'), TOFU_DEPLOYMENT_MODE='personal',
                   TOFU_PERSONAL_STORAGE_BACKEND='sqlite', TOFU_STORAGE_FASTPATH='off',
                   TOFU_API_BASE_URL='http://127.0.0.1:9', TIKTOKEN_CACHE_DIR=str(root / 'tmp'),
                   UV_OFFLINE='1', HTTP_PROXY='http://127.0.0.1:9',
                   HTTPS_PROXY='http://127.0.0.1:9', NO_PROXY='127.0.0.1,localhost')
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child', str(root)],
                             cwd=checkout, env=env, timeout=WAIT * 5)
        if run.returncode:
            log = checkout / 'logs/server-console.log'
            if log.exists():
                print(log.read_text(errors='replace')[-20000:], file=sys.stderr)
            return run.returncode
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--child':
        child(Path(sys.argv[2]))
    elif sys.argv[1:3] == ['--primary', 'serverctl_pkg.execution_host']:
        del sys.argv[1:3]
        raise SystemExit(primary())
    else:
        raise SystemExit(main())
