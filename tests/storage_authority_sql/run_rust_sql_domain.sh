#!/bin/sh
# Existing authority-boundary suites on an isolated remote SQLite lane.
set -eu
: "${1:?remote_cargo must supply the checked release binary}"
export TOFU_TEST_SQL_AUTHORITY_BIN="$1" PYTHONPATH="$PWD"
# This is the disposable oracle's personal selector only. The runner receives
# a clean environment and cannot mutate the production selector or data root.
export TOFU_PERSONAL_STORAGE_BACKEND=sqlite
exec "$TOFU_REMOTE_PYTHON" - <<'PY'
from pathlib import Path
import re
import subprocess
import sys
import os
import json
import xml.etree.ElementTree as ET

owners = re.compile(r'semantic_authorit|_chat_sidecar|StorageSupervisor\(')
files = [str(path) for directory in ('tests/storage', 'tests/tofudb')
         for path in sorted(Path(directory).glob('test_*.py'))
         if path.name != 'test_tofudb_differential.py'
         and owners.search(path.read_text(encoding='utf-8'))]
assert files, 'SQL authority lane selected no test files'
report = Path(os.environ['TMPDIR']) / 'sql-domain.xml'
status = subprocess.call([
    sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-n', '8',
    '--tb=short', '-rfE', f'--junitxml={report}', *files,
])
# Keep an auditable outcome at the END of stdout: a large setup-error log
# otherwise pushes the actual failing cases beyond the transport's window.
if report.exists():
    root = ET.parse(report).getroot()
    outcomes = []
    for case in root.iter('testcase'):
        for outcome in case:
            if outcome.tag in ('failure', 'error'):
                outcomes.append({'case': case.get('classname', '') + '::' + case.get('name', ''),
                                 'outcome': outcome.tag, 'message': outcome.get('message', '')[:1000]})
    print('SQL_DOMAIN_NONPASS=' + json.dumps(outcomes, ensure_ascii=True), flush=True)
raise SystemExit(status)
PY
