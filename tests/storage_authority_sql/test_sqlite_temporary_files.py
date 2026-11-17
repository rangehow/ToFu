"""SQLite temporary files stay host-local whatever TMPDIR the Sidecar inherits.

DolphinFS fails read/write/sync on an open file that was already unlinked
with ENOENT, and SQLite unlinks every temporary file as soon as it opens it.
The ``faultfs`` shim's ``FAULTFS_UNLINKED_ENOENT`` mode reproduces that
medium under a scratch root; each case runs in a fresh interpreter because
SQLite reads ``SQLITE_TMPDIR``/``TMPDIR`` once, when it initializes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FAULTFS = ROOT / 'benchmarks' / 'agent_storage' / 'faultfs.c'

# Fills a temp table through a one-page temp cache, so SQLite spills it to its
# unlinked temporary file and reads it back.
SPILL = '''
def spill():
    import sqlite3
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute('CREATE TEMP TABLE spill(value BLOB NOT NULL)')
        connection.execute('PRAGMA temp.cache_size=1')
        connection.executemany('INSERT INTO spill VALUES (?)', [(b'x' * 4000,)] * 200)
        return connection.execute('SELECT count(*) FROM spill').fetchone()[0]
    except sqlite3.Error as exc:
        return repr(exc)
    finally:
        connection.close()
'''

BACKEND = SPILL + '''
import json, sys
from pathlib import Path
from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters import sqlite as adapter
from tests.storage_authority_sql.test_sqlite_read_pool import _config

mode, base, medium = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
if mode == 'medium-temporary-root':
    adapter.temporary_root = lambda: medium
backend = adapter.SQLiteBackend(_config(base))
try:
    backend.start()
except StorageError as exc:
    print(json.dumps({'started': False, 'code': exc.code, 'message': str(exc)}))
    raise SystemExit(0)
try:
    print(json.dumps({'started': True, 'spill': spill()}))
finally:
    backend.close()
'''


@pytest.fixture(scope='module')
def shim(tmp_path_factory):
    compiler = shutil.which('cc')
    if compiler is None:
        pytest.skip('a C compiler is required to build the faultfs shim')
    library = tmp_path_factory.mktemp('faultfs') / 'faultfs.so'
    subprocess.run([compiler, '-O2', '-shared', '-fPIC', '-o', str(library), str(FAULTFS), '-ldl'],
                   check=True)
    return library


def _run(shim: Path, medium: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update({
        'LD_PRELOAD': ' '.join(filter(None, [str(shim), env.get('LD_PRELOAD', '')])),
        'FAULTFS_ROOT': str(medium),
        'FAULTFS_UNLINKED_ENOENT': '1',
        'TMPDIR': str(medium),
        'SQLITE_TMPDIR': str(medium),
        'PYTHONPATH': str(ROOT),
    })
    result = subprocess.run([sys.executable, '-c', *args], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-4000:]
    return result.stdout.strip().splitlines()[-1]


def test_shim_reproduces_dolphinfs_unlinked_file_failure(shim, tmp_path):
    medium = tmp_path / 'dolphinfs'
    medium.mkdir()
    out = _run(shim, medium, SPILL + 'print(repr(spill()))')
    assert 'disk I/O error' in out


def test_sidecar_keeps_temporary_files_local_despite_inherited_tmpdir(shim, tmp_path):
    medium = tmp_path / 'dolphinfs'
    medium.mkdir()
    out = json.loads(_run(shim, medium, BACKEND, 'inherited-tmpdir', str(tmp_path), str(medium)))
    assert out == {'started': True, 'spill': 200}


def test_startup_refuses_a_temporary_root_that_cannot_serve_unlinked_files(shim, tmp_path):
    medium = tmp_path / 'dolphinfs'
    medium.mkdir()
    out = json.loads(_run(shim, medium, BACKEND, 'medium-temporary-root', str(tmp_path), str(medium)))
    assert out['started'] is False
    assert out['code'] == 'database_unavailable'
    assert 'unlinked open file' in out['message'] and str(medium) in out['message']
