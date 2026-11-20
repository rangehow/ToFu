#!/usr/bin/env python3
"""Route the raw native ABI oracle to its declared internal checker.

The checker is the benchmark entry ``check_native_csr.check`` under
``benchmarks/repository_index``. Internal exports retain it; the opensource
export drops the whole ``benchmarks`` tree. Absence is detected from the
declared module/entry, never by catching unrelated exceptions, and it is
reported as a neutral outcome instead of a pass: the native buffering and
concurrency checks did not run.

``detect`` writes ``available=true|false`` for the workflow's job guard.
``run`` executes the checker when present and exits ``NEUTRAL_EXIT`` when the
checker is absent. ``docs/TESTING_STRATEGY.md`` owns this delivery policy.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

CHECKER_MODULE = 'check_native_csr'
CHECKER_ENTRY = 'check'
CHECKER_ROOT = Path('benchmarks') / 'repository_index'
NEUTRAL_EXIT = 78
ABSENT_MESSAGE = (
    'internal checker not available in this export; '
    'native buffering/concurrency checks were not run'
)


def _checker_root(root: Path) -> Path:
    return root.resolve() / CHECKER_ROOT


def detect(root: Path) -> bool:
    """Return whether the declared checker module resolves under ``root``."""
    search_root = str(_checker_root(root))
    if search_root not in sys.path:
        sys.path.insert(0, search_root)
    return importlib.util.find_spec(CHECKER_MODULE) is not None


def run(root: Path, library: str | None) -> int:
    if not detect(root):
        print(ABSENT_MESSAGE)
        return NEUTRAL_EXIT
    if library is None:
        from tofu_repo_kernel import load_kernel
        library = load_kernel().library._name
    checker = importlib.import_module(CHECKER_MODULE)
    entry = getattr(checker, CHECKER_ENTRY)
    entry(library)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('detect', 'run'))
    parser.add_argument('--root', type=Path, default=Path.cwd(),
                        help='checkout root that declares the checker')
    parser.add_argument('--library', help='compiled artifact passed to the checker')
    parser.add_argument('--github-output', type=Path,
                        help='write available=true|false for the workflow guard')
    args = parser.parse_args()

    if args.command == 'detect':
        available = detect(args.root)
        print(('internal checker %s.%s is available; native buffering/concurrency '
               'checks will run') % (CHECKER_MODULE, CHECKER_ENTRY)
              if available else ABSENT_MESSAGE)
        if args.github_output:
            with args.github_output.open('a', encoding='utf-8') as output:
                output.write(f'available={"true" if available else "false"}\n')
        return 0

    return run(args.root, args.library)


if __name__ == '__main__':
    raise SystemExit(main())