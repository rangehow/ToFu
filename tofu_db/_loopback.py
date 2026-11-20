"""Standard-library local transport recovery shared by lifecycle and storage.

Entry points: create_connection, urlopen, bind_ephemeral_loopback,
listen_backlog_capacity.
Only pre-dispatch IPv4 loopback allocation failures admit recovery. Destinations,
authentication, payloads, and deadlines remain unchanged; no runtime store switch.
"""

from __future__ import annotations

import errno
import http.client
import logging
from pathlib import Path
import random
import selectors
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.response


_ALLOCATION_ERRORS = {errno.EADDRINUSE, errno.EADDRNOTAVAIL}
_log = logging.getLogger('tofu.transport.loopback')


def _remaining(timeout, started):
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT or timeout is None:
        return timeout
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError('loopback connection deadline expired')
    return remaining


def _connect_cancellable(address, timeout, cancellation, *, source_address=None):
    # Storage admits only these loopback names. Resolve localhost locally,
    # without a resolver call that cannot borrow the cancellation token.
    host, port = address
    hosts = ('::1', '127.0.0.1') if host == 'localhost' else (host,)
    if any(host not in {'127.0.0.1', '::1'} for host in hosts):
        raise ValueError('Cancellable connection requires a storage loopback endpoint')
    started = time.monotonic()
    last_error = None
    for host in hosts:
        family = socket.AF_INET6 if host == '::1' else socket.AF_INET
        try:
            return _connect_numeric((host, port), family, _remaining(timeout, started),
                                    cancellation, source_address)
        except OSError as exc:
            if cancellation.cancelled:
                raise
            last_error = exc
    raise last_error


def _connect_numeric(address, family, timeout, cancellation, source_address):
    # shutdown() is not a portable wake for an unconnected socket. Wait for
    # connect completion and a cancellation byte on the same selector.
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        reader, writer = socket.socketpair()
        with reader, writer, selectors.DefaultSelector() as selector:
            writer.setblocking(False)
            with cancellation.bind(lambda: writer.send(b'\0')):
                sock.setblocking(False)
                if source_address is not None:
                    sock.bind(source_address)
                status = sock.connect_ex(address)
                if status in {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR}:
                    selector.register(sock, selectors.EVENT_WRITE)
                    selector.register(reader, selectors.EVENT_READ)
                    if not selector.select(timeout):
                        raise TimeoutError('loopback connection deadline expired')
                    if cancellation.cancelled:
                        raise OSError('Transport request was cancelled before dispatch')
                    status = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if status:
                    raise OSError(status, 'Loopback connection failed')
                if cancellation.cancelled:
                    raise OSError('Transport request was cancelled before dispatch')
                sock.settimeout(socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout)
        return sock
    except BaseException:
        sock.close()
        raise


def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *,
                      source_address=None, cancellation=None):
    """Connect once; recover Linux loopback tuple exhaustion before dispatch.

    Nagle is off: a message written in more than one send never waits for the
    peer's delayed acknowledgement.
    """
    started = time.monotonic()
    connect = socket.create_connection if cancellation is None else (
        lambda address, timeout, **options: _connect_cancellable(
            address, timeout, cancellation, **options))
    options = {} if source_address is None else {'source_address': source_address}
    try:
        connection = connect(address, timeout=timeout, **options)
    except OSError as error:
        if (error.errno not in _ALLOCATION_ERRORS or address[0] != '127.0.0.1'
                or source_address is not None or sys.platform != 'linux'):
            raise
        # A different local source has an independent tuple pool. The target
        # remains exactly the admitted endpoint; no Hello/RPC has been sent.
        connection = connect(
            address, timeout=_remaining(timeout, started),
            source_address=('127.0.0.2', 0),
        )
    try:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except BaseException:
        connection.close()
        raise
    return connection


