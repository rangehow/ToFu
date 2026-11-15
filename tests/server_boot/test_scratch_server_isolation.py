"""A scratch server shares nothing with the live one (tests/support/scratch_server.py).

2026-11-13 (SELF-REPAIR-2): the storage layer admits the engine into
``tempfile.gettempdir()/tofu-admitted-engines-<uid>/<sha>/`` (lib/storage/admitted_engine.py),
keyed by content. A scratch instance running the certified engine with the default temporary
directory ran its engine from production's exact admitted path, in production's admitted
directory, so checks and monitors that identify production's engine by executable path could not
tell the two apart. The scratch environment gives every instance a private ``TMPDIR`` and carries
nothing else of the live server's identity.

The child that admits checks its admitted root first and admits nothing outside the scratch root,
so this specification never writes into production's directory, even with a broken helper.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.support import production_api_guard
from tests.support.scratch_server import free_loopback_port, prepare_scratch_environment

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
# The admitted root of a process with this environment, computed without creating it.
_ADMITTED_ROOT = '''
import sys
import lib.storage.admitted_engine as admitted
admitted.ensure_private_directory = lambda path: None
print(admitted.private_root())
'''
# Admit a stand-in engine, but only into a root inside the scratch root (argv[1]).
_ADMIT_INSIDE = '''
import sys
from pathlib import Path
import lib.storage.admitted_engine as admitted
create = admitted.ensure_private_directory
admitted.ensure_private_directory = lambda path: None
root = admitted.private_root()
if not root.resolve().is_relative_to(Path(sys.argv[1]).resolve()):
    print(root)
    sys.exit(3)
admitted.ensure_private_directory = create
engine = admitted.AdmittedEngine.admit(sys.argv[2], lambda sha256: None, max_bytes=1 << 20)
print(engine.launch_path())
'''
# What a Tofu shell exports about the live server (names as found on 2026-11-13).
_LIVE_IDENTITY = {
    'TOFU_PROJECT_PATH': '/srv/production-tofu', 'PORT': '15000', 'TOFU_SUPERVISOR_PORT': '15001',
    'TOFU_PROCESS_ROLE': 'execution', 'TOFU_HEARTBEAT_DIR': '/tmp/tofu/heartbeat',
    'TOFUDB_CONNECTION_FILE': '/srv/production-tofu/data/.tofudb.json', '_TOFU_RUNTIME_PORT': '15000',
}


def _run(code: str, environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-c', code, *arguments], cwd=ROOT, env=environment,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize('inherited_tmpdir', [None, '/tmp'], ids=['production-default', 'tmpdir-is-tmp'])
def test_a_scratch_instance_admits_its_engine_outside_production_admitted_directory(tmp_path, inherited_tmpdir):
    production = {name: value for name, value in os.environ.items() if name not in ('TMPDIR', 'TEMP', 'TMP')}
    if inherited_tmpdir:
        production['TMPDIR'] = inherited_tmpdir
    # Production launches with no TMPDIR (read from the serving pid's environ, 2026-11-13).
    production_root = _run(_ADMITTED_ROOT, production)
    assert production_root.returncode == 0, production_root.stderr[-2000:]
    production_root = Path(production_root.stdout.strip())

    engine = tmp_path / 'tofu-db'
    engine.write_bytes(b'#!/bin/sh\n# stand-in engine bytes\n')
    scratch = tmp_path / 'scratch'
    environment = prepare_scratch_environment(scratch, free_loopback_port(), ROOT,
                                              {**production, **_LIVE_IDENTITY})
    admitted = _run(_ADMIT_INSIDE, environment, str(scratch), str(engine))

    assert admitted.returncode == 0, (f'the scratch instance would admit its engine into {admitted.stdout.strip()}, '
                                      f'not inside {scratch}', admitted.stderr[-2000:])
    launch_path = Path(admitted.stdout.strip())
    assert launch_path.is_relative_to((scratch / 'tmp').resolve()) and launch_path.read_bytes() == engine.read_bytes()
    assert not launch_path.is_relative_to(production_root)


def test_the_scratch_environment_carries_nothing_of_the_live_server(tmp_path):
    port = free_loopback_port()
    environment = prepare_scratch_environment(tmp_path / 'scratch', port, ROOT,
                                              {**_LIVE_IDENTITY, 'TMPDIR': '/tmp', 'PATH': '/usr/bin',
                                               'UNRELATED_SETTING': 'x'})
    scratch = (tmp_path / 'scratch').resolve()

    assert not {'TOFU_SUPERVISOR_PORT', 'TOFU_PROCESS_ROLE', 'TOFUDB_CONNECTION_FILE', '_TOFU_RUNTIME_PORT',
                'UNRELATED_SETTING'} & set(environment)
    assert environment['PATH'] == '/usr/bin' and environment['PORT'] == str(port)
    assert (environment['TOFU_RUN_SERVER'], environment['BIND_HOST']) == ('1', '127.0.0.1')
    for name in ('TOFU_DATA_DIR', 'TOFU_PROJECT_PATH', 'TOFU_HEARTBEAT_DIR', 'TMPDIR'):
        assert Path(environment[name]).is_relative_to(scratch) and Path(environment[name]).is_dir(), name
    assert production_api_guard.production_child(['server.py'], environment) == ''
