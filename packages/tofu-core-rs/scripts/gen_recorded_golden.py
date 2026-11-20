"""Python oracle digests for the existing 25 recorded requests on three faces.

No transcript or payload is duplicated: inputs remain in the existing normalized
LLM replay corpus. --check requires the committed digest manifest to be current.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from benchmarks.harness_perf.byte_identity import PROTOCOLS, _messages, recorded_requests
from lib.llm.body import build_body
from lib.llm._sse_core import prepare_request

OUT = Path(__file__).resolve().parents[1] / 'tests/fixtures/recorded_golden.json'


def build():
    cases = []
    for scenario, steps in recorded_requests():
        for protocol in PROTOCOLS:
            for step, request in enumerate(steps):
                body = build_body(request.get('model') or 'claude-sonnet-4', _messages(request),
                                  max_tokens=4096, tools=copy.deepcopy(request.get('tools')) or None,
                                  provider_id='byte-identity', precomputed_input_tokens=100,
                                  context_window=200000)
                plan = prepare_request(body, api_key='sk-byte-identity',
                                       base_url='http://127.0.0.1:9/v1', api_protocol=protocol)
                cases.append({'scenario': scenario, 'step': step, 'protocol': protocol,
                              'sha256': hashlib.sha256(plan.body.payload).hexdigest(),
                              'bytes': len(plan.body.payload), 'url': plan.url,
                              'headers': [[k, v] for k, v in plan.hdrs.items() if k != 'M-TraceId']})
    return json.dumps(cases, indent=1, ensure_ascii=False) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    text = build()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            raise SystemExit('recorded_golden.json is stale')
    else:
        OUT.write_text(text)


if __name__ == '__main__':
    main()
