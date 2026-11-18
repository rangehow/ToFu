"""Regenerate packages/tofu-core-rs/tests/fixtures/wire_golden.json from Python.

``PYTHONPATH=. .venv/bin/python3 packages/tofu-core-rs/scripts/gen_wire_golden.py``

One synthetic history exercising the ported stages (same-role merge with a
seam hint, blank text-block folding, list content flattening, an orphaned
tool result, a root ``oneOf`` schema, a ``decoded_result_schema`` and
non-ASCII text) goes through ``build_body`` + ``prepare_request`` on the three
wire faces; the sealed payloads are the cargo test's expected bytes.
"""
import base64
import copy
import json
from pathlib import Path

from lib.llm._sse_core import prepare_request
from lib.llm.body import build_body

OUT = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'wire_golden.json'
TOOLS = [
    {'type': 'function', 'function': {'name': 'run_command', 'description': 'Run a shell command \u2014 bounded.',
     'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']}}},
    {'type': 'function', 'function': {'name': 'send', 'description': 'Send to one target.',
     'parameters': {'additionalProperties': False, 'oneOf': [{'required': ['a']}, {'required': ['b']}],
                    'properties': {'a': {'type': 'string'}, 'b': {'type': 'integer'}}, 'type': 'object'}}},
    {'type': 'function', 'decoded_result_schema': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                                                   'description': 'r\u00e9sultat'},
     'function': {'name': 'read_files', 'description': 'Read files.',
                  'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}},
]
MESSAGES = [
    {'role': 'system', 'content': 'You are a test harness.'},
    {'role': 'system', 'content': 'Managed section.', '_isMeta': True},
    {'role': 'user', 'content': [{'type': 'text', 'text': 'Run it '}, {'type': 'text', 'text': '   '},
                                 {'type': 'text', 'text': 'please \u2603'}]},
    {'role': 'user', 'content': '<system-reminder>ambient</system-reminder>', '_isMeta': True},
    {'role': 'assistant', 'content': 'Running.', 'tool_calls': [
        {'id': 'call_a', 'type': 'function', 'function': {'name': 'run_command', 'arguments': '{"command": "true"}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_a', 'content': '$ true\n\n[exit code: 0]'},
    {'role': 'tool', 'tool_call_id': 'call_zzz', 'content': 'orphan'},
    {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_b', 'type': 'function', 'function': {'name': 'read_files', 'arguments': '{"path": "f0.txt"}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_b', 'content': 'File: f0.txt (1 lines)\n   1\tnoop 0\n'},
    {'role': 'user', 'content': 'A private progress event.', '_isMeta': True},
]


def main():
    cases = []
    for protocol in ('openai', 'anthropic', 'responses'):
        body = build_body('golden-model', copy.deepcopy(MESSAGES), max_tokens=4096, tools=copy.deepcopy(TOOLS),
                          provider_id='golden', precomputed_input_tokens=100, context_window=200000)
        plan = prepare_request(body, api_key='sk-golden', base_url='http://127.0.0.1:9/v1', api_protocol=protocol)
        cases.append({'protocol': protocol, 'url': plan.url,
                      'headers': [[k, v] for k, v in plan.hdrs.items() if k != 'M-TraceId'],
                      'payload_b64': base64.b64encode(plan.body.payload).decode()})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'generator': 'packages/tofu-core-rs/scripts/gen_wire_golden.py',
                               'model': 'golden-model', 'max_tokens': 4096, 'temperature': 1.0,
                               'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'sk-golden',
                               'messages': MESSAGES, 'tools': TOOLS, 'cases': cases},
                              indent=1, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
