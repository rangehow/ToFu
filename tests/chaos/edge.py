"""A controllable network edge between chaos nodes and the isolated Home.

Responsibility: play the network a real node crosses, so faults are injected
on the wire rather than in either endpoint.

- Forward proxy: a node whose environment names this edge as ``HTTP_PROXY``
  sends absolute-form requests for an origin it cannot resolve itself
  (``EDGE_HOST``), the way a CodeLab container reaches a Home only through its
  proxy. The node's outbound session must use the shared network authority's
  environment proxy route for that to work at all.
- Path prefix: ``/proxy/<port>`` is stripped before forwarding, like the
  CodeLab ``/proxy/15000/`` edge in front of a Tofu host.
- Partition: while partitioned, new requests are held unanswered and requests
  already upstream lose their response; the client sees exactly what a cut
  link produces (its own timeout). ``heal`` closes the held connections.
- Retarget: a Home restarted on a new port is reached through the same edge
  address, as a node reaches a moved Home through its saved route.

Every request is counted by path on arrival (``hits``), so tests can bound
reconnect pacing even while held polls are still open. Standard library only. Entry: ``Edge(upstream_port).start()``.
"""
from __future__ import annotations

import http.client
import json
import http.server
import socket
import threading
import time
from urllib.parse import urlsplit

EDGE_HOST = 'codelab-home.invalid'
PREFIX = '/proxy/15000'
_HOP_HEADERS = frozenset({'connection', 'keep-alive', 'proxy-connection', 'proxy-authorization',
                          'te', 'trailer', 'transfer-encoding', 'upgrade'})


class Edge:
    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self._healed = threading.Event()
        self._healed.set()
        self._generation = 0
        self._lock = threading.Lock()
        self.hits: list[tuple[float, str, str]] = []  # (monotonic arrival, path, polling node id)
        edge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                return

            def _forward(self):
                generation = edge._generation
                target = urlsplit(self.path)
                path = target.path if target.scheme else self.path.split('?', 1)[0]
                query = target.query if target.scheme else (self.path.split('?', 1) + [''])[1]
                if path.startswith(PREFIX + '/') or path == PREFIX:
                    path = path[len(PREFIX):] or '/'
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                edge._record(path, body)
                if not edge._healed.is_set():
                    edge._healed.wait()
                    self.close_connection = True
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_HEADERS}
                upstream = http.client.HTTPConnection('127.0.0.1', edge.upstream_port, timeout=120)
                try:
                    upstream.request(self.command, path + (f'?{query}' if query else ''),
                                     body=body or None, headers=headers)
                    response = upstream.getresponse()
                    payload = response.read()
                except OSError:
                    self.close_connection = True
                    try:
                        self.send_error(502)
                    except OSError:
                        pass
                    return
                finally:
                    upstream.close()
                if generation != edge._generation or not edge._healed.is_set():
                    # The link was cut while the request was upstream: the answer is lost.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in _HOP_HEADERS and key.lower() != 'content-length':
                        self.send_header(key, value)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _forward

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread: threading.Thread | None = None

    def _record(self, path: str, body: bytes) -> None:
        node = ''
        try:
            node = str((json.loads(body or b'{}').get('agent') or {}).get('agent_id') or '')
        except (ValueError, AttributeError):
            pass
        with self._lock:
            self.hits.append((time.monotonic(), path, node))

    def start(self) -> 'Edge':
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='chaos-edge')
        self._thread.start()
        return self

    @property
    def proxy_url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    @property
    def home_url(self) -> str:
        """The Home as a node behind the edge addresses it."""
        return f'http://{EDGE_HOST}{PREFIX}'

    def retarget(self, upstream_port: int) -> None:
        self.upstream_port = upstream_port

    def partition(self) -> None:
        with self._lock:
            self._generation += 1
            self._healed.clear()

    def heal(self) -> None:
        with self._lock:
            self._generation += 1
            self._healed.set()

    def count(self, path: str, since: float = 0.0, node: str | None = None) -> int:
        """Requests to ``path`` since ``since``; polls of one node when ``node`` is given."""
        with self._lock:
            return sum(1 for at, p, n in self.hits
                       if p == path and at >= since and (node is None or n == node))

    def stop(self) -> None:
        self.heal()
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(10)
