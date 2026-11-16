"""Git output is decoded as UTF-8, never with the process locale encoding.

On windows-latest the locale codec is cp1252: ``git diff HEAD`` of non-ASCII
source (the boot-identity code fingerprint) killed subprocess's reader thread
with ``UnicodeDecodeError: 'charmap'``. An ASCII locale reproduces it on POSIX.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

_CHILD = r'''
import sys
from lib.self_update import _git
_git._ROOT = sys.argv[1]
completed = _git._run_git(['diff', 'HEAD'])
assert completed.returncode == 0, completed.stderr
sys.stdout.buffer.write(completed.stdout.encode('utf-8'))
'''


@pytest.mark.skipif(shutil.which('git') is None, reason='git is required')
def test_run_git_decodes_utf8_diff_under_a_non_utf8_locale(tmp_path):
    repository = tmp_path / 'repository'
    repository.mkdir()
    git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t']
    subprocess.run([*git, 'init', '-q'], check=True)
    note = repository / 'note.txt'
    note.write_text('原始\n', encoding='utf-8')
    subprocess.run([*git, 'add', 'note.txt'], check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'seed'], check=True)
    note.write_text('修改后\n', encoding='utf-8')

    environment = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C', 'PYTHONUTF8': '0',
                   'PYTHONPATH': str(ROOT)}
    environment.pop('PYTHONIOENCODING', None)
    completed = subprocess.run(
        [sys.executable, '-X', 'utf8=0', '-c', _CHILD, str(repository)],
        cwd=ROOT, env=environment, capture_output=True, timeout=60)

    assert completed.returncode == 0, completed.stderr.decode('utf-8', 'replace')
    assert '+修改后' in completed.stdout.decode('utf-8')
