"""The native engine predates ``background_command_retirement`` and must still accept it.

A closed Turn's settlement sends the member with its round-0 evidence block.
An engine without the member must not refuse the update: the evidence attaches,
the queue rows stay, and the result names no ``removedQueueIds``, which the
settlement reads as "not retired" and never rewrites the Turn for
(``lib/background_command_delivery.py``; docs/RUN_CONTINUITY.md, defect 2). The
engine change that adds the member replaces this file with the native cases of
``test_background_receipt_retirement.py``.
"""
from __future__ import annotations

import pytest

from tests.background.test_background_receipt_retirement import (
    _block, _listed, _retire, _turn, authority,
)

__all__ = ('authority',)
pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['native'], indirect=True)]


def test_the_evidence_attaches_and_the_rows_stay(authority):  # noqa: F811
    client, owner, turn_id = authority
    before = _turn(owner, turn_id)
    block = _block(('q-late', 'bg_late'))

    result = _retire(owner, turn_id, block, ('q-late', 'bg_late'))

    assert 'removedQueueIds' not in result
    after = _turn(owner, turn_id)
    assert after['projectionRevision'] == before['projectionRevision'] + 1
    assert after['projection']['_bgCommandInjects'] == [block]
    assert _listed(client, owner) == ['q-late', 'q-later']
