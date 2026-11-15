"""Offline repository qualification checks follow real tool outcome semantics."""
from pathlib import Path
import os
import re
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
# These checks compare against reference_tags.py's extraction oracle, read from
# a retained private Git ancestor that a fresh-root public checkout lacks.
REFERENCE_ORACLE_CHECKS = {
    'check_public_tool_port.py', 'check_publication_port.py',
    'check_bootstrap_epoch_task.py', 'check_loaded_epoch_task.py',
}


def _reference_oracle_revision_present() -> bool:
    source = (ROOT / 'benchmarks/repository_index/reference_tags.py').read_text(encoding='utf-8')
    revision = re.search(r"^REVISION = '([0-9a-f]+)'$", source, re.MULTILINE).group(1)
    return subprocess.run(
        ['git', 'cat-file', '-e', f'{revision}^{{commit}}'], cwd=ROOT,
        capture_output=True).returncode == 0


@pytest.mark.parametrize('script', [
    'check_tool_port.py', 'check_public_tool_port.py', 'check_publication_port.py',
    'check_task_indexes.py', 'check_bootstrap_epoch_task.py', 'check_loaded_epoch_task.py',
])
def test_repository_port_qualification(script, tmp_path):
    """Exercise success, foreign-root refusal, settlement and owned teardown."""
    if script in REFERENCE_ORACLE_CHECKS and not _reference_oracle_revision_present():
        pytest.skip('the extraction oracle revision is not in this checkout\'s history')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join(filter(None, (
        str(ROOT), environment.get('PYTHONPATH'),
    )))
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'benchmarks/repository_index' / script)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
