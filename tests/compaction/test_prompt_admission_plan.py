"""Plan-only final prompt pressure uses frozen facts, never execution ports."""
from dataclasses import replace

import pytest

from lib.tasks_pkg.compaction import _prompt_admission as admission
from lib.tasks_pkg.compaction._prompt_admission import (
    PromptAdmissionPlanInput, plan_dispatch_prompt_limit,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('output', 'boundary'), [(10_000, 63_000), (45_000, 55_000)])
def test_plan_checks_window_safety_and_resolved_output(monkeypatch, output, boundary):
    def forbidden(*args, **kwargs):
        pytest.fail('plan entered a live admission path')

    for name in ('_measure', '_resolved_ceiling', 'refresh_turn_requirements',
                 'restore_checklist_context', 'force_compact_if_needed',
                 'recompose_context_after_compaction', '_record'):
        monkeypatch.setattr(admission, name, forbidden)

    # The frozen total already includes schemas. Equality fits; a one-token
    # increase crosses either the safety threshold or the output reserve.
    facts = PromptAdmissionPlanInput(
        total_tokens=boundary, context_window_tokens=100_000,
        resolved_output_tokens=output,
    )
    assert plan_dispatch_prompt_limit(facts) == 'fits'
    assert plan_dispatch_prompt_limit(replace(facts, total_tokens=boundary + 1)) == (
        'needs_compaction')


@pytest.mark.parametrize('overrides', [
    {'total_tokens': None}, {'context_window_tokens': None},
    {'resolved_output_tokens': None}, {'total_tokens': True},
    {'total_tokens': 0}, {'context_window_tokens': -1},
    {'resolved_output_tokens': 100_000}, {'context_window_tokens': 10**400},
    {'total_tokens': 2**53},
])
def test_missing_or_invalid_frozen_facts_remain_unknown(overrides):
    facts = PromptAdmissionPlanInput(
        total_tokens=70_000, context_window_tokens=100_000,
        resolved_output_tokens=10_000,
    )
    assert plan_dispatch_prompt_limit(replace(facts, **overrides)) == 'unknown'
