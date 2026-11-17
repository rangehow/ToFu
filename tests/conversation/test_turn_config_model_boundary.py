"""The create-turn command refuses a malformed model selection at its boundary.

Found by the chaos suite (docs/ROBUSTNESS_AUDIT_20261117.md R3): a structured
``config.model`` was accepted, persisted as an Attempt, and failed only when
the executor started, with the raw ``'dict' object has no attribute 'lower'``.
``config.model`` is a model ID string; a structured reference is ``modelRef``.
"""
from __future__ import annotations

import pytest

import lib.conversation_sync.command_service as command_module
from lib.conversation_sync.command_service import ConversationTurnCommandService
from lib.error_envelope._rejection import BadRequest

pytestmark = pytest.mark.unit


def _service():
    return ConversationTurnCommandService(
        build_user_message=lambda *_args: None,
        was_aborted_after=lambda *_args: False,
        start_task=lambda *_args: pytest.fail('a refused command must not start work'),
    )


@pytest.mark.parametrize('model', [{'creator_id': 'tofu', 'model_id': 'm'}, ['m'], 3, True])
def test_a_non_string_model_is_refused_before_any_turn_exists(monkeypatch, model):
    monkeypatch.setattr(command_module, 'create_turn_pair',
                        lambda *_a, **_k: pytest.fail('a refused command must not create Turns'))
    with pytest.raises(BadRequest) as refused:
        _service().create_turn('conv-model', 7, {
            'commandId': 'command-model', 'config': {'model': model},
            'inputTurn': {'role': 'user', 'content': 'hello'}}, request_started_at=1.0)
    assert refused.value.field == 'config.model'
    assert 'modelRef' in str(refused.value)
