"""Route-measured output speed as fast-tier evidence.

Responsibility: derive a ``fast`` verdict per logical Model from the output
throughput this process measured on its own routes (``Slot.throughput_ema``),
for Models without fresh exact-identity Artificial Analysis evidence: an
internal deployment AA never lists, or any Model while the AA snapshot is
missing or stale.
Entry point: :func:`measured_speed_evidence`.
Dependencies: ``lib.model_catalog.aa.RoutingSpeed`` (the shared verdict type).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping


def measured_speed_evidence(slots: Iterable) -> Mapping[str, object]:
    """``{logical Model: RoutingSpeed}`` from the measured throughput of *slots*.

    A Model's speed is the median throughput of its measured slots; it is fast
    when above the median of every measured Model in *slots*, the relative bar
    the AA snapshot uses, so no tokens/s threshold ages as Models speed up.
    Unmeasured slots contribute nothing, and a lone measured Model is never
    fast. The verdict carries no Intelligence Index: that is AA-only evidence.
    """
    from lib.model_catalog.aa import RoutingSpeed

    throughput_by_model: dict[str, list[float]] = defaultdict(list)
    for slot in slots:
        throughput = float(getattr(slot, 'throughput_ema', 0.0) or 0.0)
        if throughput > 0:
            identity = getattr(slot, 'logical_model', '') or slot.model
            throughput_by_model[identity].append(throughput)
    speeds = {
        model: statistics.median(values)
        for model, values in throughput_by_model.items()
    }
    if not speeds:
        return {}
    reference = statistics.median(speeds.values())
    return {
        model: RoutingSpeed(
            output_tokens_per_second=speed,
            intelligence=None,
            fast=speed > reference,
        )
        for model, speed in speeds.items()
    }


__all__ = ['measured_speed_evidence']
