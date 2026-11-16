"""Loopback OpenAI-compatible relay from competitor tools to Tofu's routing.

Usage: python -m evaluations.slides_bench.openai_proxy <port>

Competitor generators speak the OpenAI API. Tofu's ``/v1/chat/completions``
treats a request without ``tools`` as an agent task (which may run server
tools such as web search); a request with an explicit ``tools`` field is one
direct provider completion. The relay adds ``"tools": []`` when the client
sent no tools, so every competitor call is a plain model completion through
Tofu's model routing and keys, and forwards the response bytes unchanged
(including SSE streams). It also logs one line per request for accounting.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sys
import time
import urllib.error
import urllib.request

from evaluations.slides_bench.llm_bridge import ENDPOINT

_UPSTREAM = ENDPOINT.rsplit('/v1/', 1)[0]
_DEADLINE_S = 900
_UNSET_MAX_TOKENS = 32768


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # noqa: D401 - quiet default logging
        return

    def _forward(self, body: bytes | None) -> None:
        if body is not None and self.path.rstrip('/').endswith('chat/completions'):
            payload = json.loads(body or b'{}')
            # OpenAI clients that omit max_tokens expect the model's full
            # output allowance; the route default is shorter and truncates.
            if not (payload.get('max_tokens')
                    or payload.get('max_completion_tokens')):
                payload['max_tokens'] = _UNSET_MAX_TOKENS
            if not payload.get('tools'):
                payload['tools'] = []
                payload.pop('tool_choice', None)
                payload.pop('parallel_tool_calls', None)
            body = json.dumps(payload).encode('utf-8')
            sys.stderr.write(f'[relay] {time.strftime("%H:%M:%S")} '
                             f'model={payload.get("model")} '
                             f'stream={bool(payload.get("stream"))}\n')
        path = self.path if self.path.startswith('/v1/') else '/v1' + self.path
        request = urllib.request.Request(
            _UPSTREAM + path, data=body, method=self.command,
            headers={'Content-Type': 'application/json'})
        try:
            response = urllib.request.urlopen(request, timeout=_DEADLINE_S)
            status = response.status
        except urllib.error.HTTPError as exc:
            response, status = exc, exc.code
        self.send_response(status)
        content_type = response.headers.get('Content-Type', 'application/json')
        self.send_header('Content-Type', content_type)
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        while True:
            chunk = response.read1(65536) if hasattr(response, 'read1') \
                else response.read(65536)
            if not chunk:
                break
            self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
            self.wfile.flush()
        self.wfile.write(b'0\r\n\r\n')

    def do_GET(self):
        self._forward(None)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        self._forward(self.rfile.read(length))


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    _Server(('127.0.0.1', int(sys.argv[1])), _Handler).serve_forever()
