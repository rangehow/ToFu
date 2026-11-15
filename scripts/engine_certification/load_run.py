"""Run a checkout's production-shaped load test so its final storage stop completes.

The rollback drill needs data the candidate engine wrote and then shut down
cleanly. ``scripts/tofudb_load_test.py`` re-executes itself as a ``--child``
that owns the storage runtime, and harnesses before 2026-11-12 stopped it
through ``StorageRuntime.stop``'s 10 s default, SIGKILLing any engine whose
ordered shutdown takes longer (GATE-PUBLISH-CARRY's finding). This adapter
runs the checkout's own harness unchanged and gives that child the stop budget
the production native storage host uses (``NATIVE_DAEMON_STOP_SECONDS``,
passed in by the certifier), so historical commits certify like current ones.
The child logs the budget and how long the final stop took.

Usage: load_run.py parent <stop-seconds> <checkout> <tofudb_load_test args...>
       load_run.py child <stop-seconds> <harness-script> --child <args...>
"""
from __future__ import annotations

import runpy
import subprocess
import sys
import time
from pathlib import Path


def parent(stop_seconds: str, checkout: Path, argv: list[str]) -> int:
    sys.path[:0] = [str(checkout), str(checkout / 'scripts')]
    import tofudb_load_test
    if not Path(tofudb_load_test.__file__).resolve().is_relative_to(checkout):
        raise SystemExit(f'the load harness did not load from {checkout}: {tofudb_load_test.__file__}')
    script = str(Path(tofudb_load_test.__file__).resolve())
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if isinstance(command, list) and command[1:3] == [script, '--child']:
            command = [command[0], str(Path(__file__).resolve()), 'child', stop_seconds, *command[1:]]
        return real_run(command, *args, **kwargs)
    tofudb_load_test.subprocess.run = run
    return tofudb_load_test.main(argv)


def child(stop_seconds: str, script: str, argv: list[str]) -> None:
    checkout = Path(script).resolve().parents[1]
    sys.path.insert(0, str(checkout))
    import lib.storage.runtime as runtime
    if not Path(runtime.__file__).resolve().is_relative_to(checkout):
        raise SystemExit(f'the storage runtime did not load from {checkout}: {runtime.__file__}')
    budget = float(stop_seconds)
    real_stop = runtime.StorageRuntime.stop

    def stop(self, timeout=budget):
        started = time.monotonic()
        print(f'engine_certification final storage stop budget_s={timeout}', flush=True)
        try:
            return real_stop(self, timeout)
        finally:
            print(f'engine_certification final storage stop returned after '
                  f'{time.monotonic() - started:.1f}s', flush=True)
    runtime.StorageRuntime.stop = stop
    sys.argv = [script, *argv]
    runpy.run_path(script, run_name='__main__')


if __name__ == '__main__':
    mode, stop_seconds, target, *rest = sys.argv[1:]
    if mode == 'parent':
        raise SystemExit(parent(stop_seconds, Path(target).resolve(), rest))
    child(stop_seconds, target, rest)
