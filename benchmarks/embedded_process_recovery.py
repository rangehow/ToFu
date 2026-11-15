"""Verify acknowledged Turn/event state across real embedded-process SIGKILL.

Standalone durability evidence, not latency/capacity acceptance. Production
Turn facade and embedded client remain real; provider and queue drain excluded.
Each child owns one disposable authority; the parent kills and reaps only its
own child after an external acknowledgement manifest has been flushed.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--conversations', type=int, default=1000)
    parser.add_argument('--chunk-bytes', type=int, default=8192)
    parser.add_argument('--workers', type=int, default=0)
    parser.add_argument('--retain-authority', action='store_true',
        help='retain successful disposable authority and acknowledgement manifest for inspection')
    parser.add_argument('--owner-id', type=int, default=11)
    parser.add_argument('--tenant-id', type=int, default=7)
    parser.add_argument('--child-phase', choices=['running', 'settle', 'verify'])
    args = parser.parse_args()
    if args.conversations < 1 or args.chunk_bytes < 16 or args.workers < 0:
        parser.error('positive conversations, chunk bytes >=16 and nonnegative workers required')
    args.repo_root = args.repo_root.resolve()
    args.binary = args.binary.resolve()
    args.directory = args.directory.resolve()
    args.workers = args.workers or min(args.conversations, len(os.sched_getaffinity(0)))
    return args


def child(args):
    sys.path.insert(0, str(args.repo_root))
    from tofu_db.embedded import EmbeddedRuntime
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib import turn_lifecycle as turns
    import lib.storage

    secret = b'embedded-crash-probe-secret-000001'
    started = time.monotonic()
    runtime = EmbeddedRuntime(args.binary.parent / 'libtofu_db.so', args.directory / 'authority',
        owner_id=args.owner_id, tenant_id=args.tenant_id,
        projection_dir=args.directory / 'projection', secret=secret)
    port = EmbeddedStorageV2Client(runtime, secret, owner_id=args.owner_id,
        tenant_id=args.tenant_id, timeout=30)
    health = port.health(deadline=30)
    port.set_readiness_budget(health.get('resourceBudget'))
    open_and_health_ms = (time.monotonic() - started) * 1000

    class Client:
        def query(self, operation, payload, **kwargs):
            return port.query(operation, {**payload, 'user_id': args.owner_id}, **kwargs)

        def command(self, operation, payload, command_id, **kwargs):
            return port.command(operation, {**payload, 'user_id': args.owner_id}, command_id, **kwargs)

    client = Client()
    lib.storage.get_storage_client = lambda: client
    turns._turn_client = lambda **kwargs: client
    turns._drain_queue_after_settlement = lambda *args: None
    manifest = args.directory / 'acknowledged.json'

    def snapshot(item):
        return {**item, 'turn': turns.get_turn(item['conversation'], item['turn_id'], user_id=args.owner_id),
            'events': client.query('event.list', {'task_id': item['task_id'], 'after_sequence': -1, 'limit': 10})}

    def verify(item):
        current = snapshot(item)
        if current['turn'] != item['turn'] or current['events'] != item['events']:
            raise AssertionError(f"acknowledged state changed: {item['conversation']}")
        return item

    def publish(item, start, end, terminal):
        task = {'id': item['task_id'], '_userId': args.owner_id, '_attemptId': item['attempt_id'],
            '_transientRuntime': True, 'config': {}, 'content': item.get('content', ''),
            'thinking': '', 'status': 'running', 'segments': [], 'toolRounds': []}
        for sequence in range(start, end):
            task['content'] += (f"{item['index']}:{sequence}:" + 'x' * args.chunk_bytes)[:args.chunk_bytes]
            event = {'type': 'phase', 'phase': 'text'}
            result = turns.record_task_event(task, event, task_event={
                'task_id': task['id'], 'sequence': sequence + 1, 'event': event})
            if result != 'carried':
                raise AssertionError(f'event not acknowledged through carried path: {result}')
        if terminal:
            task['status'] = 'done'
            event = {'type': 'done', 'finishReason': 'stop'}
            result = turns.record_task_event(task, event, task_event={
                'task_id': task['id'], 'sequence': 5, 'event': event})
            if result != 'carried':
                raise AssertionError(f'terminal event not carried: {result}')
        state = snapshot({**item, 'content': task['content']})
        expected_status = 'completed' if terminal else 'running'
        if state['turn']['status'] != expected_status or state['turn']['projection']['content'] != task['content']:
            raise AssertionError('acknowledged Turn status/text differs')
        if len(state['events']) != (5 if terminal else end):
            raise AssertionError('acknowledged event coverage differs')
        return state

    def create(index):
        conversation = f'embedded-crash-conversation-{index}'
        task_id = f'embedded-crash-task-{index}'
        pair = turns.create_turn_pair(conversation, command_id=f'create-{index}',
            input_projection={'content': 'probe'}, config={'model': 'benchmark'}, user_id=args.owner_id,
            conversation_defaults={'allowCreate': True, 'title': 'embedded crash probe', 'settings': {}})
        attempt = pair['attempt']['attemptId']
        turns.bind_task(attempt, task_id, user_id=args.owner_id)
        turns.mark_task_started(attempt, task_id, user_id=args.owner_id)
        return publish({'index': index, 'conversation': conversation, 'task_id': task_id,
            'attempt_id': attempt, 'turn_id': pair['turn']['turnId']}, 0, 2, False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        if args.child_phase == 'running':
            rows = list(pool.map(create, range(args.conversations)))
        else:
            rows = json.loads(manifest.read_text())
            if len(rows) != args.conversations:
                raise AssertionError('manifest does not cover requested conversations')
            rows = list(pool.map(verify, rows))
            if args.child_phase == 'settle':
                rows = list(pool.map(lambda row: publish(row, 2, 4, True), rows))
    if args.child_phase != 'verify':
        temporary = manifest.with_suffix('.pending')
        with temporary.open('w') as stream:
            json.dump(rows, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(manifest)
    print(json.dumps({'phase': args.child_phase, 'pid': os.getpid(), 'exact_conversations': len(rows),
        'exact_event_rows': sum(len(row['events']) for row in rows),
        'embedded_open_and_health_ms': open_and_health_ms}), flush=True)
    # Parent must SIGKILL this open runtime, never trigger graceful/abandon close.
    sys.stdin.read(1)
    raise RuntimeError('supervisor closed stdin without SIGKILL')


def run_child(command, log, phase, conversations, event_rows, timeout=900):
    """Accept one child receipt, then kill and reap that exact owned process."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=log)
    try:
        deadline = time.monotonic() + timeout
        receipt = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while b'\n' not in receipt:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(timeout=remaining):
                    raise TimeoutError(f'{phase} child produced no complete acknowledgement')
                data = os.read(process.stdout.fileno(), 4096)
                if not data:
                    raise RuntimeError(f'{phase} child exited before acknowledgement')
                receipt.extend(data)
                if len(receipt) > 4096:
                    raise RuntimeError('child acknowledgement exceeds metadata bound')
        result = json.loads(receipt.split(b'\n', 1)[0])
        if result['pid'] != process.pid or result['phase'] != phase:
            raise AssertionError('child acknowledgement identity differs')
        if result['exact_conversations'] != conversations or result['exact_event_rows'] != event_rows:
            raise AssertionError('child acknowledgement coverage differs')
        process.kill()
        if process.wait(timeout=30) != -signal.SIGKILL:
            raise AssertionError('child did not terminate by SIGKILL')
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        process.stdin.close()
        process.stdout.close()


