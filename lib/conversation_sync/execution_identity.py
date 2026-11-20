"""Accepted conversation identity and explicitly admitted owner actions.

Command acceptance stamps least-privilege identity and only the action grants
listed in the machine contract. Executor recovery validates both before context
construction. Nonhuman dispatch uses the separate project grant authority.
The existing attempt config remains the only durable carrier.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.identity import PrincipalContext, require_user_id

_CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                        'conversation_execution_identity_v1.json').read_text())
WORKER_EXECUTION_CONFIG_KEY = json.loads((Path(__file__).resolve().parents[2] /
    'contracts' / 'worker_job_lifecycle_v1.json').read_text())['conversationExecutionWitness']['configKey']
EXECUTION_IDENTITY_CONFIG_KEY = _CONTRACT['configKey']
RESERVED_EXECUTION_FIELDS = frozenset(_CONTRACT['reservedConfigFields']) | {WORKER_EXECUTION_CONFIG_KEY}
REVIEW_SOURCE_CONFIG_KEY = _CONTRACT['reviewSource']['configKey']
REVIEW_SOURCE_SCHEMA = _CONTRACT['reviewSource']['schema']
_ENVELOPE_FIELDS = frozenset(_CONTRACT['fields'])
_CAPABILITY_FIELD = _CONTRACT['optionalCapabilityField']
_CAPABILITY_GRANTS = _CONTRACT['capabilityGrants']
_BINDING_FIELD = _CONTRACT['capabilityBindingField']
_BINDING_FIELDS = tuple(_CONTRACT['capabilityBindingFields'])
_MANUAL_DETECT = 'schedule_manage.run.provider_probe_daily'
_PRINCIPAL_FIELDS = frozenset(_CONTRACT['principalFields'])
_SUPPORTED_KINDS = frozenset(_CONTRACT['supportedPrincipalKinds'])


@dataclass(frozen=True, slots=True)
class ManualDetectAdmission:
    """Exact action authority bound to the original executor and principal."""

    task_id: str
    principal: PrincipalContext
    conversation_id: str
    turn_id: str
    attempt_id: str

    def verify_current(self, *, require_live_worker=False) -> None:
        """Require the original durable grant and its currently running carrier."""
        verify_durable_detect(
            self.principal, self.conversation_id, self.turn_id, self.attempt_id,
            running_task_id=self.task_id, require_live_worker=require_live_worker,
        )


def public_execution_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """Drop private execution authority without inspecting untrusted values."""
    return {key: item for key, item in value.items() if key not in RESERVED_EXECUTION_FIELDS}


def capture_execution_identity(
    config: Mapping[str, Any], user_id: Any,
    principal: PrincipalContext | None, *, issue_capabilities: bool = False,
) -> dict[str, Any]:
    """Discard client authority and freeze the authenticated minimal identity.

    Explicit numeric-owner internal callers retain their historical user path.
    A system principal is *not* a human authorization just because it has an
    owner. Reject it before accepting a durable executable command.
    """
    owner = require_user_id(user_id, context='conversation task admission')
    if principal is None:
        principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner)
    if not isinstance(principal, PrincipalContext) or principal.require_owner() != owner:
        raise PermissionError('Conversation task principal does not match its owner')
    if principal.kind not in _SUPPORTED_KINDS:
        raise PermissionError('Nonhuman conversation execution requires trusted grant admission')
    # Request API scopes are not durable tool grants; do not regrant them on a
    # recovered task. Preserve subject/tenant for audit, with the existing
    # conversation executor's empty task scopes.
    frozen = PrincipalContext.user(subject_id=principal.subject_id,
        owner_user_id=owner, tenant_id=principal.tenant_id,
        scopes=_CONTRACT['executionScopes'])
    accepted = public_execution_fields(config)
    accepted['_turnPrincipalTenantId'] = frozen.tenant_id or ''
    accepted[EXECUTION_IDENTITY_CONFIG_KEY] = {
        'version': _CONTRACT['version'], 'principal': frozen.to_payload(), 'attended': True,
    }
    capabilities = sorted(action for action, scope in _CAPABILITY_GRANTS.items()
                          if issue_capabilities and principal.has_scope(scope))
    if capabilities:
        accepted[EXECUTION_IDENTITY_CONFIG_KEY].update({
            _CAPABILITY_FIELD: capabilities, _BINDING_FIELD: None,
        })
    return accepted


def bind_accepted_capabilities(config, conversation_id, turn_id, attempt_id):
    """Consume an issuance marker atomically with durable Attempt creation.

    Never restamp a previously bound capability (including copied configs).
    Keep the native Turn writer's counterpart in sync with this contract.
    """
    envelope = config.get(EXECUTION_IDENTITY_CONFIG_KEY) if isinstance(config, dict) else None
    if (isinstance(envelope, dict) and _BINDING_FIELD in envelope
            and envelope[_BINDING_FIELD] is None):
        return {**config, EXECUTION_IDENTITY_CONFIG_KEY: {
            **envelope, _BINDING_FIELD: dict(zip(_BINDING_FIELDS, (
                conversation_id, turn_id, attempt_id))),
        }}
    return config


def restore_execution_identity(config: Mapping[str, Any]) -> tuple[PrincipalContext, bool]:
    """Validate a durable accepted envelope; malformed presence never defaults.

    Legacy absent envelopes preserve the explicit-owner user executor only.
    Explicit nonhuman/false-attendance markers cannot take that compatibility
    path. Live and recovered executors use the same validation port.
    """
    owner = require_user_id(config.get('_turnOwnerUserId'), context='conversation executor')

    if '_projectDispatchAdmission' in config:
        from lib.project_dispatch.admission import restore_dispatch_identity
        return restore_dispatch_identity(config)
    if EXECUTION_IDENTITY_CONFIG_KEY not in config:
        if (config.get('_turnPrincipalKind', 'user') != 'user'
                or config.get('_attended', True) is not True
                or config.get('_unattended', False) is not False
                or '_principal' in config or '_principalContext' in config
                or '_conversationExecutionAdmission' in config):
            raise PermissionError('Legacy conversation identity cannot admit nonhuman execution')
        tenant = config.get('_turnPrincipalTenantId', '')
        if not isinstance(tenant, str) or len(tenant) > 256:
            raise PermissionError('Invalid legacy conversation tenant identity')
        return PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                     tenant_id=tenant or None), True
    envelope = config[EXECUTION_IDENTITY_CONFIG_KEY]
    if (not isinstance(envelope, Mapping)
            or set(envelope) - {_CAPABILITY_FIELD, _BINDING_FIELD} != _ENVELOPE_FIELDS
            or type(envelope.get('version')) is not int
            or envelope['version'] != _CONTRACT['version']
            or envelope.get('attended') is not True):
        raise PermissionError('Invalid accepted conversation execution identity')
    capabilities = envelope.get(_CAPABILITY_FIELD, [])
    if (not isinstance(capabilities, list)
            or any(not isinstance(action, str) or action not in _CAPABILITY_GRANTS
                   for action in capabilities)
            or capabilities != sorted(set(capabilities))):
        raise PermissionError('Invalid accepted conversation capabilities')
    payload = envelope.get('principal')
    if (not isinstance(payload, Mapping) or set(payload) != _PRINCIPAL_FIELDS
            or not isinstance(payload.get('kind'), str)
            or payload.get('kind') not in _SUPPORTED_KINDS
            or type(payload.get('owner_user_id')) is not int
            or payload['owner_user_id'] != owner
            or not isinstance(payload.get('subject_id'), str)
            or not payload['subject_id'].strip()
            or len(payload['subject_id']) > 256
            or (payload.get('tenant_id') is not None and
                (not isinstance(payload['tenant_id'], str) or
                 not payload['tenant_id'].strip() or len(payload['tenant_id']) > 256))
            or payload.get('scopes') != _CONTRACT['executionScopes']):
        raise PermissionError('Invalid accepted conversation principal identity')
    principal = PrincipalContext.from_payload(payload)
    if config.get('_turnPrincipalTenantId', '') != (principal.tenant_id or ''):
        raise PermissionError('Accepted conversation tenant identity does not match')
    if capabilities or _BINDING_FIELD in envelope:
        binding = envelope.get(_BINDING_FIELD)
        if (not capabilities or not isinstance(binding, Mapping)
                or set(binding) != set(_BINDING_FIELDS)
                or any(not isinstance(binding[key], str) or not binding[key] for key in _BINDING_FIELDS)):
            raise PermissionError('Accepted conversation capability binding does not match')
    return principal, True


def verify_durable_detect(principal, conversation_id, turn_id, attempt_id, *, running_task_id=None,
                          require_live_worker=False):
    """Check actual owner-scoped membership, freshness and original grant."""
    restored = read_durable_detect(principal.require_owner(), conversation_id, turn_id,
                                   attempt_id, running_task_id=running_task_id,
                                   require_live_worker=require_live_worker)
    if restored != principal:
        raise PermissionError('Detect principal differs from accepted authority')


def read_durable_detect(owner, conversation_id, turn_id, attempt_id, *, running_task_id=None,
                        require_live_worker=False):
    """Recover the principal from the exact private accepted capability."""
    from lib.turn_lifecycle import LifecycleNotFound, get_attempt, get_turn
    try:
        attempt = get_attempt(attempt_id, user_id=owner, include_execution_config=True)
        turn = get_turn(conversation_id, turn_id, user_id=owner, include_projection=False)
    except LifecycleNotFound as exc:
        raise PermissionError('Detect attempt is not owned or no longer exists') from exc
    identity = dict(zip(_BINDING_FIELDS, (conversation_id, turn_id, attempt_id)))
    if (any(attempt.get(key) != value for key, value in identity.items())
            or turn.get('currentAttemptId') != attempt_id
            or attempt.get('status') not in ('pending', 'running')
            or (running_task_id is not None and (
                attempt.get('status') != 'running' or attempt.get('taskId') != running_task_id))):
        raise PermissionError('Detect attempt is not the current accepted execution')
    config = attempt.get('config') or {}
    envelope = config.get(EXECUTION_IDENTITY_CONFIG_KEY)
    if ('_projectDispatchAdmission' in config or not isinstance(envelope, Mapping)
            or envelope.get(_BINDING_FIELD) != identity
            or envelope.get(_CAPABILITY_FIELD) != [_MANUAL_DETECT]):
        raise PermissionError('Detect capability was not issued for this attempt')
    restored, attended = restore_execution_identity({
        **config, '_turnOwnerUserId': owner, '_turnId': turn_id, '_attemptId': attempt_id,
    })
    if attended is not True:
        raise PermissionError('Detect attempt is not attended')
    if require_live_worker:
        _require_uncancelled_detect_worker(attempt, restored)
    return restored


def _require_uncancelled_detect_worker(attempt, principal):
    import time
    from lib.storage import get_storage_client

    task_id = attempt['taskId']
    client = get_storage_client()
    job = client.query('worker_job.get', {
        'user_id': principal.require_owner(), 'task_id': task_id})
    payload = job.get('payload', {}) if job else {}
    claim = (attempt['config'].get(WORKER_EXECUTION_CONFIG_KEY) or {}).get('claim')
    if (task_id != f"conversation-attempt:{attempt['attemptId']}"
            or not job or job['taskKind'] != 'conversation-attempt' or job['status'] != 'running'
            or job['cancelRequestedAtMs'] or not job['claimOwner'] or not job['fencingToken']
            or claim != {'worker_id': job['claimOwner'], 'fencing_token': job['fencingToken']}
            or job['tenantId'] != (principal.tenant_id or '')
            or payload.get('contract') != 'tofu.conversation-attempt-job/v1'
            or any(payload.get(field) != attempt[field] for field in _BINDING_FIELDS)):
        raise PermissionError('Detect worker is cancelled or does not own the accepted Attempt')
    # The storage authority checks epoch, lease and protected-worker evidence.
    # Matching a retained row and token alone is not proof of live ownership.
    state = client.query('worker_job.claim_state', {
        'task_id': task_id, **claim, 'now_ms': time.time_ns() // 1_000_000})
    if not state or state.get('ok') is not True or state.get('cancelRequestedAtMs') != 0:
        raise PermissionError('Detect worker claim is stale or cancelled')


def restore_manual_detect_admission(
    config: Mapping[str, Any], principal: PrincipalContext, conversation_id: str,
) -> tuple[str, str, str] | None:
    """Verify the original binding before building context or registering."""
    if '_projectDispatchAdmission' in config:
        return None
    envelope = config.get(EXECUTION_IDENTITY_CONFIG_KEY, {})
    if _MANUAL_DETECT not in envelope.get(_CAPABILITY_FIELD, ()):
        return None
    binding = envelope[_BINDING_FIELD]
    if (binding['conversationId'] != conversation_id
            or binding['turnId'] != config.get('_turnId')
            or binding['attemptId'] != config.get('_attemptId')):
        raise PermissionError('Detect capability belongs to another conversation Turn or Attempt')
    turn_id, attempt_id = binding['turnId'], binding['attemptId']
    verify_durable_detect(principal, conversation_id, turn_id, attempt_id)
    return conversation_id, turn_id, attempt_id
