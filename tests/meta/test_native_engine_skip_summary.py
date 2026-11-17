"""Tests skipped for an inherited native engine are named with the command that runs them.

A Worker checkout inherits the deployed engine, which refuses source it was not
built from, so its browser and visual tests skip. Behind ``pytest -q`` that was
one ``s`` per test (2026-11-17); the run summary now says why and how to run them.
"""
# A synthetic report location, never a repository file.
_AUDIT_SYNTHETIC_REPO_PATHS = ('tests/frontend/test_x_browser.py',)

import sys

import pytest
from _pytest.reports import TestReport

from tests.support.native_engine_prerequisite import inherited_engine_skip_summary

pytestmark = pytest.mark.unit


def _skipped(reason: str) -> TestReport:
    return TestReport('tests/frontend/test_x_browser.py::test_x', ('tests/frontend/test_x_browser.py', 1, 'test_x'),
                      {}, 'skipped', ('tests/frontend/test_x_browser.py', 20, f'Skipped: {reason}'), 'setup')


def test_engine_skips_are_counted_with_the_sqlite_lane_command():
    reports = [_skipped('inherited native storage engine cannot serve this checkout: select one ...'),
               _skipped('inherited native storage engine cannot serve this checkout: select one ...'),
               _skipped('built frontend graph missing (run `npm run build:frontend`)')]

    lines = inherited_engine_skip_summary(reports, ['-m', 'visual', 'tests/frontend/test_x_browser.py'])

    assert lines[0].startswith('2 tests skipped')
    assert (f'TOFU_PERSONAL_STORAGE_BACKEND=sqlite {sys.executable} -m pytest -m visual '
            'tests/frontend/test_x_browser.py') in lines
    assert any('TOFU_TEST_TOFUDB_BIN' in line for line in lines)


def test_no_engine_skip_writes_nothing():
    assert inherited_engine_skip_summary([_skipped('needs a display')], []) == []
