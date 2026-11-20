"""Release SQL authority paging under eight independent concurrent readers.

Run with the fresh binary through run_rust_sql_release_load.sh; the original
semantic specification remains the oracle, including its transaction deadline.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.storage.test_turn_projection_reader import (
    test_every_authority_reassembles_turn_get_from_its_pages as reassemble_turn,
)

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not os.environ.get('TOFU_TEST_SQL_AUTHORITY_BIN'),
                       reason='requires a fresh remote release binary'),
]


@pytest.mark.parametrize('replica', range(24))
@pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)
def test_release_authority_reassembles_large_turn_under_load(
        semantic_authority, monkeypatch, replica):
    binary = os.environ.get('TOFU_TEST_SQL_AUTHORITY_BIN', '')
    assert binary and Path(binary).is_file(), 'run with the fresh remote release binary'
    reassemble_turn(semantic_authority, monkeypatch)
