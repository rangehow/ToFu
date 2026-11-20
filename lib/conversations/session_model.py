"""Owner model changes on existing sessions, preserving context, grant and checkout.

The owner (or a lead acting under their delegation, docs/ROUTER.md R9) answers a
provider refusal with a legitimate model change on the SAME session instead of a
clean replacement: the conversation, dispatch grant, tool checkpoint, work id and
authorization scope are untouched, and an in-flight attempt keeps its accepted
model. Only the next accepted attempt (Continue, a Coordinator follow-up, a
recovered continuation) resolves the new choice.

There is no second authority here. The choice is the ordinary server-authored
conversation setting ``modelOverride`` (contracts/conversation_settings_v1.json),
written through the same settings CAS the owner's composer uses after the same
route admission as the Worker-model setting (``admit_worker_model``, R5).
Dispatch admission (``TrustedDispatch.capture``) resolves it for every new
attempt; ``_restored_identity`` admits a carrier's model only from durable
evidence (the grant's model, the session's current choice, or the model its
bound attempt was accepted with), so a later change never revokes a running
call and a rewritten carrier gains nothing. Every change
writes one ``session_model_changed`` record to the existing audit log. A lead
acting under the owner's grant takes the delegated session-control path
(``lib.router.owner_delegation.delegated_conversation_control``, R9): grant,
owner trigger and one audit record naming the chosen model precede the same
``set_session_model`` call, as for a delegated Continue.

The conversation's configured thinking depth is resolved the same way for each
new dispatch attempt (``apply_session_policy``): it is the model view the
owner's composer set, never grant authority.

Entry points: set_session_model (``control_conversation action=set_model``, the
owner's request or the R9 grant branch), owner_set_session_model (HTTP),
validate_request (shared argument checks), apply_session_policy (admission).
Dependencies: conversations.settings_store, conversations.repository,
project_dispatch.delegation, session_control._target (owner/project scope),
lib.log.audit_log.
"""
from __future__ import annotations

from collections.abc import Mapping

from lib.log import audit_log

SETTING = 'modelOverride'


def _conversation_settings(conversation_id: str, owner: int) -> Mapping:
    """Read the owner-scoped conversation's settings, or {} when it is unavailable.

    Admission resolves the override through this seam on every new attempt; a
    conversation that cannot be read simply has no override (the grant's model
    stands), exactly as before the setting existed.
    """
    from lib.conversations.repository import ConversationRepository
    target = ConversationRepository().get(conversation_id, user_id=owner, include_messages=False)
    settings = (target.metadata.get('settings') if target is not None else None) or {}
    return settings if isinstance(settings, Mapping) else {}


def session_model(conversation_id: str, *, owner: int) -> str:
    """The owner's model override for this session's next accepted attempt, or ''."""
    value = _conversation_settings(conversation_id, owner).get(SETTING)
    return value if isinstance(value, str) else ''


# The conversation's thinking selection: model view, never grant authority.
REASONING_FIELDS = ('thinkingDepth', 'thinkingEnabled')


def apply_session_policy(conversation_id: str, config: dict, *, owner: int,
                         use_composer_model: bool = False) -> dict:
    """Set model and effort from one owned settings read; return any composer route.

    An explicit session override takes precedence. Coordinator continuations
    otherwise use the saved composer selection; Worker grants keep their own
    model. No composer route is inherited when it names a different model.
    """
    from lib.conv_config import resolve_conv_config
    settings = _conversation_settings(conversation_id, owner)
    resolved = resolve_conv_config(conv_settings=settings, is_active=False)
    override = settings.get(SETTING)
    if isinstance(override, str) and override:
        config['model'] = override
    elif use_composer_model and resolved['model']:
        config['model'] = resolved['model']
    if resolved.get('thinkingDepth'):
        config.update((key, resolved[key]) for key in REASONING_FIELDS)
    if not use_composer_model or not resolved['model'] or config['model'] != resolved['model']:
        return {}
    route = resolved['modelRef']
    if route and route.get('model_id', resolved['model']) != resolved['model']:
        return {'preset': resolved['model']}
    return {key: resolved[key] for key in ('modelRef', 'preferredProviderId', 'routing', 'preset')}


def validate_request(arguments: Mapping) -> tuple[str, str | None, str]:
    """``(conversation_id, model or None, reason)`` of a model change, before any effect or record.

    The model bound is the router contract's ``id`` (1–256 characters), the
    shape a delegated action's audit target records.
    """
    for name in ('conversation_id', 'reason', 'idempotency_key'):
        if not isinstance(arguments.get(name), str) or not arguments[name].strip():
            raise ValueError(f'{name} is required')
    model = arguments.get('model')
    if model is not None and (not isinstance(model, str) or not model.strip() or len(model.strip()) > 256):
        raise ValueError('model must be a model id of 1–256 characters, or null to clear the session override')
    if len(arguments['reason']) > 2048:
        raise ValueError('reason exceeds 2048 characters')
    return arguments['conversation_id'], (model or '').strip() or None, arguments['reason']


