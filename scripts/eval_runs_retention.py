#!/usr/bin/env python3
"""Retention owner of the evaluation root (``.eval-runs``): reclaim finished scratch.

Entry point: ``python3 scripts/eval_runs_retention.py [--root DIR] [--apply] [--json]``.
Standard library only. Dry run by default: classify every run scratch area and
report what an ``--apply`` pass would reclaim. Policy: docs/RESOURCE_CONTROL.md
("Evaluation run scratch").

What is reclaimable is fixed by the contract that creates it:
``rootless_vm.host_paths.child_storage_environment(owner_root)`` gives every
evaluation run (or session) ``<owner_root>/.runtime/{tmp,cache,state,data,home,
config}`` and recreates any of them on next use. Only ``tmp`` (TMPDIR) and
``cache`` (uv/pip/npm/HF caches) are reconstructible scratch; everything else
(``state``, ``data``, ``home``, ``config``, run manifests, trials, results,
copied evidence) is durable and never touched.

A scratch area is live, and kept, when any of these holds:
- ``open-handle``: a process has its cwd, root, executable, an open file or a
  mapping under the owner root;
- ``process-reference``: a process's command line or environment names the
  owner root (a launcher, relay or scheduler configured for that run);
- ``result-store-reference``: a manifest of a result store (``*.json``,
  ``*.md``, ``*.sh``, ``*.toml``, ``*.yaml`` in the root, directly in a run
  directory or one level below) names the scratch path;
- ``changed`` / ``unobserved``: its contents differ from the previous pass's
  recorded fingerprint, or no pass observed it yet (recent writes).
Only a scratch area unchanged across two passes with none of the above is
finished. ``--apply`` re-checks handles and process references immediately
before removing it. Processes are visible only inside this container: runs
driven from another host must stay referenced by a result store or keep writing.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = 'tofu.eval-runs-retention/v1'
RUNTIME = '.runtime'
RECLAIMABLE = ('tmp', 'cache')
OBSERVATIONS = Path('.retention') / 'observations.json'
MANIFEST_SUFFIXES = ('.json', '.md', '.sh', '.toml', '.yaml', '.yml')
# child_storage_environment owners sit at <run>, <run>/<session> or
# <run>/.definitions/<name>: the deepest owner root is three levels down.
OWNER_DEPTH = 3


@dataclass
class Process:
    pid: int
    paths: list[str] = field(default_factory=list)
    text: str = ''


@dataclass
class Unit:
    runtime: Path
    files: int = 0
    bytes: int = 0
    newest_ns: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def owner(self) -> Path:
        return self.runtime.parent

    @property
    def fingerprint(self) -> list[int]:
        return [self.files, self.bytes, self.newest_ns]


def default_root() -> Path:
    project = Path(os.environ.get('TOFU_PROJECT_PATH') or Path(__file__).resolve().parents[1])
    return Path(os.environ.get('TOFU_EVAL_ROOT') or project / '.eval-runs')


def scan_processes() -> list[Process]:
    """This user's processes: the paths they hold and the text that configures them."""
    found = []
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        base = entry.path
        try:
            if os.stat(base).st_uid != os.getuid():
                continue
        except OSError:
            continue
        process = Process(int(entry.name))
        links = [os.path.join(base, name) for name in ('cwd', 'root', 'exe')]
        try:
            links += [entry.path for entry in os.scandir(os.path.join(base, 'fd'))]
        except OSError:
            pass
        for link in links:
            try:
                process.paths.append(os.readlink(link))
            except OSError:
                continue
        try:
            with open(os.path.join(base, 'maps'), encoding='utf-8', errors='replace') as maps:
                process.paths += [line.split(None, 5)[5].strip() for line in maps
                                  if len(line.split(None, 5)) == 6]
        except OSError:
            pass
        texts = []
        for name in ('cmdline', 'environ'):
            try:
                with open(os.path.join(base, name), 'rb') as stream:
                    texts.append(stream.read().replace(b'\0', b'\n').decode('utf-8', 'replace'))
            except OSError:
                continue
        process.text = '\n'.join(texts)
        found.append(process)
    return found


def discover(root: Path) -> list[Unit]:
    units = []
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name == RUNTIME:
                units.append(Unit(Path(entry.path)))
            elif depth < OWNER_DEPTH and entry.name != OBSERVATIONS.parts[0]:
                stack.append((Path(entry.path), depth + 1))
    return sorted(units, key=lambda unit: str(unit.runtime))


def measure(unit: Unit) -> None:
    for name in RECLAIMABLE:
        stack = [str(unit.runtime / name)]
        while stack:
            path = stack.pop()
            try:
                status = os.lstat(path)
            except OSError:
                continue
            unit.newest_ns = max(unit.newest_ns, status.st_mtime_ns)
            if os.path.isdir(path) and not os.path.islink(path):
                try:
                    stack.extend(entry.path for entry in os.scandir(path))
                except OSError:
                    pass
            else:
                unit.files += 1
                unit.bytes += status.st_size


