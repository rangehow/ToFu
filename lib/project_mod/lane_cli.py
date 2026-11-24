#!/usr/bin/env python3
"""Command-line front door for lanes (``python -m lib.project_mod.lane_cli``).

Responsibility
    Parse one lane command, apply the bound-Worker guard, and delegate to the
    single ``lib.project_mod.lanes`` owner. ``scripts/lane.py`` is a thin
    wrapper so the command spelling in AGENTS.md keeps working, but the
    runtime's detached spare preparation spawns THIS module directly: a CLI
    the opensource export drops must never own a live runtime dependency.

Entry points
    ``main(argv=None, *, cwd=None, environ=None) -> int``. Commands: ``new``,
    ``restore``, ``save``, ``remove``, ``list``, ``prepare-spare``.

Dependencies: standard library; ``lib.project_mod.lanes`` (lazily imported so
    ``--help`` costs no Git work).
Contract: docs/modules/project_integration.md, "Lanes".
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Mapping

_BOUND_COMMANDS = frozenset({'new', 'restore', 'save'})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='lane.py',
        description='Create, restore, save, list, remove and pre-warm lanes.')
    commands = parser.add_subparsers(dest='command', required=True)

    new = commands.add_parser('new', help='create a lane for one unit of work')
    new.add_argument('branch', help='new branch name')
    new.add_argument('--base', help='commit to branch from (default: trunk)')

    restore = commands.add_parser('restore', help='rebuild a lane checkout')
    restore.add_argument('branch', help='existing lane branch')

    save = commands.add_parser('save', help='save uncommitted work at its WIP ref')
    save.add_argument('checkout', nargs='?', help='lane checkout (default: cwd)')

    remove = commands.add_parser('remove', help='remove a lane that holds no work')
    remove.add_argument('branch', help='lane branch to remove')

    commands.add_parser('list', help='show divergence and uncommitted files')
    commands.add_parser('prepare-spare', help='prepare the next lane checkout')
    return parser


def _format_lanes(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ['no lanes']
    return [
        f"{row['branch']}  ahead={row['ahead']} behind={row['behind']} "
        f"uncommitted={row['uncommitted']} "
        f"saved={row['savedWork'] or '-'}  {row['checkout']}"
        for row in rows
    ]


def _dispatch(args: argparse.Namespace, cwd: Path, lanes: Any) -> int:
    if args.command == 'new':
        # One invocation resolves its Repository once: creation and the next
        # spare preparation share that identity (docs/modules/project_integration.md).
        repo = lanes.repository(cwd)
        checkout, _recycled = lanes.create_lane(
            repo.top, args.branch, base=args.base, repo=repo)
        lanes.ensure_next_spare(repo.top, repo=repo)
        print(checkout)
        return 0
    if args.command == 'restore':
        checkout, outcome = lanes.restore_lane(cwd, args.branch)
        lanes.ensure_next_spare(cwd)
        print(checkout)
        print(outcome)
        return 0
    if args.command == 'save':
        checkout = Path(args.checkout) if args.checkout else cwd
        print(lanes.save_wip(checkout) or 'no uncommitted work')
        return 0
    if args.command == 'remove':
        checkout, outcome = lanes.remove_lane(cwd, args.branch)
        print(f'{checkout}: {outcome}')
        return 0
    if args.command == 'list':
        for line in _format_lanes(lanes.list_lanes(cwd)):
            print(line)
        return 0
    if args.command == 'prepare-spare':
        print(lanes.prepare_spare(cwd))
        return 0
    return 2


def main(argv: list[str] | None = None, *, cwd: Any = None,
         environ: Mapping[str, str] | None = None) -> int:
    """Run one lane command; return 0 on success, 1 on a typed lane refusal."""
    from lib.project_mod import lanes

    args = _parser().parse_args(argv)
    here = Path(cwd) if cwd is not None else Path.cwd()
    env = environ if environ is not None else os.environ
    try:
        if args.command in _BOUND_COMMANDS:
            bound = lanes.bound_task_checkout(here, env)
            if bound is not None:
                raise lanes.BoundWorktreeError(bound)
        return _dispatch(args, here, lanes)
    except (lanes.LaneError, OSError) as error:
        print(f'lane: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
