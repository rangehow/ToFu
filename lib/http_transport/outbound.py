"""Managed requests sessions and per-request network routing (docs/NETWORK.md).

The adapter owns connect-only route failover. Requests retains preparation,
redirects, authentication, hooks and response-body lifecycle. Explicit proxy
arguments pin a route; automatic requests use the shared route manager.
``network_cancellation`` and ``network_interruption`` bind an owner's stop to
the requests of one thread.
"""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import threading
import time

import requests

from lib.http_transport.connection_setup import (
    NetworkRoutesExhausted, connection_http_adapter,
)
from lib.http_transport.interruption import RequestInterruption, bind_interruption
from lib.http_transport.provider_pacing import note_wire_send


@dataclass(frozen=True)
class RequestNetworkPolicy:
    scope: str | None = None
    explicit_proxy: bool = False


_request_policy: ContextVar[RequestNetworkPolicy | None] = ContextVar(
    'tofu_request_network_policy', default=None)
_cancelled = ContextVar('tofu_network_cancelled', default=None)


class NetworkRequestCancelled(requests.exceptions.RequestException):
    """The request owner cancelled before application bytes were sent."""


@contextmanager
def network_cancellation(cancelled):
    """Bind an owner's cancellation to synchronous route waits and failover."""
    token = _cancelled.set(cancelled)
    try:
        yield
    finally:
        _cancelled.reset(token)


@contextmanager
def network_interruption(interruption: RequestInterruption):
    """Let an owner end this thread's synchronous requests (docs/NETWORK.md).

    ``interruption.interrupt()`` ends connection setup and response waits of
    requests issued in this block at once; route waits and failover observe it
    like ``network_cancellation``, so a later request never reaches the wire.
    """
    outer = _cancelled.get()
    with bind_interruption(interruption), network_cancellation(
            lambda: interruption.interrupted or (outer is not None and bool(outer()))):
        yield interruption


def _check_cancelled():
    cancelled = _cancelled.get()
    if cancelled is not None and cancelled():
        raise NetworkRequestCancelled('Network request cancelled before delivery')


def route_metadata(route, peer=None) -> dict:
    """Credential-free route facts; ``peer`` is the address the connection reached."""
    return {'routeId': route.route_id, 'routeMode': route.mode,
            'decisionReason': 'network_route',
            **({'poolId': route.pool_id} if route.pool_id else {}),
            **({'peer': peer} if peer else {})}


def _address_text(address) -> str | None:
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    host, port = address[0], address[1]
    return f'[{host}]:{port}' if ':' in str(host) else f'{host}:{port}'


def requests_peer(response) -> str | None:
    """Remote address of a streamed requests response's connection.

    A body that requests already consumed has released its connection, so
    only streamed responses can name a peer.
    """
    connection = getattr(getattr(response, 'raw', None), 'connection', None)
    sock = getattr(connection, 'sock', None)
    if sock is None:
        return None
    try:
        return _address_text(sock.getpeername())
    except OSError:
        return None


def httpx_peer(response) -> str | None:
    """Remote address of an httpx response's connection (httpcore network stream)."""
    stream = response.extensions.get('network_stream')
    if stream is None:
        return None
    return _address_text(stream.get_extra_info('server_addr'))


def explicit_route(url: str, proxies: dict):
    from lib.network_routes import Route
    from lib.proxy import network_route_specs
    selected = requests.utils.select_proxy(url, proxies)
    if not selected:
        return Route('direct', 'direct', 'direct')
    for route in network_route_specs(url, scope='subscription'):
        if route.proxy_url == selected:
            return route
    selected = str(getattr(selected, 'url', selected))
    identity = hashlib.sha256(selected.encode()).hexdigest()
    return Route('explicit:' + identity, 'explicit proxy', 'proxy', proxy_url=selected)


def append_untried(routes, attempted, candidates):
    known = {route.route_id for route in routes}
    for route in candidates:
        if route.route_id not in attempted and route.route_id not in known:
            routes.append(route)
            known.add(route.route_id)


