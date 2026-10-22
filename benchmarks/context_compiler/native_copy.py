"""Isolate one native-sidecar copy site in actual request construction.

Canonical public copy, extras/intent, token/body/protocol owners are unchanged.
No module-global patch, model registration, provider I/O or product default.
"""
import argparse
import ast
import copy
from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path

from check_provider import OWNER
from lib.llm.body import _build
from lib.llm_sanitize._fields import _copy_wire_value
from lib.llm.anthropic_outbound import openai_body_to_anthropic_checked
from lib.llm.responses_outbound import openai_body_to_responses_checked


def candidate_builder():
    source = inspect.getsource(_build.build_body)
    boundary = '_clean_msg[_sidecar] = copy.deepcopy(_source_msg[_sidecar])'
    assert source.count(boundary) == 1
    source = source.replace(boundary, '_clean_msg[_sidecar] = _copy_wire_value(_source_msg[_sidecar])')
    namespace = dict(_build.__dict__); namespace['_copy_wire_value'] = _copy_wire_value
    tree = ast.parse(source)
    exec(compile(tree, '<native-sidecar-copy-experiment>', 'exec'), namespace)
    assert _build.build_body.__globals__['copy'] is copy
    return namespace['build_body']


def project(rows, profile, builder):
    protocol = 'anthropic' if profile == 'anthropic' else 'responses'
    body = builder('context-copy-fixture' if protocol == 'anthropic' else 'gpt-5.6-sol', rows,
                   max_tokens=64, precomputed_input_tokens=32, context_window=4096,
                   defer_reasoning=True, image_user_id=OWNER)
    if protocol == 'responses':
        body['_responses_feature_profile'] = profile
        return asdict(openai_body_to_responses_checked(body))
    return asdict(openai_body_to_anthropic_checked(body))


def fixture(profile, turns=128):
    rows = []
    for index in range(turns):
        assistant = {'role': 'assistant', 'content': f'answer {index}'}
        if profile == 'anthropic':
            assistant['_anthropic_content_blocks'] = [
                {'type': 'thinking', 'thinking': f'fixture reasoning {index}', 'signature': f'opaque-signature-{index}'},
                {'type': 'text', 'text': f'answer {index}'}]
        else:
            assistant['_responses_items'] = [{'type': 'reasoning', 'id': f'fixture-reasoning-{index}',
                                             'summary': [], 'encrypted_content': f'opaque-reasoning-{index}'}]
        rows.extend([{'role': 'user', 'content': f'question {index}'}, assistant])
    return rows+[{'role': 'user', 'content': 'continue'}]


def check():
    candidate = candidate_builder(); cases = []
    for profile in ('openai', 'compatible', 'anthropic'):
        rows = fixture(profile, 8); original = copy.deepcopy(rows)
        expected = project(rows, profile, _build.build_body)
        assert project(rows, profile, candidate) == expected
        assert rows == original
        if profile == 'compatible':
            assert expected['payload'] is None
            assert expected['validation_issues'] == ('mandatory_continuity_unrepresentable',)
        else:
            assert expected['payload'] is not None and not expected['validation_issues']
        cases.append({'profile': profile, 'native_payload_rejection_receipt_parity': True})
    shared = {'value': 'original'}; cyclic = [shared, shared]; cyclic.append(cyclic)
    rows = [{'role': 'user', 'content': 'question'},
            {'role': 'assistant', 'content': 'answer', '_responses_items': cyclic},
            {'role': 'user', 'content': 'continue'}]
    kwargs = dict(max_tokens=64, precomputed_input_tokens=32, context_window=4096,
                  defer_reasoning=True, image_user_id=OWNER)
    for builder in (_build.build_body, candidate):
        body = builder('gpt-5.6-sol', rows, **kwargs)
        detached = body['messages'][1]['_responses_items']
        assert detached is not cyclic and detached[2] is detached
        assert detached[0] is detached[1] and detached[0] is not shared
        detached[0]['value'] = 'caller mutation'; assert shared['value'] == 'original'
    calls = []
    class SuccessfulHook:
        def __deepcopy__(self, memo):
            calls.append('copy')
            result = {'hook': 'preserved'};memo[id(self)] = result
            return result
    hook = SuccessfulHook();rows[1]['_responses_items'] = [hook, hook]
    for builder in (_build.build_body, candidate):
        calls.clear()
        body = builder('gpt-5.6-sol', rows, **kwargs)
        result = body['messages'][1]['_responses_items']
        assert result == [{'hook': 'preserved'}, {'hook': 'preserved'}]
        assert result[0] is result[1] and calls == ['copy']
    class Hook:
        def __deepcopy__(self, memo):
            raise OSError('injected native carrier copy failure')
    rows[1]['_responses_items'] = [Hook()]
    for builder in (_build.build_body, candidate):
        try:
            builder('gpt-5.6-sol', rows, **kwargs)
        except OSError as exc:
            assert str(exc) == 'injected native carrier copy failure'
        else:
            raise AssertionError('custom copy failure swallowed')
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    names = ('benchmarks/context_compiler/native_copy.py', 'lib/llm/body/_build.py',
             'lib/llm_sanitize/_fields.py', 'lib/llm/responses_outbound/_to_responses.py',
             'lib/llm/anthropic_outbound/_to_anthropic.py')
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete',
              'sources': {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in names},
              'checks': check(), 'cycles_aliases_mutation_hooks_and_failure_parity': True,
              'performance_admission': False}
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print('actual native payload/rejection/receipt parity; cycles/aliases/detachment/custom-hook failure preserved; one local copy seam only')


if __name__ == '__main__':
    main()
