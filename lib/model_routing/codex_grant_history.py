"""Pure planning for Codex grant history; not a persistence authority.

The caller must scope a history to one owner, ProviderAccess and credential and
commit the resulting history and grants in the same aggregate CAS.
"""
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ModelIdentity:
    creator_id: str
    model_id: str


@dataclass(frozen=True)
class Observation:
    wire_id: str
    # None means identity is pending or ambiguous.
    model: ModelIdentity | None
    # Only the caller can distinguish an unbound wire from ambiguous evidence.
    reservable_pending: bool = False


@dataclass(frozen=True)
class GrantHistory:
    # True only with an authorized baseline or provably complete history;
    # ordinary observation of a potentially pruned catalog is insufficient.
    complete: bool
    wires: frozenset[str]
    models: frozenset[ModelIdentity]
    pending_wires: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GrantPlan:
    history: GrantHistory
    grants: frozenset[ModelIdentity]


def establish_codex_baseline(history: GrantHistory,
                             observations: tuple[Observation, ...]) -> GrantHistory:
    """Record an owner-authorized baseline without granting any models.

    The caller must obtain explicit owner consent and an authoritative current
    catalog for the same account and credential, then commit under owner CAS.
    This does not reconstruct historical consent. Previous observations remain
    seen; pending reservations are cancelled rather than revived.
    """
    plan_codex_grants(history, frozenset(), ())  # Validate prior reservations.
    observed = plan_codex_grants(
        GrantHistory(False, history.wires, history.models), frozenset(), observations)
    return GrantHistory(True, observed.history.wires, observed.history.models)


def plan_codex_grants(history: GrantHistory, grants: frozenset[ModelIdentity],
                      observations: tuple[Observation, ...]) -> GrantPlan:
    """Preserve grants and propose grants only for provably novel identities.

    A previously seen wire *or* model prevents automatic reauthorization. A
    pending wire is remembered; only an explicitly reservable novel pending
    wire may receive a grant after unique identity binding.
    Multiple identities for one wire, or multiple wires for one identity, are
    ambiguous for this pass. All observations are remembered even when denied.
    Credential enablement, connection grants and wire exclusions are untouched.
    """
    if type(history.complete) is not bool:
        raise ValueError('history completeness must be an explicit boolean')
    if not history.pending_wires <= history.wires or (history.pending_wires and not history.complete):
        raise ValueError('invalid pending reservations')
    wires: dict[str, set[ModelIdentity | None]] = {}
    reservable: dict[str, bool] = {}
    models: dict[ModelIdentity, set[str]] = {}
    for observation in observations:
        if not isinstance(observation.wire_id, str) or not observation.wire_id:
            raise ValueError('wire identity must be nonempty')
        model = observation.model
        if model is not None:
            if (not isinstance(model, ModelIdentity) or
                    not isinstance(model.creator_id, str) or not model.creator_id or
                    not isinstance(model.model_id, str) or not model.model_id):
                raise ValueError('model identity must be complete')
            models.setdefault(model, set()).add(observation.wire_id)
        wires.setdefault(observation.wire_id, set()).add(model)
        reservable[observation.wire_id] = (reservable.get(observation.wire_id, True)
                                            and observation.reservable_pending)
    result = set(grants)
    pending = set(history.pending_wires)
    for wire, identities in wires.items():
        if not history.complete:
            continue
        if len(identities) != 1:
            pending.discard(wire)
            continue
        model = next(iter(identities))
        if model is None:
            if not reservable[wire]:
                pending.discard(wire)
            elif wire not in history.wires:
                pending.add(wire)
            continue
        eligible = wire not in history.wires or wire in pending
        pending.discard(wire)
        if eligible and model not in history.models and len(models[model]) == 1:
            result.add(model)
    return GrantPlan(
        GrantHistory(history.complete, history.wires | frozenset(wires),
                     history.models | frozenset(models), frozenset(pending)),
        frozenset(result),
    )
