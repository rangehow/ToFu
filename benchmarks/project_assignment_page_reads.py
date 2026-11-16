"""Count the storage work of one Project assignment history page on a real authority.

A disposable SQL Sidecar (or native TofuDB with --backend tofudb and TOFUDB_BIN)
holds one full page of bound assignments whose settled Worker Turns carry
multi-MB projections; every other row uses a file profile, so its delivery
receipt is read too. With --dependencies every file row also pins the preceding
consult row as an accepted prerequisite, so each build evaluates prerequisite
evidence. Each page build counts every storage RPC by operation name (a Turn read
is split into body and metadata reads), its round trips (a lone query is one; a
tree that pipelines independent reads through ``query_many`` counts one per
exchange), the encoded JSON bytes it returned, and the compact JSON bytes of
the page the route encodes. Every page must equal an
oracle rebuilt independently from complete Turn reads. Run each pinned tree in a
fresh process with --repository.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--backend', choices=('sqlite', 'tofudb'), default='sqlite')
    parser.add_argument('--rows', type=int, default=32)
    parser.add_argument('--projection-bytes', type=int, default=2 * 1024 * 1024)
    parser.add_argument('--builds', type=int, default=5)
    parser.add_argument('--dependencies', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.rows <= 32 or args.builds < 2 or args.projection_bytes < 4096:
        parser.error('Use one page (1..32 rows), at least two builds and >= 4 KiB projections')
    sys.path.insert(0, str(args.repository.resolve()))
    os.chdir(args.repository.resolve())
    if args.backend == 'tofudb':
        if not os.environ.get('TOFUDB_BIN'):
            parser.error('Set TOFUDB_BIN to a source-matched native engine')
        os.environ['TOFUDB_TRANSPORT'] = 'daemon'

    from lib.identity import PrincipalContext
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    from lib import turn_lifecycle as turns
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.service import ProjectDispatchService
    from tests.project_mod.test_project_dispatch_repository import context

    import pytest
    fixture_patches = pytest.MonkeyPatch()
    with tempfile.TemporaryDirectory(prefix='assignment-page-') as directory:
        supervisor = StorageSupervisor(project_root=Path(directory), backend=args.backend, startup_timeout=60)
        runtime = StorageRuntime(supervisor, auto_restart=False)
        install_runtime_for_test(runtime)
        runtime.start()
        try:
            owner = 1
            # The dispatch fixture patches what it needs through pytest's monkeypatch.
            c = context.__wrapped__(fixture_patches)
            for row in c.conversations.rows.values():
                row['user_id'] = owner
            principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                              tenant_id='tenant-a', scopes=['chat'])
            def turn_get(conversation, identity, **options):
                # The human authorization source is the fixture's; Worker results are real Turns.
                read = c.repo.turn_get if conversation == 'coordinator' else turns.get_turn
                return read(conversation, identity, **options)
            repo = ProjectDispatchRepository(owner, '/project', client=runtime.client,
                conversations=c.conversations, turn_get=turn_get,
                binding_repository=c.binding, clock=lambda: c.now[0])
            filler = 'x' * 1024
            rounds = max(1, args.projection_bytes // (64 * 1024))
            projections, pins = {}, {}
            for number in range(args.rows):
                task_id = f'worker-task-{number:02}'
                created = turns.create_turn_pair('recipient', command_id=task_id + '-pair',
                    input_projection={'content': 'brief'}, config={'model': 'model-a'}, user_id=owner,
                    conversation_defaults={'allowCreate': True, 'title': 'Worker', 'settings': {}})
                attempt_id = created['attempt']['attemptId']
                turns.bind_task(attempt_id, task_id, user_id=owner)
                task = {'_attemptId': attempt_id, 'id': task_id, 'status': 'running', '_userId': owner,
                        '_principalContext': principal.to_payload(), 'content': 'seed', 'thinking': '',
                        'toolRounds': [], 'model': 'model-a', 'config': {'model': 'model-a'},
                        'convId': 'recipient', '_turnId': created['turn']['turnId']}
                turns.record_task_event(task, {'type': 'delta', 'content': 'seed'})
                turns.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
                turn_id = created['turn']['turnId']
                current = turns.get_turn('recipient', turn_id, user_id=owner)
                content = f'Worker {number} report. ' + 'finding ' * 600
                projection = {'content': content, 'toolRounds': [
                    {'roundNum': index, 'llmRound': index, 'toolCallId': f'call-{number}-{index}',
                     'toolName': 'read_files', 'toolArgs': '{}', 'status': 'done',
                     'toolContent': filler * 64} for index in range(rounds)]}
                turns.update_turn_projection('recipient', turn_id, projection=projection,
                    expected_projection_revision=current['projectionRevision'], user_id=owner)
                request = deepcopy(c.request)
                if number % 2:
                    request['profile'] = {'mode': 'files', 'tools': ['read_files'], 'readPaths': ['.'],
                                          'writePaths': [], 'maxToolCalls': 9}
                    if args.dependencies:
                        request['dependencies'] = [pins[number - 1]]
                assignment = repo.authorize(request, principal, f'command-{number:02}')
                repo.bind_execution(assignment['id'], turn_id, attempt_id)
                projections[turn_id] = content
                pins[number] = {'assignmentId': assignment['id'], 'grantRevision': 1, 'resultRevision':
                                turns.get_turn('recipient', turn_id, user_id=owner)['projectionRevision']}
                if args.dependencies and not number % 2:
                    repo.record_review(assignment['id'], 'accept', 'Prerequisite checked',
                                       pins[number]['resultRevision'], 1)

            def oracle():
                expected = []
                for row in repo.history_page()['rows']:
                    association = row['execution']
                    turn = turns.get_turn('recipient', association['turnId'], user_id=owner)
                    attempt = turns.get_attempt(association['attemptId'], user_id=owner)
                    content = turn['projection'].get('content')
                    assert content == projections[association['turnId']]
                    expected.append({'assignmentId': row['id'], 'execution': {
                        'turnId': association['turnId'], 'attemptId': association['attemptId'],
                        'taskId': attempt.get('taskId') or '', 'status': attempt.get('status') or 'unknown',
                        'resultRevision': turn['projectionRevision'],
                        'result': {'content': content[:2000]} if isinstance(content, str) else {}}})
                    if 'dependencies' in row['request']:
                        # Each prerequisite is a completed, accepted consult result.
                        expected[-1]['dependencyStates'] = [
                            {'reference': reference, 'status': 'ready', 'reason': 'ready'}
                            for reference in row['request']['dependencies']]
                return expected

            expected = oracle()
            client = supervisor.client
            real_query = client.query
            # A tree whose client pipelines independent reads: one round trip, many reads.
            real_many = getattr(client, 'query_many', None)
            calls, exchanges, inside = [], [], threading.local()

            def read_name(operation, payload):
                if operation == 'turn.get':
                    return operation + ('[metadata]' if payload.get('include_projection') is False else '[body]')
                return operation

            def counted(operation, payload, *rest, **options):
                if getattr(inside, 'active', False):
                    return real_query(operation, payload, *rest, **options)
                result = real_query(operation, payload, *rest, **options)
                exchanges.append(1)
                calls.append((read_name(operation, payload), result))
                return result

            def counted_many(requests, *rest, **options):
                inside.active = True
                try:
                    results = real_many(requests, *rest, **options)
                finally:
                    inside.active = False
                exchanges.append(len(requests))
                calls.extend((read_name(operation, payload), result)
                             for (operation, payload), result in zip(requests, results))
                return results

            def count(enabled):
                client.query = counted if enabled else real_query
                if real_many is not None:
                    client.query_many = counted_many if enabled else real_many

            count(True)
            # Compose the service like routes/api_v1/project_brain.py does in this tree.
            composition = {}
            try:
                from lib.project_dispatch.service import PROCESS_HISTORY_PREVIEWS
                composition['history_previews'] = PROCESS_HISTORY_PREVIEWS
            except ImportError:
                pass
            service = ProjectDispatchService(repo, commands=object(), **composition)
            builds = []
            for build in range(args.builds + 1):
                if build == args.builds:
                    # One Worker Turn changes between builds; its new revision
                    # must be displayed exactly while the others stay unchanged.
                    count(False)
                    changed = repo.history_page()['rows'][0]['execution']['turnId']
                    current = turns.get_turn('recipient', changed, user_id=owner)
                    projections[changed] = 'Revised report. ' + current['projection']['content']
                    turns.update_turn_projection('recipient', changed,
                        projection={**current['projection'], 'content': projections[changed]},
                        expected_projection_revision=current['projectionRevision'], user_id=owner)
                    expected = oracle()
                    count(True)
                calls.clear()
                exchanges.clear()
                started = time.perf_counter()
                page = service.history_page(principal=principal)
                elapsed = (time.perf_counter() - started) * 1000
                projected = [{'assignmentId': view['assignmentId'], 'execution': view['execution'],
                              **({'dependencyStates': view['dependencyStates']} if 'dependencyStates' in view else {})}
                             for view in page['assignments']]
                assert projected == expected, 'page differs from the complete-read oracle'
                operations, response_bytes = Counter(), Counter()
                for name, result in calls:
                    operations[name] += 1
                    response_bytes[name] += len(json.dumps(result, ensure_ascii=False,
                                                           separators=(',', ':')).encode())
                builds.append({'build': build if build < args.builds else 'one-turn-changed',
                               'ms': round(elapsed, 1), 'rpcs': sum(operations.values()),
                               'round_trips': len(exchanges),
                               'response_bytes': sum(response_bytes.values()),
                               'page_bytes': len(json.dumps(page, ensure_ascii=False,
                                                            separators=(',', ':')).encode()),
                               'operations': dict(sorted(operations.items())),
                               'operation_bytes': dict(sorted(response_bytes.items()))})
            count(False)
            print(json.dumps({'backend': args.backend, 'rows': args.rows,
                              'projection_bytes': len(json.dumps(projection, separators=(',', ':'))),
                              'warm_median_ms': round(statistics.median(item['ms'] for item in builds[1:-1]), 1),
                              'builds': builds}, indent=1), flush=True)
        finally:
            runtime.stop()
            install_runtime_for_test(None)
            fixture_patches.undo()


if __name__ == '__main__':
    main()
