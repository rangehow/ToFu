"""Producer-bound encrypted Responses state, as served by FRIDAY's Kunlun face.

Measured on 2026-11-09 against kunlunbedrock.gpt-6.1-sol-m17 through
your-llm-gateway.example.com/v1/responses: each response mints a ``reasoning`` item whose
only payload is ``encrypted_content`` (with or without ``include``), and a
request that replays one on a different upstream account is rejected with
HTTP 400 ``invalid_encrypted_content`` ("The encrypted content for item rs_...
could not be verified"). The gateway picks the account per request; a stable
``Mt-Session-Id`` pins it most of the time, which is why a relay that drops
that header breaks every other replayed round.

This fake reproduces exactly that contract offline: the account is derived
from the request's ``Mt-Session-Id`` (a random one when the header is absent),
ciphertext is bound to the account that minted it, and a replay whose lineage
changed is rejected — never a route or key change inside Tofu itself.
"""
from __future__ import annotations

from collections import Counter
from http.server import BaseHTTPRequestHandler
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request

from tests.support.loopback import LoopbackThreadingHTTPServer

REJECTION = ('The encrypted content for item {item_id} could not be verified. '
             'Reason: Encrypted content could not be decrypted or parsed.')
_WORKER_MARKER = 'Work only on the delegated objective'


