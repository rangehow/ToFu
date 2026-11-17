"""Certification tasks that must import the checkout under test, run as child interpreters.

  build  compile the checkout's engine exactly as restart preparation does
         (its own ``native_engine._build``: same Cargo command, release
         profile and snapshot sync) into a persistent private build root
  lane   run pytest in the canonical landing-verification environment
         (the checkout's ``landing_verification._test_environment``) with the
         native backend pinned to one engine binary

Ported from GATE-PUBLISH-CARRY's build_engine.py and runlane.py.
Usage: checkout_tasks.py build <checkout> <build-root> <out.json>
       checkout_tasks.py lane <checkout> <engine> <junit.xml> <workers> <pytest args...>
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time


def _import_from(checkout: Path, name: str):
    sys.path.insert(0, str(checkout))
    module = __import__(name, fromlist=['_'])
    if not Path(module.__file__).resolve().is_relative_to(checkout):
        raise SystemExit(f'{name} did not load from {checkout}: {module.__file__}')
    return module


def build(checkout: Path, build_root: Path, out: Path) -> int:
    engines = _import_from(checkout, 'serverctl_pkg.native_engine')
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('TOFUDB_BIN', 'TOFUDB_EXPECTED_SHA256')}
    build_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with engines._preparation_budget(engines.PREPARATION_TOTAL_TIMEOUT_SECONDS):
        # Cargo locks its target, but snapshot synchronization precedes Cargo.
        # Protect both, then detach the candidate before another run can rebuild it.
        with engines._prepare_lock(build_root / '.build.lock'):
            built = engines._build(checkout, build_root, environment, build_root=build_root)
            candidate = out.parent / 'candidate'
            candidate.mkdir()
            binary = candidate / built.name
            shutil.copy2(built, binary)
            for library in ('libtofu_db.so', 'libtofu_db.dylib', 'tofu_db.dll'):
                if (built.parent / library).is_file():
                    shutil.copy2(built.parent / library, candidate / library)
    out.write_text(json.dumps({
        'binary': str(binary), 'compile_seconds': round(time.monotonic() - started, 1),
        'build_info': engines.build_identity(binary),
        'source_sha256': engines.source_fingerprint(checkout)}, indent=1))
    return 0


def lane(checkout: Path, engine: str, junit: str, workers: str, pytest_args: list[str]) -> int:
    for name in ('TOFU_TEST_TOFUDB_BIN', 'TOFU_PERSONAL_STORAGE_BACKEND'):
        os.environ.pop(name, None)
    os.environ['TOFU_PERSONAL_STORAGE_BACKEND'] = 'tofudb'
    os.environ['TOFU_TEST_TOFUDB_BIN'] = engine
    landing = _import_from(checkout, 'lib.project_mod.landing_verification')
    environment = landing._test_environment()
    environment['PYTHONPATH'] = str(checkout)
    argv = [str(checkout / '.venv/bin/python3'), '-m', 'pytest', '-p', 'no:cacheprovider', '-rfEs',
            '--continue-on-collection-errors', f'--junitxml={junit}']
    if workers != '0':
        argv += ['-n', workers]
    return subprocess.call([*argv, *pytest_args], cwd=str(checkout), env=environment,
                           start_new_session=True)


if __name__ == '__main__':
    task, checkout, *rest = sys.argv[1:]
    if task == 'build':
        raise SystemExit(build(Path(checkout).resolve(), Path(rest[0]), Path(rest[1])))
    raise SystemExit(lane(Path(checkout).resolve(), rest[0], rest[1], rest[2], rest[3:]))
