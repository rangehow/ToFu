"""A typed native-engine refusal skips unless the lane selected an engine.

Entry points: ``inherited_engine_refusal_skips`` wraps any native authority
start (the conftest setup/call hooks and the shared app fixture use it);
``require_native_engine_for_checkout`` checks before a child process starts
one; ``inherited_engine_skip_summary`` names such skips and their rerun
command in the run summary. A lane that selected ``TOFU_TEST_TOFUDB_BIN`` keeps every refusal fatal
(docs/TESTING_STRATEGY.md, "Live-app fixture prerequisites").
"""
from __future__ import annotations

import contextlib
import os
import shlex
import sys
from pathlib import Path

import pytest

INHERITED_ENGINE_SKIP = 'inherited native storage engine cannot serve this checkout'


@contextlib.contextmanager
def inherited_engine_refusal_skips():
    from serverctl_pkg.native_engine import EngineAdmissionRefusedError

    try:
        yield
    except EngineAdmissionRefusedError as exc:
        if os.environ.get('TOFU_TEST_TOFUDB_BIN'):
            raise
        pytest.skip(
            f'{INHERITED_ENGINE_SKIP}: '
            'select one built from it with TOFU_TEST_TOFUDB_BIN '
            '(docs/STORAGE_OPERATIONS.md) or run the SQLite lane with '
            f'TOFU_PERSONAL_STORAGE_BACKEND=sqlite ({exc})')


def inherited_engine_skip_summary(reports, args) -> list[str]:
    """Run-summary lines for REPORTS skipped by the inherited engine; [] when none.

    ``pytest -q`` shows such a skip as one ``s``, so a Worker checkout's browser
    and visual tests looked run. ARGS are the session's invocation arguments.
    """
    count = sum(1 for report in reports
                if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3
                and INHERITED_ENGINE_SKIP in str(report.longrepr[2]))
    if not count:
        return []
    command = shlex.join([sys.executable, '-m', 'pytest', *args])
    return [f'{count} test{"s" if count != 1 else ""} skipped: {INHERITED_ENGINE_SKIP}. '
            'Run them on the SQLite lane:',
            f'TOFU_PERSONAL_STORAGE_BACKEND=sqlite {command}',
            'or select an engine built from this checkout with TOFU_TEST_TOFUDB_BIN '
            '(docs/STORAGE_OPERATIONS.md).']


def require_native_engine_for_checkout() -> None:
    """Admit the engine pair exactly as a native authority started here would."""
    from lib.storage.supervisor import (
        _resolve_tofudb_binary, _resolve_tofudb_embedded_engine)

    with inherited_engine_refusal_skips():
        binary = Path(_resolve_tofudb_binary())
        if (binary.parent / 'libtofu_db.so').is_file():
            _resolve_tofudb_embedded_engine()