class AffinityStrippingRelay:
    """The research relay's header handling: forward the body, drop the carrier.

    analysis/real_harness/relay.py:155-157 builds its own upstream headers and
    never forwards Tofu's ``Mt-Session-Id``, so every request is placed
    independently. This proxy reproduces that hop in front of the fake.
    """

    def __init__(self, upstream: str):
        self.upstream = upstream.rstrip('/')
        self.headers: list[str] = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length)
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() not in ('host', 'content-length', 'connection')}
                relay.headers.append('Mt-Session-Id' in headers)
                headers.pop('Mt-Session-Id', None)
                request = urllib.request.Request(relay.upstream + self.path, data=body,
                                                 headers=headers, method='POST')
                try:
                    response = urllib.request.urlopen(request)
                    payload, status = response.read(), response.status
                except urllib.error.HTTPError as error:
                    payload, status = error.read(), error.code
                self.send_response(status)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self._server.server_port
        self.base_url = f'http://127.0.0.1:{self.port}/v1'
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class ProducerBoundKunlun:
    """A Responses endpoint that rejects ciphertext minted on another account."""

    def __init__(self, *, drop_affinity_header: bool = False, tool_rounds: int = 8):
        self.drop_affinity_header = drop_affinity_header
        self.tool_rounds = tool_rounds
        self.requests: list[dict] = []
        self.rejections: list[dict] = []
        self._lock = threading.Lock()
        self._sequence = Counter()
        server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.port = server.server_port
        self.base_url = f'http://127.0.0.1:{self.port}/v1'
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        self._server = server

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def lineage(self, actor: str) -> list[dict]:
        return [row for row in self.requests if row['actor'] == actor]

    def completed_lineages(self) -> set[str]:
        return {row['lineage_key'] for row in self.requests
                if row['actor'] == 'child' and row['final_answer']}

    def _account(self, affinity: str) -> str:
        return hashlib.sha256(affinity.encode()).hexdigest()[:8]

    def _handler(self):
        owner = self

        class Endpoint(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                transcript = json.dumps(body, ensure_ascii=False)
                actor = 'child' if _WORKER_MARKER in transcript else 'root'
                sent = self.headers.get('Mt-Session-Id', '')
                items = body.get('input') or []
                # One conversation = one lineage even without a carrier. The
                # account changes per request (gateway placement); the round
                # counter must not, or the fake never reaches its last round.
                identity = next((json.dumps(item, sort_keys=True) for item in items
                                 if isinstance(item, dict) and item.get('role') == 'user'), '')
                lineage_key = f'{actor}:{hashlib.sha256(identity.encode()).hexdigest()[:16]}'
                affinity = sent or 'random-per-request'
                account = owner._account(affinity) if sent else owner._account(
                    f'{lineage_key}:{time.time_ns()}')
                with owner._lock:
                    owner._sequence[lineage_key] += 1
                    sequence = owner._sequence[lineage_key]
                foreign = [item for item in items
                           if item.get('type') == 'reasoning'
                           and item.get('encrypted_content')
                           and not str(item['encrypted_content']).endswith(':' + account)]
                record = {
                    'actor': actor, 'account': account, 'sent_affinity': bool(sent),
                    'affinity': affinity, 'lineage_key': lineage_key,
                    'lineage': sent or lineage_key,
                    'include': body.get('include'), 'store': body.get('store'),
                    'context': (body.get('reasoning') or {}).get('context'),
                    'previous_response_id': body.get('previous_response_id'),
                    'replayed': [item.get('id') for item in items if item.get('type') == 'reasoning'],
                    'rejected': [item.get('id') for item in foreign],
                    'final_answer': False, 'round': sequence,
                }
                if foreign:
                    record['rejected_reason'] = REJECTION.format(item_id=foreign[0].get('id'))
                    with owner._lock:
                        owner.requests.append(record)
                        owner.rejections.append(record)
                    payload = json.dumps({'error': {
                        'code': 'invalid_encrypted_content',
                        'message': record['rejected_reason'], 'param': 'input'}}).encode()
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                completed = sequence > owner.tool_rounds
                if actor == 'root' and sequence > 2:
                    # The root may answer only after both children settled.
                    # Never block here: on a broken runtime the children never
                    # arrive, and the run must fail instead of hanging.
                    completed = len(owner.completed_lineages()) >= 2
                output = [{'type': 'reasoning', 'id': f'rs_{account}_{sequence}',
                           'summary': [], 'encrypted_content': f'cipher-{account}:{account}'}]
                if completed:
                    record['final_answer'] = True
                    output.append({
                        'type': 'message', 'id': 'msg_final', 'role': 'assistant',
                        'status': 'completed',
                        'content': [{'type': 'output_text', 'annotations': [],
                                     'text': (f'worker done after {owner.tool_rounds} rounds'
                                              if actor == 'child' else 'Both children returned.')}]})
                else:
                    name = 'spawn_agents' if actor == 'root' and sequence == 1 else (
                        'poll' if actor == 'root' else 'run_command')
                    arguments = ({'agents': [
                        {'id': 'child-a', 'role': 'general',
                         'objective': 'Do eight tool rounds as child-a and report.'},
                        {'id': 'child-b', 'role': 'general',
                         'objective': 'Do eight tool rounds as child-b and report.'}]}
                        if name == 'spawn_agents' else
                        {'ids': ['child-a', 'child-b'], 'wait_s': 60, 'mode': 'all'}
                        if name == 'poll' else {'command': f'echo round-{sequence}'})
                    output.append({'type': 'function_call', 'id': f'fc_{sequence}',
                                   'call_id': f'call_{actor}_{sequence}', 'name': name,
                                   'arguments': json.dumps(arguments), 'status': 'completed'})
                with owner._lock:
                    owner.requests.append(record)
                events = [{'type': 'response.created', 'response': {
                    'id': 'resp_1', 'status': 'in_progress', 'output': []}}]
                for index, item in enumerate(output):
                    events.append({'type': 'response.output_item.added',
                                   'output_index': index, 'item': item})
                    events.append({'type': 'response.output_item.done',
                                   'output_index': index, 'item': item})
                events.append({'type': 'response.completed', 'response': {
                    'id': 'resp_1', 'status': 'completed', 'output': output,
                    'usage': {'input_tokens': 1000, 'output_tokens': 60,
                              'output_tokens_details': {'reasoning_tokens': 28}}}})
                payload = b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n'
                                   for event in events)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Endpoint
