"""Supervised Git object maintenance for one repository, and when it is due.

Owner of the steps ``scripts/git_maintenance.py`` runs (that script is the
operator CLI) and of :func:`run_when_due`, which the delivery receiver starts
after a delivery commit (``lib/project_mod/delivery.py``). Decision record:
docs/runbooks/GIT_WORKSPACE_GUARD.md ("Automatic Git maintenance never runs
inside commands"). Dependencies: Git policy environment, workspace leases.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from lib.git_checkpoint_policy import git_policy_environment
from lib.git_runtime import git_executable
from lib.log import get_logger
from lib.tools.process_environment import inherited_child_environment
from lib.project_mod.workspace_lease import (
    WorkspaceBusyError, workspace_git_metadata_lease, workspace_lease_fds,
)

logger = get_logger(__name__)

MINIMUM_EXPIRE_DAYS = 1
LEASE_BUSY_EXIT = 3
_SPLIT_GRAPH_FILE = re.compile(r'commit-graph-chain|graph-[0-9a-f]{40,64}\.graph')


class MaintenanceError(RuntimeError):
    """A Git step failed; later steps did not run."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        [git_executable(), '-C', str(repo), *args], env=git_policy_environment(),
        text=True, capture_output=True, check=False, pass_fds=workspace_lease_fds())
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()[-2000:]
        raise MaintenanceError(f'git {" ".join(args)} exited {process.returncode}: {detail}')
    return process


def _object_counts(repo: Path) -> dict[str, int]:
    counts = {}
    for line in _git(repo, 'count-objects', '-v').stdout.splitlines():
        key, _, value = line.partition(':')
        if value.strip().isdigit():
            counts[key] = int(value)
    return counts


def _report_counts(label: str, counts: dict[str, int], emit=print) -> None:
    emit(f'{label}: loose={counts.get("count", 0)} '
          f'loose_kib={counts.get("size", 0)} packs={counts.get("packs", 0)} '
          f'pack_kib={counts.get("size-pack", 0)} garbage={counts.get("garbage", 0)}')


def _shallow(repo: Path) -> bool:
    return _git(repo, 'rev-parse', '--is-shallow-repository').stdout.strip() == 'true'


def _remove_unused_commit_graph(common: Path) -> list[str]:
    info = common / 'objects' / 'info'
    split = info / 'commit-graphs'
    files = [info / 'commit-graph']
    if split.is_dir():
        files += [path for path in split.iterdir() if _SPLIT_GRAPH_FILE.fullmatch(path.name)]
    removed = []
    for path in files:
        if path.is_file():
            path.unlink()
            removed.append(str(path.relative_to(common)))
    if split.is_dir() and not any(split.iterdir()):
        split.rmdir()
    return removed


def _commit_graph_status(repo: Path) -> str:
    try:
        _git(repo, 'commit-graph', 'verify')
    except MaintenanceError as error:
        return 'invalid: ' + str(error).splitlines()[0][:300]
    return 'valid'


def _report(repo: Path, expiry: str) -> None:
    _report_counts('objects', _object_counts(repo))
    worktrees = _git(repo, 'worktree', 'list', '--porcelain').stdout.split('\n\n')
    worktrees = [entry for entry in worktrees if entry.startswith('worktree ')]
    missing = sum('\nprunable ' in entry for entry in worktrees)
    print(f'worktrees: {len(worktrees)} (directory missing, still kept as roots: {missing})')
    candidates = _git(repo, 'prune', '--dry-run', f'--expire={expiry}').stdout.split()
    kinds = {}
    for kind in candidates[1::2]:
        kinds[kind] = kinds.get(kind, 0) + 1
    print(f'prune --expire={expiry} would remove {len(candidates) // 2} loose objects {kinds}')
    shallow = ' (shallow repository: --apply removes it, Git cannot use it)' if _shallow(repo) else ''
    print(f'commit-graph: {_commit_graph_status(repo)}{shallow}')


def _apply(repo: Path, common: Path, expiry: str, *, fsck: bool,
           emit=print) -> dict[str, int]:
    _report_counts('before', _object_counts(repo), emit)
    steps = [
        ('pack-refs', ['pack-refs', '--all', '--prune']),
        ('repack', ['repack', '-d', '-l', '-A', f'--unpack-unreachable={expiry}']),
        ('prune', ['prune', f'--expire={expiry}']),
        ('commit-graph', None if _shallow(repo) else ['commit-graph', 'write', '--reachable']),
        ('verify', ['commit-graph', 'verify']),
    ]
    if fsck:
        steps.append(('fsck', ['fsck', '--connectivity-only', '--no-dangling', '--no-progress']))
    for name, arguments in steps:
        started = time.monotonic()
        if arguments is None:
            removed = _remove_unused_commit_graph(common)
            emit(f'{name}: shallow repository, removed unused {removed or "nothing"}')
            continue
        _git(repo, *arguments)
        emit(f'{name}: ok in {time.monotonic() - started:.1f}s')
    after = _object_counts(repo)
    _report_counts('after', after, emit)
    return after


