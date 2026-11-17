"""Time boot's active work-item recovery against a real storage authority.

Usage (from a checkout, with a clean environment)::

    python3 benchmarks/startup_recovery_batch.py <scratch root> [--items N] [--projects P]
        [--backend tofudb|sqlite] [--runs R]

The scratch root holds the instance's own storage authority and Git checkouts; put it on
the volume being measured (the live host's DolphinFS mount). Every work item is active and
its task result is a restart-interrupted attempt, the shape the live execution host met at
2026-11-17 00:15 (32 such items over 4 projects, 10 s before dispatch recovery). Such items
stay active, so every run measures the same boot pass. Prints one JSON line per run and a
summary with the median. Touches nothing outside the scratch root, which is removed unless
``--keep``. ``TOFU_BENCH_CHECKOUT`` names the tree whose ``lib`` is measured (an exported
pre-change tree for the before arm); the summary prints the ``lib`` it imported.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
OWNER = 1


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(['git', '-C', str(root), *arguments], check=True, capture_output=True, timeout=120)


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, 'init', '-q', '-b', 'main')
    _git(path, '-c', 'user.name=bench', '-c', 'user.email=bench@example.invalid',
         'commit', '-q', '--allow-empty', '-m', 'base')
    return path


def _seed(client, projects: list[Path], items: int) -> None:
    from lib.conversations.project_brain import deterministic_work_id
    now = int(time.time() * 1000)
    for index in range(items):
        project = str(projects[index % len(projects)])
        task_id = f'bench-task-{index:04d}'
        work_id = deterministic_work_id(task_id)
        client.command('task_results.checkpoint', {
            'key': task_id, 'expected_version': 0,
            'value': {'task_id': task_id, 'conv_id': f'bench-conv-{index:04d}', 'user_id': OWNER,
                      'content': '', 'status': 'interrupted', 'created_at': now, 'completed_at': now,
                      'config': {'projectPath': project}},
        }, None)
        client.command('project_brain.work.start', {
            'owner_user_id': OWNER, 'project_key': project, 'timestamp': now,
            'work_item': {'id': work_id, 'taskId': task_id,
                          'conversationId': f'bench-conv-{index:04d}', 'title': 'bench',
                          'trigger': 'file_write', 'status': 'active', 'changedPaths': [], 'artifacts': [],
                          'resultSummary': '', 'startedAt': now, 'finishedAt': None,
                          '_titlePriority': 1, '_titleRefined': False},
        }, f'project-work-start:{work_id}')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('scratch', type=Path)
    parser.add_argument('--items', type=int, default=32)
    parser.add_argument('--projects', type=int, default=4)
    parser.add_argument('--backend', choices=('tofudb', 'sqlite'), default='tofudb')
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--keep', action='store_true', help='keep the scratch authority and checkouts')
    args = parser.parse_args(argv)

    from lib.storage import StorageRuntime, StorageSupervisor, get_storage_client
    from lib.storage.service import install_runtime_for_test
    import lib
    from lib.conversations import project_brain_startup

    args.scratch.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='recovery-batch-', dir=args.scratch))
    projects = [_checkout(root / 'projects' / f'p{index}') for index in range(args.projects)]
    runtime = StorageRuntime(StorageSupervisor(project_root=root / 'storage', backend=args.backend,
                                               startup_timeout=120), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        _seed(get_storage_client(write=True), projects, args.items)
        timings = []
        for run in range(args.runs):
            started = time.monotonic()
            recovered = project_brain_startup.recover_active_work_items()
            timings.append(time.monotonic() - started)
            print(json.dumps({'run': run, 'seconds': round(timings[-1], 3), 'recovered': recovered}),
                  flush=True)
        print(json.dumps({'checkout': str(Path(lib.__file__).resolve().parents[1]), 'backend': args.backend, 'items': args.items,
                          'projects': args.projects, 'median_seconds': round(statistics.median(timings), 3),
                          'runs': [round(value, 3) for value in timings]}), flush=True)
    finally:
        install_runtime_for_test(None)
        runtime.stop()
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.path.insert(0, os.environ.get('TOFU_BENCH_CHECKOUT') or str(ROOT))
    sys.exit(main())
