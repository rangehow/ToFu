"""Effect declarations decide fencing, so every registered tool's must be true.

Project dispatch neither fences nor records calls declared read-only or
idempotent (docs/PROJECT_COORDINATION.md, "An unconfirmed effect never wedges a
Worker"), and publication waits only on the rest. A side effect declared as a
read would skip both, so each declaration below is checked against its evidence.
"""
import pytest

from lib.tools.registry import all_specs, declared_effect_recovery

pytestmark = pytest.mark.unit

# Known mismatches: each once compiled to the wrong class. Evidence for the true one.
KNOWN_EFFECT_CLASSES = {
    'integration_checkpoint': ('idempotent', 'creates a checkpoint ref of the isolated worktree; '
                                             'repeating captures the same workspace again'),
    'integration_submit': ('idempotent', 'checkpoints and queues the workspace for review; '
                                         'resubmitting the same state is a no-op or a refusal'),
    'inspect_tool_effect': ('idempotent', 'records the acknowledgement of an unknown effect'),
}

# Refused by the 2026-11-11 fence although a repeat changes nothing.
INCIDENT_REPEATABLE = ('poll', 'await_project_workers', 'get_conversation', 'search_tools',
                       'read_working_facts', 'read_files', 'search_files', 'find_files')

# Side effects that must keep their fence.
SIDE_EFFECTS = ('run_command', 'write_file', 'edit_file', 'send_project_message',
                'consider_project_message')


def _owning_spec(name):
    owners = [spec for spec in all_specs() if name in spec.provides]
    assert len(owners) == 1, f'{name} is declared by {len(owners)} specs'
    return owners[0]


@pytest.mark.parametrize('name', sorted(KNOWN_EFFECT_CLASSES))
def test_known_mismatches_keep_their_true_effect_class(name):
    expected, evidence = KNOWN_EFFECT_CLASSES[name]
    assert declared_effect_recovery(name) == expected, evidence
    # An effect is never served from an earlier result of the same task.
    spec = _owning_spec(name)
    assert spec.cacheable_tools is not None and name not in spec.cacheable_tools


@pytest.mark.parametrize('name', INCIDENT_REPEATABLE)
def test_reads_the_fence_refused_are_repeatable(name):
    assert declared_effect_recovery(name) in ('read_only', 'idempotent')


@pytest.mark.parametrize('name', SIDE_EFFECTS)
def test_side_effects_keep_their_fence(name):
    assert declared_effect_recovery(name) == 'reconcile'


def test_every_registered_declaration_is_consistent():
    contradictions = []
    for spec in all_specs():
        natives = {contract.name: contract for contract in spec.contracts}
        for name in sorted(spec.provides):
            declared = declared_effect_recovery(name)
            if name in spec.write_tools and declared == 'read_only':
                contradictions.append(f'{name}: a write declared read-only')
            native = natives.get(name)
            if native is None:
                continue
            if native.effect_recovery != declared:
                contradictions.append(f'{name}: native {native.effect_recovery} != {declared}')
            if native.idempotency == 'non_idempotent' and name in spec.idempotent_tools:
                contradictions.append(f'{name}: non-idempotent contract listed as idempotent')
            if native.effect_recovery == 'read_only' and name in spec.write_tools:
                contradictions.append(f'{name}: read-only contract listed as a write')
    assert not contradictions


def test_an_undeclared_tool_reconciles():
    assert declared_effect_recovery('mcp__example__mutate') == 'reconcile'
    assert declared_effect_recovery('never_registered_tool') == 'reconcile'
