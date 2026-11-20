"""Safety guards for launch APIs whose semantics are not yet ported.

These are NOT parity/certification tests for PostgreSQL, co-container handoff
or configured checkpoint control. Python supports those APIs. Until they are
implemented here the Rust candidate must refuse before creating an authority,
not ignore a required configuration and serve with weaker guarantees.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.storage_authority_sql.test_rust_sql_authority_differential import rust_authority_binary

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('setting,value,diagnostic', [
    ('TOFU_STORAGE_TEST_BACKEND', 'postgres', 'only the SQLite backend'),
    ('TOFU_STORAGE_CONNECTION_FILE', '/unimplemented-handoff.json', 'connection-file mode'),
    ('TOFU_STORAGE_NATIVE_CHECKPOINT', '{}', 'configured native checkpoint control'),
])
def test_unported_launch_requirement_cannot_be_silently_ignored(tmp_path, setting, value, diagnostic):
    root = tmp_path / 'must-not-open'
    environment = {
        'PATH': os.environ['PATH'],
        'HOME': str(tmp_path),
        'TMPDIR': str(tmp_path),
        'TOFU_STORAGE_PROJECT_ROOT': str(root),
        'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE': '1',
        'TOFU_STORAGE_TEST_BACKEND': 'sqlite',
        'TOFU_STORAGE_TOKEN': 'isolated-launch-safety-token-' * 2,
        setting: value,
    }
    completed = subprocess.run(
        [str(rust_authority_binary()), 'serve'], env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode != 0
    message = json.loads(completed.stdout)
    assert message['type'] == 'storage.error'
    assert diagnostic in message['diagnostic']
    assert not (root / 'data' / 'tofu.db').exists()
