"""Measure one model request in the serving model's own token units.

Tofu counts locally with tiktoken; most serving models tokenize differently
(Claude counted 1.09-2.22x Tofu's count of the same request, 2026-11-13).
Every admission,
compaction and hard-window decision compares a window with a
:class:`ServingMeasurement`: :func:`measure_request` for the working request
(prompt admission and the compaction gate) and :func:`measure_body` for the
final adapted body of one selected slot (``lib/llm/body/_admission.py``).

Sources, best first:

``provider_count``
    The serving model's provider-reported prompt tokens for this owner's
    previous request (``usage_cache``), bound to the content-free ledger of
    that request, plus what changed since: ledger entries it did not carry are
    charged at the family's measured maximum provider/local ratio, entries it
    carried that are gone are credited at the measured minimum. Opaque
    reasoning the provider reported for that response is added when the
    message replaying it arrives.
``measured_bound``
    The local count times the family's measured maximum ratio
    (``lib/model_info/token_unit_facts.json``): no measured request of that
    family counted more. Also used when it is the tighter of the two bounds
    (after compaction removed most of a request).
``local_unbounded``
    No measured fact covers the model's family (or the local count is not a
    tiktoken count): the local count, flagged unbounded, or the provider
    count plus the local change when one exists.

No source has a tuning parameter: ratios are measurements with their sample
provenance, and an unmeasured family is reported as such, never guessed. A
provider count above the family's measured maximum outranks the fact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from lib.log import get_logger
from lib.model_info.token_units import token_unit_fact, token_unit_family

from .tiktoken_counter import RequestLedger, encoding_for_model
from .usage_cache import ProviderCount, provider_count

logger = get_logger(__name__)

SOURCE_PROVIDER_COUNT = 'provider_count'
SOURCE_MEASURED_BOUND = 'measured_bound'
SOURCE_LOCAL_UNBOUNDED = 'local_unbounded'


@dataclass(frozen=True, slots=True)
class ServingMeasurement:
    """A request size in ``model``'s units and how it was obtained.

    ``opaque_tokens`` is the part of ``tokens`` that is opaque reasoning the
    request replays: the provider counts it, the local tokenizer cannot see it,
    so no local bound covers it. ``ledger`` is the content-free ledger of the
    measured working request. The round request builder carries the
    measurement on the body built from it, so the stream settlement binds the
    provider's count to exactly the request that was sent; it never enters
    public evidence.
    """

    tokens: int
    model: str
    source: str
    local_tokens: int
    cold_bound_tokens: int
    family: Optional[str]
    bounded: bool
    provider_tokens: Optional[int] = None
    opaque_tokens: int = 0
    ledger: Optional[RequestLedger] = field(default=None, repr=False, compare=False)

    def evidence(self) -> dict[str, Any]:
        """Content-free public evidence for admission records."""
        evidence = {
            'model': self.model,
            'source': self.source,
            'localTokens': self.local_tokens,
            'coldBoundTokens': self.cold_bound_tokens,
            'family': self.family,
            'bounded': self.bounded,
        }
        if self.provider_tokens is not None:
            evidence['providerTokens'] = self.provider_tokens
        if self.opaque_tokens:
            evidence['opaqueReplayTokens'] = self.opaque_tokens
        return evidence


def _fact_for(model: str, encoding: Optional[str]):
    fact = token_unit_fact(model)
    if fact is None or encoding is None or fact.local_encoding != encoding:
        return None
    return fact


def _cold(local_tokens: int, fact) -> int:
    return math.ceil(fact.maximum * local_tokens) if fact else local_tokens


def _changed(anchor: ProviderCount, ledger: RequestLedger) -> tuple[int, int, bool]:
    """Local tokens added and removed since ``anchor``, and opaque replay."""
    index = anchor.index()
    used: dict[bytes, int] = {}
    added = 0
    replays = False
    for identity, tokens in ledger.entries:
        known = index.get(identity)
        consumed = used.get(identity, 0)
        if known is not None and consumed < known[0]:
            used[identity] = consumed + 1
            continue
        added += tokens
        if identity in ledger.opaque:
            replays = True
    removed = sum(tokens * (occurrences - used.get(identity, 0))
                  for identity, (occurrences, tokens) in index.items())
    return added, removed, replays


def _anchored(anchor: ProviderCount, ledger: RequestLedger,
              fact) -> tuple[int, int, bool]:
    """``(tokens, opaque_tokens, exceeds_fact)`` of ``ledger``'s request.

    The anchor's own ratio widens the measured range when its content counted
    outside it, so a fact is never trusted over the provider's statement.
    """
    added, removed, replays = _changed(anchor, ledger)
    observed = anchor.provider_tokens / max(1, anchor.local_tokens)
    high = max(fact.maximum if fact else 1.0, observed)
    low = min(fact.minimum if fact else 1.0, observed)
    opaque = anchor.opaque_replay_tokens if replays else 0
    tokens = max(0, anchor.provider_tokens + math.ceil(high * added)
                 - math.floor(low * removed) + opaque)
    exceeds = fact is not None and anchor.provider_tokens > _cold(
        anchor.local_tokens, fact)
    return tokens, opaque, exceeds


def measure_request(*, model: str, local_tokens: int,
                    ledger: Optional[RequestLedger] = None,
                    owner_key: str = '',
                    local_encoding: Optional[str] = None) -> ServingMeasurement:
    """Measure a working request for admission and compaction decisions.

    ``local_encoding`` names the tiktoken encoding of ``local_tokens`` (the
    ledger's own when one is given); ``None`` means the count is not a
    tiktoken count, which no measured fact describes.
    """
    encoding = ledger.encoding if ledger is not None else local_encoding
    fact = _fact_for(model, encoding)
    cold = _cold(local_tokens, fact)
    family = token_unit_family(model)
    anchor = (provider_count(owner_key, model)
              if ledger is not None and owner_key else None)
    if anchor is not None and anchor.encoding == ledger.encoding:
        anchored, opaque, exceeds = _anchored(anchor, ledger, fact)
        if exceeds:
            logger.warning(
                '[TokenUnits] %s counted %d tokens for %d local tokens, above '
                'the measured %s maximum; regenerate lib/model_info/'
                'token_unit_facts.json (scripts/measure_token_units.py)',
                model, anchor.provider_tokens, anchor.local_tokens, family)
        # The cold bound covers visible content only; replayed opaque
        # reasoning is known solely from the provider's reserve.
        if fact is None or exceeds or anchored <= cold + opaque:
            return ServingMeasurement(
                anchored, model, SOURCE_PROVIDER_COUNT, local_tokens, cold,
                family, fact is not None, anchor.provider_tokens, opaque, ledger)
        if opaque:
            return ServingMeasurement(
                cold + opaque, model, SOURCE_MEASURED_BOUND, local_tokens, cold,
                family, True, opaque_tokens=opaque, ledger=ledger)
    return ServingMeasurement(
        cold, model, SOURCE_MEASURED_BOUND if fact else SOURCE_LOCAL_UNBOUNDED,
        local_tokens, cold, family, fact is not None, ledger=ledger)


def measure_body(*, model: str, local_tokens: int,
                 admitted: Any = None) -> ServingMeasurement:
    """Measure a final adapted body for the selected slot's hard window.

    ``local_tokens`` is the body's local tiktoken recount. ``admitted`` is the
    root admission's measurement of the request the body was built from
    (``evidence.ADMITTED_SERVING_UNITS_KEY``). It applies only to the model it
    measured; recount growth above its local count (route adaptation) is
    charged at the measured maximum ratio. Any other model, a fallback,
    failover or pool-rescue destination included, is measured from the local
    recount alone.
    """
    fact = _fact_for(model, encoding_for_model(model))
    cold = _cold(local_tokens, fact)
    family = token_unit_family(model)
    if isinstance(admitted, ServingMeasurement) and admitted.model == model:
        high = fact.maximum if fact else 1.0
        opaque = admitted.opaque_tokens
        tokens = admitted.tokens + math.ceil(
            high * max(0, local_tokens - admitted.local_tokens))
        if (fact is None or tokens <= cold + opaque
                or admitted.source == SOURCE_PROVIDER_COUNT):
            return ServingMeasurement(
                tokens, model, admitted.source, local_tokens, cold, family,
                fact is not None, admitted.provider_tokens, opaque)
        return ServingMeasurement(
            cold + opaque, model, SOURCE_MEASURED_BOUND, local_tokens, cold,
            family, True, opaque_tokens=opaque)
    return ServingMeasurement(
        cold, model, SOURCE_MEASURED_BOUND if fact else SOURCE_LOCAL_UNBOUNDED,
        local_tokens, cold, family, fact is not None)


__all__ = [
    'SOURCE_LOCAL_UNBOUNDED',
    'SOURCE_MEASURED_BOUND',
    'SOURCE_PROVIDER_COUNT',
    'ServingMeasurement',
    'measure_body',
    'measure_request',
]
