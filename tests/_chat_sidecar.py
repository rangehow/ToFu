"""Shared real Sidecar authority for chat-domain storage tests.

The runtime is module-scoped and rooted in a disposable system directory,
while activation is explicitly requested by each test module. This keeps
storage state isolated from the repository database and makes persistence
tests exercise the same client/service contract as production.

Usage in a test module::

    pytest_plugins = ('tests._chat_sidecar',)
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid

import pytest

from tests.support.process_scratch import process_scratch_directory
from tests.support.sidecar_fixtures import installed_storage_runtime


@contextmanager
def _started_sqlite_sidecar():
    from lib.storage import StorageRuntime, StorageSupervisor
    scratch = process_scratch_directory('chat-sidecar-')
    project_root = scratch / uuid.uuid4().hex[:8]
    project_root.mkdir(parents=True, exist_ok=True)
    runtime = StorageRuntime(
        StorageSupervisor(
            project_root=project_root, backend='sqlite', startup_timeout=60),
        auto_restart=False,
    )
    try:
        with installed_storage_runtime(runtime):
            runtime.start()
            yield runtime
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture(scope='module')
def _chat_sidecar_runtime():
    """Start one real SQLite-backed Sidecar for an opted-in test module."""
    with _started_sqlite_sidecar() as runtime:
        yield runtime


@pytest.fixture
def isolated_chat_sidecar():
    """Give one test an empty authority of its own.

    For assertions over authority-wide scans (such as boot recovery), which
    would otherwise also observe rows earlier tests left in the module's
    shared Sidecar.
    """
    with _started_sqlite_sidecar() as runtime:
        yield runtime


def require_authority_for_owner(request, owner: int) -> None:
    """Keep the ready process authority only when it serves ``owner``.

    Task fixtures keep an authority their module installed (for example a
    native parameter bound to the tasks' owner). The shared test authority
    that ``start_storage()`` opens also stays ready between modules; when it
    is native TofuDB it is bound to the owner it started with
    (``TOFUDB_OWNER_ID``, default ``PERSONAL_USER_ID``) and refuses every
    owned write for another owner with ``authority_scope_mismatch``. SQL
    authorities scope each request and serve any owner. Every other case
    installs this module's Sidecar.
    """
    from lib.storage import get_storage_client, storage_status

    status = storage_status()
    if status.get('ready'):
        if status.get('backend') != 'tofudb':
            return
        bound_owner = getattr(get_storage_client(), '_owner_id', None)
        if bound_owner is None:
            raise AssertionError('the ready native authority exposes no bound owner')
        if bound_owner == owner:
            return
    request.getfixturevalue('chat_sidecar')


@pytest.fixture
def chat_sidecar(_chat_sidecar_runtime, monkeypatch):
    """Expose the module's Sidecar authority to one test.

    The environment marker is temporary test-harness isolation while the
    repository-wide fixture stops exporting the retired legacy mode; the
    production storage modules exercised here do not branch on it.
    """
    yield _chat_sidecar_runtime
