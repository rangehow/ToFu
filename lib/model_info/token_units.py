"""lib/model_info/token_units.py — measured token units per model family.

Responsibility: own the maintained relation between Tofu's local token count
of a request and the count the serving model's provider reports for the same
request. ``token_unit_facts.json`` records, per token-unit family, the observed
range of ``provider prompt tokens / local tokens`` over persisted request
rounds, with its sample size, measured member models, window and the local
encoding it was measured against. A family without a fact is unmeasured: its
local count carries no error bound, which is never the same as "equal".

The values are measurements, not settings: ``scripts/measure_token_units.py``
derives them from persisted ``apiRounds`` and this module rejects a fact that
lacks its provenance. There is no environment or configuration override.

Entry points: token_unit_family, token_unit_fact, token_unit_facts,
validate_token_unit_catalog. Dependencies: the family predicates in _family.
No network, credentials or persistence; the catalog is read once per process.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from lib.model_info._family import (
    is_claude,
    is_deepseek,
    is_doubao,
    is_gemini,
    is_glm,
    is_gpt,
    is_grok,
    is_kimi,
    is_longcat,
    is_minimax,
    is_qwen,
)

CATALOG_CONTRACT = 'tofu.token-unit-facts/v1'
CATALOG_PATH = Path(__file__).with_name('token_unit_facts.json')
LOCAL_ENCODINGS = frozenset({'cl100k_base', 'o200k_base'})

# One tokenizer per vendor line; the first matching predicate wins, so the
# substring-broad ``is_gpt`` stays last.
_FAMILIES = (
    ('claude', is_claude),
    ('kimi', is_kimi),
    ('deepseek', is_deepseek),
    ('glm', is_glm),
    ('gemini', is_gemini),
    ('qwen', is_qwen),
    ('minimax', is_minimax),
    ('doubao', is_doubao),
    ('longcat', is_longcat),
    ('grok', is_grok),
    ('gpt', is_gpt),
)
_FAMILY_NAMES = frozenset(name for name, _predicate in _FAMILIES)
_DOCUMENT_FIELDS = frozenset({'contract', 'definition', 'generator', 'families'})
_FACT_FIELDS = frozenset({
    'local_encoding', 'provider_per_local', 'samples', 'members',
    'conversations', 'measured_from', 'measured_to'})


class TokenUnitFactError(ValueError):
    """The maintained catalog violates its contract; ``field`` names where."""

    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class TokenUnitFact:
    """Observed ``provider / local`` range for one family's measured requests."""

    family: str
    local_encoding: str
    minimum: float
    maximum: float
    samples: int
    members: tuple[tuple[str, int], ...]
    conversations: int
    measured_from: str
    measured_to: str


def token_unit_family(model: str) -> str | None:
    """Return the token-unit family of a model id, or ``None`` when unknown."""
    for name, predicate in _FAMILIES:
        if predicate(model or ''):
            return name
    return None


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise TokenUnitFactError(f'{field} must be a positive integer', field=field)
    return value


def _iso_day(value: Any, field: str) -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise TokenUnitFactError(f'{field} must be an ISO date', field=field) from None
    return value


def _fact(family: str, raw: Any) -> TokenUnitFact:
    field = f'families.{family}'
    if family not in _FAMILY_NAMES:
        raise TokenUnitFactError(f'{field}: unknown token-unit family', field=field)
    if not isinstance(raw, dict) or set(raw) != _FACT_FIELDS:
        raise TokenUnitFactError(
            f'{field} requires exactly {sorted(_FACT_FIELDS)}', field=field)
    if raw['local_encoding'] not in LOCAL_ENCODINGS:
        raise TokenUnitFactError(f'{field}.local_encoding is not a local encoding',
                                 field=f'{field}.local_encoding')
    bound = raw['provider_per_local']
    minimum = bound.get('min') if isinstance(bound, dict) else None
    maximum = bound.get('max') if isinstance(bound, dict) else None
    if (not isinstance(bound, dict) or set(bound) != {'min', 'max'}
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in (minimum, maximum))
            or not 0 < minimum <= maximum):
        raise TokenUnitFactError(
            f'{field}.provider_per_local requires finite 0 < min <= max',
            field=f'{field}.provider_per_local')
    samples = _positive_int(raw['samples'], f'{field}.samples')
    members = raw['members']
    if not isinstance(members, dict) or not members:
        raise TokenUnitFactError(f'{field}.members must name the measured models',
                                 field=f'{field}.members')
    for model, count in members.items():
        _positive_int(count, f'{field}.members.{model}')
        if token_unit_family(model) != family:
            raise TokenUnitFactError(
                f'{field}.members.{model} belongs to another family',
                field=f'{field}.members')
    if sum(members.values()) != samples:
        raise TokenUnitFactError(f'{field}.members must account for every sample',
                                 field=f'{field}.members')
    measured_from = _iso_day(raw['measured_from'], f'{field}.measured_from')
    measured_to = _iso_day(raw['measured_to'], f'{field}.measured_to')
    if measured_from > measured_to:
        raise TokenUnitFactError(f'{field} measurement window is reversed',
                                 field=f'{field}.measured_from')
    return TokenUnitFact(
        family=family,
        local_encoding=raw['local_encoding'],
        minimum=float(minimum),
        maximum=float(maximum),
        samples=samples,
        members=tuple(sorted(members.items())),
        conversations=_positive_int(raw['conversations'], f'{field}.conversations'),
        measured_from=measured_from,
        measured_to=measured_to,
    )


def validate_token_unit_catalog(document: Any) -> dict[str, TokenUnitFact]:
    """Validate a catalog document and return its facts by family."""
    if (not isinstance(document, dict) or set(document) != _DOCUMENT_FIELDS
            or document.get('contract') != CATALOG_CONTRACT):
        raise TokenUnitFactError(
            f'token-unit catalog must be {CATALOG_CONTRACT} with {sorted(_DOCUMENT_FIELDS)}',
            field='contract')
    families = document['families']
    if not isinstance(families, dict):
        raise TokenUnitFactError('families must be an object', field='families')
    return {family: _fact(family, raw) for family, raw in families.items()}


@lru_cache(maxsize=1)
def token_unit_facts() -> dict[str, TokenUnitFact]:
    """The maintained facts, validated once per process."""
    return validate_token_unit_catalog(
        json.loads(CATALOG_PATH.read_text(encoding='utf-8')))


def token_unit_fact(model: str) -> TokenUnitFact | None:
    """The measured fact for ``model``'s family, or ``None`` when unmeasured."""
    family = token_unit_family(model)
    return token_unit_facts().get(family) if family is not None else None


__all__ = [
    'CATALOG_CONTRACT',
    'CATALOG_PATH',
    'LOCAL_ENCODINGS',
    'TokenUnitFact',
    'TokenUnitFactError',
    'token_unit_fact',
    'token_unit_facts',
    'token_unit_family',
    'validate_token_unit_catalog',
]
