#!/usr/bin/env python3
"""Research self-check: headless spawn_agents children survive producer-bound ciphertext.

Runs the real `tofu-agent run` (Python engine) against a loopback Kunlun-face
fake that mints reasoning ciphertext bound to the request's Mt-Session-Id and
rejects any replay on a different account. The route declares
``responses_reasoning_replay`` exactly as the research envelope does. No
network, keys or model spend.

    python3 tests/support/headless_encrypted_replay_selfcheck.py --checkout <stock commit checkout>

Expected on a stock commit with the headless-routing and encrypted-state fixes:

    PASS: 2/2 children completed >= 8 tool rounds through the run envelope
    replay rejections: 0; recovered: 0; child requests: ...

It fails on the pinned research runtime (children die in request-prepare, in
model-routing authority, or at the first rejected replay). Use
--drop-affinity-header to emulate the research relay: the same children must
still complete, with each rejected replay recovered instead of a dead child.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

WORKER_MARK = 'Work only on the delegated objective'


def envelope(base_url: str) -> dict:
    """The TB-harness Responses envelope on the template Kunlun face."""
    return {
        'model_routing': {
            'contract_version': 'tofu.model-routing/v2', 'revision': 0,
            'creators': [{'creator_id': 'openai', 'name': 'openai'}],
            'models': [{'creator_id': 'openai', 'model_id': 'gpt-6.1-sol',
                        'display_name': 'gpt-6.1-sol',
                        'capabilities': ['text', 'thinking', 'tools'],
                        'context_window': 262144, 'quality_rank': 10}],
            'providers': [{'provider_id': 'example-corp', 'name': 'example-corp', 'scope': 'owner'}],
            'provider_accesses': [{'provider_access_id': 'example-corp-access',
                                   'provider_id': 'example-corp', 'enabled': True, 'quota_policy': {}}],
            'connections': [{'connection_id': 'example-corp-kunlun',
                             'provider_access_id': 'example-corp-access', 'base_url': base_url,
                             'protocol': 'openai_responses', 'responses_profile': 'compatible',
                             'enabled': True, 'priority': 0, 'extra_headers': {},
                             'inference_profile': {'features': {
                                 'responses_reasoning_replay': 'supported',
                                 'cache_affinity_mt_session_id': 'supported'}}}],
            'credentials': [{'credential_id': 'example-corp-credential',
                             'provider_access_id': 'example-corp-access', 'kind': 'api_key',
                             'secret_reference': 'example-corp-secret', 'key_hint': 'configured',
                             'enabled': True,
                             'authorization': {'connection_ids': ['example-corp-kunlun'],
                                               'models': [{'creator_id': 'openai',
                                                           'model_id': 'gpt-6.1-sol'}]},
                             'quota_policy': {}}],
            'offerings': [{'offering_id': 'example-corp-offering',
                           'provider_access_id': 'example-corp-access', 'identity_state': 'confirmed',
                           'model': {'creator_id': 'openai', 'model_id': 'gpt-6.1-sol'},
                           'enabled': True, 'capabilities': ['text', 'thinking', 'tools'],
                           'context_window': 262144, 'priority': 0}],
            'deployments': [{'deployment_id': 'example-corp-kunlun-gpt-6.1-sol',
                             'offering_id': 'example-corp-offering',
                             'connection_id': 'example-corp-kunlun',
                             'wire_model_id': 'kunlunbedrock.gpt-6.1-sol-m17', 'enabled': True,
                             'identity_confidence': 'high', 'probe_status': 'passed',
                             'priority': 0, 'max_output_tokens': 8192}],
        },
        'model': {'creator_id': 'openai', 'model_id': 'gpt-6.1-sol'},
        'routing': {'preferred_provider_id': 'example-corp'},
        'credential_secrets': {'example-corp-secret': 'synthetic-not-a-key'},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True)
    parser.add_argument('--python', default='')
    parser.add_argument('--drop-affinity-header', action='store_true')
    parser.add_argument('--tool-rounds', type=int, default=8)
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    checkout = Path(args.checkout).resolve()
    python = args.python or sys.executable
    # Load the fixture from this script's own tree, so the self-check can run
    # against an older checkout that does not contain it.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.support.producer_bound_responses import ProducerBoundKunlun

    fake = ProducerBoundKunlun(drop_affinity_header=args.drop_affinity_header,
                               tool_rounds=args.tool_rounds)
    relay = None
    if args.drop_affinity_header:
        from tests.support.producer_bound_responses import AffinityStrippingRelay
        relay = AffinityStrippingRelay(fake.base_url)
    scratch = tempfile.TemporaryDirectory(prefix='headless-encrypted-selfcheck-')
    try:
        home, data, work = (Path(scratch.name) / name for name in ('home', 'data', 'work'))
        for path in (home, data, work):
            path.mkdir()
        routing = Path(scratch.name) / 'model-routing.json'
        routing.write_text(json.dumps(envelope(relay.base_url if relay else fake.base_url)))
        routing.chmod(0o600)
        result_path = Path(scratch.name) / 'result.json'
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('TOFU_', 'OPENAI', 'ANTHROPIC'))}
        env.update(HOME=str(home), TOFU_DATA_DIR=str(data), PYTHONPATH=str(checkout),
                   PYTHONDONTWRITEBYTECODE='1', NO_PROXY='127.0.0.1,localhost',
                   no_proxy='127.0.0.1,localhost')
        command = [
            python, '-B', '-m', 'tofu_agent', 'run',
            '--task', 'Delegate two workers (child-a, child-b), wait for both, then report.',
            '--model-routing-file', str(routing), '--cwd', str(work), '--timeout-s', '300',
            '--tools', '*', '--thinking', 'medium', '--trajectory', '',
            '--output', str(result_path),
        ]
        completed = subprocess.run(command, cwd=work, env=env, capture_output=True,
                                   text=True, timeout=360)
        if completed.returncode == 2 and '--thinking' in completed.stderr:
            # Older pinned runtimes predate the CLI thinking flag; the route's
            # own medium-effort default then applies.
            command = [part for part in command if part != '--thinking']
            command.remove('medium')
            completed = subprocess.run(command, cwd=work, env=env, capture_output=True,
                                       text=True, timeout=360)
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
    finally:
        if relay is not None:
            relay.close()
        fake.close()
        if not args.keep:
            scratch.cleanup()
    if args.keep:
        print('scratch:', scratch.name)
        print('result:', json.dumps(result)[:1500])
        print('stderr tail:', (completed.stderr or '')[-3000:])
    children = {}
    for row in fake.requests:
        if row['actor'] != 'child':
            continue
        bucket = children.setdefault(row['lineage_key'], {'rounds': 0, 'rejected': 0, 'done': False})
        bucket['rounds'] = max(bucket['rounds'], row['round'])
        bucket['rejected'] += int(bool(row['rejected']))
        bucket['done'] = bucket['done'] or row['final_answer']
    summary = {
        'exit_code': completed.returncode,
        'status': result.get('status'),
        'children': children,
        'rejected_requests': len(fake.rejections),
        'drop_affinity_header': args.drop_affinity_header,
        'tool_rounds_required': args.tool_rounds,
    }
    passed = (completed.returncode == 0 and result.get('status') == 'done'
              and len(children) == 2
              and all(bucket['done'] and bucket['rounds'] >= args.tool_rounds
                      for bucket in children.values()))
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for affinity, bucket in children.items():
            print(f"child {affinity}: rounds={bucket['rounds']} done={bucket['done']} "
                  f"rejected={bucket['rejected']}")
        print(f"replay rejections: {summary['rejected_requests']}")
        print(f"run status: {summary['status']} exit={completed.returncode}")
    if passed:
        print(f"PASS: 2/2 children completed >= {args.tool_rounds} tool rounds through the run envelope")
        return 0
    print('FAIL: headless children did not all complete the required tool rounds')
    if not args.json:
        print((completed.stderr or '')[-2000:])
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
