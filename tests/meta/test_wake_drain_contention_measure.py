"""The wake-drain contention reproduction reports complete outcomes for this tree."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.api


def test_every_drain_completes_while_writers_commit_and_the_report_is_complete():
    result = subprocess.run(
        [sys.executable, str(ROOT / 'benchmarks/wake_drain_contention.py'), '--repository', str(ROOT),
         '--admissions', '12', '--writers', '3', '--writer-interval-ms', '20', '--ledger', '8',
         '--drains', '3'],
        capture_output=True, text=True, timeout=600, cwd=ROOT)
    assert result.returncode == 0, result.stderr[-4000:]
    report = json.loads(result.stdout)
    assert report['outcomes'] == {'completed': 3} and report['completedRate'] == 1.0
    assert report['writerCommits'] > 0
    assert set(report['sources']) == {'lib/project_dispatch/recovery.py', 'lib/project_dispatch/segmented.py',
                                      'lib/project_dispatch/repository.py'}
