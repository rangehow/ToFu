"""Synthetic-only specifications for the byte-identity benchmark boundary."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.llm_replay.normalize import json_digest
from tests.support.llm_replay.request_store import stored_request_path

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).resolve().parents[2] / 'benchmarks' / 'harness_perf' / 'byte_identity.py'


@pytest.fixture
def synthetic(tmp_path):
    spec = importlib.util.spec_from_file_location('byte_identity', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.FIXTURES = tmp_path
    text = ''.join(f'Benign generated sentence {i}. ' for i in range(12))
    request = {'model': 'claude-sonnet-4',
               'messages': [{'role': 'user', 'content': text}], 'tools': []}
    digests = {name: json_digest(value) for name, value in request.items()}
    (tmp_path / 'requests').mkdir()
    for name, value in request.items():
        stored_request_path(tmp_path / 'requests', name, digests[name]).write_text(
            json.dumps(value, ensure_ascii=False), encoding='utf-8')
    scenario = tmp_path / 'synthetic'
    scenario.mkdir()
    (scenario / 'script.jsonl').write_text(json.dumps({'field_digests': digests}) + '\n')
    return module, request, digests, text


def test_every_recorded_request_is_sealed_on_every_wire_face(synthetic):
    module, request, _, text = synthetic
    canonical = json.dumps(request['messages'], ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8')
    assert module._field('messages', json_digest(request['messages'])) == json.loads(canonical)
    assert json_digest(request['messages']) == hashlib.sha256(canonical).hexdigest()
    assert list(module.recorded_requests()) == [('synthetic', [request])]
    out = io.StringIO()
    assert module.run(out) == len(module.PROTOCOLS)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert {line['protocol'] for line in lines} == set(module.PROTOCOLS)
    assert text not in out.getvalue()
    assert 'Benign generated sentence' not in out.getvalue()
    for line in lines:
        assert len(line['payload']) == 64 and line['bytes'] > 0
        assert line['lineage'] != module._digest(None)


@pytest.mark.parametrize('mode', ['match', 'mismatch', 'shorter', 'error'])
def test_standard_comparator_emits_only_non_content_diagnostics(synthetic, monkeypatch, mode):
    from lib.llm import _sse_core
    module, _, _, text = synthetic
    prepare = _sse_core.prepare_request
    plans = []

    def capture(*args, **kwargs):
        plan = prepare(*args, **kwargs)
        plans.append(plan)
        return plan

    def comparator(*args, **kwargs):
        answers = []
        for plan in plans:
            payload = plan.body.payload
            if mode == 'error':
                answers.append({'error': text})
                continue
            if mode == 'mismatch':
                payload = payload[:30] + bytes([payload[30] ^ 1]) + payload[31:]
            elif mode == 'shorter':
                payload = payload[:-1]
            answers.append({'payload_b64': base64.b64encode(payload).decode(),
                            'url': plan.url,
                            'headers': {k: v for k, v in plan.hdrs.items() if k != 'M-TraceId'},
                            'tools': module._names(json.loads(plan.body.payload).get('tools') or ())})
        return SimpleNamespace(returncode=0, stdout='\n'.join(map(json.dumps, answers)))

    monkeypatch.setattr(_sse_core, 'prepare_request', capture)
    monkeypatch.setattr('subprocess.run', comparator)
    out = io.StringIO()
    matched, count = module.run_rust(out, 'synthetic-comparator-not-executed')
    assert count == len(module.PROTOCOLS)
    assert matched == (count if mode == 'match' else 0)
    assert text not in out.getvalue()
    assert 'Benign generated sentence' not in out.getvalue()
    for line, plan in zip(map(json.loads, out.getvalue().splitlines()), plans):
        assert set(line) <= {'scenario', 'protocol', 'step', 'python_payload', 'python_bytes',
                             'rust_payload', 'rust_bytes', 'checks', 'match', 'cause', 'first_diff'}
        assert line['python_payload'] == hashlib.sha256(plan.body.payload).hexdigest()
        if mode == 'mismatch':
            assert line['first_diff'] == 30
        elif mode == 'shorter':
            assert line['first_diff'] == len(plan.body.payload) - 1
        elif mode == 'error':
            assert line['cause'] == 'comparator_error'


@pytest.mark.parametrize('bad', ['short_digest', 'wrong_digest', 'unknown_field', 'duplicate', 'duplicate_field'])
def test_invalid_identity_refuses_without_content_or_path(synthetic, bad):
    module, _, digests, text = synthetic
    script = module.FIXTURES / 'synthetic' / 'script.jsonl'
    if bad == 'duplicate':
        script.write_text('{"field_digests": {}, "field_digests": {}}')
    elif bad == 'duplicate_field':
        script.write_text('{"field_digests": {"messages": "' + digests['messages']
                          + '", "messages": "' + digests['messages'] + '"}}')
    else:
        if bad == 'short_digest':
            digests['messages'] = digests['messages'][:16]
        elif bad == 'wrong_digest':
            # Same store locator, different complete identity.
            digests['messages'] = digests['messages'][:16] + '0' * 48
        else:
            digests['unknown'] = digests['messages']
        script.write_text(json.dumps({'field_digests': digests}))
    with pytest.raises(SystemExit) as error:
        list(module.recorded_requests())
    assert text not in str(error.value)
    assert str(module.FIXTURES) not in str(error.value)


def test_duplicate_store_value_refuses(synthetic):
    module, _, digests, _ = synthetic
    path = stored_request_path(module.FIXTURES / 'requests', 'messages', digests['messages'])
    path.write_text('[{"role": "user", "role": "assistant"}]')
    with pytest.raises(SystemExit, match='invalid byte-identity JSON'):
        list(module.recorded_requests())


@pytest.mark.parametrize('digests', [None, [], '', False, 1])
def test_malformed_identity_container_refuses(synthetic, digests):
    module, _, _, _ = synthetic
    (module.FIXTURES / 'synthetic' / 'script.jsonl').write_text(
        json.dumps({'field_digests': digests}))
    with pytest.raises(SystemExit, match='invalid byte-identity field identities'):
        list(module.recorded_requests())


@pytest.mark.parametrize('mode', ['process_error', 'answer_count', 'launch_error'])
def test_comparator_refusal_does_not_echo_process_output_or_binary_path(synthetic, monkeypatch, mode):
    module, _, _, text = synthetic
    def comparator(*args, **kwargs):
        if mode == 'launch_error':
            raise OSError('benign-untrusted-binary-path')
        return SimpleNamespace(returncode=1 if mode == 'process_error' else 0,
                               stdout='', stderr=text)

    monkeypatch.setattr('subprocess.run', comparator)
    out = io.StringIO()
    with pytest.raises(SystemExit) as error:
        module.run_rust(out, 'benign-untrusted-binary-path')
    assert text not in str(error.value)
    assert 'benign-untrusted-binary-path' not in str(error.value)
    assert out.getvalue() == ''


@pytest.mark.parametrize('failure', ['permission', 'missing', 'unicode'])
def test_scenario_read_refusal_hides_path_and_details(synthetic, monkeypatch, failure):
    module, _, _, _ = synthetic
    script = module.FIXTURES / 'synthetic' / 'script.jsonl'
    detail = 'generated private read detail'
    errors = {'permission': PermissionError(13, detail, str(script)),
              'missing': FileNotFoundError(2, detail, str(script)),
              'unicode': UnicodeError(detail + str(script))}

    def fail_read(self, *args, **kwargs):
        assert self == script
        raise errors[failure]

    monkeypatch.setattr(Path, 'read_text', fail_read)
    with pytest.raises(SystemExit) as error:
        list(module.recorded_requests())
    assert str(error.value) == 'byte-identity scenario unavailable'
    assert str(script) not in str(error.value)
    assert detail not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize('stage', ['open', 'write', 'close'])
@pytest.mark.parametrize('error_type', [OSError, UnicodeError])
def test_cli_output_io_refusal_hides_path_and_details(synthetic, monkeypatch, stage, error_type):
    module, _, _, _ = synthetic
    output_path = 'generated-benign-output.jsonl'
    detail = 'generated private output detail'

    class Output:
        def __enter__(self):
            return self

        def write(self, value):
            if stage == 'write':
                raise error_type(detail + output_path)

        def __exit__(self, *args):
            if stage == 'close':
                raise error_type(detail + output_path)

    def open_output(*args, **kwargs):
        assert args[0] == output_path
        if stage == 'open':
            raise error_type(detail + output_path)
        return Output()

    monkeypatch.setattr('builtins.open', open_output)
    monkeypatch.setattr(module, 'run', lambda out: out.write('synthetic result'))
    with pytest.raises(SystemExit) as error:
        module.main([output_path])
    assert str(error.value) == 'byte-identity output unavailable'
    assert output_path not in str(error.value)
    assert detail not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize('arm', ['python', 'rust_match', 'rust_mismatch'])
def test_cli_preserves_success_and_mismatch_exit_behavior(synthetic, monkeypatch, capsys, arm):
    from contextlib import nullcontext
    module, _, _, _ = synthetic
    out = io.StringIO()
    monkeypatch.setattr('builtins.open', lambda *args, **kwargs: nullcontext(out))
    monkeypatch.setattr(module, 'run', lambda handle: handle.write('synthetic row\n') and 3)
    matched = 0 if arm == 'rust_mismatch' else 3
    monkeypatch.setattr(module, 'run_rust', lambda handle, binary: (matched, 3))
    if arm == 'python':
        assert module.main(['generated-output.jsonl']) == 0
        assert out.getvalue() == 'synthetic row\n'
        assert capsys.readouterr().out == '3 requests\n'
    else:
        assert module.main(['generated-output.jsonl', '--rust', 'generated-comparator']) == (
            1 if arm == 'rust_mismatch' else 0)
        assert capsys.readouterr().out == f'{matched}/3 requests byte-identical\n'
