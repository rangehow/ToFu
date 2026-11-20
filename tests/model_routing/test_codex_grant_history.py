"""The optional future Codex planner must never infer revoked consent."""
import pytest

from lib.model_routing.codex_grant_history import (
    GrantHistory, ModelIdentity, Observation, establish_codex_baseline, plan_codex_grants,
)

pytestmark = pytest.mark.unit

A = ModelIdentity('openai', 'a')
B = ModelIdentity('openai', 'b')
EMPTY = frozenset()


def history(complete=True, wires=(), models=()):
    return GrantHistory(complete, frozenset(wires), frozenset(models))


def test_new_identity_is_granted_once_and_revocation_survives_reappearance():
    first = plan_codex_grants(history(), EMPTY, (Observation('wire-a', A),))
    assert first.grants == {A}
    absent = plan_codex_grants(first.history, EMPTY, ())
    again = plan_codex_grants(absent.history, absent.grants, (Observation('wire-a', A),))
    assert again.grants == EMPTY
    assert again.history == first.history


@pytest.mark.parametrize('old_wires,old_models', [({'wire-a'}, set()), (set(), {A})])
def test_seen_wire_or_model_refuses_reauthorization(old_wires, old_models):
    result = plan_codex_grants(history(wires=old_wires, models=old_models), EMPTY,
                               (Observation('wire-a', A),))
    assert result.grants == EMPTY


def test_legacy_incomplete_history_never_becomes_complete_by_observation():
    result = plan_codex_grants(history(False), frozenset({B}), (Observation('wire-a', A),))
    assert result.grants == {B}
    assert not result.history.complete
    assert result.history.models == {A}


def test_pending_binding_is_not_evidence_of_new_consent():
    pending = plan_codex_grants(history(), EMPTY, (Observation('wire-a', None),))
    resolved = plan_codex_grants(pending.history, EMPTY, (Observation('wire-a', A),))
    assert resolved.grants == EMPTY


@pytest.mark.parametrize('observations', [
    (Observation('w', A), Observation('w', B)),
    (Observation('w', A), Observation('w', None)),
    (Observation('w', A), Observation('other', A)),
])
def test_ambiguous_batch_is_never_granted_even_later(observations):
    first = plan_codex_grants(history(), EMPTY, observations)
    assert first.grants == EMPTY
    later = plan_codex_grants(first.history, EMPTY, (Observation('w', A),))
    assert later.grants == EMPTY


def test_duplicate_identical_observation_is_idempotent():
    obs = (Observation('w', A), Observation('w', A))
    first = plan_codex_grants(history(), EMPTY, obs)
    second = plan_codex_grants(first.history, first.grants, obs)
    assert first == second
    assert first.grants == {A}


@pytest.mark.parametrize('obs', [Observation('', A), Observation('w', ModelIdentity('', 'a'))])
def test_invalid_identity_rejected(obs):
    with pytest.raises(ValueError):
        plan_codex_grants(history(), EMPTY, (obs,))


def test_provably_novel_pending_reservation_grants_once_after_unique_binding():
    pending = plan_codex_grants(history(), EMPTY, (Observation('w', None, True),))
    assert pending.history.pending_wires == {'w'}
    resolved = plan_codex_grants(pending.history, EMPTY, (Observation('w', A),))
    assert resolved.grants == {A}
    assert not resolved.history.pending_wires
    assert plan_codex_grants(resolved.history, EMPTY, (Observation('w', A),)).grants == EMPTY


def test_pending_reservation_lost_on_ambiguity_and_never_revives():
    pending = plan_codex_grants(history(), EMPTY, (Observation('w', None, True),))
    ambiguous = plan_codex_grants(pending.history, EMPTY, (Observation('w', None),))
    assert not ambiguous.history.pending_wires
    assert not plan_codex_grants(ambiguous.history, EMPTY, (Observation('w', A),)).grants


def test_explicit_baseline_records_current_and_prior_without_grant_or_reservation():
    baseline = establish_codex_baseline(
        history(False, {'old-wire'}, {B}),
        (Observation('wire-a', A), Observation('pending', None, True)))
    assert baseline.complete
    assert baseline.wires == {'old-wire', 'wire-a', 'pending'}
    assert baseline.models == {A, B}
    assert not baseline.pending_wires
    assert not plan_codex_grants(baseline, EMPTY, (Observation('wire-a', A),)).grants
    assert not plan_codex_grants(baseline, EMPTY, (Observation('pending', B),)).grants


def test_rebaseline_cancels_old_reservations():
    previous = GrantHistory(True, frozenset({'w'}), EMPTY, frozenset({'w'}))
    baseline = establish_codex_baseline(previous, ())
    assert not baseline.pending_wires
    assert not plan_codex_grants(baseline, EMPTY, (Observation('w', A),)).grants


def test_baseline_rejects_invalid_observations_and_history():
    with pytest.raises(ValueError):
        establish_codex_baseline(history(False), (Observation('', A),))
    with pytest.raises(ValueError):
        establish_codex_baseline(GrantHistory(False, EMPTY, EMPTY, frozenset({'w'})), ())


def test_incomplete_history_cannot_reserve_pending():
    result = plan_codex_grants(history(False), EMPTY, (Observation('w', None, True),))
    assert not result.history.pending_wires
