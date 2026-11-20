"""Executable CLI tranche checks; set TOFU_CORE_RS_BIN to a remotely built binary.

Run: TOFU_CORE_RS_BIN=/path/to/tofu-core-rs .venv/bin/python3 -m pytest -q
     tests/llm/test_core_rs_parity_integration.py
This certifies only the cases below, not general input parity.
"""
import base64
import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def binary():
    path = os.environ.get('TOFU_CORE_RS_BIN')
    if not path:
        pytest.skip('set TOFU_CORE_RS_BIN to a remotely built tofu-core-rs')
    assert Path(path).is_file()
    return path


def request(**extra):
    return dict(model='harness-perf-stub', max_tokens=100, temperature=0.2,
                base_url='http://127.0.0.1:9/v1', api_key='test',
                messages=[{'role': 'user', 'content': 'hello'}],
                protocols=['openai'], **extra)


def invoke(binary, inputs):
    rows = [dict(id=i, boundary=boundary, input=value)
            for i, (boundary, value) in enumerate(inputs)]
    result = subprocess.run([binary, 'parity'], input='\n'.join(map(json.dumps, rows)) + '\n',
                            text=True, capture_output=True, check=True, timeout=30)
    answers = [json.loads(line) for line in result.stdout.splitlines()]
    assert [a['id'] for a in answers] == list(range(len(rows)))
    return answers


def payload(answer, protocol='openai'):
    parts = {p['name']: base64.b64decode(p['b64']) for p in answer['parts']}
    assert parts[f'{protocol}:url'].startswith(b'http://127.0.0.1:9/')
    return json.loads(parts[f'{protocol}:payload'])


def test_parity_entrypoint_propagates_reasoning_and_body_extra(binary):
    value = request(reasoning={'effort': 'high'},
                    body_extra={'tool_choice': {'type': 'function', 'function': {'name': 'read'}},
                                'metadata': {'probe': 'preserved'}},
                    tools=[{'type': 'function', 'function': {'name': 'read',
                           'parameters': {'type': 'object', 'properties': {}}}}])
    value['model'] = 'gpt-5'
    answer = invoke(binary, [('provider_wire', value)])[0]
    wire = payload(answer)
    assert wire['reasoning_effort'] == 'high'
    assert wire['metadata'] == {'probe': 'preserved'}
    assert wire['tool_choice']['function']['name'] == 'read'
    # Compare against byte-identity: both CLI paths must use the same ingress.
    direct = subprocess.run([binary, 'byte-identity'],
                            input=json.dumps(dict(value, protocol='openai')) + '\n',
                            text=True, capture_output=True, check=True, timeout=30)
    assert json.loads(base64.b64decode(json.loads(direct.stdout)['payload_b64'])) == wire


@pytest.mark.parametrize('extra,fragment', [
    ({'oauth': 'codex'}, 'OAuth'),
    ({'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'x'}}]}]}, 'image'),
    ({'messages': [{'role': 'assistant', 'content': 'a', 'reasoning_content': 'private'}]}, 'replay'),
    ({'body_extra': {'_programmatic_tool_calling': 'on', '_programmatic_eligible_tools': True},
      'inference_profile': {'features': {'responses_programmatic': True}}}, 'responses_programmatic'),
    ({'body_extra': {'_multi_agent_mode': 'read_only'},
      'inference_profile': {'features': {'responses_multi_agent': True}}}, 'responses_multi_agent'),
    ({'body_extra': {'_tool_schema_budget_tokens': 1},
      'tools': [{'type': 'function', 'function': {'name': 'read', 'description': 'long schema',
                 'parameters': {'type': 'object', 'properties': {}}}}]}, 'budget'),
])
def test_unported_tranches_refuse_without_parts(binary, extra, fragment):
    value = request()
    value.update(extra)
    if fragment == 'budget':
        value['body_extra']['_executable_tool_catalog'] = value['tools']
    answer = invoke(binary, [('provider_wire', value)])[0]
    assert fragment in answer['unsupported']
    assert 'parts' not in answer
    assert 'error' not in answer


def test_unknown_boundary_refuses_and_next_line_survives(binary):
    answers = invoke(binary, [('sync_snapshot', {}), ('provider_wire', request())])
    assert 'unsupported' in answers[0]
    assert payload(answers[1])['model'] == 'harness-perf-stub'
