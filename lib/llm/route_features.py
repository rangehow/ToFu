"""Protocol-extension bindings for a selected route's declared capabilities.

This module grants wire support only, never tool, credential, or owner authority.
Profiles are closed-world except for the installed subscription-dialect wire
bindings in ``SUBSCRIPTION_DIALECTS``; explicit unknown/malformed declarations
still deny support. ``None`` marks a legacy caller without a route contract.
Core protocol support grants no extensions.
"""
from __future__ import annotations

from collections.abc import Mapping

# Feature names are stable contract keys, not provider or model names. A new
# dialect adds an installed binding here and a focused serializer, not a regex.
_FEATURE_PROTOCOLS = {
    'chat_cache_breakpoints': 'openai',
    'responses_programmatic': 'responses',
    'responses_multi_agent': 'responses',
    'responses_compaction': 'responses',
    'responses_websocket': 'responses',
    'responses_prompt_cache': 'responses',

    'responses_cache_breakpoints': 'responses',
    'responses_safety_identifier': 'responses',
    'responses_reasoning_replay': 'responses',

    'responses_reasoning_summary': 'responses',
    'responses_reasoning_mode': 'responses',
    'responses_text_verbosity': 'responses',
    'responses_image_detail': 'responses',
    'anthropic_compaction': 'anthropic',
}
# Gateway bindings ride HTTP headers rather than a protocol body, so one
# declaration holds on every wire face of that gateway. Their serializer is
# lib.llm.cache_affinity.CACHE_AFFINITY_HEADER_CARRIERS.
_GATEWAY_FEATURES = frozenset({
    'cache_affinity_mt_session_id',
})
# Declarative facts of every OAuth-subscription dialect: the protocol
# extensions its installed wire adapter provides without a profile declaration,
# and the cache-timing profile its traffic needs (dispatch passes it through
# ``Slot.cache_profile`` to ``lib.llm_dispatch.cache_settle``). Adding a
# subscription provider is one entry here and never a provider-name branch in
# ``route_feature``; its actual wire transform stays in the provider module.
#
# Codex's installed Responses dialect requires replay of its encrypted
# reasoning items with store=false, supports prompt-cache affinity and readable
# reasoning summaries. These are independent wire capabilities. Catalog
# reasoning/sampling metadata is not a complete wire-feature list: treating an
# omitted feature as unsupported makes a successful tool round impossible to
# continue as soon as the backend emits opaque reasoning. This binding is
# exclusive to the authenticated dialect; explicit unsupported/unknown
# declarations still narrow it, and other subscription or keyed Responses
# routes remain closed-world.
SUBSCRIPTION_DIALECTS = {
    'codex': {
        'features': frozenset({
            'responses_reasoning_replay', 'responses_prompt_cache',
            'responses_reasoning_summary',
        }),
        'cache_profile': 'codex',
    },
}


def route_feature(profile, feature: str, *, protocol: str, oauth: str = '') -> bool | None:
    """Return a bounded declared binding; None means legacy/no profile only.

    Subscription public-API extensions remain disabled even when misdeclared.
    Raw authorization or probing results cannot widen the installed adapters.
    """
    if profile is None:
        return None
    if not isinstance(profile, Mapping):
        return False
    wire = 'responses' if protocol == 'openai_responses' else protocol
    if (feature not in _GATEWAY_FEATURES
            and _FEATURE_PROTOCOLS.get(feature) != wire):
        return False
    # Subscription routes speak first-party dialects: only a feature that some
    # declared dialect installs can be granted, whatever the profile says.
    if oauth and not any(feature in dialect['features']
                         for dialect in SUBSCRIPTION_DIALECTS.values()):
        return False
    features = profile.get('features')
    if isinstance(features, Mapping) and feature in features:
        return features[feature] == 'supported'
    dialect = SUBSCRIPTION_DIALECTS.get(oauth)
    return bool(dialect and feature in dialect['features'])


def body_route_profile(body: Mapping):
    """Read the selected-route sidecar, including its rollout-era spelling."""
    return body.get('_route_inference_profile', body.get('_inference_profile'))
