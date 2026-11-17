"""Owned, shutdown-aware MCP prewarm, auto-connect and reconcile worker.

One thread at a time does this process's MCP connection work: the boot
prewarm/auto-connect pass, then any queued reconcile requests. A reconcile
request applies a management action (connect, disconnect, the per-tool
filter) that another
process acknowledged, to this process's bridge: in the two-process runtime
the API host's routes manage its own bridge while Turns call tools through the
execution host's (docs/modules/tools_execution.md, "MCP management reaches the
execution host"). Stop prevents late connects and joins the worker.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any


_worker: threading.Thread | None = None
_worker_stop = threading.Event()
_worker_lock = threading.Lock()
# Reconcile requests (owner, server, action) waiting for the worker, which
# drains them before it exits; ``_draining`` is true while it still will.
_pending: list[tuple[int, str, str]] = []
_draining = False
RECONCILE_ACTIONS = frozenset({'connect', 'disconnect', 'tools'})


def _drain_reconciles(logger: logging.Logger) -> None:
    global _draining
    while True:
        with _worker_lock:
            if _worker_stop.is_set() or not _pending:
                _draining = False
                return
            owner_user_id, server, action = _pending.pop(0)
        try:
            _reconcile_one(owner_user_id, server, action, logger=logger)
        except Exception as exc:
            logger.warning('[MCP] reconcile %s %s failed: %s', action, server, exc)


def _reconcile_one(owner_user_id: int, server: str, action: str, *, logger: logging.Logger) -> None:
    from lib.mcp.client import get_bridge
    from lib.mcp.config import load_mcp_config
    bridge = get_bridge(owner_user_id)
    row = load_mcp_config(user_id=owner_user_id).get(server) if action != 'disconnect' else None
    if action == 'tools':
        # The owner's per-tool grant; it applies without a reconnect.
        bridge.set_disabled_tools(server, (row or {}).get('disabled_tools') or [])
        logger.info('[MCP] reconciled %s: tool filter', server)
        return
    if row is None or not row.get('enabled', True):
        if (server in {entry['name'] for entry in bridge.list_servers()}
                or bridge.get_breaker_state(server) is not None):
            bridge._disconnect_one(server, forget=True)
            logger.info('[MCP] reconciled %s: disconnected', server)
        return
    try:
        bridge.connect_server(server, row)
    except Exception:
        # Like a failed boot connect: the keepalive retries it with backoff.
        bridge._retry_failed_first_connect(server, row)
        raise
    if _worker_stop.is_set():
        # Shutdown raced the handshake; never leave a late connection behind.
        bridge._disconnect_one(server, forget=True)
        return
    logger.info('[MCP] reconciled %s: connected', server)


def _run_auto_connect(
    enabled: int,
    owner_user_id: int,
    *,
    logger: logging.Logger,
) -> None:
    try:
        try:
            from lib.mcp.client import prewarm_all_vendored
            warmed = prewarm_all_vendored(stop=_worker_stop)
            if warmed:
                logger.info('[MCP] Pre-warm: %s', warmed)
        except Exception as exc:
            logger.warning('[MCP] Pre-warm failed: %s', exc)

        if enabled > 0 and not _worker_stop.is_set():
            try:
                from lib.mcp.client import get_bridge
                bridge = get_bridge(owner_user_id)
                result = bridge.connect_all()
                if _worker_stop.is_set():
                    # Shutdown may have raced an uninterruptible handshake. Never
                    # let that late completion resurrect connections after the
                    # production cleanup already started.
                    bridge.disconnect_all()
                    return
                total = sum(len(tools) for tools in result.values())
                logger.info(
                    '[MCP] Auto-connect: %d servers, %d tools', len(result), total)
            except Exception as exc:
                logger.error('[MCP] Auto-connect failed: %s', exc, exc_info=True)
        _drain_reconciles(logger)
    finally:
        # Keep the finished object visible until the next start/stop takes the
        # lock; ownership state is reconciled there without a self-join.
        logger.debug('[MCP] startup worker finished')


def _start_worker_locked(target, kwargs) -> threading.Thread:
    global _worker, _draining
    thread = threading.Thread(target=target, kwargs=kwargs, name='mcp-auto-connect', daemon=True)
    _worker = thread
    _draining = True
    try:
        thread.start()
    except BaseException:
        _worker = None
        _draining = False
        raise
    return thread


def start_mcp_auto_connect(
    config: Mapping[str, Mapping[str, Any]] | None,
    *,
    logger: logging.Logger | None = None,
    owner_user_id: int,
) -> bool:
    """Start one prewarm/connect worker; duplicate starts share its owner."""
    from lib.identity import require_user_id
    owner_user_id = require_user_id(owner_user_id, context='MCP startup owner')
    log = logger or logging.getLogger(__name__)
    enabled = sum(
        1 for row in (config or {}).values() if row.get('enabled', True))
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return False
        _worker_stop.clear()
        try:
            _start_worker_locked(_run_auto_connect, {
                'enabled': enabled, 'logger': log, 'owner_user_id': owner_user_id})
        except BaseException:
            _worker_stop.set()
            raise
    return True


def request_mcp_reconcile(owner_user_id: int, server: str, action: str, *,
                          logger: logging.Logger | None = None) -> bool:
    """Queue one management action for this process's bridge; False after stop.

    Returns at once (the caller is a two-second control acknowledgement); the
    owned worker connects or disconnects with the owner's current stored row.
    """
    from lib.identity import require_user_id
    owner_user_id = require_user_id(owner_user_id, context='MCP reconcile owner')
    if action not in RECONCILE_ACTIONS or not isinstance(server, str) or not server.strip():
        raise ValueError('MCP reconcile needs a server name and a connect, disconnect or tools action')
    log = logger or logging.getLogger(__name__)
    with _worker_lock:
        if _worker_stop.is_set():
            return False
        _pending.append((owner_user_id, server.strip(), action))
        if _draining and _worker is not None and _worker.is_alive():
            return True
        _start_worker_locked(_drain_reconciles, {'logger': log})
    return True


def stop_mcp_auto_connect(*, timeout: float = 2.0) -> bool:
    """Prevent a late connect and wait boundedly for the startup worker."""
    global _worker
    _worker_stop.set()
    with _worker_lock:
        thread = _worker
        _pending.clear()
    if thread is None:
        return True
    if thread.is_alive():
        thread.join(max(0.0, timeout))
    stopped = not thread.is_alive()
    if stopped:
        with _worker_lock:
            if _worker is thread:
                _worker = None
    return stopped


__all__ = ['request_mcp_reconcile', 'start_mcp_auto_connect', 'stop_mcp_auto_connect']
