"""Measure one delivery-shaped ``workspace_claims_lease`` admission.

Task-delta delivery admits one ``('file', path)`` claim per changed path of a
task delta (``lib/project_mod/task_delta_delivery.py``). Production deltas
reach ~1,100 paths in a checkout nine directories below ``/``. This builds a
disposable Git checkout with --files tracked files spread over --dirs nested
directories below a --root-depth deep root, then times complete entry and exit
of that admission (process CPU and wall clock) in this one process.

Run each pinned tree in a fresh process with --repository; the checkout and
claim set are deterministic, and the result reports the physical key count so
compared trees must agree on the admitted set. Clocks are host observations of
this one component (local disk, uncontended), not delivery latency.
"""
import argparse
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def build_checkout(base, args):
    root = Path(base)
    for depth in range(args.root_depth - len(root.parts) + 1):
        root = root / f'level{depth}'
    root.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    paths = []
    for number in range(args.files):
        directory = root
        for level in range(args.dir_depth):
            directory = directory / f'd{level}-{(number // (args.files // args.dirs or 1)) % args.dirs:03}'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'file{number:05}.txt'
        path.write_text(f'{number}\n')
        paths.append(str(path))
    subprocess.run(['git', '-C', str(root), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=b', '-c', 'user.email=b@b',
                    'commit', '-q', '-m', 'seed'], check=True)
    return root, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, help='tree whose lib/ is measured')
    parser.add_argument('--files', type=int, default=1100)
    parser.add_argument('--dirs', type=int, default=60)
    parser.add_argument('--dir-depth', type=int, default=3)
    parser.add_argument('--root-depth', type=int, default=9)
    parser.add_argument('--samples', type=int, default=3)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repository).resolve()))
    import lib
    from lib.project_mod import workspace_lease

    base = tempfile.mkdtemp(prefix='claims-lease-')
    try:
        root, paths = build_checkout(base, args)
        claims = [('file', path) for path in paths]
        cpu, wall, keys = [], [], set()
        for _ in range(args.samples):
            started_cpu, started_wall = time.process_time(), time.perf_counter()
            with workspace_lease.workspace_claims_lease(claims, timeout=5.0, purpose='benchmark'):
                keys.add(len(workspace_lease._registry))
            cpu.append(time.process_time() - started_cpu)
            wall.append(time.perf_counter() - started_wall)
        assert not workspace_lease._registry and not workspace_lease._active_descriptors
        print(json.dumps({
            'lib': lib.__file__, 'files': len(claims), 'root': str(root),
            'registry_keys_while_held': sorted(keys),
            'cpu_seconds': [round(value, 3) for value in cpu],
            'wall_seconds': [round(value, 3) for value in wall],
            'cpu_median_ms_per_file': round(statistics.median(cpu) * 1000 / len(claims), 3),
        }))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    main()
