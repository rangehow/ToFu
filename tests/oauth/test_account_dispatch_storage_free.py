"""Ordinary API-key dispatch can enter the account scope without storage."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from tests.support.agent_wheel_run import ROOT, dependency_paths

pytestmark = pytest.mark.unit


def test_ordinary_dispatch_scope_does_not_import_storage(tmp_path):
    source = textwrap.dedent('''
        import importlib.abc
        import sys
        from types import SimpleNamespace

        class NoStorage(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lib.storage' or fullname.startswith('lib.storage.'):
                    raise AssertionError('ordinary dispatch imported storage: ' + fullname)

        sys.meta_path.insert(0, NoStorage())
        from lib.oauth.account_dispatch import call_in_account_scope, current_account_dispatch
        assert current_account_dispatch() is None
        result = call_in_account_scope(
            SimpleNamespace(subscription_account=None),
            lambda value: (value, current_account_dispatch()), 'synthetic')
        assert result == ('synthetic', None)
        assert current_account_dispatch() is None
        assert not any(name == 'lib.storage' or name.startswith('lib.storage.')
                       for name in sys.modules)
    ''')
    environment = {
        'PYTHONPATH': os.pathsep.join([str(ROOT), *dependency_paths()]),
        'HOME': str(tmp_path),
        'TOFU_DATA_DIR': str(tmp_path / 'data'),
    }
    completed = subprocess.run(
        [sys.executable, '-S', '-c', source], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr


def test_account_dispatch_preserves_storage_error_identity():
    from lib.oauth.account_dispatch import StorageError
    from lib.storage.errors import StorageError as ReexportedStorageError

    assert StorageError is ReexportedStorageError
