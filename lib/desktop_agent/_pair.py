#!/usr/bin/env python3
"""lib/desktop_agent/_pair.py — probe-first route repair for one attachment.

``resume_attachment(url, secret, candidates=…, persist=…)``: a saved route
that stops answering is re-pointed only to the attachment's own candidate
routes — the ones its pairing link carried (contracts/desktop_node_v1
``PairingLink.candidates``). Unauthenticated discovery (LAN broadcast,
loopback services, ssh config hosts) never authorizes forwarding an existing
bearer credential to another server, so there is no discovery here.

Pairing itself lives in :mod:`lib.desktop_agent.attachments`.
"""

from __future__ import annotations

from lib.desktop_agent._probe import _is_route_url, probe_server
from lib.log import get_logger

logger = get_logger(__name__)


def _noop_log(_msg: str) -> None:
    pass


def _route_candidates(saved_url: str, candidates) -> list:
    out = []
    for u in candidates or ():
        if not isinstance(u, str):
            continue
        u = u.strip().rstrip('/')
        if _is_route_url(u) and u != saved_url and u not in out:
            out.append(u)
    return out


def resume_attachment(url: str, secret: str, log=_noop_log, *, candidates=(),
                      persist=None):
    """Probe-first resume for one saved attachment. Returns ``(url, secret)``.

    * saved address answers → unchanged;
    * saved address dead → walk ``candidates``; the first live one keeps the
      credential and is persisted through ``persist(url)`` when it differs;
    * nothing answers → unchanged: the server may simply be off, and the poll
      loop keeps retrying while the link line reports 'unreachable'.
    """
    ok, _ = probe_server(url, timeout=3.0)
    if ok:
        return url, secret
    log('Saved attachment %s is not answering — checking its routes' % url)
    for cand in _route_candidates(url, candidates):
        ok, reason = probe_server(cand, timeout=2.5)
        if not ok:
            log('Attachment route %s not reachable: %s' % (cand, reason))
            continue
        log('Attachment route %s answers — re-pointing (credential kept)' % cand)
        if persist is not None:
            try:
                persist(cand)
            except Exception as e:
                log('Could not persist re-pointed attachment: %s' % e)
                logger.warning('[Agent] could not persist re-pointed attachment: %s', e)
        return cand, secret
    log('No route of this attachment answers; retrying the saved one')
    return url, secret


__all__ = ['resume_attachment']
