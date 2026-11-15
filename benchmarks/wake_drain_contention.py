"""Settlement-drain recovery passes on a real authority while Workers keep committing.

Reproduces the 11-10 production failure in isolation: a disposable SQL Sidecar
(or native TofuDB with --backend tofudb and TOFUDB_BIN) holds --admissions
grants admitted through the real repository (production tofu: 534), and
--writers threads each keep committing one ledger change to their own grant
every --writer-interval-ms, like active Workers' tool and model ledgers. Each
of --drains passes then runs the tree's ``recover_project_dispatch`` for one
settled lane exactly as the settlement drain calls it, with a recording
dispatch port, and is classified as completed or failed by error type; a
completed pass must have retried that lane's one unstarted grant. Every grant
carries --ledger model-attempt entries, so documents have the ledger-sized
bodies of long-running Workers. Run each pinned tree in a fresh process with
--repository; success counts are the result, clocks are host observations.
Writes use an uncounted client.
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

SOURCES = ('lib/project_dispatch/recovery.py', 'lib/project_dispatch/segmented.py',
           'lib/project_dispatch/repository.py')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--backend', choices=('sqlite', 'tofudb'), default='sqlite')
    parser.add_argument('--admissions', type=int, default=534)
    parser.add_argument('--writers', type=int, default=20)
    parser.add_argument('--writer-interval-ms', type=int, default=250)
    parser.add_argument('--ledger', type=int, default=100)
    parser.add_argument('--drains', type=int, default=6)
    args = parser.parse_args()
    if args.admissions < args.writers + 1 or args.drains < 1 or args.writers < 0:
        parser.error('Use more admissions than writers, at least one drain and nonnegative writers')
    sys.path.insert(0, str(args.repository.resolve()))
    os.chdir(args.repository.resolve())
    if args.backend == 'tofudb':
        if not os.environ.get('TOFUDB_BIN'):
            parser.error('Set TOFUDB_BIN to a source-matched native engine')
        os.environ['TOFUDB_TRANSPORT'] = 'daemon'

    from lib.identity import PrincipalContext
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    from lib.project_dispatch import recovery
    from lib.project_dispatch.repository import DispatchContention, ProjectDispatchRepository
    import pytest
    from tests.project_mod.test_project_dispatch_repository import context, model_attempt

    with tempfile.TemporaryDirectory(prefix='wake-drain-contention-') as directory:
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

            def repository(storage):
                return ProjectDispatchRepository(owner, '/project', client=lambda write=False: storage,
                    conversations=c.conversations, turn_get=c.repo.turn_get,
                    binding_repository=c.binding, clock=lambda: c.now[0])
            writer = repository(uncounted)
            # Production grants carry briefs and ledgers; the brief keeps each
            # document at a realistic size without exceeding any request bound.
            brief = ('Delegated project work with its acceptance criteria. ' * 70)[:3800]
            budget = {**c.request['budget'], 'maxModelRequests': args.ledger + 1}
            grants = []
            for number in range(args.admissions):
                recipient = f'worker-{number:05}'
                c.conversations.rows[recipient] = {'user_id': owner, 'settings': {'projectPath': '/project'}}
                grant = writer.authorize({**c.request, 'recipientId': recipient, 'brief': brief,
                                          'budget': budget}, principal, f'admit-{number:05}')
                if args.ledger:
                    writer.mutate(grant['id'], lambda row, number=number: row.update(modelAttempts=[
                        model_attempt(f'ledger-{number}-{entry}') for entry in range(args.ledger)]))
                grants.append(grant)
            calls = Counter()

            def counted(operation, payload, *rest, **options):
                calls[operation] += 1
                return real_query(operation, payload, *rest, **options)
            reader = repository(SimpleNamespace(query=counted, command=client.command))
            stop, committed, writer_errors, commits = threading.Event(), threading.Event(), [], Counter()

            def keep_committing(index):
                ledger = itertools.count()
                own = repository(uncounted)
                try:
                    while not stop.wait(args.writer_interval_ms / 1000):
                        label = f'writer-{index}-{next(ledger)}'
                        try:
                            own.mutate(grants[index]['id'], lambda row: row.update(
                                modelAttempts=row['modelAttempts'][:-1] + [model_attempt(label)]))
                        except DispatchContention:
                            commits['conflicts'] += 1
                            continue
                        commits['commits'] += 1
                        committed.set()
                except BaseException as exc:
                    writer_errors.append(exc)
                    committed.set()

            threads = [threading.Thread(target=keep_committing, args=(index,), name=f'wake-writer-{index}')
                       for index in range(args.writers)]
            for thread in threads:
                thread.start()
            outcomes, samples, rpcs = Counter(), [], []
            try:
                for drain in range(args.drains):
                    # A settled lane at the end of the admission order: its one grant
                    # never ran, so a completed pass must retry exactly it.
                    lane = grants[args.writers + drain % (args.admissions - args.writers)]
                    dispatched = []
                    service = SimpleNamespace(dispatch_authorized=lambda binding: (
                        dispatched.append(binding['assignmentId']) or {'dispatch': 'deferred'}))
                    # Each drain starts after a fresh commit while the writers keep committing.
                    if args.writers:
                        committed.wait()
                        committed.clear()
                    calls.clear()
                    started = time.perf_counter()
                    try:
                        stats = recovery.recover_project_dispatch(
                            owner, '/project', recipient_id=lane['request']['recipientId'], repository=reader,
                            service=service, was_aborted_after=lambda *rest: False, clock=lambda: c.now[0])
                    except Exception as exc:
                        outcomes[type(exc).__name__] += 1
                    else:
                        assert stats['failed'] == 0 and dispatched == [lane['id']], (stats, dispatched)
                        outcomes['completed'] += 1
                    samples.append(round((time.perf_counter() - started) * 1000, 1))
                    rpcs.append(sum(calls.values()))
                    if writer_errors:
                        raise writer_errors[0]
            finally:
                stop.set()
                for thread in threads:
                    thread.join()
            print(json.dumps({
                'backend': args.backend, 'admissions': args.admissions, 'writers': args.writers,
                'writerIntervalMs': args.writer_interval_ms, 'ledger': args.ledger, 'drains': args.drains,
                'sources': {name: hashlib.sha256((args.repository / name).read_bytes()).hexdigest()[:12]
                            for name in SOURCES},
                'outcomes': dict(outcomes),
                'completedRate': round(outcomes['completed'] / args.drains, 3),
                'drainMs': {'median': statistics.median(samples), 'max': max(samples)},
                'drainRpcs': {'median': statistics.median(rpcs), 'max': max(rpcs)},
                'writerCommits': commits['commits'], 'writerConflicts': commits['conflicts']}, indent=1),
                flush=True)
        finally:
            fixture_patch.undo()
            runtime.stop()
            install_runtime_for_test(None)


if __name__ == '__main__':
    main()
