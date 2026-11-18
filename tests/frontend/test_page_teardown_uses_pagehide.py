"""Page-lifetime teardown runs on a final pagehide, never on beforeunload.

beforeunload also fires for navigations that never unload the document (a
cancelled leave prompt, a link answered with a download). Tearing the
retained composition down there left a live page whose dialogs resolved as
dismissed (send-while-generating silently did nothing) and whose sidebar
listeners were gone (the copy-ID button stopped working).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# beforeunload is legitimate only where it does not end the page's lifetime:
# the unsaved-changes prompt itself and a best-effort final flush.
BEFOREUNLOAD_ALLOWED = {
    'frontend/src/features/orchestration/dirty-guard.ts',
    'frontend/src/features/paper/lifecycle.ts',
}


def _authored_sources() -> list[str]:
    listed = subprocess.run(
        ['git', 'ls-files', 'frontend/src'], cwd=ROOT, check=True,
        capture_output=True, text=True).stdout.split()
    return [
        path for path in listed
        if path.endswith(('.ts', '.js'))
        and '.test.' not in path
        and 'generated' not in path
        and path != 'frontend/src/runtime/app-runtime.js'
    ]


def test_no_page_teardown_is_bound_to_beforeunload():
    pattern = re.compile(r"""['"]beforeunload['"]""")
    offenders = [
        path for path in _authored_sources()
        if path not in BEFOREUNLOAD_ALLOWED
        and pattern.search((ROOT / path).read_text(encoding='utf-8'))
    ]
    assert offenders == [], offenders
