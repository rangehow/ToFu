"""Declared gateway quirks: the core reads facts, overlays declare which hosts have them.

Responsibility: load and validate ``gateway_profiles/*.json`` from installed
bundles (``tofu_extensions.bundle_directories``) and answer trait lookups for
a request's base URL or provider id. A public core with no bundle installed
declares no profile, so every trait is absent and requests take the plain
provider path. Company gateways (for example FRIDAY) ship their profile in a
private overlay instead of a hostname literal in core (``docs/PUBLIC_CORE.md``).

Profile contract ``tofu.gateway-profile/v1``::

    {"contract": "tofu.gateway-profile/v1", "id": "friday",
     "hosts": ["aigc.example.com", ".example.com"],
     "provider_ids": ["friday", "friday*"],
     "traits": {"blocked_term_sanitize": true}}

``hosts`` entries match a URL hostname exactly; a leading ``.`` matches the
domain and its subdomains on a dot boundary. ``provider_ids`` entries match
exactly; a trailing ``*`` matches a prefix. Traits are closed-world:
``TRAIT_VALIDATORS`` names each one with its value check, and an unknown or
malformed trait is skipped with a warning rather than guessed at.

Profiles load once per process: bundle installation already requires a
restart. Entry points: ``gateway_profiles``, ``gateway_trait``,
``clear_gateway_profile_cache``. Dependencies: ``tofu_extensions``, stdlib.
"""
from __future__ import annotations

import json
import math
import re
import threading
from types import MappingProxyType
from urllib.parse import urlparse

from lib.log import get_logger
from tofu_extensions import bundle_directories

logger = get_logger(__name__)

GATEWAY_PROFILE_CONTRACT = 'tofu.gateway-profile/v1'
GATEWAY_PROFILE_KIND = 'gateway_profiles'
CLAUDE_CACHE_MULTIPLIER_KEYS = frozenset({'cacheWriteMul', 'cacheWrite1hMul', 'cacheReadMul'})
IMAGE_GENERATION_APIS = frozenset({'friday_async'})
# RFC 9110 field-name token.
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _is_positive_int(value) -> bool:
    return type(value) is int and value > 0


def _is_system_identity(value) -> bool:
    return (isinstance(value, dict) and set(value) == {'text', 'accepted_prefix'}
            and all(isinstance(v, str) and v.strip() for v in value.values())
            and value['text'].lower().startswith(value['accepted_prefix'].lower()))


def _is_cache_multipliers(value) -> bool:
    return (isinstance(value, dict) and value and set(value) <= CLAUDE_CACHE_MULTIPLIER_KEYS
            and all(type(v) in (int, float) and math.isfinite(v) and v >= 0
                    for v in value.values()))


#: Trait name -> value validator. Each trait has exactly one core consumer:
#: blocked_term_sanitize -> lib.llm.body._build / lib.tasks_pkg.wire_messages,
#: anthropic_claude_tool_name_budget_bytes -> lib.llm.tool_wire,
#: claude_cache_multipliers -> lib.pricing._provider,
#: image_generation_api -> lib.image_gen._slots,
#: anthropic_system_identity / working_directory_header -> lib.llm.gateway_shape.
TRAIT_VALIDATORS = MappingProxyType({
    'blocked_term_sanitize': lambda value: value is True,
    'anthropic_claude_tool_name_budget_bytes': _is_positive_int,
    'claude_cache_multipliers': _is_cache_multipliers,
    'image_generation_api': lambda value: value in IMAGE_GENERATION_APIS,
    'anthropic_system_identity': _is_system_identity,
    'working_directory_header': lambda value: (isinstance(value, str)
                                               and _HEADER_NAME_RE.fullmatch(value) is not None),
})

_cache: tuple[dict, ...] | None = None
_cache_lock = threading.Lock()


def _string_list(raw, field: str, source: str) -> tuple[str, ...]:
    values = raw.get(field, [])
    if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
        raise ValueError(f'{source}: {field} must be a list of non-empty strings')
    return tuple(v.lower() for v in values)


def parse_gateway_profile(raw, source: str) -> dict:
    if not isinstance(raw, dict) or raw.get('contract') != GATEWAY_PROFILE_CONTRACT:
        raise ValueError(f'{source}: not a {GATEWAY_PROFILE_CONTRACT} document')
    profile_id = raw.get('id')
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError(f'{source}: id must be a non-empty string')
    raw_traits = raw.get('traits')
    if not isinstance(raw_traits, dict):
        raise ValueError(f'{source}: traits must be an object')
    traits = {}
    for name, value in raw_traits.items():
        validator = TRAIT_VALIDATORS.get(name)
        if validator is None or not validator(value):
            logger.warning('[GatewayProfiles] %s: skipping unknown or invalid trait %r',
                           source, name)
            continue
        traits[name] = value
    return {
        'id': profile_id,
        'hosts': _string_list(raw, 'hosts', source),
        'provider_ids': _string_list(raw, 'provider_ids', source),
        'traits': MappingProxyType(traits),
        'source': source,
    }


def _load_profiles() -> tuple[dict, ...]:
    profiles = []
    for directory in bundle_directories(GATEWAY_PROFILE_KIND):
        for path in sorted(directory.glob('*.json')):
            try:
                profiles.append(parse_gateway_profile(
                    json.loads(path.read_text(encoding='utf-8')), str(path)))
            except (OSError, ValueError) as exc:
                logger.warning('[GatewayProfiles] skipping %s: %s', path, exc)
    return tuple(profiles)


def gateway_profiles() -> tuple[dict, ...]:
    """Return validated profiles in bundle order (loaded once per process)."""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _load_profiles()
        return _cache


def clear_gateway_profile_cache() -> None:
    """Forget loaded profiles; for tests that install synthetic bundles."""
    global _cache
    with _cache_lock:
        _cache = None


def _host_matches(host: str, patterns) -> bool:
    return any(host == p if not p.startswith('.') else (host == p[1:] or host.endswith(p))
               for p in patterns)


def _provider_matches(provider_id: str, patterns) -> bool:
    return any(provider_id.startswith(p[:-1]) if p.endswith('*') else provider_id == p
               for p in patterns)


def _hostname(base_url) -> str:
    try:
        return (urlparse(str(base_url or '')).hostname or '').lower()
    except ValueError:
        return ''


def gateway_trait(trait: str, *, base_url=None, provider_id=None):
    """Return the first matching profile's declared ``trait`` value, else None.

    A profile matches when ``provider_id`` matches its ``provider_ids`` or
    ``base_url``'s hostname matches its ``hosts``. Callers pass whichever
    identity their legacy gate used; the lookup grants no credential or tool
    authority, only a wire-shape fact.
    """
    if trait not in TRAIT_VALIDATORS:
        raise KeyError(f'unknown gateway trait {trait!r}')
    host = _hostname(base_url) if base_url else ''
    pid = str(provider_id or '').lower()
    for profile in gateway_profiles():
        if trait not in profile['traits']:
            continue
        if ((pid and _provider_matches(pid, profile['provider_ids']))
                or (host and _host_matches(host, profile['hosts']))):
            return profile['traits'][trait]
    return None


__all__ = [
    'GATEWAY_PROFILE_CONTRACT', 'GATEWAY_PROFILE_KIND', 'TRAIT_VALIDATORS',
    'clear_gateway_profile_cache', 'gateway_profiles', 'gateway_trait',
]
