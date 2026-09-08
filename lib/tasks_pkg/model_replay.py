"""Recover a validated, previously sent model view across task boundaries.

Turn rows remain the sole transcript authority. Validated completed chat
continuations are durable derived state, one bounded checkpoint per lane.
Edits and incompatible permissions/sources force an observable cold rebuild;
process restarts and disposable cache eviction do not erase compaction work.
Preparation happens before Turn settlement; Sidecar publishes in its transaction.
"""
from __future__ import annotations

import hashlib
import json

from lib.log import get_logger
from lib.storage.model_continuation import ModelContinuationStore, settled_witness

logger = get_logger(__name__)
VERSION = 1
MAX_TURN_WITNESSES = 4096
_POLICY_FIELDS = (
    'model', 'provider_id', 'providerId', 'preset', 'thinkingDepth',

    'modelRef', 'preferredProviderId', 'routing',
    'systemPrompt', 'systemPromptMode', 'systemPromptBlocks', 'responseFormat',
    'projectPath', 'projectEnabled', 'memoryEnabled', 'searchEnabled',
    'grantedToolPermissions', 'tools', 'enabledTools', 'disabledTools',
    'mcpServers', 'programmaticTools', 'multiAgent', 'contextExperimentFlags',
    'chatMode', 'interactionMode', 'thinkingEnabled', 'defaultThinkingDepth',
    'searchMode', 'fetchEnabled', 'codeExecEnabled', 'browserEnabled',
    'desktopEnabled', 'imageGenEnabled', 'humanGuidanceEnabled', 'schedulerEnabled',
    '_explicitToolSchemas', '_principalContext',
)



def _digest(value):
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256()
    for part in encoder.iterencode(value):
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def turn_witness(row):
    return settled_witness(row)


def policy_witness(config):
    from lib.context_experiment_flags import normalize_context_experiment_flags
    return _digest([VERSION, {key: config.get(key) for key in _POLICY_FIELDS},
                    normalize_context_experiment_flags(config)])


_ROUTE_FIELDS = frozenset({'model', 'provider_id', 'providerId', 'preset', 'thinkingDepth'})


def portable_policy_witness(config):
    """Model choice may change; policy, permissions and source scope may not."""
    return policy_witness({key: value for key, value in config.items()
                           if key not in _ROUTE_FIELDS})


def effort_policy_witness(config):
    """Same adaptive Opus and requested policy; only enabled effort may differ.

    Effort changes upstream cache rendering, not validity of prior thinking.
    Keep the full continuation rather than treating this as cross-model
    migration. Off/mode changes, other families and old checkpoints retain
    the conservative rebuild path. One fixed-size witness adds no payload copy.
    """
    from lib.model_info import is_claude_opus_47
    if (not is_claude_opus_47(config.get('model') or '')
            or config.get('thinkingEnabled') is False
            or (config.get('thinkingDepth') or config.get('defaultThinkingDepth')) == 'off'):
        return ''
    return policy_witness({key: value for key, value in config.items()
                           if key not in ('thinkingDepth', 'defaultThinkingDepth')})


def _portable_messages(messages):
    """Detach a text-only continuation; opaque/provider-bound state cannot migrate.

    Refuse multimodal history rather than silently losing evidence. Composer
    carriers are rebuilt for the destination model; user policy remains guarded
    by portable_policy_witness. Ordinary call/result text keeps its exact bytes.
    """
    import copy
    projected = []
    for message in messages:
        if message.get('_contextComposer'):
            continue
        if not isinstance(message.get('content', ''), (str, type(None))):
            return None
        if any(message.get(key) for key in (
                'reasoning_content', 'reasoning', 'thinking',
                '_responses_items', '_responsesOutputItems', '_modelImageSources')):
            return None
        # Unknown private provider state is not portable. These narrow flags
        # describe local text provenance only.
        allowed_private = {'_isMeta', '_isCompactionSummary', '_compactionArchiveId',
                           '_estimatedPromptTokens', '_attachmentTextChars'}
        if any(str(key).startswith('_') and key not in allowed_private for key in message):
            return None
        projected.append(copy.deepcopy(message))
    return projected if projected and projected[-1].get('role') == 'assistant' else None


