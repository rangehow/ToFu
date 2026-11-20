"""Write-time redaction of known credential values.

Responsibility: replace every *known* credential value with a stable marker
before Tofu persists or re-shows text. The known set is exact, never guessed:

* every credential-vault entry (``lib/credentials_vault.py``),
* every provider key in ``server_config.json`` (``providers[].api_key``,
  ``providers[].api_keys[]``, ``mt_provider.api_key``), and
* for an explicit owner, every model-routing v2 credential key of that owner,
  read through the model-routing owner's own decrypting read
  (``lib/model_routing/known_secrets.owner_credential_keys``).

Each value is matched raw (which also covers ``Bearer <value>`` and any other
prefix) and in its URL-encoded forms. The redactor keeps no plaintext: the
model-routing owner forbids a plaintext cache, and the vault's values deserve
no weaker treatment. It holds, per variant, its length, a keyed BLAKE2b digest
and its marker, plus the built-in (SipHash) hash of every ``b``-character
substring of every variant. Every variant is at least ``2b - 1`` characters
long, so any occurrence fully contains one of the text's aligned ``b``-blocks.
One pass hashes those blocks and intersects them with the substring index; a
hit names the candidate start, which is verified by digest. Accepted matches
are replaced longest first. The scan costs ``len(text) / b`` hash operations,
independent of how many values are known; ``b`` follows from the shortest
known value.

The set is rebuilt only when a change signal moves: the vault store or
``server_config.json`` (inode, size, mtime), and for an owner that owner's
model-routing stamp, which the durable model-routing repository replaces after
every commit or secret write in whichever server process made it. A failed
model-routing read keeps the previous set and is retried on the next call;
the other sources keep their previous set until their file changes again.

Invocations enter :func:`credential_redaction_scope` with the admitted task.
Durable reads and cached fingerprints use its full owner/tenant boundary;
the numeric owner's stamp invalidates every cached boundary for that owner.
Storage-free invocations borrow the task's request-routing repository, never
the durable repository or its change stamp. Nested scopes of the same authority
share fingerprints until the invocation exits; independent runs never share
their owner credential sets. Vault and server-config change signals still apply.

Entry points: :func:`known_credential_redactor`, :func:`redact_value`,
:func:`credential_redaction_scope`.
Values are never logged; failures log only the exception type. Contract:
``docs/contracts/TOOL_EXECUTION_POLICY.md``, "Known credentials are redacted
at write time".
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

from lib.log import get_logger

if TYPE_CHECKING:
    from lib.model_routing.repository import OwnerBoundary, RepositoryPort

logger = get_logger(__name__)

__all__ = [
    'KnownCredentialRedactor',
    'known_credential_redactor',
    'credential_redaction_scope',
    'redact_value',
]

# A correctness floor, not a tuning knob: shorter strings are placeholders
# ("EMPTY", "none", "sk-xxx") rather than credentials, and replacing them
# would rewrite ordinary words throughout every transcript.
_MIN_CREDENTIAL_CHARS = 8


def _marker(label: str) -> str:
    return f'[redacted credential: {label}]'


def credential_variants(value: str) -> tuple[str, ...]:
    """The matched forms of one value: raw, URL-encoded, form-encoded."""
    return tuple(dict.fromkeys((value, quote(value, safe=''), quote_plus(value, safe=''))))


@dataclass(frozen=True)
class KnownCredentialRedactor:
    block: int        # aligned block length b; every variant has >= 2b - 1 chars
    longest: int      # longest variant length (streaming writers carry longest - 1)
    variants: tuple   # (length, keyed digest, marker) per variant
    grams: dict       # hash(b-substring) -> ((variant index, offset), ...)
    key: bytes        # per-build BLAKE2b key

    @classmethod
    def build(cls, labelled_values) -> 'KnownCredentialRedactor':
        key = os.urandom(32)
        markers: dict[bytes, tuple[int, str]] = {}
        grams: dict[int, list] = {}
        pending = []
        for label, value in labelled_values:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if len(value) < _MIN_CREDENTIAL_CHARS:
                continue
            pending.extend((variant, _marker(label)) for variant in credential_variants(value))
        if not pending:
            return cls(0, 0, (), {}, key)
        block = (min(len(variant) for variant, _ in pending) + 1) // 2
        variants = []
        for variant, marker in pending:
            digest = _digest(key, variant)
            if digest in markers:
                continue
            index = len(variants)
            markers[digest] = (index, marker)
            variants.append((len(variant), digest, marker))
            for offset in range(len(variant) - block + 1):
                grams.setdefault(hash(variant[offset:offset + block]), []).append((index, offset))
        return cls(block, max(length for length, _, _ in variants), tuple(variants),
                   {gram: tuple(found) for gram, found in grams.items()}, key)

    def __bool__(self) -> bool:
        return bool(self.variants)

    def _matches(self, text: str) -> list[tuple[int, int, str]]:
        """Non-overlapping ``(start, end, marker)`` of known variants, in text order."""
        block = self.block
        if not block or len(text) < 2 * block - 1:
            return []
        hashes = [hash(text[start:start + block])
                  for start in range(0, len(text) - block + 1, block)]
        grams = self.grams
        if grams.keys().isdisjoint(hashes):
            return []
        found = set()
        for position, gram in enumerate(hashes):
            entries = grams.get(gram)
            if entries is None:
                continue
            for index, offset in entries:
                length, digest, _ = self.variants[index]
                start = position * block - offset
                if start < 0 or start + length > len(text):
                    continue
                if _digest(self.key, text[start:start + length]) == digest:
                    found.add((start, length, index))
        accepted: list[tuple[int, int, str]] = []
        for start, length, index in sorted(found, key=lambda hit: (-hit[1], hit[0])):
            end = start + length
            if all(end <= taken_start or start >= taken_end
                   for taken_start, taken_end, _ in accepted):
                accepted.append((start, end, self.variants[index][2]))
        accepted.sort()
        return accepted

    def contains(self, text: str) -> bool:
        return bool(self._matches(text))

    def redact(self, text: str) -> str:
        matches = self._matches(text)
        if not matches:
            return text
        parts, cursor = [], 0
        for start, end, marker in matches:
            parts.append(text[cursor:start])
            parts.append(marker)
            cursor = end
        parts.append(text[cursor:])
        return ''.join(parts)

    def redact_stream(self, pending: str, chunk: str, *, final: bool = False) -> tuple[str, str]:
        """Redact a stream: return ``(safe output, new pending)``.

        Output never ends inside a possible occurrence: the last
        ``longest - 1`` characters, and any match crossing that cut, stay
        pending until more text or ``final`` decides them.
        """
        text = pending + chunk
        if not self.variants:
            return text, ''
        if final:
            return self.redact(text), ''
        cut = max(0, len(text) - (self.longest - 1))
        matches = self._matches(text)
        for start, end, _ in matches:
            if start < cut < end:
                cut = start
        return self.redact(text[:cut]), text[cut:]

    def redact_value(self, value: Any) -> Any:
        """Redact every string inside JSON-shaped data; unchanged data is returned as is."""
        if not self.variants:
            return value
        # Shared phase/carrier strings need one scan under this redactor snapshot.
        # Retain their identity only until this walk returns, never on the redactor.
        return self._walk(value, {})

    def _walk(self, value, strings):
        if type(value) is str:
            held = strings.get(id(value))
            if held is None:
                held = (value, self.redact(value))
                strings[id(value)] = held
            return held[1]
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            changed = None
            for key, item in value.items():
                new = self._walk(item, strings)
                if new is not item:
                    if changed is None:
                        changed = dict(value)
                    changed[key] = new
            return value if changed is None else changed
        if isinstance(value, (list, tuple)):
            items = [self._walk(item, strings) for item in value]
            if all(new is old for new, old in zip(items, value)):
                return value
            return items if isinstance(value, list) else tuple(items)
        return value


def _digest(key: bytes, text: str) -> bytes:
    return hashlib.blake2b(text.encode('utf-8', 'surrogatepass'), key=key, digest_size=32).digest()


def _vault_values():
    from lib import credentials_vault
    for name, value in sorted(credentials_vault.get_entries_with_prefixes(('',)).items()):
        yield credentials_vault.env_var_name(name), value


def _server_config_path() -> str:
    from lib.config_dir import config_path
    return config_path('server_config.json')


def _server_config_values(path: str):
    try:
        with open(path, encoding='utf-8') as handle:
            config = json.load(handle)
    except FileNotFoundError:
        return
    if not isinstance(config, dict):
        return
    for provider in config.get('providers') or []:
        if not isinstance(provider, dict) or provider.get('oauth'):
            # OAuth providers carry the 'oauth-managed' sentinel, not a key.
            continue
        label = f"provider {provider.get('id') or provider.get('name') or '?'}"
        keys = provider.get('api_keys')
        for value in [provider.get('api_key'), *(keys if isinstance(keys, list) else [])]:
            yield label, value
    mt_provider = config.get('mt_provider')
    if isinstance(mt_provider, dict):
        yield 'provider mt_provider', mt_provider.get('api_key')


def _model_routing_values(boundary: OwnerBoundary, *, repository=None):
    from lib.model_routing.known_secrets import owner_credential_keys
    for credential_id, key in owner_credential_keys(boundary, repository=repository):
        yield f'model-routing {credential_id}', key


def _stat_identity(path) -> tuple | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _source_identity(owner_user_id: int | None) -> tuple:
    from lib import credentials_vault
    config_path = _server_config_path()
    identity = (str(credentials_vault._STORE_PATH), _stat_identity(credentials_vault._STORE_PATH),
                config_path, _stat_identity(config_path))
    if owner_user_id is None:
        return identity
    from lib.model_routing.change_notifications import model_routing_secrets_stamp_path
    stamp = model_routing_secrets_stamp_path(user_id=owner_user_id)
    return (*identity, stamp, _stat_identity(stamp))


_EMPTY = KnownCredentialRedactor(0, 0, (), {}, b'')
_lock = threading.Lock()
_building = threading.local()
# Per OwnerBoundary (None = no owner): (source identity, redactor).
# Fingerprints only. The stamp is shared by an owner's tenant entries.
_cached: dict = {}
_failing: set = set()


@dataclass
class _ScopedCredentials:
    boundary: OwnerBoundary
    repository: RepositoryPort | None
    cached: dict = field(default_factory=dict)
    failing: set = field(default_factory=set)
    lock: Any = field(default_factory=threading.Lock)


_scope: ContextVar[_ScopedCredentials | None] = ContextVar('credential_redaction_scope', default=None)


@contextmanager
def credential_redaction_scope(*, task: dict, owner_user_id: int):
    """Carry the task's owner boundary and, when issued, request authority.

    Nested calls with the same request authority share fingerprints, not
    secrets. The caller's scope is restored even on failure; no request set
    enters the process-wide durable cache.
    """
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.request_authority import request_routing_repository
    boundary = OwnerBoundary.create(owner_user_id, task.get('_tenant_id'))
    repository = request_routing_repository(task, boundary)
    current = _scope.get()
    if (repository is not None and current is not None
            and current.repository is repository and current.boundary == boundary):
        yield
        return
    token = _scope.set(_ScopedCredentials(boundary, repository))
    try:
        if repository is not None:
            known_credential_redactor(owner_user_id=owner_user_id)
        yield
    finally:
        _scope.reset(token)


def known_credential_redactor(*, owner_user_id: int | None = None) -> KnownCredentialRedactor:
    """The redactor for the current vault, server_config and owner secrets.

    ``owner_user_id`` adds model-routing keys for the matching task boundary
    (or the default tenant outside that boundary). ``None`` covers only the
    owner-independent sources.
    """
    scope = _scope.get() if owner_user_id is not None else None
    scoped = scope if scope is not None and scope.repository is not None else None
    if scoped is not None and owner_user_id != scoped.boundary.owner_user_id:
        from lib.model_routing.domain import ModelRoutingError
        raise ModelRoutingError(
            "credential owner is outside its run's model-routing authority",
            kind='model_route_unavailable',
        )
    boundary = None
    if scope is not None and owner_user_id == scope.boundary.owner_user_id:
        boundary = scope.boundary
    elif owner_user_id is not None:
        from lib.model_routing import OwnerBoundary
        boundary = OwnerBoundary.create(owner_user_id)
    identity = _source_identity(None if scoped is not None else owner_user_id)
    cache = scoped.cached if scoped is not None else _cached
    lock = scoped.lock if scoped is not None else _lock
    cached = cache.get(boundary)
    if cached is not None and cached[0] == identity:
        return cached[1]
    if getattr(_building, 'active', False):
        # A source read that itself writes through storage must not rebuild.
        return cached[1] if cached is not None else _EMPTY
    with lock:
        cached = cache.get(boundary)
        if cached is not None and cached[0] == identity:
            return cached[1]
        _building.active = True
        try:
            return _rebuild(boundary, identity, cached, scoped=scoped)
        finally:
            _building.active = False


def _rebuild(boundary, identity, cached, *, scoped=None) -> KnownCredentialRedactor:
    cache = scoped.cached if scoped is not None else _cached
    failing = scoped.failing if scoped is not None else _failing
    previous = cached[1] if cached is not None else _EMPTY
    try:
        labelled = [*_vault_values(), *_server_config_values(_server_config_path())]
    except Exception as exc:
        if scoped is not None:
            from lib.model_routing.domain import ModelRoutingError
            raise ModelRoutingError(
                'local credential recognition is unavailable',
                kind='model_route_unavailable',
            ) from exc
        # Keep the previous set rather than drop to none; the next change
        # of either file retries.
        logger.error('[CredentialRedaction] known-credential set reload failed: %s',
                     type(exc).__name__)
        cache[boundary] = (identity, previous)
        return previous
    if boundary is not None:
        try:
            values = (_model_routing_values(boundary, repository=scoped.repository)
                      if scoped is not None else _model_routing_values(boundary))
            labelled.extend(values)
        except Exception as exc:
            if scoped is not None:
                from lib.model_routing.domain import ModelRoutingError
                raise ModelRoutingError(
                    'request credential recognition is unavailable',
                    kind='model_route_unavailable',
                ) from exc
            # Not recorded under this identity: the next call retries, so a
            # storage outage at the first write cannot leave the owner's keys
            # unknown until the next model-routing change.
            if boundary not in failing:
                failing.add(boundary)
                logger.error('[CredentialRedaction] model-routing keys of owner %s unavailable: %s',
                             boundary.owner_user_id, type(exc).__name__)
            return previous or KnownCredentialRedactor.build(labelled)
        failing.discard(boundary)
    redactor = KnownCredentialRedactor.build(labelled)
    cache[boundary] = (identity, redactor)
    return redactor


def redact_value(value: Any, *, owner_user_id: int | None = None) -> Any:
    return known_credential_redactor(owner_user_id=owner_user_id).redact_value(value)
