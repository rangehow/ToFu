"""Model-context message identity and counting; no storage or transport I/O.

Public readers classify producer-owned metadata, never text or XML. History
projection preserves these fields until protocol compilation strips them.
Transport role is not source identity: automatic context often travels as user.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Any

from lib.turn_initiation import INITIATOR_HUMAN, resolve_initiator


SOURCE_FIELDS = (
    '_turnId', '_sourceActor', '_initiator', '_isMeta', '_contextComposer',
    '_isVuDirective', '_isVirtualUser', '_isInboxInject', '_containsHumanSteer',
)


def is_request_message(message: Any) -> bool:
    """The request a model context answers, excluding historical/context carriers.

    That is human input, operator steering, and a durable Turn's own input
    (``_turnId``) whoever initiated it: a Worker's brief, a relayed notice or a
    timer prompt is the request its Turn answers. Context retention, history
    carriers and memory selection anchor on it. Whether it is the owner's own
    words is ``is_human_message``; authority reads the stored Turn
    (``lib.turn_initiation.require_human_turn``).
    """
    if not isinstance(message, Mapping) or message.get('role') != 'user':
        return False
    if any(message.get(key) for key in (
            '_isMeta', '_contextComposer', '_isVuDirective', '_isVirtualUser')):
        return False
    if message.get('_containsHumanSteer'):
        return True
    if message.get('_isInboxInject'):
        return False
    if message.get('_sourceActor', 'human') not in ('human', 'operator'):
        return False
    return bool(message.get('_turnId')) or resolve_initiator(message) in ('human', 'operator')


def is_human_message(message: Any) -> bool:
    """Human requests/operator steering, excluding program-initiated input and carriers."""
    return is_request_message(message) and (
        bool(message.get('_containsHumanSteer')) or resolve_initiator(message) in ('human', 'operator'))


def preserve_source(source: Mapping, target: dict) -> dict:
    """Copy only declared source identity, never arbitrary private fields.

    A stored Turn keeps its initiator only as ``origin.initiator`` (the
    transient ``_initiator`` stamp is folded into it), and ``origin`` itself is
    not model context, so the initiator is carried over as ``_initiator``.
    """
    for key in SOURCE_FIELDS:
        if key in source:
            target[key] = source[key]
    if '_initiator' not in target:
        initiator = resolve_initiator(source)
        if initiator != INITIATOR_HUMAN:
            target['_initiator'] = initiator
    return target


def same_source_boundary(left: Mapping, right: Mapping) -> bool:
    """Legacy messages may merge; explicit Turn/source boundaries must survive."""
    if left.get('_turnId') or right.get('_turnId'):
        return False
    return all(left.get(key) == right.get(key) for key in SOURCE_FIELDS)


@dataclass(frozen=True, slots=True)
class MessageCounts:
    """Explicit units; wire item and durable Turn counts belong to their owners."""
    model_messages: int
    human_messages: int
    tool_results: int
    tool_batches: int


def count_messages(messages) -> MessageCounts:
    model = human = results = batches = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        model += 1
        human += is_human_message(message)
        results += message.get('role') == 'tool'
        batches += bool(message.get('role') == 'assistant' and message.get('tool_calls'))
    return MessageCounts(model, human, results, batches)


def has_model_media(message: Mapping) -> bool:
    """Conservative external-source gate for revision-only history validation.

    Text block arrays and native assistant reasoning/phase remain text context.
    Media/attachment-bearing messages retain the full authority/media read path.
    """
    if message.get('_modelImageSources') or message.get('attachments'):
        return True
    content = message.get('content')
    if not isinstance(content, (str, type(None))):
        if not isinstance(content, list) or any(
                not isinstance(block, dict) or block.get('type') not in ('text', 'input_text', 'output_text')
                for block in content):
            return True
    return any(has_nested_model_media(message.get(key))
               for key in ('_responses_items', '_anthropic_content_blocks'))


def has_nested_model_media(value) -> bool:
    """Detect structured media even inside retained tool results or segments."""
    def media(value):
        if isinstance(value, dict):
            if value.get('type') in ('image', 'image_url', 'input_image', 'document',
                                     'input_file', 'file', 'input_audio', 'audio'):
                return True
            return any(media(item) for item in value.values())
        if isinstance(value, list):
            return any(media(item) for item in value)
        return False
    return media(value)