def _publication_status(task, reason):
    task['_modelReplayPublishStatus'] = reason
    logger.info('[ModelReplay] conv=%s checkpoint=%s',
                str(task.get('convId') or '')[:8], reason)


def _eligible(config):
    return not any(config.get(key) for key in (
        'flowId', 'flowBuiltin', 'flowDefinition',
        'autopilot', 'toolHistory', 'checkpointToolRounds', 'contentPrefix',
    ))


def restore_prefix(rows, config, *, conversation_id, user_id):
    """Return (retained model messages, number of covered Turn rows)."""
    config.pop('_modelReplayBase', None)
    config.pop('_modelReplayToolLatch', None)
    config['_modelReplayStatus'] = 'cold'
    if not rows or not _eligible(config):
        config['_modelReplayStatus'] = 'empty_history' if not rows else 'unsupported_mode'
        return [], 0
    if len(rows) > MAX_TURN_WITNESSES:
        config['_modelReplayStatus'] = 'over_budget'
        return [], 0
    # excludeLast is ordinary command-service plumbing for the empty output
    # target. Resume payloads are rejected above and nonempty targets below.
    target = rows[-1]
    if target.get('projection', {}).get('content') or target.get('projection', {}).get('toolRounds'):
        config['_modelReplayStatus'] = 'resume_target'
        return [], 0
    witnesses = [turn_witness(row) for row in rows[:-1]]
    config['_modelReplayBase'] = witnesses
    config['_modelReplayPolicy'] = policy_witness(config)
    config['_modelReplayPortablePolicy'] = portable_policy_witness(config)
    config['_modelReplayEffortPolicy'] = effort_policy_witness(config)
    try:
        cached = ModelContinuationStore().get(user_id=user_id, conversation_id=conversation_id,
                                       lane_id=target.get('laneId') or 'main')
        if not isinstance(cached, dict):
            config['_modelReplayStatus'] = 'checkpoint_missing'
            return [], 0
        same_policy = cached.get('policy') == policy_witness(config)
        effort_policy = config['_modelReplayEffortPolicy']
        same_effort_policy = bool(effort_policy and cached.get('effortPolicy') == effort_policy)
        same_prefix_policy = same_policy or same_effort_policy
        portable = (not same_prefix_policy and cached.get('portablePolicy')
                    == portable_policy_witness(config))
        if not same_prefix_policy and not portable:
            config['_modelReplayStatus'] = 'policy_changed'
            return [], 0
        prior = cached.get('turns') or []
        if not prior or witnesses[:len(prior)] != prior:
            config['_modelReplayStatus'] = 'authority_changed'
            return [], 0
        messages = cached.get('messages')
        if not isinstance(messages, list) or not messages or messages[-1].get('role') != 'assistant':
            config['_modelReplayStatus'] = 'invalid_checkpoint'
            return [], 0
        if portable:
            messages = _portable_messages(messages)
            if messages is None:
                config['_modelReplayStatus'] = 'nonportable_checkpoint'
                return [], 0
        from lib.knowledge import read_source_path
        for message in messages:
            for source in message.get('_modelImageSources') or []:
                if source.startswith('tofu-media:') and read_source_path(
                        source[len('tofu-media:'):], user_id=user_id) is None:
                    config['_modelReplayStatus'] = 'media_unavailable'
                    return [], 0
        # Attachment ownership/readiness is rechecked even though the text
        # projection remains pinned. A removed source must never be recovered
        # solely from an old cache file.
        from lib.media_attachments import resolve_client_refs
        for row in rows[:len(prior)]:
            refs = (row.get('projection') or {}).get('attachments') or []
            if refs and resolve_client_refs(refs, user_id=user_id) != refs:
                config['_modelReplayStatus'] = 'media_changed'
                return [], 0
        from lib.mcp.tool_search import freeze_wire_definitions, mcp_selection_scope_id
        if same_prefix_policy and isinstance(cached.get('mcpWire'), list):
            freeze_wire_definitions(mcp_selection_scope_id(
                task_id='', conv_id=conversation_id, owner_user_id=user_id), cached['mcpWire'])
        if same_prefix_policy and isinstance(cached.get('toolLatch'), dict):
            config['_modelReplayToolLatch'] = cached['toolLatch']
        config['_modelReplayStatus'] = (
            'restored_portable' if portable else
            'restored_effort' if not same_policy and same_effort_policy else 'restored')
        return messages, len(prior)
    except Exception as exc:
        config['_modelReplayStatus'] = 'unavailable'
        logger.warning('[ModelReplay] cold rebuild: %s', type(exc).__name__)
        return [], 0


