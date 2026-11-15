"""Measure resume passes over held task-delta conflicts (the delivery-lane shape).

Production on 2026-11-08 held 29 settled ``task_delta_conflict`` receipts, the
largest with ~1,100-path deltas, and every resume pass rechecked each of them.
This builds a disposable SQLite Sidecar and canonical Git checkout below a
--root-depth deep root, gives --tasks automatic Worker workspaces --files new
files each that canonical also created with different content (add/add), and
settles them through the production receivers until each is a held conflict.
After the filesystem timestamp resolution has passed, it runs --passes
``resume_pending_workspace_deliveries`` passes and reports process CPU (this
interpreter, all threads), reaped Git child CPU, wall time and claims-lease
admissions per pass. Every arm must leave the identical held receipts (digest).

Run each pinned tree in a fresh process with --repository; it writes only below
a private temporary directory it removes. Local-disk clocks under-state the
network-volume stat costs of the lease; Git work runs in child processes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, help='tree whose lib/ is measured')
    parser.add_argument('--tasks', type=int, default=2)
    parser.add_argument('--files', type=int, default=1100)
    parser.add_argument('--dirs', type=int, default=60)
    parser.add_argument('--root-depth', type=int, default=9)
    parser.add_argument('--passes', type=int, default=4)
    args = parser.parse_args()
    base = Path(tempfile.mkdtemp(prefix='held-conflicts-'))
    os.environ['TOFU_DATA_DIR'] = str(base / 'data')
    os.environ['TOFU_INTEGRATION_TEST_CMD'] = ''
    os.environ['TOFU_INTEGRATION_WORKSPACE_DIR'] = str(base / 'managed')
    sys.path.insert(0, str(Path(args.repository).resolve()))
    import lib
    from lib import integration_control as ic, integration_state_repository as state
    from lib.project_mod import delivery, task_delta_delivery
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test

    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=base, backend='sqlite', startup_timeout=120), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        ic._start_or_wake_worker = lambda: None
        ic._push = lambda *a, **k: None
        delivery._canonical_in_use = lambda *a: False
        root = base
        for depth in range(args.root_depth - len(base.parts) + 1):
            root = root / f'level{depth}'
        root.mkdir(parents=True)
        git(root, 'init', '-q', '-b', 'main')
        git(root, 'config', 'user.name', 'bench')
        git(root, 'config', 'user.email', 'bench@example.invalid')
        (root / 'base.txt').write_text('base\n')
        git(root, 'add', 'base.txt')
        git(root, 'commit', '-q', '-m', 'base')
        paths = [f'd{n % args.dirs:03}/e{n % 7}/f{n:05}.txt' for n in range(args.files)]
        owner = 1
        for task in range(args.tasks):
            task_id = f'held-{task}'
            created = ic.create_task_workspace(str(root), task_id, user_id=owner, conversation_id='bench')
            delivery.bind_task_workspace(str(root), task_id, user_id=owner, conversation_id='bench')
            work = Path(created['workspacePath'])
            for path in paths:
                for checkout, text in ((work, f'task {task}\n'), (root, f'canonical {task}\n')):
                    target = checkout / f't{task}' / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
            delivery.settle_task_workspace(str(root), task_id, user_id=owner, outcome='completed')
        delivery.resume_pending_workspace_deliveries()

        def receipts():
            return {f'held-{task}': state.get_workspace(str(root), f'held-{task}', user_id=owner)
                    ['origin']['delivery'] for task in range(args.tasks)}
        held = receipts()
        states = sorted({(value['state'], (value.get('retry') or {}).get('condition')) for value in held.values()})
        time.sleep(2.1)  # past the timestamp resolution of every file written above
        # As after a restart: the first measured pass starts with nothing remembered.
        getattr(task_delta_delivery, '_UNCHANGED_HOLDS', {}).clear()
        leases = []
        original = task_delta_delivery.workspace_claims_lease

        def counting(claims, **kwargs):
            leases.append(len(claims))
            return original(claims, **kwargs)
        task_delta_delivery.workspace_claims_lease = counting
        passes = []
        for _ in range(args.passes):
            before = os.times()
            started, cpu, leased = time.perf_counter(), time.process_time(), len(leases)
            delivery.resume_pending_workspace_deliveries()
            after = os.times()
            passes.append({'cpu_s': round(time.process_time() - cpu, 3),
                           'git_child_cpu_s': round(after.children_user + after.children_system
                                                    - before.children_user - before.children_system, 3),
                           'wall_s': round(time.perf_counter() - started, 3),
                           'claims_leases': len(leases) - leased})
        final = receipts()
        assert final == held, 'a pass changed a held receipt'
        digest = hashlib.sha256(json.dumps(
            {task: {k: v for k, v in value.items() if k != 'updatedAt'} for task, value in final.items()},
            sort_keys=True).encode()).hexdigest()
        print(json.dumps({'lib': lib.__file__, 'tasks': args.tasks, 'files_per_delta': args.files,
                          'held': states, 'receipts_sha256': digest[:16], 'passes': passes,
                          'cpu_s_total': round(sum(p['cpu_s'] for p in passes), 3)}))
    finally:
        install_runtime_for_test(None)
        runtime.stop()
        shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    main()