def store_session_model(conversation_id: str, model: str | None, *, owner: int,
                        tenant_id: str | None = None) -> dict:
    """Persist the owner's choice through the conversation settings CAS, route-admitted."""
    previous = session_model(conversation_id, owner=owner)
    if model is not None and model == previous:
        # Set-to-value replay: nothing is written and no route admission repeats.
        return {'conversationId': conversation_id, 'modelOverride': previous, 'previousModel': previous}
    value = None
    if model is not None:
        from lib.project_dispatch.delegation import admit_worker_model
        value = admit_worker_model(owner, model, tenant_id=tenant_id, field='model')
    if (value or '') == previous:
        return {'conversationId': conversation_id, 'modelOverride': previous, 'previousModel': previous}
    from lib.conversations.settings_store import update_conversation_settings

    def apply(settings):
        if value is None:
            settings.pop(SETTING, None)
        else:
            settings[SETTING] = value
    if update_conversation_settings(conversation_id, apply, user_id=owner) is None:
        raise PermissionError('Conversation is unavailable')
    return {'conversationId': conversation_id, 'modelOverride': value or '', 'previousModel': previous}


def set_session_model(task: dict, scope: dict, principal, arguments: Mapping, *,
                      delegated: bool = False) -> dict:
    """Change one owned session's model; no attempt is created or rewritten.

    The human's current request may route any session in scope (a Coordinator's
    project, any owned session for the Global Router), exactly as
    control_conversation's Continue does. A synthetic continuation has no direct
    authority to change the model; delegated calls arrive with the principal
    issued by the existing owner-delegation gate.
    """
    from .session_control import _target
    conversation_id, model, reason = validate_request(arguments)
    if principal.kind != 'user' or principal.require_owner() != scope['user_id']:
        raise PermissionError('Only the owner can change a session’s model')
    # A principal from another tenant cannot act on this owner's sessions: the
    # tenant is checked against the session's own accepted execution identity.
    # ``delegated``: the owner's delegation was checked and recorded; it reaches other projects.
    conversation_id, _ = _target(scope, {'conversation_id': conversation_id}, write=True,
                                 reach=(lambda _project: None) if delegated else None)
    _check_tenant(scope, principal, conversation_id, arguments)
    result = store_session_model(conversation_id, model, owner=scope['user_id'],
                                 tenant_id=principal.tenant_id)
    if result['modelOverride'] != result['previousModel']:
        audit_log('session_model_changed', owner=scope['user_id'], conversation_id=conversation_id,
                  model=result['modelOverride'] or None, previous_model=result['previousModel'],
                  reason=reason, actor=scope['conversation_id'], turn_id=task.get('_turnId'),
                  attempt_id=task.get('_attemptId'), idempotency_key=arguments['idempotency_key'],
                  tenant=principal.tenant_id)
    return {**result, 'model': result['modelOverride'], 'previous_model': result['previousModel'],
            'execution_started': False,
            'next_step': 'The session keeps its conversation, grant, checkout and tool checkpoint. '
                         'An attempt already running keeps its model; Continue, follow_up or the next '
                         'continuation uses this model. Unchanged arguments and idempotency_key replay '
                         'this choice without writing it again.'}


def _check_tenant(scope: dict, principal, conversation_id: str, arguments: Mapping) -> None:
    """The session's accepted execution identity fixes the tenant, as control_session fences it.

    The caller names the Turn/Attempt it read; an attempt that does not belong
    to the conversation is refused, exactly as for Continue.
    """
    from lib.turn_lifecycle import get_attempt
    attempt_id = arguments.get('attempt_id')
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError('attempt_id is required: pin the session state you read')
    attempt = get_attempt(attempt_id, user_id=scope['user_id'], include_execution_config=True)
    if not isinstance(attempt, Mapping) or attempt.get('conversationId') != conversation_id:
        raise PermissionError('Attempt does not belong to the selected conversation')
    config = attempt.get('config')
    if isinstance(config, Mapping) and (config.get('_turnPrincipalTenantId') or None) != principal.tenant_id:
        raise PermissionError('Conversation execution belongs to another tenant')


def owner_set_session_model(owner: int, principal, arguments: Mapping) -> dict:
    """The owner's own HTTP request: change any owned session's model (no model loop).

    Same setting, route admission and audit as the Coordinator tool path. The
    owner addresses any of their conversations. The tenant fence of the tool
    path does not apply here: this is the owner's own authenticated request,
    whose principal tenant is the one the route admission resolves.
    """
    conversation_id, model, reason = validate_request(arguments)
    if principal.kind != 'user' or principal.require_owner() != owner:
        raise PermissionError('Only the owner can change a session’s model')
    from lib.conversations.repository import ConversationRepository
    if ConversationRepository().get(conversation_id, user_id=owner, include_messages=False) is None:
        raise PermissionError('Conversation not found in this owner scope')
    result = store_session_model(conversation_id, model, owner=owner, tenant_id=principal.tenant_id)
    if result['modelOverride'] != result['previousModel']:
        audit_log('session_model_changed', owner=owner, conversation_id=conversation_id,
                  model=result['modelOverride'] or None, previous_model=result['previousModel'],
                  reason=reason, actor='http:owner', idempotency_key=arguments['idempotency_key'],
                  tenant=principal.tenant_id)
    return {**result, 'model': result['modelOverride'], 'previous_model': result['previousModel'],
            'execution_started': False,
            'next_step': 'The session keeps its conversation, grant, checkout and tool checkpoint. '
                         'An attempt already running keeps its model; Continue, follow_up or the next '
                         'continuation uses this model.'}


__all__ = ['SETTING', 'apply_session_policy', 'owner_set_session_model', 'session_model',
           'set_session_model', 'store_session_model', 'validate_request']
