"""Declared gateway request shapes for the Anthropic Messages wire.

Some Anthropic-compatible gateways admit a request only when it carries a
fingerprint: the first system text must start with a fixed client identity,
and a header must name the caller's working directory. Those facts belong to
the gateway, so its profile declares them (``lib.provider.gateway_profiles``
traits ``anthropic_system_identity`` and ``working_directory_header``) and
core applies them for whichever base URL the profile names. With no profile
installed nothing is added.

Entry points: ``apply_gateway_anthropic_shape``, ``gateway_anthropic_headers``.
Dependencies: ``lib.provider.gateway_profiles``.
"""
from __future__ import annotations

import os

from lib.provider.gateway_profiles import gateway_trait


def apply_gateway_anthropic_shape(body: dict, base_url) -> dict:
    """Make ``body['system']`` start with the gateway's declared identity.

    Operates on the output of ``openai_body_to_anthropic`` (a fresh dict, so
    mutation is safe). The caller's system text is preserved verbatim after
    the identity: appended to the same string, or kept as later blocks. A
    system text that already starts with the declared accepted prefix
    (compared case-insensitively) is left unchanged.
    """
    declared = gateway_trait('anthropic_system_identity', base_url=base_url)
    if not declared:
        return body
    identity, accepted = declared['text'], declared['accepted_prefix'].lower()
    system = body.get('system')
    if isinstance(system, str):
        if not system.lstrip().lower().startswith(accepted):
            body['system'] = identity if not system.strip() else f'{identity}\n\n{system}'
        return body
    if isinstance(system, list) and system:
        blocks = [block for block in system if isinstance(block, dict)]
        first_text = next((str(block.get('text') or '') for block in blocks
                           if block.get('type') == 'text'), '')
        if not first_text.lstrip().lower().startswith(accepted):
            body['system'] = [{'type': 'text', 'text': identity}, *blocks]
    else:
        body['system'] = identity
    return body


def gateway_anthropic_headers(base_url) -> dict:
    """Headers the gateway's profile requires: its working-directory header, if declared."""
    header = gateway_trait('working_directory_header', base_url=base_url)
    return {header: os.getcwd() or '/'} if header else {}


__all__ = ['apply_gateway_anthropic_shape', 'gateway_anthropic_headers']
