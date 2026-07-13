# HOT_PATH
"""Outbound direction: OpenAI Chat Completions body → Anthropic Messages body.

Contains the block/tool/message converters and the top-level
``openai_body_to_anthropic`` entry point. Depends only on stdlib + the
``lib.llm.body`` image sniffer; no dependency on the inbound module.
"""

import json

from lib.log import get_logger

logger = get_logger(__name__)


def _media_type_and_data(url: str):
    """Split a ``data:`` URI into (media_type, base64_data). Returns None on
    a non-data URL.

    Boundary reconciliation: this is the LAST transform before the Anthropic
    Messages API, which HARD-REJECTS a request whose declared ``media_type``
    disagrees with the actual image bytes (HTTP 400 "messages.N.content.0.
    image.source.base64: The image was specified using the image/jpeg media
    type, but the image does not appear to be in that format."). Every upstream
    feeder (``build_body`` → ``_validate_image_blocks``, both dispatch swap-path
    branches) already reconciles, but to make the strict path self-consistent
    REGARDLESS of which seam fed it, we sniff the real MIME from the base64
    prefix here and prefer it over the declared header. Best-effort: if the
    bytes can't be decoded/recognized, keep the declared header unchanged.
    """
    if not url.startswith('data:'):
        return None
    try:
        header, data = url.split(',', 1)
        media_type = header[len('data:'):].split(';', 1)[0] or 'image/png'
        try:
            import base64 as _b64

            from lib.llm.body import sniff_image_mime
            _sample = _b64.b64decode(data[:1364])
            _true = sniff_image_mime(_sample)
            if _true and _true != media_type:
                logger.warning(
                    '[AnthropicOut] Reconciled image media type %r → %r at the '
                    'Anthropic boundary (bytes sniffed as %s)',
                    media_type, _true, _true)
                media_type = _true
        except Exception as _se:
            logger.debug('[AnthropicOut] media-type sniff skipped: %s', _se)
        return media_type, data
    except (ValueError, IndexError) as e:
        logger.debug('[AnthropicOut] malformed data URI: %s', e)
        return None


def _image_block(image_url: dict) -> dict:
    """OpenAI image_url block → Anthropic image block."""
    url = (image_url or {}).get('url', '')
    parsed = _media_type_and_data(url)
    if parsed:
        media_type, data = parsed
        return {'type': 'image',
                'source': {'type': 'base64', 'media_type': media_type, 'data': data}}
    return {'type': 'image', 'source': {'type': 'url', 'url': url}}


def _convert_content_blocks(content) -> list:
    """OpenAI message content (str | list) → Anthropic content blocks."""
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get('type')
        cc = block.get('cache_control')
        if btype == 'text':
            nb = {'type': 'text', 'text': block.get('text', '')}
            if cc:
                nb['cache_control'] = cc
            out.append(nb)
        elif btype == 'image_url':
            nb = _image_block(block.get('image_url', {}))
            if cc:
                nb['cache_control'] = cc
            out.append(nb)
    return out


def _convert_tools(tools) -> list:
    """OpenAI function tools → Anthropic tools (cache_control hoisted out of
    the nested ``function`` object)."""
    out = []
    for tool in tools or []:
        fn = tool.get('function') if isinstance(tool, dict) else None
        if not fn:
            continue
        at = {
            'name': fn.get('name', ''),
            'description': fn.get('description', ''),
            'input_schema': fn.get('parameters') or {'type': 'object', 'properties': {}},
        }
        if fn.get('cache_control'):
            at['cache_control'] = fn['cache_control']
        out.append(at)
    return out


def _assistant_blocks(msg: dict) -> list:
    """Assistant message → Anthropic content blocks (thinking + text + tool_use).

    When the message carries both ``reasoning_content`` and its opaque
    ``thinking_signature`` (captured from the stream and round-tripped on
    Continue), the thinking block is re-emitted FIRST so the Messages API
    can verify tool-use continuity. A ``reasoning_content`` without a
    signature is dropped: sending a thinking block without its signature
    returns HTTP 400, so a lossy "fresh reasoning" continuation is better.
    """
    blocks = []
    th_text = msg.get('reasoning_content') or ''
    th_sig = msg.get('thinking_signature') or ''
    if th_text and th_sig:
        blocks.append({'type': 'thinking', 'thinking': th_text, 'signature': th_sig})
    blocks.extend(_convert_content_blocks(msg.get('content') or ''))
    for tc in msg.get('tool_calls') or []:
        fn = tc.get('function', {})
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[AnthropicOut] tool_call args JSON decode failed: %s', e)
            args = {}
        blocks.append({
            'type': 'tool_use',
            'id': tc.get('id') or '',
            'name': fn.get('name', ''),
            'input': args,
        })
    return blocks


def openai_body_to_anthropic(body: dict) -> dict:
    """Translate a built OpenAI Chat Completions body to an Anthropic body.

    Preserves model / max_tokens / stream / temperature / top_p / thinking /
    effort (build_body already produces Claude-correct thinking params).
    Hoists ``system`` out of the message list and merges consecutive
    tool-result messages into a single Anthropic user turn.
    """
    out = {
        'model': body.get('model', ''),
        'max_tokens': body.get('max_tokens', 4096),
    }
    if body.get('stream'):
        out['stream'] = True

    # Carry model-level params Anthropic accepts (build_body already pruned
    # the ones Opus 4.7+ rejects, e.g. temperature/top_p/top_k).
    for k in ('temperature', 'top_p', 'top_k', 'stop_sequences', 'thinking',
              'effort', 'tool_choice'):
        if k in body:
            out[k] = body[k]

    tools = _convert_tools(body.get('tools'))
    if tools:
        out['tools'] = tools

    system_blocks = []
    messages = []
    for msg in body.get('messages', []):
        role = msg.get('role')
        if role == 'system':
            system_blocks.extend(_convert_content_blocks(msg.get('content') or ''))
        elif role == 'tool':
            block = {
                'type': 'tool_result',
                'tool_use_id': msg.get('tool_call_id', ''),
                'content': msg.get('content') if isinstance(msg.get('content'), str)
                else (_convert_content_blocks(msg.get('content')) or ''),
            }
            # Merge into the preceding user turn when it's a tool_result batch.
            if (messages and messages[-1]['role'] == 'user'
                    and isinstance(messages[-1]['content'], list)
                    and messages[-1]['content']
                    and messages[-1]['content'][0].get('type') == 'tool_result'):
                messages[-1]['content'].append(block)
            else:
                messages.append({'role': 'user', 'content': [block]})
        elif role == 'assistant':
            blocks = _assistant_blocks(msg)
            if not blocks:
                blocks = [{'type': 'text', 'text': ''}]
            messages.append({'role': 'assistant', 'content': blocks})
        elif role == 'user':
            blocks = _convert_content_blocks(msg.get('content') or '')
            if not blocks:
                blocks = [{'type': 'text', 'text': ''}]
            messages.append({'role': 'user', 'content': blocks})

    if system_blocks:
        # A single plain-text system block can stay a string; keep the block
        # list when cache_control is present so caching survives.
        if len(system_blocks) == 1 and not system_blocks[0].get('cache_control'):
            out['system'] = system_blocks[0].get('text', '')
        else:
            out['system'] = system_blocks
    out['messages'] = messages
    return out
