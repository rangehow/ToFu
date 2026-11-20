"""The Rust SQL authority's error taxonomy is the Python owner's, exactly.

``packages/tofu-sql-authority/src/error.rs::ERROR_CODES`` folds any other
code to ``database_internal`` like ``lib/agent_core/storage_errors.py``; a
code added on one side only would silently change wire errors.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from lib.agent_core.storage_errors import COMMAND_ID_REUSED_MESSAGE, ERROR_CODES

pytestmark = pytest.mark.unit

SOURCE = Path(__file__).resolve().parents[2] / 'packages' / 'tofu-sql-authority' / 'src' / 'error.rs'


def test_rust_error_codes_equal_the_python_taxonomy():
    text = SOURCE.read_text(encoding='utf-8')
    block = re.search(r'pub const ERROR_CODES: &\[&str\] = &\[(.*?)\];', text, re.S)
    assert block is not None
    assert set(re.findall(r'"([a-z_]+)"', block.group(1))) == set(ERROR_CODES)
    assert f'"{COMMAND_ID_REUSED_MESSAGE}"' in text
