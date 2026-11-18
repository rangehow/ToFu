"""benchmarks/harness_perf/rust_prototype.py stays runnable against the corpus.

The Rust prototype arm takes its system prompt and tool schemas from the LLM
replay corpus filtered to Tofu's offered tool surface; it must keep offering
the two tools the measured scenarios call, and the byte-identity Rust arm must
stay importable beside the Python-only run.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parents[2] / 'benchmarks' / 'harness_perf'


def _module(name):
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location(name, HERE / f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HERE))


def test_prototype_prompt_offers_the_measured_tools_in_tofu_order():
    prototype = _module('rust_prototype')
    sys.path.insert(0, str(HERE))
    try:
        system, tools = prototype.corpus_prompt()
    finally:
        sys.path.remove(str(HERE))
    names = [tool['function']['name'] for tool in tools]
    assert system and {'read_files', 'run_command'} <= set(names)
    assert 'todo_write' not in names
    assert names == [name for name in prototype.TOFU_OFFERED_TOOLS if name in names]


def test_byte_identity_keeps_its_python_and_rust_arms():
    byte_identity = _module('byte_identity')
    assert callable(byte_identity.run) and callable(byte_identity.run_rust)
