#!/usr/bin/env python3
"""Explicit, supervised Git maintenance for one repository.

Model- and harness-run Git never starts automatic maintenance
(``lib/git_runtime.py``); this command replaces it. Entry point:
``python3 scripts/git_maintenance.py --repo CHECKOUT [--expire-days N] [--apply]``.

The steps and the due rule live in ``lib/project_mod/git_object_maintenance.py``;
the delivery receiver starts the same pass after a delivery commit when Git's
own thresholds say it is due. ``--apply`` records each pass in
``<git-common-dir>/tofu-maintenance.json`` for that rule.

The default run only reports: object and pack counts, how many unreachable
loose objects pruning would remove, and whether the commit-graph verifies.
``--apply`` waits for the repository's workspace Git metadata lease
(``lib/project_mod/workspace_lease.py``), then runs with the lease descriptors
inherited by every child:

1. ``pack-refs --all --prune``
2. ``repack -d -l -A --unpack-unreachable=<expiry>``: one pack; unreachable
   packed objects newer than the expiry become loose instead of being dropped
3. ``prune --expire=<expiry>``
4. ``commit-graph write --reachable``, replacing a stale graph. Git neither
   reads nor writes a commit-graph in a shallow repository, so there a graph
   left from before is removed instead: unused, it only fails verification.

and then verifies the commit-graph (``--fsck`` adds a connectivity check).

Other sessions' in-flight work survives: every ref, reflog, worktree HEAD and
index (also of linked worktrees whose directory is missing) is a reachability
root, and an object newer than the expiry keeps everything it references, which
covers staged blobs in private indexes and commits not yet referenced. Reflogs
are never expired and worktree administration is never pruned. The expiry is
at least one day. Decision record: docs/runbooks/GIT_WORKSPACE_GUARD.md
("Automatic Git maintenance never runs inside commands").
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.project_mod.git_object_maintenance import (  # noqa: E402
    LEASE_BUSY_EXIT, MINIMUM_EXPIRE_DAYS, MaintenanceError, WorkspaceBusyError, _apply, _git,
    _report, record_pass, workspace_git_metadata_lease,
)


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--repo', required=True, type=Path,
                        help='checkout of the repository to maintain')
    parser.add_argument('--expire-days', type=int, default=MINIMUM_EXPIRE_DAYS,
                        help='prune unreachable objects older than this many days '
                             f'(minimum and default {MINIMUM_EXPIRE_DAYS})')
    parser.add_argument('--apply', action='store_true',
                        help='run the maintenance; without it only report')
    parser.add_argument('--lease-timeout', type=float, default=None,
                        help='seconds to wait for the Git metadata lease (default: wait)')
    parser.add_argument('--fsck', action='store_true',
                        help='with --apply, finish with git fsck --connectivity-only')
    arguments = parser.parse_args(argv)
    if arguments.expire_days < MINIMUM_EXPIRE_DAYS:
        parser.error(f'--expire-days must be at least {MINIMUM_EXPIRE_DAYS}: a shorter '
                     'expiry can delete objects that concurrent Git commands are still writing')
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    expiry = f'{arguments.expire_days}.days.ago'
    try:
        repo = Path(_git(arguments.repo, 'rev-parse', '--show-toplevel').stdout.strip())
        common = Path(repo, _git(repo, 'rev-parse', '--git-common-dir').stdout.strip())
        print(f'repository: {repo} (common directory {common.resolve()})')
        if not arguments.apply:
            _report(repo, expiry)
            print('report only; rerun with --apply to maintain')
            return 0
        if (common / 'gc.pid').exists():
            raise MaintenanceError(f'{common / "gc.pid"} exists: another git gc may be '
                                   'running; nothing changed')
        print(f'waiting for the Git metadata lease of {repo}', flush=True)
        with workspace_git_metadata_lease(str(repo), timeout=arguments.lease_timeout):
            after = _apply(repo, common, expiry, fsck=arguments.fsck,
                           emit=lambda line: print(line, flush=True))
        record_pass(common.resolve(), after, arguments.expire_days)
    except WorkspaceBusyError as error:
        print(f'error: Git metadata of {arguments.repo} is busy; nothing changed: {error}',
              file=sys.stderr)
        return LEASE_BUSY_EXIT
    except MaintenanceError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
