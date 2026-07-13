# HOT_PATH
"""Streaming direction: Anthropic SSE events → OpenAI chat.completion chunks.

Holds ``AnthropicSSETranslator``. Depends on the inbound module for the
shared ``_STOP_REASON_MAP`` table and the ``_convert_usage`` helper.
"""

import json

from lib.log import get_logger

from lib.llm.anthropic_outbound._from_anthropic import (
    _STOP_REASON_MAP,
    _convert_usage,
)

logger = get_logger(__name__)


class AnthropicSSETranslator:
    """Translate Anthropic SSE event payloads into OpenAI chat.completion
    chunks. Plugs into ``SSEAccumulator`` like ``CodexSSETranslator``.

    ``translate(data_str)`` accepts the JSON string from one ``data:`` line
    (Anthropic payloads are self-describing via their ``type`` field, so the
    preceding ``event:`` line can be ignored) and returns a list of OpenAI
    chunk dicts and/or the literal ``'[DONE]'`` sentinel.
    """

    def __init__(self, model: str = ''):
        self.model = model
        # content-block index → 'text' | 'tool_use' | 'thinking'
        self._block_types: dict = {}

    def translate(self, data_str: str) -> list:
        try:
            ev = json.loads(data_str)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[AnthropicOut] SSE JSON parse failed: %s', e)
            return []
        etype = ev.get('type', '')

        if etype == 'message_start':
            usage = (ev.get('message') or {}).get('usage') or {}
            if usage:
                return [{'choices': [{'delta': {}}], 'usage': _convert_usage(usage)}]
            return []

        if etype == 'content_block_start':
            idx = ev.get('index', 0)
            block = ev.get('content_block') or {}
            btype = block.get('type')
            self._block_types[idx] = btype
            if btype == 'tool_use':
                return [{'choices': [{'delta': {'tool_calls': [{
                    'index': idx,
                    'id': block.get('id', ''),
                    'type': 'function',
                    'function': {'name': block.get('name', ''), 'arguments': ''},
                }]}}]}]
            return []

        if etype == 'content_block_delta':
            idx = ev.get('index', 0)
            delta = ev.get('delta') or {}
            dtype = delta.get('type')
            if dtype == 'text_delta':
                return [{'choices': [{'delta': {'content': delta.get('text', '')}}]}]
            if dtype == 'thinking_delta':
                return [{'choices': [{'delta': {'reasoning_content': delta.get('thinking', '')}}]}]
            if dtype == 'signature_delta':
                # Opaque signature for the thinking block — required when
                # replaying the thinking block on a later tool-use turn,
                # else the Messages API returns HTTP 400. Surface it as a
                # synthetic OpenAI delta field the accumulator collects.
                return [{'choices': [{'delta': {'thinking_signature': delta.get('signature', '')}}]}]
            if dtype == 'input_json_delta':
                return [{'choices': [{'delta': {'tool_calls': [{
                    'index': idx,
                    'function': {'arguments': delta.get('partial_json', '')},
                }]}}]}]
            return []

        if etype == 'message_delta':
            delta = ev.get('delta') or {}
            chunk = {'choices': [{'delta': {}}]}
            stop = delta.get('stop_reason')
            if stop:
                chunk['choices'][0]['finish_reason'] = _STOP_REASON_MAP.get(stop, 'stop')
            if ev.get('usage'):
                chunk['usage'] = _convert_usage(ev['usage'])
            return [chunk]

        if etype == 'message_stop':
            return ['[DONE]']

        if etype == 'error':
            return [{'error': ev.get('error') or {'message': 'anthropic stream error'}}]

        # ping / content_block_stop / unknown → no-op
        return []
