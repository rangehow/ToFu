"""Compare ordered final request history at a common provider boundary.

Callers pass sealed HTTP requests or their detached wire mappings. A verdict
describes local request compatibility. Connection/account validity, native
output replay, and provider cache behavior belong to the selected transport.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field


def request_history_field(protocol: str) -> str:
    """Select the ordered item array emitted by the wire protocol."""
    from lib.llm.request import require_wire_protocol

    return 'input' if require_wire_protocol(protocol) == 'responses' else 'messages'


@dataclass(frozen=True)
class RequestPrefixComparison:
    """A local verdict and the suffix after an unchanged previous request."""

    full_reason: str = ''
    suffix: list = field(default_factory=list, repr=False, compare=False)


def compare_request_prefix(
        previous: Mapping | None,
        current: Mapping,
        *,
        protocol: str,
        variable_fields: frozenset[str] = frozenset(),
) -> RequestPrefixComparison:
    """Compare request fields and ordered input by ordered decoded values.

    Adapters explicitly declare fields that can vary. The history array is
    always compared separately. A suffix is only evidence for the adapter's
    own continuation check.
    """
    if previous is None:
        return RequestPrefixComparison('no_checkpoint')

    history_field = request_history_field(protocol)
    optional = variable_fields | {history_field}
    previous_fixed = {key: previous[key] for key in previous if key not in optional}
    current_fixed = {key: current[key] for key in current if key not in optional}
    if previous_fixed != current_fixed:
        return RequestPrefixComparison('request_fields_changed')

    prior = previous.get(history_field)
    present = current.get(history_field)
    if not isinstance(prior, list) or not isinstance(present, list):
        return RequestPrefixComparison('input_not_list')
    if len(present) < len(prior):
        return RequestPrefixComparison('history_shortened')
    if present[:len(prior)] != prior:
        return RequestPrefixComparison('history_changed')
    return RequestPrefixComparison(suffix=present[len(prior):])
