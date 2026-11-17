"""Disposable real Sidecar fixture for artifact-domain tests."""

from __future__ import annotations

import pytest

from tests.support.sidecar_fixtures import (
    installed_storage_runtime, module_declares_plugin)


_PLUGIN_NAME = 'tests._artifact_sidecar'


@pytest.fixture(scope='module', autouse=True)
def artifact_storage(request, tmp_path_factory):
    """Install an isolated storage.v1 runtime."""
    if not module_declares_plugin(request, _PLUGIN_NAME):
        yield None
        return

    from lib.storage import StorageRuntime, StorageSupervisor

    project_root = tmp_path_factory.mktemp('artifact-sidecar')
    runtime = StorageRuntime(
        StorageSupervisor(
            project_root=project_root, backend='sqlite', startup_timeout=60),
        auto_restart=False,
    )
    with installed_storage_runtime(runtime):
        runtime.start()
        yield runtime
