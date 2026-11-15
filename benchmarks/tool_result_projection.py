"""Offline fixed-input comparison of native model projections, not paid savings.

python3 benchmarks/tool_result_projection.py --output data/tool-result-projection.json
Both arms use the production capture/budget/delivery path; control bypasses only
project_model_source. Retention is captured by an in-memory test port: no network, models, databases
or provider invoices. Canonical equality and complete model views gate counting.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tools.result_envelope import split_tool_result_delivery
from lib.token_counter import count_text


def corpus():
    ref = 'tool-result:' + 'a' * 64
    other = 'tool-result:' + 'b' * 64
    def page(q, index):
        return {'index': index, 'status': 'ok', 'artifactRef': ref, 'query': q,
                'scope': 'text_only', 'coverage': 'complete', 'items': [],
                'nextCursor': None, 'resumeCursor': '0', 'truncated': False, 'unavailable': []}
    spawn = {'status': 'async_launched', 'swarm_id': 'wave', 'swarm_run_id': 'run',
             'swarm_key': 'owner-conversation', 'is_followup': False,
             'orchestration': {'backend': 'local_swarm', 'mode': 'standard'},
             'agents': [{'id': f'worker-{i}', 'role': 'coder',
                         'objective': 'Read exact evidence and implement safely.\n' * 60,
                         'required_for_completion': True} for i in range(3)],
             'message': 'Continue useful work; completion arrives automatically.'}
    search_empty = {'status': 'ok', 'items': [page('"thinkingDepth"', 0), page('"depth"', 1)]}
    search_match = {**page('depth', 0), 'items': [
        {'artifactRef': ref, 'offset': 12, 'text': 'depth: medium\n完整证据'},
        {'artifactRef': other, 'offset': 9007199254740993, 'text': 'Different child evidence'}]}
    read = {'status': 'ok', 'artifactRef': ref, 'coverage': 'complete', 'scope': 'text_only',
            'sourceCoverage': 'complete', 'retentionCoverage': 'complete',
            'content': '# Report\n' + 'Exact accepted evidence.\n' * 30,
            'offset': 0, 'sizeBytes': 9007199254740993, 'nextCursor': None,
            'resumeCursor': '0', 'truncated': False, 'unavailable': []}
    def child(i, output, status='done'):
        return {'call_id': f'call-{i}', 'name': 'read_files', 'status': status,
                'approval': {'required': False, 'status': 'not_required'},
                'duration': 120, 'source': 'execute_tools', 'output': output,
                'output_truncated': False, 'raw_output_bytes': len(output.encode()),
                'output_bytes': len(output.encode()), 'resultId': f'result-{i}'}
    batch = {'status': 'ok', 'results': [child(i, f'File {i}\n' + 'line\n' * 60) for i in range(3)], 'warnings': []}
    failed = {'status': 'partial_failure', 'results': [child(0, 'Complete\nReport'),
              {'call_id': 'call-1', 'name': 'write_file', 'status': 'rejected',
               'approval': {'required': True, 'status': 'rejected'},
               'error': 'Permission refused\nNo write performed', 'duration': 0,
               'source': 'execute_tools'}], 'warnings': ['Some calls did not execute.']}
    return [('spawn_agents', spawn), ('search_tool_artifact', search_empty),
            ('search_tool_artifact', search_match), ('read_tool_artifact', read),
            ('execute_tools', batch), ('execute_tools', failed)]


def measure():
    records = []
    inputs = []
    for index, (name, value) in enumerate(corpus()):
        source = json.dumps(value, ensure_ascii=False)
        inputs.append([name, value])
        source_hash = hashlib.sha256(source.encode()).hexdigest()

        def retain(canonical, *, user_id):
            assert user_id == 17 and canonical.text() == source
            return 'tool-result:' + source_hash

        with patch('lib.tools.result_value.persist_result', retain):
            with patch('lib.tools.result_surfaces.project_model_source', lambda _name, body: body):
                control_value = budget_tool_result_v2(name, source, user_id=17, model='gpt-4o')
            candidate_value = budget_tool_result_v2(name, source, user_id=17, model='gpt-4o')
        assert control_value.canonical.text() == candidate_value.canonical.text() == source
        control = split_tool_result_delivery(control_value)
        optimized = split_tool_result_delivery(candidate_value)
        assert not control.evidence['truncated'] and not optimized.evidence['truncated'], 'Cannot count clipped data as savings'
        before, after = control.model_text, optimized.model_text
        assert len(after.encode()) < len(before.encode()), f'No improvement for fixed fixture {index}: {name}'
        b, a = len(before.encode()), len(after.encode())
        records.append({'sample': index, 'tool': name, 'source_sha256': source_hash,
                        'canonical_before_sha256': hashlib.sha256(control_value.canonical.text().encode()).hexdigest(),
                        'canonical_after_sha256': hashlib.sha256(candidate_value.canonical.text().encode()).hexdigest(),
                        'control_view_complete': not control.evidence['truncated'],
                        'candidate_view_complete': not optimized.evidence['truncated'],
                        'before_utf8_bytes': b,
                        'after_utf8_bytes': a, 'reduction_percent': round((b-a)*100/b, 2),
                        'before_count_text': count_text(before, model='gpt-4o'),
                        'after_count_text': count_text(after, model='gpt-4o'),
                        'before_sha256': hashlib.sha256(before.encode()).hexdigest(),
                        'after_sha256': hashlib.sha256(after.encode()).hexdigest()})
    before = sum(r['before_utf8_bytes'] for r in records)
    after = sum(r['after_utf8_bytes'] for r in records)
    return {'scope': 'six fixed synthetic native receipts, production capture/budget/delivery',
            'counter': 'lib.token_counter.count_text; local tiktoken if available, otherwise heuristic; not provider billing',
            'tiktoken_module_installed': importlib.util.find_spec('tiktoken') is not None,
            'model_for_counter': 'gpt-4o', 'input_sha256': hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            'provider_wire_verified': False, 'source_authority_changed': False,
            'records': records, 'before_utf8_bytes': before, 'after_utf8_bytes': after,
            'reduction_percent': round((before-after)*100/before, 2),
            'caveats': ['Fixture-size-weighted ratio is not production-wide saving',
                        'No behavior, billing or cache-hit experiment performed',
                        'Original canonical values and decoded program inputs remain unchanged']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = measure()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text+'\n')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
