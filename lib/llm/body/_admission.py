"""Selected-route input safety, after body adaptation and before transport.

Task prompt admission owns proactive compaction and working-set policy. This
boundary only refuses input exceeding the selected provider's hard window; it
never truncates history or calls a provider. Opaque reasoning/signature payloads
are not text tokens: use the canonical counter, not serialized body byte size.
The window is compared with the body's size in the selected model's own units
(``lib/token_counter/serving_units.measure_body``), never with Tofu's local
count: Claude counts a request up to the measured maximum in
``lib/model_info/token_unit_facts.json`` (2.22x the local count, 2026-11-13).
"""

from lib.llm.llm_errors import PromptTooLongError
from lib.token_counter.evidence import (
    ADMITTED_INPUT_TOKENS_KEY,
    ADMITTED_LOCAL_TOKENS_KEY,
    ADMITTED_SERVING_UNITS_KEY,
    TEXT_COUNT_VIEW_KEY,
)


def enforce_body_input_limit(body, *, model, provider_id='', reuse_admission=False,
                             context_window=0, logical_model=''):
    """Validate the final adapted prompt and retain its count for this attempt.

    A positive count and the caller's reuse hint do not prove content identity:
    messages, system text, or nested tool schemas may have changed in place.
    Always recount with the local backend, whose bounded content-digest cache
    reuses unchanged text safely; a task body's provider view adds its owner's
    digest-keyed counts, and schemas are still re-encoded from content.
    Network counters must not participate in this guard. ``reuse_admission``
    remains accepted for callers, but cannot authorize skipping this check.

    ``logical_model`` is the selected slot's logical model (defaulting to the
    body's ``_admission_logical_model``, then the wire ``model``); the recount
    is converted to its units. Root admission's measurement on the body
    applies only when it measured that same model, so a fallback, failover
    or pool-rescue destination is always re-measured in its own units.
    Returns the admitted size in those units.
    """
    from lib.tasks_pkg.compaction._tokens import resolve_model_context_limit

    if context_window:
        body['_route_context_window'] = int(context_window)
        body['_route_context_model'] = model
    window = (body.get('_route_context_window')
              if body.get('_route_context_model') == model else None)
    window = window or resolve_model_context_limit(model, provider_id)
    from lib.token_counter import count_tokens, request_text_count_view
    from lib.token_counter.serving_units import measure_body

    units_model = logical_model or body.get('_admission_logical_model') or model
    view = request_text_count_view(body.get(TEXT_COUNT_VIEW_KEY))
    local = count_tokens(
        body.get('messages') or [], model=units_model,
        system=body.get('system'), tools=body.get('tools'),
        mode='tiktoken', **({'text_count_memo': view} if view is not None else {}),
    )['tokens']
    tokens = measure_body(model=units_model, local_tokens=local,
                          admitted=body.get(ADMITTED_SERVING_UNITS_KEY)).tokens
    if window and tokens > window:
        raise PromptTooLongError(
            'Provider prompt refused before transport: selected model input '
            f'exceeds its hard context window ({tokens:,} tokens; window '
            f'{window:,}) in {units_model} units, {local:,} counted locally.',
            origin='local', total_tokens=tokens, context_window_tokens=window,
        )
    body[ADMITTED_INPUT_TOKENS_KEY] = tokens
    body[ADMITTED_LOCAL_TOKENS_KEY] = local
    if body.get('max_tokens'):
        from lib.llm.body._clamp import _clamp_completion_to_context_window
        body['max_tokens'] = _clamp_completion_to_context_window(
            model, body.get('messages'), body['max_tokens'],
            provider_id=provider_id, context_window=window,
            precomputed_input_tokens=tokens)
    # Routing metadata is internal only; final ingress consumes then strips it.
    body['_admission_provider_id'] = str(provider_id or '')
    return tokens
