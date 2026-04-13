"""Server-side conversation message builder — replaces frontend buildApiMessages().

Loads raw conversation messages from PostgreSQL and transforms them into
the API-ready format that the LLM orchestrator expects.  This eliminates
the need for the frontend to construct messages — the POST body only needs
``{convId, config}``.

The transformations mirror what the old frontend ``buildApiMessages()`` did:
  1. Inject user system prompt (from config)
  2. Skip endpoint-mode display-only messages (_isEndpointPlanner, _isEndpointReview, _epIteration)
  3. Strip <notranslate>/<nt> tags from user text
  4. Prepend reply quotes
  5. Prepend conversation references
  6. Inline PDF text into user content
  7. Build multimodal image blocks (resolve /api/images/ URLs from disk)
  8. Build toolSummary fallback for empty assistant messages
  9. Merge consecutive same-role messages
"""

from __future__ import annotations

import json
import os
import re

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

# Regex to strip <notranslate> and <nt> wrapper tags
_NT_RE = re.compile(r'</?(?:notranslate|nt)>', re.IGNORECASE)

# Where uploaded images are stored on disk
_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'uploads')


def build_api_messages_from_db(
    conv_id: str,
    config: dict,
    *,
    exclude_last: bool = False,
) -> list[dict] | None:
    """Load conversation messages from DB and build API-ready messages.

    Parameters
    ----------
    conv_id : str
        Conversation ID to load from.
    config : dict
        Task config dict (reads ``systemPrompt``).
    exclude_last : bool
        If True, exclude the last message (used by continueAssistant where
        the last assistant message is the one being regenerated).

    Returns
    -------
    list[dict] | None
        API-ready message list, or None if conversation not found.
    """
    raw_messages = _load_messages_from_db(conv_id)
    if raw_messages is None:
        return None

    return _transform_messages(raw_messages, config, exclude_last=exclude_last)


def _load_messages_from_db(conv_id: str) -> list[dict] | None:
    """Load raw messages from PostgreSQL for a conversation."""
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('[MsgBuilder] conv=%s not found in DB', conv_id[:8])
            return None
        messages = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if not isinstance(messages, list):
            logger.warning('[MsgBuilder] conv=%s messages is not a list: %s',
                           conv_id[:8], type(messages).__name__)
            return None
        return messages
    except Exception as e:
        logger.error('[MsgBuilder] Failed to load conv=%s: %s', conv_id[:8], e, exc_info=True)
        return None


def _transform_messages(
    raw_messages: list[dict],
    config: dict,
    *,
    exclude_last: bool = False,
) -> list[dict]:
    """Transform raw conversation messages into API-ready format.

    This is the server-side equivalent of the frontend's buildApiMessages().
    """
    messages = []

    # 1. System prompt from user settings
    sys_prompt = (config.get('systemPrompt') or '').strip()
    if sys_prompt:
        messages.append({'role': 'system', 'content': sys_prompt})

    # Determine source slice — exclude last message if requested
    src = raw_messages[:-1] if (exclude_last and raw_messages) else raw_messages
    # For normal flow: exclude the trailing assistant message (it's the one being generated)
    # The frontend's buildApiMessages did: conv.messages.slice(0, -1)
    # But here we get the FULL DB state. The frontend pushed the empty assistant msg
    # and then sliced it off. Since the empty assistant is persisted after the task starts,
    # we should exclude trailing empty assistant messages.
    if not exclude_last and src:
        last = src[-1]
        if (last.get('role') == 'assistant'
                and not last.get('content')
                and not last.get('toolSummary')
                and not last.get('toolRounds')):
            src = src[:-1]

    for msg in src:
        # 2. Skip endpoint-mode display-only messages
        #    _isEndpointReview = critic feedback (role=user)
        #    _isEndpointPlanner = planner output (role=assistant)
        #    _epIteration = worker turn output (role=assistant)
        #    These are all display-only; the endpoint orchestrator manages its
        #    own working message list for the LLM (see endpoint.py).
        if msg.get('_isEndpointReview'):
            continue
        if msg.get('_isEndpointPlanner'):
            continue
        if msg.get('_epIteration'):
            continue

        role = msg.get('role', '')

        if role == 'user':
            messages.append(_build_user_message(msg))

        elif role == 'assistant':
            messages.append(_build_assistant_message(msg))

        # Skip other roles (system messages in the middle, etc.)

    # 9. Post-processing: merge consecutive same-role messages
    _merge_consecutive_same_role(messages)

    return messages