# ── When a pass is due ────────────────────────────────────────────────────
#
# Git's own thresholds say when objects need maintenance (gc.auto loose
# objects, gc.autoPackLimit packs; Git's defaults apply when unset and 0
# disables a criterion, exactly as for `git gc --auto`). What they miss is the
# residue a pass must leave: unreachable objects newer than the expiry stay
# loose, so a count above gc.auto would make every later delivery start a pass
# that cannot remove them (the storm in GIT_WORKSPACE_GUARD.md). The record of
# the last pass answers that: new loose objects beyond its residue are due at
# once, and its residue is due when its expiry has passed.

RECORD_NAME = 'tofu-maintenance.json'
_GIT_DEFAULTS = {'gc.auto': 6700, 'gc.autoPackLimit': 50}
_RUNNING: set[str] = set()
_RUNNING_GUARD = threading.Lock()


def _git_threshold(repo: Path, name: str) -> int:
    # git_policy_environment() forces gc.auto=0 for every child; read the
    # repository's and the user's own configuration instead.
    environment = {key: value for key, value in inherited_child_environment().items()
                   if not key.startswith('GIT_')}
    result = subprocess.run([git_executable(), '-C', str(repo), 'config', '--int', '--get', name],
                            env=environment, text=True, capture_output=True, check=False)
    value = result.stdout.strip()
    return int(value) if result.returncode == 0 and value.lstrip('-').isdigit() \
        else _GIT_DEFAULTS[name]


def read_record(common: Path) -> dict | None:
    try:
        return json.loads((common / RECORD_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def record_pass(common: Path, after: dict[str, int], expire_days: int, *,
                now: float | None = None) -> None:
    record = {'finishedAt': time.time() if now is None else now,
              'looseAfter': int(after.get('count', 0)), 'packsAfter': int(after.get('packs', 0)),
              'expireDays': expire_days}
    staged = common / f'.{RECORD_NAME}.{os.getpid()}'
    staged.write_text(json.dumps(record) + '\n', encoding='utf-8')
    os.replace(staged, common / RECORD_NAME)


def due_reason(counts: dict[str, int], record: dict | None, *, loose_limit: int,
               pack_limit: int, now: float) -> str:
    """Why a pass is due now, or '' when it would only repeat the last one."""
    loose, packs = int(counts.get('count', 0)), int(counts.get('packs', 0))
    if pack_limit > 0 and packs > pack_limit:
        return f'{packs} packs > gc.autoPackLimit {pack_limit}'
    if loose_limit <= 0:
        return ''
    residue = int(record.get('looseAfter', 0)) if record else 0
    if loose - residue > loose_limit:
        return f'{loose - residue} loose objects since the last pass > gc.auto {loose_limit}'
    if record and residue > loose_limit and loose > loose_limit:
        expires = float(record.get('finishedAt', 0)) + 86400 * int(
            record.get('expireDays', MINIMUM_EXPIRE_DAYS))
        if now >= expires:
            return f'the last pass left {residue} loose objects and their expiry has passed'
    return ''


def run_when_due(root: Path, *, expire_days: int = MINIMUM_EXPIRE_DAYS) -> bool:
    """Start one background pass over ROOT's repository if one is due and none runs here.

    Started by events (a delivery commit), never by a timer. A busy Git
    metadata lease or a running ``git gc`` skips the pass; the next event
    decides again. Returns whether a pass thread started.
    """
    try:
        common = Path(root, _git(Path(root), 'rev-parse', '--git-common-dir').stdout.strip())
        common = common.resolve()
    except (MaintenanceError, OSError):
        return False
    key = str(common)
    with _RUNNING_GUARD:
        if key in _RUNNING:
            return False
        _RUNNING.add(key)

    def run() -> None:
        try:
            repo = Path(root)
            reason = due_reason(_object_counts(repo), read_record(common),
                                loose_limit=_git_threshold(repo, 'gc.auto'),
                                pack_limit=_git_threshold(repo, 'gc.autoPackLimit'),
                                now=time.time())
            if not reason or (common / 'gc.pid').exists():
                return
            logger.info('[GitMaintenance] %s: pass due (%s)', repo, reason)
            with workspace_git_metadata_lease(str(repo), timeout=0.0):
                after = _apply(repo, common, f'{expire_days}.days.ago', fsck=False,
                               emit=lambda line: logger.info('[GitMaintenance] %s', line))
            record_pass(common, after, expire_days)
        except WorkspaceBusyError:
            logger.info('[GitMaintenance] %s: metadata lease busy; the next event decides again',
                        root)
        except Exception as error:
            logger.warning('[GitMaintenance] pass over %s failed: %s', root, error)
        finally:
            with _RUNNING_GUARD:
                _RUNNING.discard(key)

    threading.Thread(target=run, name=f'tofu-git-maintenance-{common.parent.name}',
                     daemon=True).start()
    return True