def run(args):
    sys.path.insert(0, str(args.repo_root))
    from serverctl_pkg.native_engine import source_fingerprint, verify_current, _cached_library_matches_source
    source = source_fingerprint(args.repo_root)
    common_paths = ['lib/turn_lifecycle.py', 'lib/tasks_pkg/turn_trace.py',
        'lib/storage/v2_client.py', 'lib/storage/v2_embedded_client.py',
        'lib/storage/v2_semantics.py', 'tofu_db/embedded.py']
    def common_hashes():
        return {name: hashlib.sha256((args.repo_root / name).read_bytes()).hexdigest()
            for name in common_paths}
    common_before = common_hashes()
    verify_current(args.binary, args.repo_root)
    if not _cached_library_matches_source(args.binary, source):
        raise RuntimeError('embedded library source differs from executable')
    args.directory.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='embedded-process-crash-', dir=args.directory))
    print(f'Disposable authority: {directory}', file=sys.stderr, flush=True)
    pinned = directory / 'build'
    pinned.mkdir()
    for name in ['tofu-db', 'libtofu_db.so']:
        shutil.copy2(args.binary.parent / name, pinned / name)
    args.binary = pinned / 'tofu-db'
    verify_current(args.binary, args.repo_root)
    if not _cached_library_matches_source(args.binary, source):
        raise RuntimeError('pinned library does not match source')
    args.binary.chmod(0o500)
    (pinned / 'libtofu_db.so').chmod(0o400)
    subprocess.run([str(args.binary), 'init', '--data-dir', str(directory / 'authority')],
        check=True, capture_output=True, timeout=120)
    rows = []
    for phase in ['running', 'settle', 'verify']:
        command = [sys.executable, str(Path(__file__).resolve()), '--repo-root', str(args.repo_root),
            '--binary', str(args.binary), '--directory', str(directory), '--output', str(args.output),
            '--conversations', str(args.conversations), '--chunk-bytes', str(args.chunk_bytes),
            '--workers', str(args.workers), '--owner-id', str(args.owner_id), '--tenant-id', str(args.tenant_id),
            '--child-phase', phase]
        with (directory / f'{phase}.log').open('w') as log:
            result = run_child(command, log, phase, args.conversations,
                args.conversations * (2 if phase == 'running' else 5))
            rows.append(result)
            print(json.dumps(result), flush=True)
    verify = subprocess.run([str(args.binary), 'verify', '--data-dir', str(directory / 'authority')],
        capture_output=True, text=True, timeout=900)
    common_after = common_hashes()
    if verify.returncode or source_fingerprint(args.repo_root) != source or common_after != common_before:
        raise RuntimeError('offline integrity failed or native/declared common sources changed')
    temporary_file_bytes = sum((Path(parent) / name).stat().st_size
        for parent, _, names in os.walk(directory) for name in names)
    report = {'format': 'tofu.embedded-process-recovery/v1', 'source': source, 'durability': 'strict',
        'crash_mechanism': 'process-sigkill', 'conversations': args.conversations,
        'chunk_bytes': args.chunk_bytes, 'workers': args.workers, 'phases': rows,
        'integrity_returncode': verify.returncode, 'authority_directory': str(directory),
        'authority_retained': args.retain_authority, 'temporary_file_bytes_before_cleanup': temporary_file_bytes,
        'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        'library_sha256': hashlib.sha256((args.binary.parent / 'libtofu_db.so').read_bytes()).hexdigest(),
        'benchmark_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'common_source_sha256_before': common_before, 'common_source_sha256_after': common_after,
        'timing_scope': 'Embedded open and client health only; excludes process launch and replay comparison. No latency verdict.',
        'scope': 'Exact acknowledged running/final Turn documents, text and event rows including timestamps. '
            'Real embedded process kill/reap/reopen. No performance verdict; original abandon benchmark coverage remains incomplete.'}
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    if not args.retain_authority:
        # Only our mkdtemp child is disposable; never delete the supplied parent.
        # Every actor and offline verifier has exited before reclamation.
        shutil.rmtree(directory)


if __name__ == '__main__':
    options = arguments()
    child(options) if options.child_phase else run(options)
