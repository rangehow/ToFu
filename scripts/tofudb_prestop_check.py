#!/usr/bin/env python3
"""Read-only pre-restart check of the native TofuDB authority's WAL windows.

Responsibility: before a planned restart, tell the operator whether the
running engine's graceful stop has bounded WAL work. A graceful stop finishes
the rotation in flight and retires the newest WAL window; the contract bounds
a healthy window by the rotation soft threshold
(``contracts/tofudb_ir_v1.json`` ``activeWalRotation.softThresholdBytes``).
The check passes when the newest active WAL and its immediate predecessor
generation (a sealed window the stop may still have to install, when its file
is still present) are both within that threshold. Older generations are
reported, never judged: the engine no longer holds them. Run it once ahead of
a restart and again immediately before it; restart only when both runs pass.
See docs/NATIVE_STORAGE_LIFECYCLE.md.

Entry point: ``python3 scripts/tofudb_prestop_check.py [--data-dir DIR]
[--json]``. Without ``--data-dir`` the authority is the one production
launches, ``<runtime base>/data/tofudb-authority``, with the runtime base
resolved read-only (``runtime_path_policy.resolve_runtime_base``).

Reads only the contract, one listing of the authority directory (no
recursion, no store walk) and the sizes of its ``active-<generation>.wal``
files. Writes nothing, creates nothing, signals no process.

Exit status: 0 pass; 1 fail (a held window exceeds the threshold);
2 cannot decide (no authority directory, no active WAL, or no threshold in
the contract).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPOSITORY_ROOT / 'contracts' / 'tofudb_ir_v1.json'
WAL_NAME = re.compile(r'active-([0-9a-f]{16})\.wal')
EXIT_PASS, EXIT_FAIL, EXIT_UNDECIDED = 0, 1, 2


class Undecided(Exception):
    """The check cannot reach a verdict; nothing about the stop is implied."""


def soft_threshold_bytes(contract: Path) -> int:
    try:
        document = json.loads(contract.read_text(encoding='utf-8'))
        value = document['activeWalRotation']['softThresholdBytes']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Undecided(f'no activeWalRotation.softThresholdBytes in {contract}: {error}') from error
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Undecided(f'activeWalRotation.softThresholdBytes in {contract} is not a positive integer')
    return value


def default_authority() -> Path:
    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    from runtime_path_policy import resolve_runtime_base

    return Path(resolve_runtime_base(allow_probe=False)) / 'data' / 'tofudb-authority'


def wal_windows(authority: Path) -> list[dict]:
    try:
        entries = list(os.scandir(authority))
    except OSError as error:
        raise Undecided(f'cannot list the authority {authority}: {error}') from error
    windows = []
    for entry in entries:
        match = WAL_NAME.fullmatch(entry.name)
        if match and entry.is_file(follow_symlinks=False):
            windows.append({'file': entry.name, 'generation': int(match.group(1), 16),
                            'bytes': entry.stat(follow_symlinks=False).st_size})
    if not windows:
        raise Undecided(f'no active-<generation>.wal file in {authority}')
    return sorted(windows, key=lambda window: window['generation'], reverse=True)


def check(authority: Path, contract: Path = CONTRACT_PATH) -> dict:
    threshold = soft_threshold_bytes(contract)
    windows = wal_windows(authority)
    newest = windows[0]
    predecessor = next((window for window in windows[1:]
                        if window['generation'] == newest['generation'] - 1), None)
    held = [newest] + ([predecessor] if predecessor else [])
    over = [window for window in held if window['bytes'] > threshold]
    return {
        'authority': str(authority),
        'contract': str(contract),
        'threshold_bytes': threshold,
        'newest': newest,
        'predecessor': predecessor,
        'older': [window for window in windows[1:] if window is not predecessor],
        'verdict': 'fail' if over else 'pass',
        'over_threshold': [window['file'] for window in over],
    }


def _describe(window: dict | None) -> str:
    return 'none' if window is None else f"{window['file']} {window['bytes']:,} bytes"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    parser.add_argument('--data-dir', type=Path, default=None,
                        help='authority directory (default: the one production launches)')
    parser.add_argument('--json', action='store_true', help='print one JSON object')
    args = parser.parse_args(argv)
    authority = args.data_dir if args.data_dir is not None else default_authority()
    try:
        result = check(authority)
    except Undecided as error:
        result = {'authority': str(authority), 'verdict': 'undecided', 'reason': str(error)}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result['verdict'] == 'undecided':
        print(f"UNDECIDED: {result['reason']}")
    else:
        print(f"authority: {result['authority']}")
        print(f"threshold: {result['threshold_bytes']:,} bytes "
              '(contracts/tofudb_ir_v1.json activeWalRotation.softThresholdBytes)')
        print(f"newest active WAL: {_describe(result['newest'])}")
        print(f"predecessor still present: {_describe(result['predecessor'])}")
        for window in result['older']:
            print(f'older generation (not held): {_describe(window)}')
        if result['verdict'] == 'pass':
            print('PASS: every held WAL window is within the threshold; '
                  'run this again immediately before the restart')
        else:
            print('FAIL: ' + ', '.join(result['over_threshold'])
                  + ' exceeds the threshold; wait for rotation and check again')
    return {'pass': EXIT_PASS, 'fail': EXIT_FAIL}.get(result['verdict'], EXIT_UNDECIDED)


if __name__ == '__main__':
    sys.exit(main())
