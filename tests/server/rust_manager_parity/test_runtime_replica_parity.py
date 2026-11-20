"""Worker runtime replica and shared bytecode cache, against both managers.

The live manager runs the worker from a verified local replica of its Python
environment when that environment lives on a slow volume, and points the
worker's bytecode at a private local cache; it holds a shared lease on each
for the worker's lifetime so collection never removes them underneath it
(``serverctl_pkg.runtime_replica``, ``serverctl_pkg.python_bytecode_cache``).

The manager runs with the installed interpreter on its own (network) volume,
as production does. The test publishes a replica generation of that
environment under a private placement root (``TOFU_LOCAL_DIR``): the real
manifest fingerprint, and an interpreter that marks the worker it runs and
then execs the source interpreter. Selection, leases, suspicion and rejection
are the real policy; nothing is copied or built.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import pytest

from tests.server.rust_manager_parity.conftest import wait_for

pytestmark = pytest.mark.api
INSTALLED_PYTHON = os.path.realpath(sys.executable)


@pytest.fixture
def placement_root():
    # A /tmp root is age-cleaned, so placement never admits it for interpreters.
    root = Path.home() / '.cache' / 'tofu-parity-replicas' / uuid.uuid4().hex
    root.mkdir(parents=True, mode=0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def generation(placement_root) -> Path:
    from serverctl_pkg import local_placement
    from serverctl_pkg.runtime_replica import describe_environment, fingerprint, registry_path
    placement = local_placement.choose_local_root(
        os.path.dirname(INSTALLED_PYTHON), local_placement.PURPOSE_RUNTIME,
        environ={'TOFU_LOCAL_DIR': str(placement_root)})
    environment = describe_environment(INSTALLED_PYTHON)
    if not placement.selected or environment is None or environment.kind != 'prefix':
        pytest.skip(f'{INSTALLED_PYTHON} is not on a volume that placement replicates ({placement.reason})')
    published = registry_path(placement.root, environment.prefix) / 'g-parity'
    interpreter = published / environment.python
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f'#!/bin/sh\nTOFU_PARITY_INTERPRETER="$0" exec {INSTALLED_PYTHON} "$@"\n')
    interpreter.chmod(0o755)
    (published / '.tofu-replica.lease').write_text('')
    (published / '.tofu-replica.json').write_text(json.dumps({
        'schema': 1, 'fingerprint': fingerprint(environment), 'sourcePrefix': environment.prefix,
        'sourcePython': INSTALLED_PYTHON}))
    (published.parent / 'live').symlink_to(published.name)
    return published


@pytest.fixture
def manager_environment() -> dict[str, str]:
    return {'TOFU_SUPERVISOR_PYTHON': INSTALLED_PYTHON}


@pytest.fixture
def prepare_project(placement_root, generation, tmp_path):
    cache = tmp_path / 'pycache-root'

    def prepare(project: Path) -> None:
        (project / '.env').write_text(
            f'TOFU_LOCAL_DIR={placement_root}\nTOFU_SERVER_PYTHON_CACHE=on\n'
            f'TOFU_SERVER_PYTHON_CACHE_DIR={cache}\n')
    return prepare


def flock_holders(path: Path) -> set[int]:
    """PIDs holding a flock on ``path`` (``/proc/locks``)."""
    info = os.stat(path)
    device = f'{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}'
    holders = set()
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if '->' in fields:  # a blocked waiter, not a holder
            continue
        if len(fields) >= 6 and fields[1] == 'FLOCK' and fields[5] == device:
            holders.add(int(fields[4]))
    return holders


def worker_interpreter(manager) -> str | None:
    return json.loads((manager.project / 'env-worker.json').read_text())['TOFU_PARITY_INTERPRETER']


def test_the_worker_runs_on_the_leased_replica_with_the_leased_bytecode_cache(manager, generation):
    manager.start_serving()
    state = manager.state()
    runtime, cache = state['workerRuntime'], state['workerBytecodeCache']

    assert runtime['replica'] is True and runtime['reason'] == 'replica', runtime
    assert runtime['generationPaths'] == [str(generation)]
    assert runtime['sourcePython'] == INSTALLED_PYTHON
    assert worker_interpreter(manager) == runtime['python'] and runtime['python'].startswith(str(generation))
    assert manager.process.pid in flock_holders(generation / '.tofu-replica.lease')

    assert cache['managed'] is True and cache['reason'] == 'forced', cache
    namespace = Path(cache['cacheRoot']) / cache['namespace']
    worker = json.loads((manager.project / 'env-worker.json').read_text())
    assert worker['PYTHONPYCACHEPREFIX'] == str(namespace / 'pycache')
    assert manager.process.pid in flock_holders(namespace / '.active.lock')

    code, stopped = manager.control('stop')
    assert code == 200 and stopped.get('ok') is True
    wait_for(lambda: manager.process.pid not in flock_holders(namespace / '.active.lock')
             and manager.process.pid not in flock_holders(generation / '.tofu-replica.lease'),
             "the stopped worker's leases to be released")


def test_a_replica_that_fails_before_readiness_is_rejected_once_the_source_serves(manager, generation):
    (manager.project / 'worker-crash-once').write_text('')

    code, started = manager.control('start')

    assert code == 200, (code, started)
    current = manager.wait_serving()
    state = manager.state()
    assert state['workerRuntime']['replica'] is False and state['workerRuntime']['reason'] == 'replica-suspect'
    assert worker_interpreter(manager) is None, 'the attribution spawn must run the source interpreter'
    assert current['observed'] == 'running'
    wait_for(lambda: (generation / '.tofu-replica.rejected').exists(), 'the replica to be rejected')
    wait_for(lambda: manager.state()['runtimeSuspect'] == {}, 'the settled suspicion to clear')
    log = manager.log_text()
    assert f'worker failed before readiness on runtime replica {generation}' in log
    assert f'worker runtime replica {generation} rejected' in log