def manifests(root: Path) -> list[str]:
    """Result-store manifests in the root, each run directory and one level below."""
    directories = [root]
    for run_directory in _subdirectories(root):
        directories += [run_directory, *_subdirectories(run_directory)]
    texts = []
    for directory in directories:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(MANIFEST_SUFFIXES):
                try:
                    texts.append(Path(entry.path).read_text(encoding='utf-8', errors='replace'))
                except OSError:
                    continue
    return texts


def _subdirectories(directory: Path) -> list[Path]:
    try:
        return [Path(entry.path) for entry in os.scandir(directory)
                if entry.is_dir(follow_symlinks=False)
                and entry.name not in (RUNTIME, OBSERVATIONS.parts[0])]
    except OSError:
        return []


def _names(path: Path, root: Path) -> list[str]:
    names = {str(path), os.path.realpath(path)}
    try:
        names.add(str(Path(root.name) / path.relative_to(root)))
    except ValueError:
        pass
    return sorted(names)


def _under(candidate: str, owner_names: list[str]) -> bool:
    return any(candidate == name or candidate.startswith(name.rstrip('/') + '/') for name in owner_names)


def live_reasons(unit: Unit, root: Path, processes: list[Process]) -> list[str]:
    owner_names = [name for name in _names(unit.owner, root) if os.path.isabs(name)]
    reasons = []
    if any(_under(path, owner_names) for process in processes for path in process.paths):
        reasons.append('open-handle')
    if any(name in process.text for process in processes for name in owner_names):
        reasons.append('process-reference')
    return reasons


def classify(root: Path, processes: list[Process], observed: dict, texts: list[str]) -> list[Unit]:
    units = discover(root)
    for unit in units:
        measure(unit)
        unit.reasons = live_reasons(unit, root, processes)
        scratch = [name for leaf in RECLAIMABLE for name in _names(unit.runtime / leaf, root)]
        if any(name in text for text in texts for name in scratch):
            unit.reasons.append('result-store-reference')
        previous = observed.get(str(unit.runtime))
        if previous is None:
            unit.reasons.append('unobserved')
        elif previous != unit.fingerprint:
            unit.reasons.append('changed')
    return units


def reclaim(unit: Unit, root: Path) -> list[str]:
    """Remove a finished unit's tmp and cache unless it became live meanwhile."""
    late = live_reasons(unit, root, scan_processes())
    if late:
        unit.reasons = late
        return []
    removed = []
    for name in RECLAIMABLE:
        target = unit.runtime / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(str(target))
    unit.files = unit.bytes = 0
    return removed


def _load(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return record.get('units', {}) if record.get('format') == FORMAT else {}


def _store(path: Path, units: list[Unit]) -> str:
    record = {'format': FORMAT, 'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'units': {str(unit.runtime): unit.fingerprint for unit in units}}
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
        temporary.write_text(json.dumps(record), encoding='utf-8')
        os.replace(temporary, path)
    except OSError as error:
        return f'observation not recorded: {error}'
    return ''


def run(root: Path, *, apply: bool, processes: list[Process] | None = None) -> dict:
    root = root.absolute()
    history = root / OBSERVATIONS
    units = classify(root, scan_processes() if processes is None else processes, _load(history), manifests(root))
    report = {'format': FORMAT, 'root': str(root), 'apply': apply, 'units': []}
    for unit in units:
        state = 'live' if unit.reasons else 'finished'
        row = {'runtime': str(unit.runtime.relative_to(root)), 'state': state, 'reasons': list(unit.reasons),
               'reclaimable_bytes': unit.bytes, 'reclaimable_files': unit.files, 'removed': []}
        if apply and state == 'finished' and (unit.files or unit.bytes):
            row['removed'] = reclaim(unit, root)
            if unit.reasons:
                row.update(state='live', reasons=list(unit.reasons))
        report['units'].append(row)
    finished = [row for row in report['units'] if row['state'] == 'finished']
    report['finished'] = {'units': len(finished), 'bytes': sum(row['reclaimable_bytes'] for row in finished),
                          'files': sum(row['reclaimable_files'] for row in finished)}
    report['observation'] = _store(history, units) or str(history)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=None, help='evaluation root (default: <project>/.eval-runs)')
    parser.add_argument('--apply', action='store_true', help='remove the tmp/cache of finished runs')
    parser.add_argument('--json', action='store_true')
    arguments = parser.parse_args(argv)
    root = arguments.root or default_root()
    if not (root / '.').is_dir():
        parser.error(f'{root} is not a directory')
    report = run(root, apply=arguments.apply)
    if arguments.json:
        print(json.dumps(report, indent=1))
        return 0
    verb = 'removed' if arguments.apply else 'would reclaim'
    for row in report['units']:
        print(f"{row['state']:8} {row['reclaimable_bytes']:>15,} B {row['reclaimable_files']:>9,} files  "
              f"{row['runtime']}  {','.join(row['reasons']) or verb}")
    total = report['finished']
    print(f"{verb}: {total['units']} finished scratch areas, {total['bytes']:,} bytes, {total['files']:,} files; "
          f"observation: {report['observation']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
