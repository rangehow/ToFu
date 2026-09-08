"""Stable visible-phase identities shared by chat delivery and storage.

One executor owns a finite ordered phase sequence. These are Turn identities,
not provider IDs or browser array positions; replay uses the same sequence.
"""

import uuid


def visible_phase_identity(root_turn_id: str, attempt_id: str, index: int) -> tuple[str, str]:
    if index < 0 or index >= 4096:
        raise ValueError('visible phase index must be in [0, 4096)')
    if index == 0:
        return root_turn_id, attempt_id
    return (
        str(uuid.uuid5(uuid.NAMESPACE_URL, f'turn-attempt:{attempt_id}:visible:{index}')),
        str(uuid.uuid5(uuid.NAMESPACE_URL, f'turn-attempt:{attempt_id}:visible-attempt:{index}')),
    )
