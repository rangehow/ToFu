"""Count the storage work of catalog Coordinator provenance on a real authority.

A disposable SQL Sidecar (or native TofuDB with --backend tofudb and TOFUDB_BIN)
holds --admissions delegated assignments, one Worker each, admitted through the
real repository. Each read calls the tree's ``project_catalog_origins`` composed
like its provenance routes and counts every storage RPC by operation, wall
and thread CPU time; every result must equal an oracle of each Worker's oldest
Coordinator. One more admission then measures the incremental read. --writer
--writers threads keep admitting to an existing Worker, which leaves the oracle
unchanged; each read starts after a fresh commit while they keep committing, and
failed reads are reported by error type. Writes use an uncounted client. Run each pinned tree in a fresh process with --repository; counts and
failures are the result, clocks are host observations.
"""
import argparse
from collections import Counter
import hashlib
import itertools
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

SOURCES = ('lib/conversations/coordinator_conversations.py', 'lib/project_dispatch/history.py')


def retained_bytes(memo):
    """Bytes of every distinct object a provenance memo keeps alive."""
    seen, total = set(), 0
    for entry in getattr(memo, '_consumed', {}).values():
        for value in (entry, entry.after, entry.origins, *entry.origins.keys(), *entry.origins.values()):
            if id(value) not in seen:
                seen.add(id(value))
                total += sys.getsizeof(value)
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--backend', choices=('sqlite', 'tofudb'), default='sqlite')
    parser.add_argument('--admissions', type=int, default=94)
    parser.add_argument('--reads', type=int, default=5)
    parser.add_argument('--writers', type=int, default=0)
    args = parser.parse_args()
    if args.admissions < 1 or args.reads < 2 or args.writers < 0:
        parser.error('Use at least one admission, two reads and a nonnegative writer count')
    sys.path.insert(0, str(args.repository.resolve()))
    os.chdir(args.repository.resolve())
    if args.backend == 'tofudb':
        if not os.environ.get('TOFUDB_BIN'):
            parser.error('Set TOFUDB_BIN to a source-matched native engine')
        os.environ['TOFUDB_TRANSPORT'] = 'daemon'

    from lib.identity import PrincipalContext
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    from lib.conversations import coordinator_conversations
    from lib.project_dispatch.repository import DispatchContention, ProjectDispatchRepository
    import pytest
    from tests.project_mod.test_project_dispatch_repository import context

    with tempfile.TemporaryDirectory(prefix='catalog-origins-') as directory:
        supervisor = StorageSupervisor(project_root=Path(directory), backend=args.backend, startup_timeout=60)
        runtime = StorageRuntime(supervisor, auto_restart=False)
        install_runtime_for_test(runtime)
        runtime.start()
        # The dispatch fixture patches its session-model seam through pytest's monkeypatch.
        fixture_patch = pytest.MonkeyPatch()
        try:
            owner = 1
            c = context.__wrapped__(fixture_patch)
            for row in c.conversations.rows.values():
                row['user_id'] = owner
            principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                              tenant_id='tenant-a', scopes=['chat'])
            client = supervisor.client
            real_query = client.query
            uncounted = SimpleNamespace(query=real_query, command=client.command)
            writer = ProjectDispatchRepository(owner, '/project', client=lambda write=False: uncounted,
                conversations=c.conversations, turn_get=c.repo.turn_get,
                binding_repository=c.binding, clock=lambda: c.now[0])
            oracle = {}

            def admit(number, recipient=None):
                recipient = recipient or f'worker-{number:05}'
                c.conversations.rows[recipient] = {'user_id': owner, 'settings': {'projectPath': '/project'}}
                writer.authorize({**c.request, 'recipientId': recipient}, principal, f'admit-{number:05}')
                oracle.setdefault(recipient, c.request['coordinatorId'])

            for number in range(args.admissions):
                admit(number)
            calls = Counter()

            def counted(operation, payload, *rest, **options):
                collection = payload.get('collection') if isinstance(payload, dict) else None
                calls[operation + (f'[{collection}]' if collection else '')] += 1
                return real_query(operation, payload, *rest, **options)

            client.query = counted
            # Compose the projection like this tree's provenance routes do.
            composition = {}
            try:
                from lib.project_dispatch.history import PROCESS_RECIPIENT_ORIGINS
                composition['recipient_origins'] = PROCESS_RECIPIENT_ORIGINS
            except ImportError:
                pass

            def read():
                calls.clear()
                started, cpu = time.perf_counter(), time.thread_time()
                origins = coordinator_conversations.project_catalog_origins(owner, '/project', **composition)
                sample = {'ms': round((time.perf_counter() - started) * 1000, 2),
                          'cpuMs': round((time.thread_time() - cpu) * 1000, 2),
                          'rpcs': sum(calls.values()), 'operations': dict(sorted(calls.items()))}
                assert dict(origins) == oracle, 'origins differ from the admission oracle'
                return sample

            reads = [read() for _ in range(args.reads)]
            admit(args.admissions)
            after_admission = read()
            contention = None
            if args.writers:
                stop, committed = threading.Event(), threading.Event()
                numbers, writer_errors, failures, samples = itertools.count(args.admissions + 1), [], Counter(), []
                commits = Counter()

                def keep_admitting(writer_index):
                    try:
                        while not stop.is_set():
                            try:
                                admit(next(numbers), recipient='worker-00000')
                            except DispatchContention:
                                commits['conflicts'] += 1
                                continue
                            commits[writer_index] += 1
                            committed.set()
                    except BaseException as exc:
                        writer_errors.append(exc)
                        committed.set()

                threads = [threading.Thread(target=keep_admitting, args=(index,), name=f'catalog-origin-writer-{index}')
                           for index in range(args.writers)]
                for thread in threads:
                    thread.start()
                try:
                    for _ in range(args.reads):
                        # Each read starts after a fresh commit while the writer keeps committing.
                        committed.wait()
                        committed.clear()
                        if writer_errors:
                            raise writer_errors[0]
                        try:
                            samples.append(read()['ms'])
                        except Exception as exc:
                            failures[type(exc).__name__] += 1
                finally:
                    stop.set()
                    for thread in threads:
                        thread.join()
                if writer_errors:
                    raise writer_errors[0]
                settled = read()
                conflicts = commits.pop('conflicts', 0)
                contention = {'reads': args.reads, 'writers': args.writers, 'failures': dict(failures),
                              'writerCommits': sum(commits.values()), 'writerConflicts': conflicts,
                              'succeededMs': samples, 'settledRead': settled}
            client.query = real_query
            warm = [sample['ms'] for sample in reads[1:]]
            print(json.dumps({
                'backend': args.backend, 'admissions': args.admissions,
                'composition': sorted(composition),
                'sources': {name: hashlib.sha256((args.repository / name).read_bytes()).hexdigest()
                            for name in SOURCES},
                'cold': reads[0], 'warm': reads[1:], 'warmMedianMs': statistics.median(warm),
                'afterOneAdmission': after_admission,
                'retainedBytes': retained_bytes(composition.get('recipient_origins')),
                'contention': contention}, indent=1), flush=True)
        finally:
            fixture_patch.undo()
            runtime.stop()
            install_runtime_for_test(None)


if __name__ == '__main__':
    main()