class RoutingHTTPAdapter(requests.adapters.HTTPAdapter):
    """Route one prepared request before any response body can be consumed."""

    def __init__(self, *, url_validator=None, **kwargs):
        self._url_validator = url_validator
        self._proxy_pool_lock = threading.RLock()
        self._proxy_epoch = None
        super().__init__(**kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        from lib.proxy import network_topology_epoch
        with self._proxy_pool_lock:
            epoch = network_topology_epoch()
            if epoch != self._proxy_epoch:
                self._proxy_epoch = epoch
                retired, self.proxy_manager = self.proxy_manager, {}
                for manager in retired.values():
                    # urllib3 drops idle sockets; borrowed connections finish
                    # normally and are discarded on their eventual return.
                    manager.clear()
            return super().proxy_manager_for(*args, **kwargs)

    def send(self, request, **kwargs):
        if self._url_validator is not None:
            self._url_validator(request.url)
        policy = _request_policy.get() or RequestNetworkPolicy(
            explicit_proxy=kwargs.get('proxies') is not None)
        return send_routed_request(super().send, request, policy=policy, **kwargs)


def send_routed_request(wire_send, request, *, policy, **kwargs):
    """SDK transport port after preparation and domain URL authorization."""
    from lib import proxy
    from lib.network_routes import is_safe_connect_failure

    _check_cancelled()
    routes = ([explicit_route(request.url, kwargs.get('proxies') or {})]
              if policy.explicit_proxy else
              list(proxy.network_route_candidates(
                  request.url, scope=policy.scope, cancelled=_cancelled.get())))
    attempted = set()
    for route in routes:
        _check_cancelled()
        attempted.add(route.route_id)
        started = time.monotonic()
        options = {**kwargs, 'proxies': route.requests_proxies()}
        note_wire_send()
        try:
            response = wire_send(request, **options)
        except requests.exceptions.RequestException as error:
            if not is_safe_connect_failure(error):
                raise
            _check_cancelled()  # the owner's own interruption is no evidence against the route
            proxy.report_network_route(request.url, route, False)
            if policy.explicit_proxy:
                raise
            append_untried(routes, attempted, proxy.network_route_candidates(
                request.url, scope=policy.scope, cancelled=_cancelled.get()))
            continue
        latency = (time.monotonic() - started) * 1000.0
        proxy.report_network_route(request.url, route, True, latency)
        response.tofu_network_route = route_metadata(route, requests_peer(response))
        response.tofu_network_latency_ms = latency
        response.tofu_route = route
        return response
    _check_cancelled()
    raise NetworkRoutesExhausted('No configured network route connected') from None


class ManagedSession(requests.Session):
    """Request-scoped proxy policy without changing global library behavior."""

    def __init__(self, *, scope=None):
        super().__init__()
        self.network_scope = scope

    def request(self, method, url, **kwargs):
        scope = kwargs.pop('network_scope', self.network_scope)
        if self.proxies and kwargs.get('proxies') is None:
            kwargs['proxies'] = dict(self.proxies)
        token = _request_policy.set(RequestNetworkPolicy(
            scope=scope, explicit_proxy=kwargs.get('proxies') is not None))
        try:
            return super().request(method, url, **kwargs)
        finally:
            _request_policy.reset(token)

    def send(self, request, **kwargs):
        # SDKs can send PreparedRequest directly, outside Session.request.
        if _request_policy.get() is not None:
            return super().send(request, **kwargs)
        token = _request_policy.set(RequestNetworkPolicy(
            scope=self.network_scope, explicit_proxy=kwargs.get('proxies') is not None))
        try:
            return super().send(request, **kwargs)
        finally:
            _request_policy.reset(token)


def managed_session(*, scope=None, adapter=None, **adapter_options) -> ManagedSession:
    """Create an owned session for an application or SDK; callers close it."""
    session = ManagedSession(scope=scope)
    try:
        if adapter is None:
            adapter = connection_http_adapter(
                adapter_class=RoutingHTTPAdapter, **adapter_options)
        if not isinstance(adapter, RoutingHTTPAdapter):
            raise TypeError('managed sessions require a routing HTTP adapter')
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session
    except BaseException:
        session.close()
        raise
