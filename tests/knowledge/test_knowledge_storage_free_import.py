"""The knowledge facade imports without the wheel-excluded ``lib.storage``.

The storage-free ``tofu-agent`` wheel leaves ``lib.storage`` out. Importing any
``lib.knowledge`` module still runs the package facade, and it used to import
``StorageError`` at module scope from ``lib.knowledge.store``. The failure was
silently swallowed by the tool-argument repair index builder, which then
disabled repair for the whole run. Keep the storage admission at the one
publication call that needs it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

_PROBE = r'''
import importlib.abc
import sys

blocked = []


class BlockStorage(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == 'lib.storage' or name.startswith('lib.storage.'):
            blocked.append(name)
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return None


sys.meta_path.insert(0, BlockStorage())
from lib.knowledge.tool import SEARCH_KNOWLEDGE_TOOL
from lib.tool_input_repair._schema import _build_schema_index

index = _build_schema_index()
assert SEARCH_KNOWLEDGE_TOOL['function']['name'] == 'search_knowledge'
assert 'search_knowledge' in index, sorted(index)
assert blocked == [], blocked
'''


def test_knowledge_schema_repair_imports_without_storage():
    environment = {**os.environ, 'PYTHONPATH': str(ROOT)}
    completed = subprocess.run(
        [sys.executable, '-c', _PROBE], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr[-4000:]