def prepare_completed_task(task):
    """Prepare a bounded candidate; only Sidecar may mint its settled witness."""
    config = task.get('config') or {}
    base = config.get('_modelReplayBase')
    owner = task.get('_userId')
    conv_id, turn_id = task.get('convId'), task.get('_turnId')
    checks = (
        ('missing_base', not isinstance(base, list)),
        ('missing_identity', not owner or not conv_id or not turn_id),
        ('unsupported_mode', not _eligible(config)),
        ('failed_or_aborted', task.get('error') or task.get('aborted')),
        ('steered_history', task.get('_userSteerInjects')),
        ('multi_turn_run', task.get('_turnVisibleRunTurnIds')),
    )
    for reason, blocked in checks:
        if blocked:
            _publication_status(task, reason)
            return
    try:
        messages = task.get('messages') or []
        if not messages or messages[-1].get('role') != 'assistant':
            _publication_status(task, 'missing_final_model_message')
            return
        # A length/incomplete terminal may retain a complete text boundary,
        # but an unfinished tool batch must never become a reusable prefix.
        if messages[-1].get('tool_calls') or messages[-1].get('function_call'):
            _publication_status(task, 'unfinished_tool_batch')
            return
        if config.get('model') and task.get('model') != config['model']:
            _publication_status(task, 'model_changed_during_run')
            return
        from lib.mcp.tool_search import frozen_wire_definitions, mcp_selection_scope_id
        mcp_wire = frozen_wire_definitions(mcp_selection_scope_id(
            task_id='', conv_id=conv_id, owner_user_id=owner))
        from lib.storage.model_continuation import encode_projection
        candidate = {'policy': config.get('_modelReplayPolicy') or policy_witness(config),
                     'portablePolicy': (config.get('_modelReplayPortablePolicy')
                                        or portable_policy_witness(config)),
                     'effortPolicy': config.get('_modelReplayEffortPolicy',
                                              effort_policy_witness(config)),
                     'turns': base + [[turn_id, '0' * 64]], 'messages': messages,
                     'mcpWire': mcp_wire,
                     'toolLatch': task.get('_toolOrchestrationPromptLatch')}
        encoded = encode_projection(candidate)
        if encoded is None:
            _publication_status(task, 'invalid_or_over_budget')
            return
        _publication_status(task, 'prepared')
        # Detach mutable executor messages before command serialization/retries.
        return {'projection': json.loads(encoded), 'content': task.get('content', '')}
    except Exception as exc:
        logger.warning('[ModelReplay] checkpoint unavailable: %s', type(exc).__name__)
        _publication_status(task, 'unavailable')


def publish_completed_task(task):
    """Explicit settled import compatibility seam; live lifecycle never calls this."""
    candidate = prepare_completed_task(task)
    if candidate is None:
        return
    try:
        from lib.storage import get_storage_client
        target = get_storage_client().query('turn.get', {
            'conversation_id': task['convId'], 'turn_id': task['_turnId'],
            'user_id': task['_userId']}, deadline=2.0)
        if not target or target.get('status') != 'completed':
            _publication_status(task, 'turn_not_completed')
            return
        if (target.get('projection') or {}).get('content') != candidate['content']:
            _publication_status(task, 'settled_content_changed')
            return
        projection = candidate['projection']
        projection['turns'][-1] = turn_witness(target)
        stored = ModelContinuationStore().put(
            user_id=task['_userId'], conversation_id=task['convId'],
            lane_id=target.get('laneId') or 'main', projection=projection)
        _publication_status(task, 'stored' if stored else 'rejected')
    except Exception:
        _publication_status(task, 'unavailable')