def urlopen(request, *, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, context=None):
    """Preserve urllib semantics; retry only a local connect allocation error."""
    started = time.monotonic()
    original_url = request.full_url if isinstance(request, urllib.request.Request) else request
    original_target = urllib.parse.urlsplit(original_url)
    if (isinstance(request, urllib.request.Request)
            and request.get_method() not in {'GET', 'HEAD'}
            and original_target.hostname == '127.0.0.1'
            and original_target.scheme in {'http', 'https'}
            and original_target.username is None and original_target.password is None):
        # Mutating control calls own connect separately from dispatch. A urllib
        # redirect/connect error after a successful POST must never replay it.
        return _local_response(request, original_url, original_target, started,
                               timeout, context, source_address=None)
    options = {'timeout': timeout}
    if context is not None:
        options['context'] = context
    try:
        return urllib.request.urlopen(request, **options)
    except (OSError, urllib.error.URLError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        target = urllib.parse.urlsplit(url)
        if (getattr(reason, 'errno', None) not in _ALLOCATION_ERRORS
                or target.hostname != '127.0.0.1'
                or target.scheme not in ('http', 'https')
                or target.username is not None or target.password is not None
                or sys.platform != 'linux'):
            raise
    return _local_response(request, url, target, started, timeout, context,
                           source_address=('127.0.0.2', 0))


def _local_response(request, url, target, started, timeout, context, *, source_address):
    options = {'timeout': _remaining(timeout, started)}
    if source_address is not None:
        options['source_address'] = source_address
    connection_type = http.client.HTTPConnection
    if target.scheme == 'https':
        connection_type = http.client.HTTPSConnection
        options['context'] = context
    connection = connection_type(target.hostname, target.port, **options)
    path = urllib.parse.urlunsplit(('', '', target.path or '/', target.query, ''))
    headers = dict(request.header_items()) if isinstance(request, urllib.request.Request) else {}
    headers['Connection'] = 'close'
    method = request.get_method() if isinstance(request, urllib.request.Request) else 'GET'
    body = request.data if isinstance(request, urllib.request.Request) else None
    try:
        if source_address is None:
            try:
                connection.connect()
            except OSError as error:
                if error.errno not in _ALLOCATION_ERRORS or sys.platform != 'linux':
                    raise
                connection.close()
                options['source_address'] = ('127.0.0.2', 0)
                options['timeout'] = _remaining(timeout, started)
                connection = connection_type(target.hostname, target.port, **options)
                connection.connect()
        request_options = {'headers': headers}
        if body is not None:
            request_options['body'] = body
        connection.request(method, path, **request_options)
        response = connection.getresponse()
    except BaseException:
        connection.close()
        raise
    # An HTTP/1.1 keep-alive reply stays attached to its connection, and closing
    # the connection closes the reply unread. The caller reads the reply (as much
    # as it bounds) and closing it closes the connection.
    answer = urllib.response.addclosehook(response, connection.close)
    if response.status >= 300:
        # Never follow a redirect to a second destination in the recovery
        # lane; HTTPError owns and closes the received response stream.
        raise urllib.error.HTTPError(
            url, response.status, response.reason, response.headers, answer)
    return urllib.response.addinfourl(answer, response.headers, url, response.status)


def bind_ephemeral_loopback(sock):
    """Bind the original address, with bounded atomic admission under pressure."""
    try:
        sock.bind(('127.0.0.1', 0))
        return
    except OSError as error:
        if error.errno != errno.EADDRINUSE or sys.platform != 'linux':
            raise
    try:
        low, high = map(int, Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split())
        if not 1024 <= low <= high <= 65535:
            raise ValueError('invalid ephemeral port range')
    except (OSError, ValueError):
        low, high = 32768, 65535
    candidates = list(range(1024, low)) + list(range(high + 1, 65536))
    for port in random.SystemRandom().sample(candidates, min(128, len(candidates))):
        try:
            sock.bind(('127.0.0.1', port))
        except OSError as error:
            if error.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
        else:
            _log.warning('Loopback ephemeral pool exhausted; listener admitted outside pool')
            return
    raise OSError(errno.EADDRINUSE, 'no available loopback listener port within recovery budget')


def listen_backlog_capacity() -> int:
    """Observe the kernel listen capability once at listener construction.

    Python's compile-time SOMAXCONN can understate a Linux host's live setting.
    Unavailable/invalid observations retain the platform fallback; this is a
    backlog sizing hint, never an application request-admission quota.
    """
    fallback = max(1, int(socket.SOMAXCONN))
    if sys.platform != 'linux':
        return fallback
    try:
        with Path('/proc/sys/net/core/somaxconn').open('r') as source:
            value = int(source.read(32).strip())
        return value if 1 <= value <= 2**31 - 1 else fallback
    except (OSError, ValueError):
        return fallback
