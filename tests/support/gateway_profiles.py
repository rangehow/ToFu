"""Synthetic gateway profiles: core tests declare the gateway quirks they exercise.

Core behavior keyed on a gateway (``lib.provider.gateway_profiles``) must not
depend on which private overlay bundle the test host happens to have. These
helpers replace the loaded profiles for one test with explicit documents.
The Meituan overlay's real declaration is pinned by bundles/meituan/tests.
"""
from __future__ import annotations

import pytest

#: The Claude tool-name budget the tool-surface tests size eager catalogs to.
CLAUDE_TOOL_NAME_BUDGET_BYTES = 448
#: Host of the Anthropic face the tool-name budget tests route through.
CLAUDE_BUDGET_GATEWAY_HOST = 'claude-gateway.example.test'


def install_gateway_profiles(monkeypatch, *documents: dict) -> tuple[dict, ...]:
    """Make exactly ``documents`` the loaded gateway profiles for this test."""
    from lib.provider import gateway_profiles as module
    parsed = tuple(module.parse_gateway_profile(
        {'contract': module.GATEWAY_PROFILE_CONTRACT, **document},
        f'<test profile {document["id"]}>') for document in documents)
    monkeypatch.setattr(module, 'gateway_profiles', lambda: parsed)
    return parsed


@pytest.fixture
def claude_tool_name_budget_gateway(monkeypatch):
    """Declare the Claude tool-name budget on the budget tests' gateway host."""
    return install_gateway_profiles(monkeypatch, {
        'id': 'test-claude-tool-name-budget',
        'hosts': [CLAUDE_BUDGET_GATEWAY_HOST],
        'traits': {'anthropic_claude_tool_name_budget_bytes': CLAUDE_TOOL_NAME_BUDGET_BYTES},
    })
