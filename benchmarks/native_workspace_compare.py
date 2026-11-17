"""Compare native workspace operations with Git on identical synthetic files.

This is a component diagnostic, not production integration or an application
speed claim. Fresh alternating workers measure equivalent complete captures,
path-scoped commits, full WIP captures, and revision switches. A full content
digest after each operation makes unequal outcomes visible before comparison.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def populate(root, files):
    for index in range(files):
        path = root / f'dir-{index % 60:03}' / 'nested' / f'file-{index:05}.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'initial deterministic content {index}\n')


def entries(root):
    result = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in {'.git', '.tofu-workspace'})
        for name in sorted(filenames):
            path = Path(directory) / name
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def digest(root):
    hasher = hashlib.sha256()
    by_path = entries(root)
    for relative in sorted(by_path):
        hasher.update(relative.encode())
        hasher.update(b'\0')
        hasher.update((root / relative).read_bytes())
        hasher.update(b'\0')
    return hasher.hexdigest(), len(by_path)


def run_git(root, files, repository):
    def git(*args):
        env = dict(os.environ, GIT_AUTHOR_NAME='measure', GIT_AUTHOR_EMAIL='m@localhost',
                   GIT_COMMITTER_NAME='measure', GIT_COMMITTER_EMAIL='m@localhost')
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=root,
                       env=env, check=True, stdout=subprocess.DEVNULL)

    def measure(operation, function):
        started_cpu, started_wall = time.process_time(), time.perf_counter()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        function()
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        content, count = digest(root)
        return {'engine': 'git', 'operation': operation,
                'wall_ms': (time.perf_counter() - started_wall) * 1000,
                'cpu_ms': (time.process_time() - started_cpu) * 1000,
                'child_cpu_ms': ((after.ru_utime + after.ru_stime)
                                  - (before.ru_utime + before.ru_stime)) * 1000,
                'cpu_scope': 'collector and joined Git children reported separately',
                'content_sha256': content, 'files': count}

    output = []
    git('init', '-q', '-b', 'main')
    output.append(measure('full_capture', lambda: (git('add', '-A'), git('commit', '-q', '-m', 'base'))))
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    (root / 'dir-000/nested/file-00000.txt').write_text('changed\n')
    output.append(measure('declared_path_commit', lambda: (
        git('add', '--', 'dir-000/nested/file-00000.txt'),
        git('commit', '-q', '-m', 'changed'))))
    (root / 'wip.txt').write_text('recoverable\n')
    sys.path.insert(0, str(repository.resolve()))
    from lib.project_mod import lanes
    output.append(measure('wip_save', lambda: lanes.save_wip(root)))
    output.append(measure('checkout', lambda: (
        git('read-tree', '-m', '-u', base), git('reset', '-q'))))
    assert output[-1]['files'] == files + 1
    return output


def run_native(root, files, binary):
    root.mkdir()
    populate(root, files)
    started_wall = time.perf_counter()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = subprocess.run([binary, str(root), str(files)], capture_output=True, text=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if result.returncode:
        raise RuntimeError(f'native worker failed ({result.returncode}): {result.stderr}')
    samples = json.loads(result.stdout)
    for sample in samples:
        sample['cpu_ms'] = ((after.ru_utime + after.ru_stime)
                            - (before.ru_utime + before.ru_stime)) * 1000
        sample['cpu_scope'] = 'one joined native worker, all operations'
    sample_wall = (time.perf_counter() - started_wall) * 1000
    assert sample_wall > 0
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--files', type=int, default=1000)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.files < 1 or args.samples < 1:
        raise SystemExit('files and samples must be positive')
    repository = Path(args.repository).resolve()
    source = Path(__file__).read_bytes()
    report = {'schema': 'native_workspace_comparison_v1',
              'files': args.files, 'samples': args.samples,
              'native_source_sha256': hashlib.sha256(source).hexdigest(),
              'native_binary': str(Path(args.binary).resolve()),
              'operations': {}, 'observations': []}
    for number in range(args.samples):
        for engine in ('native', 'git') if number % 2 == 0 else ('git', 'native'):
            directory = Path(tempfile.mkdtemp(prefix=f'workspace-{engine}-'))
            try:
                root_dir = directory / 'project'
                if engine == 'native':
                    rows = run_native(root_dir, args.files, args.binary)
                else:
                    root_dir.mkdir()
                    populate(root_dir, args.files)
                    rows = run_git(root_dir, args.files, repository)
                assert [row['operation'] for row in rows] == [
                    'full_capture', 'declared_path_commit', 'wip_save', 'checkout']
                report['observations'].extend(rows)
            finally:
                shutil.rmtree(directory, ignore_errors=True)
    for operation in ('full_capture', 'declared_path_commit', 'wip_save', 'checkout'):
        rows = [row for row in report['observations'] if row['operation'] == operation]
        by_engine = {engine: [row for row in rows if row['engine'] == engine]
                     for engine in ('native', 'git')}
        assert all(len(items) == args.samples for items in by_engine.values())
        digests = {row['content_sha256'] for row in rows}
        if len(digests) != 1:
            raise AssertionError(f'{operation}: unequal content outcomes: {digests}')
        values = {engine: [row['wall_ms'] for row in items] for engine, items in by_engine.items()}
        report['operations'][operation] = {
            'wall_ms': values,
            'median_ms': {engine: statistics.median(items) for engine, items in values.items()},
            'ratio_git_over_native': statistics.median(values['git']) / statistics.median(values['native']),
            'content_sha256': next(iter(digests)),
        }
    report['provenance'] = {'python': sys.version, 'platform': sys.platform}
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps(report['operations'], indent=2))


if __name__ == '__main__':
    main()
