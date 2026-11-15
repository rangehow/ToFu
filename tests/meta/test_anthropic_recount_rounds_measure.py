"""The Anthropic recount benchmark measures a real round pair with stable counts.

``benchmarks/anthropic_recount_rounds.py`` is evidence only while its history
has the documented shape and its recount result equals the former
implementation's count of the same translated body.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_round_pair_counts_equal_the_former_recount():
    spec = importlib.util.spec_from_file_location(
        'anthropic_recount_rounds', ROOT / 'benchmarks' / 'anthropic_recount_rounds.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from tests.llm.test_anthropic_recount_incremental import _legacy_recount
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.llm_sanitize._fields import _strip_non_api_fields

    result = module.run(2)
    history = module.history()
    assert result['messages'] == len(history) == 202
    assert result['toolUse'] == 100
    assert result['argumentBytes'] > 1_000_000
    plain = openai_body_to_anthropic({
        'model': module.MODEL, 'max_tokens': 4096, 'tools': module.tool_schemas(),
        'messages': _strip_non_api_fields(history)})
    assert result['counts'] == [_legacy_recount(plain, model=module.MODEL)]
