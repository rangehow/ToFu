"""Owner-scoped read of an owner's model-routing credential secrets.

Responsibility: the model-routing owner's one decrypting read for consumers
that must recognise an owner's keys without dispatching with them (known-
credential redaction, ``lib/tool_results/credential_redaction.py``). It yields
``(credential_id, usable key)`` for every credential of one explicit owner,
enabled or not, and leaves retention to the caller, which must keep only
non-reversible fingerprints (this package never adds a plaintext cache).

Entry point: :func:`owner_credential_keys`. Change signal for callers that
derive state from it: ``change_notifications.model_routing_secrets_stamp_path``.
"""

from __future__ import annotations

from collections.abc import Iterator

from .credential_envelope import decode_credential_envelope
from .domain import ModelRoutingError
from .repository import ModelRoutingRepository, OwnerBoundary, RepositoryPort


def owner_credential_keys(
    boundary: OwnerBoundary, *, repository: RepositoryPort | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield ``(credential_id, key)`` for the owner's stored credentials.

    Envelope secrets yield their ``api_key``; raw secrets of ``api_key``
    credentials yield themselves. OAuth/subscription/local identities are
    account references, not keys a command could print, and are skipped.
    A credential whose secret is missing is skipped; other failures raise.
    An explicit repository borrows the admitted request authority; otherwise
    the durable owner repository remains authoritative.
    """
    if repository is None:
        repository = ModelRoutingRepository()
    authority = repository.get(boundary)
    for credential in authority.document.get("credentials") or []:
        reference = str(credential.get("secret_reference") or "")
        if not reference:
            continue
        try:
            plaintext = repository.resolve_secret(boundary, reference)
        except ModelRoutingError as exc:
            if exc.kind == "credential_secret_missing":
                continue
            raise
        envelope = decode_credential_envelope(plaintext)
        if envelope is not None:
            key = str(envelope.get("api_key") or "")
        elif credential.get("kind") == "api_key":
            key = plaintext
        else:
            continue
        if key:
            yield str(credential.get("credential_id") or "?"), key


__all__ = ["owner_credential_keys"]
