#!/usr/bin/env python3
"""Measure every filesystem and Git artifact Tofu creates, and its growth.

Entry point: ``python3 scripts/resource_inventory.py --project CHECKOUT
[--record] [--json]``. Standard library plus ``serverctl_pkg.local_placement``
(placement roots). Read-only unless ``--record``, which appends the
measurement to ``<project>/data/resource-inventory.jsonl``; each run prints
its growth against the last recorded measurement, so growth is measured over
time instead of estimated. Each artifact names its lifecycle owner, the
authority docs/RESOURCE_CONTROL.md ("Filesystem and Git artifact lifecycles")
explains.

Bytes are allocated blocks with hard links counted once. Copy-on-write
clones (seeded Cargo targets) share extents the filesystem does not report,
so their bytes are an upper bound.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serverctl_pkg import local_placement  # noqa: E402

FORMAT = 'tofu.resource-inventory/v1'
HISTORY = Path('data') / 'resource-inventory.jsonl'


def measure(paths: list[str]) -> dict:
    """Allocated bytes and entries under PATHS, each inode counted once."""
    seen: set[tuple[int, int]] = set()
    total = entries = 0
    for top in paths:
        stack = [top]
        while stack:
            path = stack.pop()
            try:
                status = os.lstat(path)
            except OSError:
                continue
            key = (status.st_dev, status.st_ino)
            if key in seen:
                continue
            seen.add(key)
            entries += 1
            blocks = getattr(status, 'st_blocks', None)
            total += status.st_size if blocks is None else blocks * 512
            if os.path.isdir(path) and not os.path.islink(path):
                try:
                    stack.extend(entry.path for entry in os.scandir(path))
                except OSError:
                    continue
    return {'bytes': total, 'entries': entries, 'paths': len(paths)}


def _placed(purpose: str) -> list[str]:
    return [str(root) for root in local_placement.candidate_roots(purpose) if root.exists()]


def _children(roots: list[str], predicate) -> list[str]:
    found = []
    for root in roots:
        for repository in glob.glob(os.path.join(root, '*', '*')):
            for owner in glob.glob(os.path.join(repository, 'u*')) + [repository]:
                found.extend(path for path in glob.glob(os.path.join(owner, '*'))
                             + glob.glob(os.path.join(owner, '.*'))
                             if predicate(Path(path)))
    return sorted(set(found))


def artifacts(project: Path) -> list[tuple[str, str, list[str]]]:
    """(name, lifecycle owner, paths) of every artifact this host holds."""
    tmp = local_placement.temporary_root()
    home = Path.home()
    uid = os.getuid()
    workspaces = _placed(local_placement.PURPOSE_WORKSPACES)

    def is_checkout(path: Path) -> bool:
        return path.name.startswith('project-worker-') or (path / '.git').is_file() \
            and not path.name.startswith('.')

    def tmp_glob(pattern: str) -> list[str]:
        return sorted(glob.glob(os.path.join(tmp, pattern)))

    indexes = _placed(local_placement.PURPOSE_INDEXES)
    namespaces = sorted(glob.glob(os.path.join(indexes[0], 'tofudb-search', '*'))) if indexes else []
    serving = _serving_projection(project)
    return [
        ('task checkouts', 'delivery or discard recycles or removes the checkout',
         _children(workspaces, lambda p: p.is_dir() and not p.name.startswith('.')
                   and is_checkout(p))),
        ('task checkout spares', 'task_spares: recycled by demand, oldest removed near the reserve',
         _children(workspaces, lambda p: p.name.startswith(('.spare-', '.preparing-')))),
        ('task command scratch (.tmp-<checkout>)', 'removed when its checkout retires',
         _children(workspaces, lambda p: p.name.startswith('.tmp-'))),
        ('stray files beside task checkouts', 'none: agents write them by name (AGENTS.md: use $TMPDIR)',
         _children(workspaces, lambda p: not p.name.startswith('.') and not is_checkout(p)
                   and p.parent.name.startswith('u'))),
        ('lanes', 'git_land recycles; lane.py remove', _placed(local_placement.PURPOSE_LANES)),
        ('runtime replicas', 'replaced per source generation', _placed(local_placement.PURPOSE_RUNTIME)),
        ('native compiler caches', 'removed when their checkout is gone',
         _placed(local_placement.PURPOSE_NATIVE_BUILD)),
        ('serving search projection', 'the storage authority', [serving] if serving else []),
        ('other search projection namespaces', 'none (finding for the storage owner)',
         [path for path in namespaces if path != serving]),
        ('test bundle cache', 'tests/_runtime_sections.py prune', tmp_glob(f'tofu-test-bundle-cache-{uid}')),
        ('test scratch (process-owned)', 'removed after the owning process exits',
         tmp_glob('tofu-runtime-sections-*') + tmp_glob('tofu-frontend-test-root-*')
         + tmp_glob('chat-sidecar-*')),
        ('pytest roots', 'pytest keeps its last three', tmp_glob(f'tofu-pytest-runs-{uid}')),
        ('other tofu-* entries in the temporary directory', 'none',
         [path for path in tmp_glob('tofu*') if not any(
             marker in path for marker in (f'tofu-{uid}-', 'tofu-test-bundle-cache-',
                                           'tofu-runtime-sections-', 'tofu-frontend-test-root-',
                                           'tofu-pytest-runs-'))]),
        ('all other temporary-directory entries', 'none', sorted(
            path for path in glob.glob(os.path.join(tmp, '*')) + glob.glob(os.path.join(tmp, '.*'))
            if not os.path.basename(path).startswith(('tofu', 'chat-sidecar-')))),
        ('Playwright browsers', "Playwright's registry (one shared install)",
         [str(home / '.cache' / 'ms-playwright')]),
        ('pip and uv caches', "the tools' own cache prune",
         [str(home / '.cache' / name) for name in ('pip', 'uv')]),
        ('logs', 'lib/logging_pkg/log_retention.py', [str(project / 'logs')]),
        ('data/mcp-cache', 'shared tool cache and installed MCP tools (kept)',
         [str(project / 'data' / 'mcp-cache')]),
        ('data/host-state', 'host-move snapshot (user state, kept)', [str(project / 'data' / 'host-state')]),
        ('data/export', 'release exports (kept)', [str(project / 'data' / 'export')]),
        ('data/backups', 'native online backup retention', [str(project / 'data' / 'backups')]),
        ('data/git-hygiene', 'archived refs (user work, kept)', [str(project / 'data' / 'git-hygiene')]),
        ('static/', 'Vite publication', [str(project / 'static')]),
        ('.git/objects', 'git_object_maintenance after delivery commits',
         [str(project / '.git' / 'objects')]),
    ]


def _serving_projection(project: Path) -> str:
    try:
        connection = json.loads((project / 'data' / 'native-storage-connection.json').read_text())
    except (OSError, ValueError):
        return ''
    return str(connection.get('searchProjectionDirectory') or '')


def git_counts(project: Path) -> dict:
    env = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
           'GIT_OPTIONAL_LOCKS': '0'}

    def git(*args: str) -> str:
        return subprocess.run(['git', '-C', str(project), *args], capture_output=True,
                              text=True, env=env, check=False).stdout

    namespaces: dict[str, int] = {}
    for ref in git('for-each-ref', '--format=%(refname)').split():
        parts = ref.split('/')
        key = ('refs/heads/recovered' if ref.startswith('refs/heads/recovered/')
               else '/'.join(parts[:3]) if ref.startswith('refs/tofu/') else '/'.join(parts[:2]))
        namespaces[key] = namespaces.get(key, 0) + 1
    objects = {}
    for line in git('count-objects', '-v').splitlines():
        key, _, value = line.partition(':')
        if value.strip().isdigit():
            objects[key] = int(value)
    return {'refs': namespaces, 'objects': objects}


def _previous(history: Path) -> dict | None:
    try:
        lines = history.read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    return json.loads(lines[-1]) if lines else None


def _size(value: int) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value} B'
        value /= 1024
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--record', action='store_true',
                        help=f'append the measurement to <project>/{HISTORY}')
    parser.add_argument('--json', action='store_true')
    arguments = parser.parse_args(argv)
    project = arguments.project.resolve()
    report = {'format': FORMAT, 'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'host': os.uname().nodename, 'artifacts': {}, 'git': git_counts(project)}
    for name, owner, paths in artifacts(project):
        report['artifacts'][name] = {'owner': owner, **measure(paths)}
    previous = _previous(project / HISTORY)
    if arguments.record:
        with open(project / HISTORY, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(report) + '\n')
    if arguments.json:
        print(json.dumps(report, indent=1))
        return 0
    since = f' (growth since {previous["at"]})' if previous else ''
    print(f'{"artifact":48} {"bytes":>11} {"entries":>9} {"growth":>11}{since}  lifecycle owner')
    for name, item in report['artifacts'].items():
        before = ((previous or {}).get('artifacts') or {}).get(name)
        growth = _size(item['bytes'] - before['bytes']) if before else '-'
        print(f'{name:48} {_size(item["bytes"]):>11} {item["entries"]:>9} {growth:>11}  {item["owner"]}')
    objects = report['git']['objects']
    print(f"git: {sum(report['git']['refs'].values())} refs {report['git']['refs']}; "
          f"loose {objects.get('count', 0)} ({_size(objects.get('size', 0) * 1024)}), "
          f"packs {objects.get('packs', 0)} ({_size(objects.get('size-pack', 0) * 1024)})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