def _build_user_message(msg: dict) -> dict:
    """Build a single user message for the API."""
    text_content = msg.get('content') or ''

    # 3. Strip <notranslate>/<nt> wrapper tags
    if '<notranslate>' in text_content or '<nt>' in text_content:
        text_content = _NT_RE.sub('', text_content)

    # 4. Prepend reply quotes
    quotes = msg.get('replyQuotes') or []
    if not quotes and msg.get('replyQuote'):
        quotes = [msg['replyQuote']]
    if quotes:
        if len(quotes) == 1:
            quotes_block = f'[引用]\n{quotes[0]}\n[/引用]'
        else:
            parts = []
            for i, q in enumerate(quotes, 1):
                parts.append(f'[引用{i}]\n{q}\n[/引用{i}]')
            quotes_block = '\n\n'.join(parts)
        text_content = f'{quotes_block}\n\n{text_content}'

    # 5. Prepend conversation references
    conv_ref_texts = msg.get('convRefTexts') or []
    if conv_ref_texts:
        if len(conv_ref_texts) == 1:
            cr = conv_ref_texts[0]
            refs_block = (
                f'[REFERENCED_CONVERSATION title="{cr.get("title", "")}" '
                f'id="{cr.get("id", "")}"]\n{cr.get("text", "")}\n'
                f'[/REFERENCED_CONVERSATION]'
            )
        else:
            parts = []
            for i, cr in enumerate(conv_ref_texts, 1):
                parts.append(
                    f'[REFERENCED_CONVERSATION #{i} title="{cr.get("title", "")}" '
                    f'id="{cr.get("id", "")}"]\n{cr.get("text", "")}\n'
                    f'[/REFERENCED_CONVERSATION]'
                )
            refs_block = '\n\n'.join(parts)
        text_content = (
            f'The user has attached the following conversation(s) for reference:\n\n'
            f'{refs_block}\n\n---\n\n{text_content}'
        )

    # 6. Inline PDF text
    pdf_texts = msg.get('pdfTexts') or []
    for pdf in pdf_texts:
        name = pdf.get('name', 'document.pdf')
        pages = pdf.get('pages', '?')
        text_len = pdf.get('textLength', len(pdf.get('text', '')))
        text = pdf.get('text', '')
        text_content += (
            f'\n\n{"═" * 50}\n'
            f'PDF Document: {name} ({pages} pages, {text_len / 1024:.1f}KB)\n'
            f'{"═" * 50}\n{text}'
        )

    # 7. Build multimodal image blocks
    images = msg.get('images') or []
    has_images = any(img.get('base64') or img.get('url') for img in images)

    if has_images:
        content_blocks = []
        for img in images:
            img_url = ''
            if img.get('base64'):
                media_type = img.get('mediaType', 'image/png')
                img_url = f'data:{media_type};base64,{img["base64"]}'
            elif img.get('url'):
                # Pass through — backend _validate_image_blocks resolves
                # local /api/images/ URLs from disk
                img_url = img['url']

            if img_url:
                content_blocks.append({
                    'type': 'image_url',
                    'image_url': {'url': img_url},
                })
                if img.get('caption'):
                    content_blocks.append({
                        'type': 'text',
                        'text': f'[PDF p{img.get("pdfPage", "?")}: {img["caption"]}]',
                    })
                elif img.get('pdfPage'):
                    content_blocks.append({
                        'type': 'text',
                        'text': f'[PDF page {img["pdfPage"]}/{img.get("pdfTotal", "?")}]',
                    })

        if text_content:
            content_blocks.append({'type': 'text', 'text': text_content})
        return {'role': 'user', 'content': content_blocks}
    else:
        return {'role': 'user', 'content': text_content}


def _build_assistant_message(msg: dict) -> dict:
    """Build a single assistant message for the API."""
    # 8. Build toolSummary fallback for empty assistant messages
    tool_ctx = ''
    if msg.get('toolSummary'):
        tool_ctx = msg['toolSummary']
    else:
        rounds = msg.get('toolRounds') or []
        if rounds:
            calls = []
            for r in rounds:
                call = {'name': r.get('toolName', 'unknown')}
                if r.get('toolArgs'):
                    args = r['toolArgs']
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = None
                    if isinstance(args, dict):
                        call.update(args)
                    elif args is not None:
                        logger.debug('Skipping non-dict toolArgs type=%s', type(args).__name__)
                elif r.get('query'):
                    call['query'] = r['query']
                calls.append(call)
            try:
                tool_ctx = json.dumps(calls, ensure_ascii=False)
            except (TypeError, ValueError):
                tool_ctx = str(calls)

    # Never skip assistant messages — use tool summary as placeholder
    content = msg.get('content') or tool_ctx
    return {'role': 'assistant', 'content': content}


def _merge_consecutive_same_role(messages: list) -> None:
    """Merge consecutive same-role messages in-place.

    After filtering out endpoint-mode messages (_isEndpointPlanner,
    _isEndpointReview, _epIteration), there may still be consecutive
    same-role messages from normal conversation flow. Merge by concatenation.
    """
    i = len(messages) - 1
    while i > 0:
        curr = messages[i]
        prev = messages[i - 1]
        if (curr.get('role') == prev.get('role')
                and curr.get('role') in ('user', 'assistant')):
            prev_content = prev.get('content', '') or ''
            curr_content = curr.get('content', '') or ''
            # Handle multimodal content (arrays)
            if isinstance(prev_content, list) or isinstance(curr_content, list):
                if isinstance(prev_content, str):
                    prev_content = [{'type': 'text', 'text': prev_content}] if prev_content else []
                if isinstance(curr_content, str):
                    curr_content = [{'type': 'text', 'text': curr_content}] if curr_content else []
                messages[i - 1] = dict(prev)
                messages[i - 1]['content'] = prev_content + curr_content
            else:
                sep = '\n\n' if prev_content and curr_content else ''
                messages[i - 1] = dict(prev)
                messages[i - 1]['content'] = prev_content + sep + curr_content
            messages.pop(i)
        i -= 1
