"""Request-local route eligibility, credential grants and stable ranking.

Entry points: RouteCandidateCompiler and standalone compile helpers. Selection
indexes are validated once per compiler; primary, deployment-witness and fallback
queries share them. Health is explicit; repository and secret IO stay outside.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import heapq
import time
from typing import Any

from .domain import ModelRef, ModelRoutingError, NativeModelSelection
from .health import RouteHealthRegistry
from .selection_resolution import _indexes, _resolve_model_ref


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    required_capabilities: frozenset[str] = frozenset({"text"})
    required_context: int = 1
    required_protocols: frozenset[str] = frozenset()
    max_input_price: float | None = None
    max_output_price: float | None = None
    price_currency: str = "USD"
    cache_affinity_connection_id: str = ""


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    model: dict[str, Any] | None
    provider: dict[str, Any]
    provider_access: dict[str, Any]
    offering: dict[str, Any]
    deployment: dict[str, Any]
    connection: dict[str, Any]
    credential: dict[str, Any]
    score: tuple[Any, ...]
    selection_reasons: tuple[str, ...] = ()
    codex_account_fingerprints: tuple[str, ...] = ()
    subscription_account_id: str = ""

    @property
    def dispatch_capabilities(self) -> set[str]:
        capabilities = set(self.offering["capabilities"])
        if self.codex_account_fingerprints:
            # Legacy personal media callers can pick configured slots based on
            # capability alone. Scoped bearer authorization lives in chat only.
            from .operations import OPENAI_OPERATION_PROTOCOLS, protocols_for_capability
            capabilities = {cap for cap in capabilities
                            if protocols_for_capability(cap) != OPENAI_OPERATION_PROTOCOLS}
        return capabilities

    @property
    def provider_id(self) -> str:
        return str(self.provider["provider_id"])


def _account_quota_rank(
    snapshot: Mapping[str, Any] | None, now: float,
) -> tuple[bool, int]:
    """One account's quota projection as (exhausted, rank).

    Only windows that have not rolled and snapshots fresh enough to describe
    the current window count; the provider's own 429 remains the authoritative
    exhaustion signal, so a stale projection never hard-excludes an account.
    """
    if not isinstance(snapshot, Mapping):
        return False, 0
    age = now - float(snapshot.get('captured_at') or 0)
    worst = 0.0
    for name in ('primary', 'secondary'):
        window = snapshot.get(name)
        if not isinstance(window, Mapping):
            continue
        resets_at = window.get('resets_at')
        if resets_at is not None and float(resets_at) <= now:
            continue
        minutes = window.get('window_minutes')
        if minutes and age > float(minutes) * 60:
            continue
        if not minutes and age > 6 * 3600:
            continue
        worst = max(worst, float(window.get('used_percent') or 0.0))
    if worst >= 100.0:
        return True, 2
    if worst >= 90.0:
        return False, 1
    return False, 0


def _eligible_accounts(
    idx: Mapping[str, Any], account_fingerprints: tuple[str, ...],
) -> list[tuple[str, bool]]:
    """Inventory accounts authorized for this wire, default first, stable order.

    Secret-free: reads only the normalized account rows (identity, default
    flag, grant expiry), never token material. An account whose sign-in has
    expired cannot serve, so it is not a rotation candidate.
    """
    if not account_fingerprints:
        return [("", False)]
    now = time.time()
    rows = [
        row for row in idx["document"].get("subscription_accounts", ())
        if row.get("provider") == "codex"
        and row.get("subject_fingerprint") in account_fingerprints
        and float(row.get("expires_at") or 0.0) > now
    ]
    rows.sort(key=lambda row: (0 if row.get("is_default") else 1, row["account_id"]))
    return [(row["account_id"], bool(row.get("is_default"))) for row in rows]


def _pricing_allowed(offering: Mapping[str, Any], policy: RoutePolicy) -> bool:
    if policy.max_input_price is None and policy.max_output_price is None:
        return True
    pricing = offering.get("actual_pricing")
    if not isinstance(pricing, Mapping):
        return False
    if str(pricing.get("currency") or "USD").upper() != policy.price_currency.upper():
        return False
    if (
        policy.max_input_price is not None
        and float(pricing.get("input") or 0.0) > policy.max_input_price
    ):
        return False
    if (
        policy.max_output_price is not None
        and float(pricing.get("output") or 0.0) > policy.max_output_price
    ):
        return False
    return True


def _credential_authorization(
    credential: Mapping[str, Any],
    *,
    connection_id: str,
    model_ref: ModelRef | None,
    wire_model_id: str,
    revoked_grants: Mapping,
) -> tuple[str | None, tuple[str, ...]]:
    authorization = credential["authorization"]
    if connection_id not in authorization["connection_ids"]:
        return "credential_connection_denied", ()
    account_grants = authorization.get("codex_account_grants") or {}
    if model_ref is None:
        # Legacy pending selections have no scoped identity to prove. A
        # credential with automatic grants cannot use that path to bypass one.
        return ("credential_model_denied" if account_grants else None), ()

    def matches(grant):
        return (grant["creator_id"] == model_ref.creator_id
                and grant["model_id"] == model_ref.model_id)

    for grant in authorization["models"]:
        if matches(grant):
            if wire_model_id in grant.get("excluded_wire_model_ids", ()):
                return "credential_wire_denied", ()
            return None, ()
    if not account_grants or credential["kind"] != "oauth":
        return "credential_model_denied", ()
    revoked = revoked_grants.get((credential["credential_id"], model_ref.creator_id, model_ref.model_id))
    if revoked:
        if revoked["model_revoked"]:
            return "credential_model_denied", ()
        if wire_model_id in revoked["excluded_wire_model_ids"]:
            return "credential_wire_denied", ()
    accounts = tuple(sorted(account for account, grants in account_grants.items()
        if any(matches(grant) and wire_model_id == grant["wire_model_id"]
               for grant in grants)))
    if accounts:
        return None, accounts
    return "credential_model_denied", ()


class RouteCandidateCompiler:
    """Request-scoped compiler that validates/indexes one authority once.

    Capability catalogs and fallback scans evaluate multiple selections from
    the same revision. Re-normalizing and deep-copying the complete aggregate
    for every Offering made that bounded query quadratic in aggregate size.
    This owner retains only one request-local normalized document and indexes;
    it is never a cross-owner or cross-revision cache.
    """

    def __init__(self, document: Mapping[str, Any], *, normalized: bool = False) -> None:
        # ``normalized`` admits normalize_document's own output unchanged
        # (StoredAuthority.normalized); any other document is validated here.
        self._indexes = _indexes(document, normalized=normalized)

    def resolve_model_id(self, model_id: str) -> ModelRef:
        """Resolve one exact logical ID using this request's validated authority.

        A string selector cannot choose between creators. Keep that ambiguity
        explicit rather than interpreting display names or provider wire IDs.
        """
        resolved=None
        for reference in self._indexes['models']:
            if reference.model_id!=model_id:
                continue
            if resolved is not None:
                raise ModelRoutingError('logical model ID has multiple creator identities',
                    kind='model_identity_ambiguous')
            resolved=reference
        if resolved is None:
            raise ModelRoutingError('logical model ID is not registered',kind='model_not_found')
        return resolved

    def compile_deployments(
        self,
        selection: NativeModelSelection,
        *,
        policy: RoutePolicy | None = None,
    ) -> list[RouteCandidate]:
        """Secret-free eligibility projection, one authorization witness per deployment.

        Controls need to know whether a deployment is accessible, not rank all
        of its credential alternatives. Stop after the first authorized grant
        to avoid materializing a deployment × credential product. Execution
        continues to use ``compile`` with full health and credential scoring.
        """
        return _compile_candidates(
            self._indexes, selection, policy=policy,
            one_authorization_witness=True,
        )

    def compile(
        self,
        selection: NativeModelSelection,
        *,
        policy: RoutePolicy | None = None,
        health: RouteHealthRegistry | None = None,
        excluded_deployment_ids: Sequence[str] = (),
        excluded_connection_ids: Sequence[str] = (),
        excluded_credential_ids: Sequence[str] = (),
        rejections: dict[str, int] | None = None,
        max_candidates: int | None = None,
        account_quota: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[RouteCandidate]:
        return _compile_candidates(
            self._indexes,
            selection,
            policy=policy,
            health=health,
            excluded_deployment_ids=excluded_deployment_ids,
            excluded_connection_ids=excluded_connection_ids,
            excluded_credential_ids=excluded_credential_ids,
            rejections=rejections,
            max_candidates=max_candidates,
            account_quota=account_quota,
        )


    def compile_fallbacks(
        self,
        failed_selection: NativeModelSelection,
        *,
        policy: RoutePolicy | None = None,
        health: RouteHealthRegistry | None = None,
        original_provider_id: str = "",
        excluded_models: Sequence[ModelRef] = (),
        max_candidates: int | None = None,
        account_quota: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[RouteCandidate]:
        """Choose quality-ranked model degradation after one model fully fails."""
        policy = policy or RoutePolicy()
        idx = self._indexes
        excluded = set(excluded_models)
        if failed_selection.model is not None:
            excluded.add(_resolve_model_ref(idx, failed_selection.model) or failed_selection.model)
        def eligible_candidates():
            for ref, model in idx["models"].items():
                if ref in excluded:
                    continue
                if not policy.required_capabilities.issubset(set(model["capabilities"])):
                    continue
                if policy.required_context > model["context_window"]:
                    continue
                selection = NativeModelSelection(
                    ref,
                    None,
                    original_provider_id or failed_selection.preferred_provider_id,
                )
                yield from _iter_candidates(idx, selection, policy=policy, health=health,
                                            account_quota=account_quota)
        return _rank_candidates(eligible_candidates(), max_candidates=max_candidates, key=lambda candidate: (
            0 if original_provider_id and candidate.provider_id == original_provider_id else 1,
            -float((candidate.model or {}).get("quality_rank") or 0.0),
            candidate.score,
        ))


def _compile_candidates(
    idx: Mapping[str, Any],
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    excluded_deployment_ids: Sequence[str] = (),
    excluded_connection_ids: Sequence[str] = (),
    excluded_credential_ids: Sequence[str] = (),
    rejections: dict[str, int] | None = None,
    one_authorization_witness: bool = False,
    max_candidates: int | None = None,
    account_quota: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[RouteCandidate]:
    return _rank_candidates(_iter_candidates(
        idx, selection, policy=policy, health=health,
        excluded_deployment_ids=excluded_deployment_ids,
        excluded_connection_ids=excluded_connection_ids,
        excluded_credential_ids=excluded_credential_ids,
        rejections=rejections,
        one_authorization_witness=one_authorization_witness,
        account_quota=account_quota,
    ), max_candidates=max_candidates, key=lambda candidate: candidate.score)


def _rank_candidates(candidates: Iterable[RouteCandidate], *, max_candidates, key):
    if max_candidates is None:
        return sorted(candidates, key=key)
    if (isinstance(max_candidates, bool) or not isinstance(max_candidates, int)
            or max_candidates < 1):
        raise ModelRoutingError('candidate limit must be a positive integer',
                                kind='model_route_resource_budget_invalid',
                                field='max_candidates')
    # nsmallest is stable on equal keys, matching sorted(... )[:limit], while
    # retaining only the ranked prefix during the deployment × credential scan.
    return heapq.nsmallest(max_candidates, candidates, key=key)


def _iter_candidates(
    idx: Mapping[str, Any],
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    excluded_deployment_ids: Sequence[str] = (),
    excluded_connection_ids: Sequence[str] = (),
    excluded_credential_ids: Sequence[str] = (),
    rejections: dict[str, int] | None = None,
    one_authorization_witness: bool = False,
    account_quota: Mapping[str, Mapping[str, Any]] | None = None,
) -> Iterator[RouteCandidate]:
    policy = policy or RoutePolicy()
    excluded_deployments = set(excluded_deployment_ids)
    excluded_connections = set(excluded_connection_ids)
    excluded_credentials = set(excluded_credential_ids)
    preferred_provider_id = selection.preferred_provider_id

    target_offerings: list[dict[str, Any]] = []
    target_model: ModelRef | None = selection.model
    if selection.provider_offering is not None:
        scoped = selection.provider_offering
        offering = idx["offerings"].get(scoped.offering_id)
        if offering is None:
            raise ModelRoutingError(
                "provider-scoped offering does not exist", kind="offering_not_found")
        access = idx["accesses"][offering["provider_access_id"]]
        if access["provider_id"] != scoped.provider_id:
            raise ModelRoutingError(
                "offering does not belong to the selected provider",
                kind="provider_scope_violation",
            )
        target_offerings = [offering]
        if offering["identity_state"] == "confirmed":
            target_model = ModelRef.from_value(offering["model"])
    elif selection.model is not None:
        resolved = _resolve_model_ref(idx, selection.model)
        if resolved is None:
            raise ModelRoutingError(
                "official model is not registered", kind="model_not_found")
        target_model = resolved
        target_offerings = idx["offerings_by_model"].get(resolved, [])
    else:
        raise ModelRoutingError("model selection is empty")

    def reject(reason: str) -> None:
        # Fixed reason codes only: no credential, endpoint or caller content.
        if rejections is not None:
            rejections[reason] = rejections.get(reason, 0) + 1

    if not target_offerings:
        reject("offering_missing")
    for offering in target_offerings:
        from .registration import model_registration_ready
        bound_model = idx['models'].get(ModelRef.from_value(offering['model'])) if offering.get('model') else None
        if bound_model is None:
            reject('model_identity_pending')
            continue
        if bound_model is not None and not model_registration_ready(bound_model):
            reject('model_registration_incomplete')
            continue
        if not offering["enabled"]:
            reject("offering_disabled")
            continue
        if offering.get("stale"):
            reject("offering_stale")
            continue
        if not policy.required_capabilities.issubset(set(offering["capabilities"])):
            reject("capabilities_missing")
            continue
        if policy.required_context > offering["context_window"]:
            reject("context_insufficient")
            continue
        if not _pricing_allowed(offering, policy):
            reject("pricing_policy_denied")
            continue
        access = idx["accesses"][offering["provider_access_id"]]
        provider = idx["providers"][access["provider_id"]]
        # Discovery/probes may retain unresolved Codex rows for inspection,
        # but ordinary inference requires an admitted model and model grant.
        if provider["provider_id"] == "oauth_codex" and offering["identity_state"] != "confirmed":
            reject("model_identity_pending")
            continue
        if not access["enabled"]:
            reject("provider_access_disabled")
            continue
        if selection.provider_offering is not None and provider["provider_id"] != selection.provider_offering.provider_id:
            continue
        if not idx["credentials_by_access"][access["provider_access_id"]]:
            reject("credential_missing")
        for deployment in idx["deployments_by_offering"][offering["offering_id"]]:
            if deployment["deployment_id"] in excluded_deployments:
                reject("deployment_excluded")
                continue
            # Pending identities are reachable only through an explicit
            # provider+offering selection, never automatic official routing.
            if (
                offering["identity_state"] == "pending_identity"
                and selection.provider_offering is None
            ):
                continue
            connection = idx["connections"][deployment["connection_id"]]
            if deployment.get('registration_issues'):
                reject('model_registration_incomplete')
                continue
            if not connection["enabled"]:
                reject("connection_disabled")
                continue
            if connection["connection_id"] in excluded_connections:
                reject("connection_excluded")
                continue
            if policy.required_protocols and connection["protocol"] not in policy.required_protocols:
                reject("protocol_mismatch")
                continue
            for credential in idx["credentials_by_access"][access["provider_access_id"]]:
                if not credential["enabled"]:
                    reject("credential_disabled")
                    continue
                if credential["credential_id"] in excluded_credentials:
                    reject("credential_excluded")
                    continue
                credential_rejection, account_fingerprints = _credential_authorization(
                    credential,
                    connection_id=connection["connection_id"],
                    model_ref=target_model if offering["identity_state"] == "confirmed" else None,
                    wire_model_id=deployment["wire_model_id"],
                    revoked_grants=idx["revoked_grants"],
                )
                if credential_rejection:
                    reject(credential_rejection)
                    continue
                if account_fingerprints and (
                        connection["protocol"] != "openai_responses" or connection.get("adapter")):
                    reject("protocol_mismatch")
                    continue
                partial = RouteCandidate(
                    model=(idx["models"].get(target_model) if target_model is not None else None),
                    provider=provider,
                    provider_access=access,
                    offering=offering,
                    deployment=deployment,
                    connection=connection,
                    credential=credential,
                    score=(),
                    codex_account_fingerprints=account_fingerprints,
                )
                if account_fingerprints and not policy.required_capabilities.issubset(partial.dispatch_capabilities):
                    reject("capabilities_missing")
                    continue
                # A scoped grant names every account authorized for this wire;
                # each eligible account becomes its own candidate so dispatch
                # rotation reaches a sibling account, not the same bearer.
                accounts = _eligible_accounts(idx, account_fingerprints)
                if not accounts:
                    # No inventory row proves the grant: retain the legacy
                    # candidate so mint surfaces the typed account error.
                    accounts = [("", False)]
                witness_yielded = False
                for account_id, account_default in accounts:
                    scoped = (
                        RouteCandidate(
                            model=partial.model,
                            provider=provider,
                            provider_access=access,
                            offering=offering,
                            deployment=deployment,
                            connection=connection,
                            credential=credential,
                            score=(),
                            codex_account_fingerprints=account_fingerprints,
                            subscription_account_id=account_id,
                        ) if account_id else partial
                    )
                    unavailable, health_penalty, health_reasons = (
                        health.candidate_state(scoped) if health is not None
                        else (False, 0.0, [])
                    )
                    if unavailable:
                        reject("health_unavailable")
                        continue
                    quota_exhausted, quota_rank = _account_quota_rank(
                        (account_quota or {}).get(account_id), time.time())
                    if quota_exhausted:
                        reject("account_quota_exhausted")
                        continue
                    pricing = offering.get("actual_pricing") or {}
                    price_score = float(pricing.get("input") or 0.0) + float(pricing.get("output") or 0.0)
                    preferred_rank = (
                        0 if preferred_provider_id and provider["provider_id"] == preferred_provider_id
                        else 1 if preferred_provider_id else 0
                    )
                    cache_rank = (
                        0 if policy.cache_affinity_connection_id == connection["connection_id"]
                        else 1
                    )
                    score = (
                        preferred_rank,
                        int(offering.get("priority", 100)),
                        health_penalty,
                        cache_rank,
                        int(connection.get("priority", 100)),
                        int(deployment.get("priority", 100)),
                        float(connection.get("latency_ms") or offering.get("latency_ms") or 0.0),
                        price_score,
                        provider["provider_id"],
                        deployment["deployment_id"],
                        credential["credential_id"],
                        quota_rank,
                        0 if account_default else 1,
                        account_id,
                    )
                    reasons = [
                        "preferred_provider" if preferred_rank == 0 and preferred_provider_id else "eligible_provider",
                        "cache_affinity" if cache_rank == 0 and policy.cache_affinity_connection_id else "score_ranked",
                        *health_reasons,
                    ]
                    if account_id:
                        reasons.append("default_account" if account_default else "eligible_account")
                        if quota_rank:
                            reasons.append("quota_nearly_spent")
                    yield RouteCandidate(
                        model=partial.model,
                        provider=provider,
                        provider_access=access,
                        offering=offering,
                        deployment=deployment,
                        connection=connection,
                        credential=credential,
                        score=score,
                        selection_reasons=tuple(reasons),
                        codex_account_fingerprints=account_fingerprints,
                        subscription_account_id=account_id,
                    )
                    if one_authorization_witness:
                        witness_yielded = True
                        break
                if witness_yielded:
                    break


def compile_candidates(
    document: Mapping[str, Any],
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    excluded_deployment_ids: Sequence[str] = (),
    excluded_connection_ids: Sequence[str] = (),
    excluded_credential_ids: Sequence[str] = (),
    rejections: dict[str, int] | None = None,
    max_candidates: int | None = None,
    account_quota: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[RouteCandidate]:
    """Compile bounded runtime candidates; no configured Route entity exists."""
    return RouteCandidateCompiler(document).compile(
        selection,
        policy=policy,
        health=health,
        excluded_deployment_ids=excluded_deployment_ids,
        excluded_connection_ids=excluded_connection_ids,
        excluded_credential_ids=excluded_credential_ids,
        rejections=rejections,
        max_candidates=max_candidates,
        account_quota=account_quota,
    )


def compile_model_fallback_candidates(
    document: Mapping[str, Any],
    failed_selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    original_provider_id: str = "",
    excluded_models: Sequence[ModelRef] = (),
    max_candidates: int | None = None,
    account_quota: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[RouteCandidate]:
    """Choose quality-ranked model degradation after one model fully fails."""
    return RouteCandidateCompiler(document).compile_fallbacks(
        failed_selection, policy=policy, health=health,
        original_provider_id=original_provider_id, excluded_models=excluded_models,
        max_candidates=max_candidates, account_quota=account_quota,
    )
