"""Disposable storage.v1 runtime for tenant-user integration tests."""

from __future__ import annotations

import pytest

from tests.support.sidecar_fixtures import (
    installed_storage_runtime, module_declares_plugin)


_PLUGIN_NAME = 'tests._billing_user_sidecar'


@pytest.fixture(scope='module', autouse=True)
def billing_user_storage(request, tmp_path_factory):
    if not module_declares_plugin(request, _PLUGIN_NAME):
        yield None
        return

    from lib.storage import StorageRuntime, StorageSupervisor

    project_root = tmp_path_factory.mktemp('billing-user-sidecar')
    runtime = StorageRuntime(
        StorageSupervisor(
            project_root=project_root, backend='sqlite', startup_timeout=60),
        auto_restart=False,
    )
    with installed_storage_runtime(runtime):
        runtime.start()
        yield runtime
