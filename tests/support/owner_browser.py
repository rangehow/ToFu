"""Tasks answering the owner's own attended message, which may borrow the owner's browser.

``lib.search.browser_scope.owner_browser_refusal`` lends the owner's real
browser only to such a task and verifies the reply Turn's human input through
``lib.turn_lifecycle.get_turn``. Unit tests of the owner's own browser path use
these helpers for that owned Turn pair instead of a storage authority.
"""

from lib.identity import PrincipalContext

OWNER_CONVERSATION = 'conv-owner-request'
_TURNS = {
    'owner-reply': {'actor': 'assistant', 'parentTurnId': 'owner-message'},
    'owner-message': {'actor': 'human', 'projection': {'role': 'user', 'content': 'owner request'}},
}


def owner_message_reply(monkeypatch, task):
    """Mark ``task`` as the attended reply to its owner's own message and return it."""
    import lib.turn_lifecycle as turn_lifecycle

    def get_turn(conversation_id, turn_id, **_kwargs):
        return {'conversationId': conversation_id, 'turnId': turn_id, **_TURNS[turn_id]}

    monkeypatch.setattr(turn_lifecycle, 'get_turn', get_turn)
    task.update(convId=OWNER_CONVERSATION, _turnId='owner-reply', _attended=True)
    return task


def owner_attended_task(monkeypatch, *, owner, **fields):
    """A new attended task of ``owner`` replying to the owner's own message."""
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner)
    return owner_message_reply(monkeypatch, {
        '_userId': owner, '_principalContext': principal.to_payload(), **fields})
