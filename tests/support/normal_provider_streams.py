"""Synthetic ordinary text/tool SSE shared by CLI and controlled chaos specs."""
from __future__ import annotations

import json


def normal_stream(protocol: str, *, call_id: str, response_id: str,
                  name: str | None = None, fragments: list[str] | None = None,
                  text: str = '') -> bytes:
    fragments = fragments or []
    if protocol == 'openai':
        if name:
            deltas = [{'tool_calls': [{'index': 0, 'id': call_id, 'type': 'function',
                'function': {'name': name, 'arguments': fragments[0]}}]}]
            deltas.extend({'tool_calls': [{'index': 0, 'function': {'arguments': part}}]}
                          for part in fragments[1:])
        else:
            deltas = [{'content': text}]
        events = [{'choices': [{'index': 0, 'delta': delta}]} for delta in deltas]
        events.append({'choices': [{'index': 0, 'delta': {},
            'finish_reason': 'tool_calls' if name else 'stop'}],
            'usage': {'prompt_tokens': 200, 'completion_tokens': 10}})
    else:
        assert protocol == 'openai_responses'
        arguments = ''.join(fragments)
        item = ({'type': 'function_call', 'id': 'fc-' + call_id, 'call_id': call_id,
                 'name': name, 'arguments': arguments, 'status': 'completed'}
                if name else {'type': 'message', 'id': 'msg-' + response_id,
                 'role': 'assistant', 'status': 'completed', 'content': [
                     {'type': 'output_text', 'text': text, 'annotations': []}]})
        events = [{'type': 'response.created', 'response': {
            'id': response_id, 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': {**item, 'arguments': ''} if name else item}]
        if name:
            events.extend({'type': 'response.function_call_arguments.delta',
                'item_id': item['id'], 'output_index': 0, 'delta': part}
                for part in fragments)
            events.append({'type': 'response.function_call_arguments.done',
                'item_id': item['id'], 'output_index': 0, 'arguments': arguments})
        events.extend([
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': {
                'id': response_id, 'status': 'completed', 'output': [item],
                'usage': {'input_tokens': 200, 'output_tokens': 10}}}])
    wire = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
    if protocol == 'openai':
        wire += 'data: [DONE]\n\n'
    return wire.encode()
