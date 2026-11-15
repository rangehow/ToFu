#!/usr/bin/env python3
"""Archive the per-task Git refs of assignments that are no longer live.

Entry point: ``python3 scripts/git_task_ref_archive.py --repo MAIN_CHECKOUT
[--live-status FILE]... [--apply]``. Standard library plus
``scripts/git_hygiene.py`` (its Git runner and repository identity).

Every Worker leaves refs under ``refs/tofu/{inputs,checkpoints,
delivery-inputs,delivery-checkpoints,workspace-wip}/u<owner>/<task>``.
Settlement retires a delivered task's refs once its branch holds the
delivery (``integration_control.retire_task_refs``); discarded, failed and
older rows kept theirs, and on 2026-11-15 4,335 such refs pinned 7.1 GiB of
objects that no branch references (docs/modules/project_integration.md,
"Repository hygiene").

A task is live while a registered worktree carries its checkout name or a
``--live-status`` file (the JSON of ``GET /api/v4/project/integration/status``)
lists its workspace; its refs are never touched. For every other task ref:

* a ref whose commit the branch already contains is deleted: its content is
  reachable from the branch;
* any other ref is archived first. Its commit is kept unchanged unless its
  tree holds directories tagged with ``CACHEDIR.TAG`` (the Cache Directory
  Tagging convention Cargo and Tofu write into every build target); those
  directories are reset to the merge base's version, because they are
  reconstructible build output, and the archived commit names the original.
  All archived commits go into one bundle (prerequisite: the branch tip),
  verified with ``git bundle verify`` before any ref is deleted.

The default run only reports. ``--apply`` writes the bundle and a manifest to
``data/git-hygiene/task-refs-<UTC>.{bundle,json}`` and deletes the refs in one
``update-ref`` transaction that verifies each still holds the value read, so a
ref a concurrent writer moved stays. ``git fetch <bundle>
'refs/tofu/archived/<UTC>/*:refs/tofu/archived/<UTC>/*'`` restores the archive.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.git_hygiene import HygieneError, _environment, _git, _repository  # noqa: E402

FORMAT = 'tofu.git-task-ref-archive/v1'
TASK_NAMESPACES = ('inputs', 'checkpoints', 'delivery-inputs', 'delivery-checkpoints',
                   'workspace-wip')
CACHE_TAG = 'CACHEDIR.TAG'
_LOST_HEAD = re.compile(r'\.lost-head\Z')


def task_key(ref: str) -> str:
    """The checkout name a task ref belongs to (its last component)."""
    return _LOST_HEAD.sub('', ref.rsplit('/', 1)[-1])


def live_task_keys(top: Path, status_files: list[str]) -> set[str]:
    from lib.integration_control import _safe_task  # checkout and ref name of a task

    listing = _git(top, 'worktree', 'list', '--porcelain').stdout
    keys = {Path(line[len('worktree '):]).name for line in listing.splitlines()
            if line.startswith('worktree ')}
    for path in status_files:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        rows = (data.get('data') or data).get('workspaces') or []
        for row in rows:
            delivery = (row.get('origin') or {}).get('_workspaceDelivery') or {}
            for value in (delivery.get('workId'), row.get('workId'), row.get('taskId')):
                if value:
                    keys.add(_safe_task(str(value)))
            for value in (delivery.get('workspacePath'), row.get('path')):
                if value:
                    keys.add(Path(str(value)).name)
    return keys


def _task_refs(top: Path) -> list[tuple[str, str]]:
    patterns = [f'refs/tofu/{namespace}' for namespace in TASK_NAMESPACES]
    output = _git(top, 'for-each-ref', '--format=%(refname) %(objectname)', *patterns).stdout
    return [tuple(line.split(' ', 1)) for line in output.splitlines() if ' ' in line]


def _merged(top: Path, tip: str) -> set[str]:
    patterns = [f'refs/tofu/{namespace}' for namespace in TASK_NAMESPACES]
    return set(_git(top, 'for-each-ref', f'--merged={tip}', '--format=%(refname)',
                    *patterns).stdout.split())


def _tagged_directories(top: Path, commit: str) -> list[str]:
    names = _git(top, 'ls-tree', '-r', '-z', '--name-only', '--full-tree', commit).stdout
    return sorted({name[:-len(CACHE_TAG) - 1] for name in names.split('\0')
                   if name == CACHE_TAG or name.endswith('/' + CACHE_TAG)})


def _without_caches(top: Path, commit: str, base: str, tagged: list[str],
                    original_ref: str) -> str:
    """COMMIT's tree with every tagged directory reset to BASE's version, as a new commit."""
    import subprocess

    with tempfile.TemporaryDirectory(prefix='tofu-task-ref-archive-') as scratch:
        env = {**_environment(), 'GIT_INDEX_FILE': os.path.join(scratch, 'index')}

        def git(*args: str, stdin: str | None = None) -> str:
            result = subprocess.run(['git', '-C', str(top), *args], env=env, input=stdin,
                                    capture_output=True, text=True, encoding='utf-8',
                                    errors='surrogateescape', check=False)
            if result.returncode:
                raise HygieneError(f'git {args[0]} failed: {result.stderr.strip()[:500]}')
            return result.stdout

        git('read-tree', commit)
        listed = git('ls-files', '-z', '--', *tagged)
        if listed:
            git('update-index', '--force-remove', '-z', '--stdin', stdin=listed)
        if base:
            kept = git('ls-tree', '-r', '-z', '--full-tree', base, '--', *tagged)
            if kept:
                git('update-index', '-z', '--index-info', stdin=kept)
        tree = git('write-tree').strip()
        message = (f'Archived {original_ref}\n\nTofu-Original-Commit: {commit}\n'
                   f'Tofu-Dropped-Cache-Directories: {len(tagged)}\n')
        parents = ['-p', base] if base else []
        return git('commit-tree', tree, *parents, stdin=message).strip()


def plan(top: Path, tip: str, live: set[str]) -> dict:
    refs = _task_refs(top)
    merged = _merged(top, tip)
    entries = []
    for ref, sha in refs:
        if task_key(ref) in live:
            continue
        entries.append({'ref': ref, 'sha': sha,
                        'action': 'delete-reachable' if ref in merged else 'archive'})
    return {'tip': tip, 'taskRefs': len(refs), 'live': len(refs) - len(entries),
            'entries': entries}


def _bundle(top: Path, stamp: str, tip: str, entries: list[dict], directory: Path) -> Path | None:
    archived = [entry for entry in entries if entry['action'] == 'archive']
    if not archived:
        return None
    staging = f'refs/tofu/archived/{stamp}'
    for entry in archived:
        base = _git(top, 'merge-base', entry['sha'], tip, check=False).stdout.strip()
        tagged = _tagged_directories(top, entry['sha'])
        entry['droppedCacheDirectories'] = tagged
        entry['archived'] = (_without_caches(top, entry['sha'], base, tagged, entry['ref'])
                             if tagged else entry['sha'])
        entry['archiveRef'] = staging + '/' + entry['ref'][len('refs/tofu/'):]
    transaction = ''.join(f'create {entry["archiveRef"]} {entry["archived"]}\n'
                          for entry in archived)
    _git(top, 'update-ref', '--stdin', stdin=transaction)
    bundle = directory / f'task-refs-{stamp}.bundle'
    try:
        _git(top, 'bundle', 'create', str(bundle), '--stdin',
             stdin=''.join(entry['archiveRef'] + '\n' for entry in archived) + f'^{tip}\n')
        _git(top, 'bundle', 'verify', str(bundle))
    finally:
        _git(top, 'update-ref', '--stdin',
             stdin=''.join(f'delete {entry["archiveRef"]} {entry["archived"]}\n'
                           for entry in archived))
    return bundle


def apply(top: Path, report: dict) -> dict:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = top / 'data' / 'git-hygiene'
    directory.mkdir(parents=True, exist_ok=True)
    bundle = _bundle(top, stamp, report['tip'], report['entries'], directory)
    report.update(format=FORMAT, bundle=str(bundle) if bundle else '',
                  restore=(f"git fetch {bundle} 'refs/tofu/archived/{stamp}/*:"
                           f"refs/tofu/archived/{stamp}/*'") if bundle else '')
    manifest = directory / f'task-refs-{stamp}.json'
    manifest.write_text(json.dumps(report, indent=1) + '\n', encoding='utf-8')
    if report['entries']:
        _git(top, 'update-ref', '-m', 'tofu: archive task refs of finished assignments',
             '--stdin', stdin=''.join(f'delete {entry["ref"]} {entry["sha"]}\n'
                                      for entry in report['entries']))
    report['manifest'] = str(manifest)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--repo', required=True)
    parser.add_argument('--branch', default='HEAD',
                        help='revision whose history makes a ref reachable (default HEAD)')
    parser.add_argument('--live-status', action='append', default=[],
                        help='integration status JSON naming live workspaces (repeatable)')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--json', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        top = _repository(arguments.repo)['top']
        tip = _git(top, 'rev-parse', '--verify', '--end-of-options',
                   arguments.branch + '^{commit}').stdout.strip()
        report = plan(top, tip, live_task_keys(top, arguments.live_status))
        if arguments.apply:
            report = apply(top, report)
    except HygieneError as error:
        print(f'git_task_ref_archive: {error}', file=sys.stderr)
        return 2
    if arguments.json:
        print(json.dumps(report, indent=1))
    else:
        counts: dict[str, int] = {}
        for entry in report['entries']:
            counts[entry['action']] = counts.get(entry['action'], 0) + 1
        print(f"task refs {report['taskRefs']}, live {report['live']}, "
              + ', '.join(f'{name} {count}' for name, count in sorted(counts.items())))
        for name in ('bundle', 'manifest', 'restore'):
            if report.get(name):
                print(f'{name}: {report[name]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
