"""Shared owner-scoped matrix planning for HTTP and scheduled probes."""
import copy
from collections.abc import Mapping
from typing import Any
from .repository import ModelRoutingRepository, OwnerBoundary
from .domain import ModelRoutingError
from .dispatch_adapter import decode_credential_secret

def provider_bundle(document: Mapping[str, Any], provider_id: str) -> dict[str, Any] | None:
    provider = next(
        (row for row in document["providers"] if row["provider_id"] == provider_id),
        None,
    )
    if provider is None:
        return None
    accesses = [
        row for row in document["provider_accesses"]
        if row["provider_id"] == provider_id
    ]
    access_ids = {row["provider_access_id"] for row in accesses}
    connections = [
        row for row in document["connections"]
        if row["provider_access_id"] in access_ids
    ]
    connection_ids = {row["connection_id"] for row in connections}
    credentials = [
        row for row in document["credentials"]
        if row["provider_access_id"] in access_ids
    ]
    offerings = [
        row for row in document["offerings"]
        if row["provider_access_id"] in access_ids
    ]
    offering_ids = {row["offering_id"] for row in offerings}
    deployments = [
        row for row in document["deployments"]
        if row["offering_id"] in offering_ids
        and row["connection_id"] in connection_ids
    ]
    return {
        "provider": copy.deepcopy(provider),
        "provider_access": copy.deepcopy(accesses[0]) if accesses else None,
        "connections": copy.deepcopy(connections),
        "credentials": copy.deepcopy(credentials),
        "offerings": copy.deepcopy(offerings),
        "deployments": copy.deepcopy(deployments),
    }


def matrix_probe_plan(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve one ProviderAccess bundle into engine probe inputs.

    Returns ``{provider, models, api_keys, oauth, adapter, extra_headers,
    disabled_cells}`` — ``disabled_cells`` lists ``"<root>::<wire id>"``
    keys for deployments whose enabled flag is off (they ARE probed, for
    discovery, but their verdicts must never drive disable recommendations):
    ``api_keys[i]`` is the resolved plaintext key for column i (never leaves
    the process; ``public_probe_snapshot`` strips the task's private fields).
    A pure subscription access (every credential decodes to an oauth name)
    switches the task to the engine's oauth mode, which resolves the live
    token per cell instead of probing the stored sentinel.
    """
    connections = [
        row for row in bundle["connections"]
        if row.get("enabled", True) and str(row.get("base_url") or "").strip()
    ]
    if not connections:
        raise ModelRoutingError(
            "provider access has no enabled connection with a base_url",
            kind="provider_probe_no_connection",
        )
    primary = connections[0]
    credentials = list(bundle["credentials"])
    if not credentials:
        raise ModelRoutingError(
            "provider access has no credentials to probe",
            kind="provider_probe_no_credentials",
        )
    api_keys: list[str] = []
    oauth_names: list[str] = []
    for credential in credentials:
        reference = str(credential.get("secret_reference") or "")
        plaintext = ""
        if reference:
            try:
                plaintext = repository.resolve_secret(boundary, reference)
            except ModelRoutingError:
                plaintext = ""
        api_key, oauth, _headers = decode_credential_secret(
            plaintext, kind=str(credential.get("kind") or "api_key"))
        api_keys.append(api_key)
        oauth_names.append(oauth)
    oauth_mode = (
        bool(credentials)
        and all(name and name == oauth_names[0] for name in oauth_names)
    )
    enabled_by_offering: dict[str, list[str]] = {}
    disabled_by_offering: dict[str, list[str]] = {}
    for deployment in bundle["deployments"]:
        wire_id = str(deployment.get("wire_model_id") or "").strip()
        if not wire_id:
            continue
        # Disabled deployments (a freshly added request name awaiting its
        # first probe) stay in the plan: the matrix probes the full
        # (credential × id) grid regardless of routing state — that probe is
        # the only way such an id can earn its enable. Consumers also get
        # ``disabled_cells`` so a failing disabled id can never generate a
        # disable recommendation against the offering's grant.
        bucket = (disabled_by_offering if deployment.get("enabled", True) is False
                  else enabled_by_offering)
        rows = bucket.setdefault(deployment["offering_id"], [])
        if wire_id not in rows:
            rows.append(wire_id)
    models: list[dict[str, Any]] = []
    disabled_cells: list[str] = []
    for offering in bundle["offerings"]:
        if offering.get("identity_state") != "confirmed" or not offering.get("model"):
            continue
        root_id = str(offering["model"].get("model_id") or "")
        entry: dict[str, Any] = {
            "model_id": root_id,
            "aliases": [],
            "capabilities": list(offering.get("capabilities") or []),
        }
        enabled_wire_ids = enabled_by_offering.get(offering["offering_id"], [])
        disabled_wire_ids = disabled_by_offering.get(offering["offering_id"], [])
        combined = enabled_wire_ids + [
            wire_id for wire_id in disabled_wire_ids if wire_id not in enabled_wire_ids]
        if combined:
            entry["request_ids"] = combined
        models.append(entry)
        disabled_cells.extend(
            "%s::%s" % (root_id, wire_id) for wire_id in disabled_wire_ids)
    if not models:
        raise ModelRoutingError(
            "provider access has no confirmed offerings to probe",
            kind="provider_probe_no_offerings",
        )
    # "<root model id>::<wire id>" keys of deployments whose enabled flag is
    # off; the probe engine marks those cells deployment_enabled=False.
    return {
        "disabled_cells": disabled_cells,
        "provider": {
            "id": str(bundle["provider"]["provider_id"]),
            "base_url": str(primary.get("base_url") or ""),
            "protocol": str(primary.get("protocol") or "openai") or "openai",
            "faces": {},
        },
        "models": models,
        "api_keys": (["oauth-managed"] * len(credentials)) if oauth_mode else api_keys,
        "oauth": oauth_names[0] if oauth_mode else "",
        "adapter": dict(primary.get("adapter") or {}),
        "extra_headers": dict(primary.get("extra_headers") or {}),
    }
