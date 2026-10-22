"""Offline native-protocol integration probe; mismatch is a coverage failure.

Compare raw history -> actual body/protocol owners with compiler output -> the
same owners. No provider calls, registration or reasoning-capability claims.
"""
import argparse
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from compiler import Compiler, History
from lib.llm.body import build_body
from lib.llm.anthropic_outbound import openai_body_to_anthropic_checked
from lib.llm.responses_outbound import openai_body_to_responses_checked

OWNER = 900001


def project(rows, protocol, profile):
    # Wire-format fixture identifiers; no registered route or provider call.
    # Reasoning is deferred and tokens supplied; this probes continuity only.
    model = 'gpt-5.6-sol' if protocol == 'responses' else 'context-projection-fixture'
    body = build_body(model, rows, max_tokens=64,
                      precomputed_input_tokens=32, context_window=4096,
                      defer_reasoning=True, image_user_id=OWNER)
    if protocol == 'responses':
        body['_responses_feature_profile'] = profile
        return asdict(openai_body_to_responses_checked(body))
    return asdict(openai_body_to_anthropic_checked(body))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    fixtures = {
        'responses': {'role': 'assistant', 'content': 'answer',
                      '_responses_items': [{'type': 'reasoning', 'id': 'fixture-reasoning',
                                            'summary': [], 'encrypted_content': 'opaque-fixture'}]},
        'anthropic': {'role': 'assistant', 'content': 'answer',
                      '_anthropic_content_blocks': [{'type': 'thinking', 'thinking': 'fixture reasoning',
                                                     'signature': 'opaque-fixture-signature'},
                                                    {'type': 'text', 'text': 'answer'}]},
    }
    report = {'scope': 'actual build_body and native protocol projection; synthetic continuity fixtures',
              'whole_system_verdict': 'incomplete', 'cases': [],
              'sources': {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                          for name in ('compiler.py', 'check_provider.py')},
              'production_sources': {name: hashlib.sha256((Path(__file__).resolve().parents[2] / name).read_bytes()).hexdigest()
                                     for name in ('lib/tasks_pkg/wire_messages.py', 'lib/llm/body/_build.py',
                                                  'lib/llm/conversion.py', 'lib/llm/responses_outbound/_to_responses.py',
                                                  'lib/llm/anthropic_outbound/_to_anthropic.py')}}
    for protocol, profile in (('responses', 'openai'), ('responses', 'compatible'), ('anthropic', 'native')):
        assistant = copy.deepcopy(fixtures[protocol])
        rows = [{'role': 'user', 'content': 'question'}, assistant,
                {'role': 'user', 'content': 'continue'}]
        untouched = copy.deepcopy(rows)
        expected = project(rows, protocol, profile)
        if profile == 'compatible':
            assert expected['validation_issues'] == ('mandatory_continuity_unrepresentable',)
            assert expected['payload'] is None
        else:
            assert not expected['validation_issues'] and expected['payload'] is not None
        history = History(OWNER)
        for row in rows:
            history = history.append(row)
        compiler = Compiler(OWNER, 1024 * 1024)
        def verify_revision(stage, rows, history):
            expected = project(rows, protocol, profile)
            compiled = json.loads(compiler.compile(history))
            actual = project(compiled, protocol, profile)
            source_input = history.materialize_provider_input(OWNER)
            prepared = project(source_input, protocol, profile)
            assert prepared == expected, (protocol, profile, 'source IR projection mismatch')
            source_input[0]['content'] = 'caller mutation'
            assert history.materialize_provider_input(OWNER)[0]['content'] == rows[0]['content']
            for foreign in (OWNER + 1, False, 0, str(OWNER), float(OWNER)):
                try:
                    history.materialize_provider_input(foreign)
                except PermissionError:
                    pass
                else:
                    raise AssertionError('foreign source IR owner accepted')
            report['cases'].append({'protocol': protocol, 'profile': profile, 'stage': stage, 'equal': expected == actual,
                                    'source_ir_equal': prepared == expected,
                                    'reference': expected, 'candidate': actual, 'source_ir': prepared})
        try:
            verify_revision('initial', rows, history)
            added = {'role': 'assistant', 'content': 'new answer'}
            rows = rows + [added]
            history = history.append(added)
            verify_revision('append', rows, history)
            edited = copy.deepcopy(assistant)
            if protocol == 'responses':
                edited['_responses_items'][0]['encrypted_content'] = 'edited-opaque-fixture'
            else:
                edited['_anthropic_content_blocks'][0]['signature'] = 'edited-fixture-signature'
            rows = rows[:1] + [edited] + rows[2:]
            history = history.replace(1, edited)
            verify_revision('edit_native_carrier', rows, history)
            summary = {'role': 'user', 'content': 'summary'}
            rows = [summary] + rows[2:]
            history = history.compact(2, summary)
            verify_revision('compact', rows, history)
        finally:
            compiler.close()
        assert assistant == untouched[1]
    report['verdict'] = 'projection_parity' if all(row['equal'] for row in report['cases']) else 'projection_regression'
    report['source_ir_verdict'] = 'native_projection_and_rejection_parity'
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(report['verdict'], 'source IR', report['source_ir_verdict'], 'cases', len(report['cases']))


if __name__ == '__main__':
    main()
