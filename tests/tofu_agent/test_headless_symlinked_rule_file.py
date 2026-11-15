"""Headless `tofu-agent run` in a repository whose rule file is a symlink.

FRONTIER-HARNESS-2 (2026-11-13): the FHE v1 DeepSWE task
python-statemachine-state-data-scoping ships ``/app/CLAUDE.md -> AGENTS.md``,
a common convention. The rule reader accepted the in-root link, then opened it
with O_NOFOLLOW; ELOOP failed the run with exit 4 after 2.7 s, before any model
call. A link that cannot be read must not fail the run either: it becomes a
visible notice. Real CLI, real composer and the real provider wire against a
loopback provider (pattern of tests/evaluations/test_harness_stream_end_to_end.py).
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tests.support.standalone_model_routing import standalone_model_routing_envelope

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TASK = 'Reply with the single word OK.'
RULE = 'Run the statemachine tests with pytest -q before finishing.'


def _first_request(tmp_path: Path, sandbox: Path) -> tuple[subprocess.CompletedProcess, list, dict]:
    requests_seen = []

    class Provider(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            requests_seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            frames = [
                {'choices': [{'index': 0, 'delta': {'content': 'OK'}}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 100, 'completion_tokens': 1}},
            ]
            wire = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write((wire + 'data: [DONE]\n\n').encode())
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1', secret='test-only',
        model_id='headless-symlink-rule-test')
    for model in envelope['model_routing']['models']:
        model['capabilities'].append('tools')
    for offering in envelope['model_routing']['offerings']:
        offering['capabilities'].append('tools')
    env = {**os.environ,
           'PYTHONPATH': str(ROOT),
           'TOFU_AGENT_MODEL_ROUTING': json.dumps(envelope),
           'TOFU_DISABLE_CONFIGURED_SLOTS': '1',
           'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
           'LLM_DEBUG_RAW_SSE': '',
           'TOFU_HOME': str(tmp_path / 'home')}
    result_path = tmp_path / 'run.json'
    try:
        proc = subprocess.run([
            sys.executable, '-m', 'tofu_agent', '--env-file', str(tmp_path / 'absent.env'),
            'run', '--task', TASK, '--cwd', str(sandbox), '--output', str(result_path),
            '--timeout-s', '45',
        ], cwd=sandbox, env=env, text=True, capture_output=True, timeout=90)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    return proc, requests_seen, result


def _text(request: dict) -> str:
    parts = []
    for message in request.get('messages') or []:
        content = message.get('content')
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(part.get('text') or '') for part in content if isinstance(part, dict))
    return '\n'.join(parts)


def test_in_root_symlinked_claude_md_is_read_and_the_run_reaches_the_model(tmp_path):
    sandbox = tmp_path / 'app'
    sandbox.mkdir()
    (sandbox / 'AGENTS.md').write_text(RULE + '\n', encoding='utf-8')
    (sandbox / 'CLAUDE.md').symlink_to('AGENTS.md')

    proc, requests_seen, result = _first_request(tmp_path, sandbox)

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert requests_seen, proc.stderr[-3000:]
    assert result.get('ok') is True, result.get('error')
    text = _text(requests_seen[0])
    assert TASK in text and RULE in text
    assert 'project rule file' not in text


def test_a_rule_symlink_that_cannot_be_read_is_a_notice_not_a_failed_run(tmp_path):
    sandbox = tmp_path / 'app'
    sandbox.mkdir()
    (sandbox / 'AGENTS.md').write_text(RULE + '\n', encoding='utf-8')
    (sandbox / 'CLAUDE.md').symlink_to('missing-shared-rules.md')

    proc, requests_seen, result = _first_request(tmp_path, sandbox)

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert requests_seen, proc.stderr[-3000:]
    assert result.get('ok') is True, result.get('error')
    text = _text(requests_seen[0])
    assert RULE in text
    assert f'project rule file {sandbox / "CLAUDE.md"} skipped: dangling_symlink' in text
