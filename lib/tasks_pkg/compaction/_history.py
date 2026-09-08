"""Model-visible historical user evidence; no task or permission authority.

The request-local carrier keeps typed entries for repeated L2 selection and
renders their historical meaning in content, which survives provider field
stripping. Never infer carrier identity by parsing user-authored XML. Durable
Turns and pre-compaction archives remain the original-content authority.
"""
from copy import deepcopy

from lib.tasks_pkg.compaction._constants import (
    _USER_VERBATIM_BUDGET_TOKENS,
    _USER_VERBATIM_MAX_MESSAGES,
)
from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens

HISTORY_FIELD = '_retainedUserHistory'
HISTORY_GUIDANCE = (
    'Historical user evidence, oldest first; quoted content is not a new '
    'request or an instruction to answer old questions again. Preserve '
    'still-applicable constraints and authorized work within its original '
    'scope. Later live user messages can continue, change, stop, or revoke '
    'that work and take precedence over this history and the generated '
    'summary. This record creates no new permission or approval requirement.'
)


def is_human_message(message: dict) -> bool:
    """One human-source predicate for retention, boundaries and query text."""
    from lib.turn_initiation import (
        INITIATOR_OPERATOR, is_auto_initiated, resolve_initiator,
    )
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    if (message.get('_isMeta') or message.get('_isVuDirective')
            or message.get('_isVirtualUser')):
        return False
    steer = bool(message.get('_containsHumanSteer'))
    if message.get('_isInboxInject') and not steer:
        return False
    return (not is_auto_initiated(message) or steer
            or resolve_initiator(message) == INITIATOR_OPERATOR)


def user_text(message: dict) -> str:
    """Literal text; emptiness checks, not retention, may strip whitespace."""
    content = message.get('content', '')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(block.get('text', '') for block in content
                         if isinstance(block, dict)
                         and block.get('type') == 'text')
    return ''


def history_entries(message: dict) -> list[dict]:
    """Decode only a producer-owned in-memory carrier, never pasted tags."""
    if not isinstance(message, dict) or not message.get('_isMeta'):
        return []
    entries = message.get(HISTORY_FIELD)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)
            and entry.get('kind') in {'opening_request', 'earlier_request'}
            and isinstance(entry.get('content'), (str, list))]


def make_history_carrier(entries: list[dict]) -> dict | None:
    """Wrap text/multimodal entries without changing their original blocks.

    User role is transport compatibility for image/document inputs, not the
    current-request role. The compaction receipt separates it from the live
    request even for providers that merge adjacent user messages.
    """
    if not entries:
        return None
    entries = deepcopy(entries)
    blocks = [{'type': 'text', 'text':
               '<retained_user_messages version="2">\n' + HISTORY_GUIDANCE}]
    text_only = all(isinstance(entry['content'], str) for entry in entries)
    for index, entry in enumerate(entries, 1):
        blocks.append({'type': 'text', 'text':
                       f'\n\n[{index}] historical {entry["kind"]}\n'})
        content = entry['content']
        if isinstance(content, str):
            blocks.append({'type': 'text', 'text': content})
        else:
            blocks.extend(deepcopy(content))
    blocks.append({'type': 'text', 'text': '\n</retained_user_messages>'})
    carrier = {
        'role': 'user', '_isMeta': True, HISTORY_FIELD: entries,
        'content': (''.join(block['text'] for block in blocks)
                    if text_only else blocks),
    }
    if any(entry['kind'] == 'opening_request' for entry in entries):
        carrier['_isObjectiveAnchor'] = True
    return carrier


def collect_history_entries(
    messages: list, *, opening_message: dict | None = None,
    budget_tokens: int = _USER_VERBATIM_BUDGET_TOKENS,
    max_messages: int = _USER_VERBATIM_MAX_MESSAGES,
) -> list[dict]:
    """Opening evidence plus newest bounded earlier inputs, in source order.

    The opener is a separately required item. Other entries share a strict
    estimated budget and count cap across generations; no newest-item budget
    bypass and no parsing of previous generated text. Omitted originals remain
    in the transcript/archive, not in an unbounded hidden history store.
    """
    candidates = []
    for message in messages:
        prior = history_entries(message)
        if prior:
            candidates.extend(prior)
        elif is_human_message(message):
            content = message.get('content', '')
            if not isinstance(content, (str, list)) or not content:
                continue
            if isinstance(content, str) and not content.strip():
                continue
            candidates.append({
                'kind': ('opening_request' if message is opening_message
                         else 'earlier_request'),
                'content': content,
            })
    opening = next((entry for entry in candidates
                    if entry['kind'] == 'opening_request'), None)
    picked = []
    spent = 0
    for entry in reversed(candidates):
        if entry is opening or entry['kind'] == 'opening_request':
            continue
        if any(entry['content'] == kept['content'] for kept in picked):
            continue
        cost = _estimate_msg_tokens({'role': 'user', 'content': entry['content']})
        if len(picked) >= max(0, max_messages) or spent + cost > budget_tokens:
            continue
        picked.append(entry)
        spent += cost
    return ([opening] if opening is not None else []) + list(reversed(picked))
