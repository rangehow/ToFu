"""Provider-wire byte identity of request preparation over recorded requests.

``python3 benchmarks/harness_perf/byte_identity.py OUT.jsonl``

Replays every recorded request of the LLM replay corpus
(``tests/fixtures/llm_replay/<scenario>/script.jsonl`` and its request store)
through ``build_body`` and ``prepare_request`` on the OpenAI, Anthropic and
Responses wire faces, scenario by scenario in recording order, as one Turn
would: one text-memo scope, the task-owned memos ``build_round_request``
attaches (settled canonical rows reused across rounds, the text-count memo's
provider view, the Anthropic tool-input memo) and a persistent settled-row
holder, so later rounds take the memoized and resumed paths, and a managed
context section plus an event
injection before the latest user message so that lineage is mapped and
sealed. Each request writes one line: the SHA-256 of the sealed payload, the
headers (without the per-request trace id), the off-wire lineage evidence
(without its trace id), the request identities, and the offered tool names.
Run it on two trees and compare the files byte for byte: a structural
optimization of request preparation must leave every line unchanged.
Method: docs/HARNESS_PERFORMANCE.md, "Round floor IV".

``python3 benchmarks/harness_perf/byte_identity.py OUT.jsonl --rust BIN`` is
the Rust arm: the same history of every request goes through Python and
through ``BIN byte-identity`` (``packages/tofu-core-rs``), and each line
records whether the sealed payload bytes, endpoint, headers (without the
trace id) and offered tool names are identical, with the first differing
byte offset when they are not. Off-wire evidence (lineage, identities,
admitted token counts) is Python-only and not compared.
Evidence: docs/RUST_REWRITE_EVIDENCE.md, "Core loop prototype".
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT_ROOT / 'tests' / 'fixtures' / 'llm_replay'
PROTOCOLS = ('openai', 'anthropic', 'responses')


def _field(name: str, digest: str):
    return json.loads((FIXTURES / 'requests' / f'{name}-{digest[:16]}.json').read_text())


def recorded_requests():
    for script in sorted(FIXTURES.glob('*/script.jsonl')):
        steps = []
        for line in script.read_text().splitlines():
            step = json.loads(line)
            digests = step.get('field_digests') or {}
            if 'messages' not in digests:
                continue
            steps.append({name: _field(name, digest) for name, digest in digests.items()})
        if steps:
            yield script.parent.name, steps


def _messages(request: dict) -> list:
    from lib.tasks_pkg.context_composer import append_injection
    messages = copy.deepcopy(request['messages'])
    system = request.get('system')
    if system and not (messages and messages[0].get('role') == 'system'):
        messages.insert(0, {'role': 'system', 'content': system})
    last_user = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=None)
    if last_user is not None:
        head, tail = messages[:last_user], messages[last_user:]
        append_injection(head, 'worker_policy', 'Managed section body.\nSecond line.')
        messages = head + tail
        append_injection(messages, 'tool_progress', 'A private progress event.')
    return messages


def _without_event_ids(value):
    # append_injection mints a random event id per run; positions, hashes and
    # statuses are the evidence compared.
    if isinstance(value, dict):
        return {k: _without_event_ids(v) for k, v in value.items() if k != 'eventId'}
    if isinstance(value, list):
        return [_without_event_ids(v) for v in value]
    return value


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=repr).encode()).hexdigest()


def run(out) -> int:
    from lib.llm._sse_core import prepare_request
    from lib.llm.body import build_body
    from lib.conversation_sync.settled_rows import settle_items
    from lib.llm.anthropic_outbound._tool_inputs import ANTHROPIC_TOOL_INPUTS_KEY, ToolInputs
    from lib.llm.request import WIRE_MESSAGE_ROWS_KEY
    from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY
    from lib.token_counter.tiktoken_counter import RequestTextCounts
    from lib.llm_sanitize.context_sections import next_text_memo_round, text_memo_scope
    count = 0
    for scenario, steps in recorded_requests():
        for protocol in PROTOCOLS:
            holder: dict = {}
            canonical: dict = {}
            text_counts = RequestTextCounts()
            tool_inputs = ToolInputs()
            with text_memo_scope():
                for index, request in enumerate(steps):
                    next_text_memo_round()
                    body = build_body(request.get('model') or 'claude-sonnet-4',
                                      _messages(request), max_tokens=4096,
                                      tools=copy.deepcopy(request.get('tools')) or None,
                                      provider_id='byte-identity',
                                      precomputed_input_tokens=100, context_window=200000)
                    body['messages'] = canonical['rows'] = settle_items(
                        body['messages'], previous=canonical.get('rows'))
                    body[TEXT_COUNT_VIEW_KEY] = text_counts.view('provider')
                    body[ANTHROPIC_TOOL_INPUTS_KEY] = tool_inputs
                    body[WIRE_MESSAGE_ROWS_KEY] = holder
                    plan = prepare_request(body, api_key='sk-byte-identity',
                                           base_url='http://127.0.0.1:9/v1',
                                           api_protocol=protocol)
                    sealed = plan.body
                    headers = {k: v for k, v in plan.hdrs.items() if k != 'M-TraceId'}
                    lineage = sealed.context_diagnostics()
                    if lineage:
                        lineage.pop('traceId', None)
                    out.write(json.dumps({
                        'scenario': scenario, 'protocol': protocol, 'step': index,
                        'payload': hashlib.sha256(sealed.payload).hexdigest(),
                        'bytes': len(sealed.payload), 'url': plan.url,
                        'headers': _digest(headers), 'lineage': _digest(_without_event_ids(lineage)),
                        'identity': sealed.diagnostics(),
                        'tools': sorted(_names(json.loads(sealed.payload).get('tools') or ())),
                    }, sort_keys=True) + '\n')
                    count += 1
    return count


def _names(tools) -> list:
    names = []
    for tool in tools:
        function = tool.get('function') if isinstance(tool, dict) else None
        name = (function or {}).get('name') or (tool.get('name') if isinstance(tool, dict) else None)
        if name:
            names.append(str(name))
    return names


def run_rust(out, binary: str) -> tuple[int, int]:
    """Compare Python and ``binary byte-identity`` on every recorded request."""
    import base64
    import subprocess
    from lib.llm._sse_core import prepare_request
    from lib.llm.body import build_body
    from lib.llm.request import WIRE_MESSAGE_ROWS_KEY
    from lib.llm_sanitize.context_sections import next_text_memo_round, text_memo_scope
    rows, inputs = [], []
    for scenario, steps in recorded_requests():
        for protocol in PROTOCOLS:
            holder: dict = {}
            with text_memo_scope():
                for index, request in enumerate(steps):
                    next_text_memo_round()
                    messages = _messages(request)
                    model = request.get('model') or 'claude-sonnet-4'
                    tools = copy.deepcopy(request.get('tools')) or None
                    inputs.append(json.dumps({
                        'protocol': protocol, 'model': model, 'max_tokens': 4096, 'temperature': 1.0,
                        'tools': tools, 'messages': messages, 'base_url': 'http://127.0.0.1:9/v1',
                        'api_key': 'sk-byte-identity'}, ensure_ascii=False, default=repr))
                    body = build_body(model, messages, max_tokens=4096, tools=tools,
                                      provider_id='byte-identity',
                                      precomputed_input_tokens=100, context_window=200000)
                    body[WIRE_MESSAGE_ROWS_KEY] = holder
                    plan = prepare_request(body, api_key='sk-byte-identity',
                                           base_url='http://127.0.0.1:9/v1', api_protocol=protocol)
                    rows.append((scenario, protocol, index, plan))
    completed = subprocess.run([binary, 'byte-identity'], input='\n'.join(inputs) + '\n',
                               capture_output=True, text=True, check=True)
    answers = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    if len(answers) != len(rows):
        raise SystemExit(f'{binary} answered {len(answers)} of {len(rows)} requests')
    matched = 0
    for (scenario, protocol, index, plan), answer in zip(rows, answers):
        python_payload = plan.body.payload
        python_headers = {k: v for k, v in plan.hdrs.items() if k != 'M-TraceId'}
        line = {'scenario': scenario, 'protocol': protocol, 'step': index,
                'python_payload': hashlib.sha256(python_payload).hexdigest(),
                'python_bytes': len(python_payload)}
        if 'error' in answer:
            line.update(match=False, cause=answer['error'])
        else:
            rust_payload = base64.b64decode(answer['payload_b64'])
            checks = {
                'payload': rust_payload == python_payload,
                'url': answer['url'] == plan.url,
                'headers': dict(answer['headers']) == python_headers,
                'tools': answer['tools'] == _names(json.loads(python_payload).get('tools') or ()),
            }
            line.update(rust_payload=hashlib.sha256(rust_payload).hexdigest(),
                        rust_bytes=len(rust_payload), checks=checks, match=all(checks.values()))
            if not checks['payload']:
                offset = next((i for i, (a, b) in enumerate(zip(python_payload, rust_payload)) if a != b),
                              min(len(python_payload), len(rust_payload)))
                line.update(first_diff=offset,
                            python_context=python_payload[max(0, offset - 80):offset + 80].decode('utf-8', 'replace'),
                            rust_context=rust_payload[max(0, offset - 80):offset + 80].decode('utf-8', 'replace'))
        matched += bool(line['match'])
        out.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + '\n')
    return matched, len(rows)


if __name__ == '__main__':
    with open(sys.argv[1], 'w', encoding='utf-8') as handle:
        if sys.argv[2:3] == ['--rust']:
            matched, total = run_rust(handle, sys.argv[3])
            print(f'{matched}/{total} requests byte-identical')
            raise SystemExit(0 if matched == total else 1)
        print(run(handle), 'requests')
