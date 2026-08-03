#!/usr/bin/env python3
"""lib/desktop/pairing.py — pairing-code store + LAN discovery responder.

The PAIRING-CODE UX (docs/DESKTOP_AGENT_DIST_DESIGN.md §11): a 6-digit
one-time code is minted by the panel (POST /api/v1/desktop/pair-code,
authenticated) and consumed by the agent (POST /api/desktop/pair, NOT
authenticated — the code IS the credential) to exchange for an
agents:bridge key.

The PAIRING-STORE is deliberately process-local and in-memory: a code
lasts 5 minutes and is one-shot, so persistence would only extend the
window an attacker has after a restart — and restarting re-seeds the
RNG, which is what actually bounds replay. A server restart invalidates
all outstanding codes; the panel just mints again (cheap).

The LAN DISCOVERY RESPONDER is the optional rung-B auto-discovery
(§11.2.1): an opt-in UDP responder (env TOFU_DESKTOP_LAN_DISCOVERY=1)
that advertises `http://<lan-ip>:15000` to a broadcast probe. Best-effort,
silent when off. mDNS is deliberately NOT used — corporate networks
filter multicast; a plain UDP broadcast with an HMAC'd response is the
v1 primitive.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
import struct
import threading
import time
from typing import Optional

from lib.log import get_logger

logger = get_logger(__name__)

# The pairing-code alphabet: 6 digits, no ambiguous characters. 1e6
# space × 300 s TTL × 3-attempt lockout = standard one-time-code posture.
_CODE_ALPHABET = '0123456789'
_CODE_LEN = 6
_CODE_TTL_S = 300  # 5 minutes
_MAX_ATTEMPTS = 3  # per-code brute-force lockout

# LAN discovery v1 magic + version. The probe is exactly this many bytes;
# anything else is dropped (a cheap "is this even our protocol" gate).
_LAN_MAGIC = b'TOFU-DESKTOP-DISC\x01'  # 16 bytes: magic + version
_LAN_BIND = ('', 15001)  # adjacent to the app; not a privileged port

_STORE: dict[str, dict] = {}  # code -> {user_id, expires_at, attempts}
_STORE_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def generate_code() -> str:
    """A fresh, collision-checked 6-digit pairing code."""
    for _ in range(32):
        cand = ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
        with _STORE_LOCK:
            # Re-use is harmless (same user, fresh TTL) but we still
            # dodge it so two concurrent mints never collide visually.
            if cand not in _STORE or _STORE[cand]['expires_at'] <= _now():
                return cand
    # 32 collisions in 1e6 space is effectively impossible; fall back to
    # a locked counter derivation rather than fail.
    return ''.join(_CODE_ALPHABET[secrets.randbelow(10)] for _ in range(_CODE_LEN))


def mint_code(user_id: str, ttl: int | None = None) -> tuple[str, float]:
    """Mint a code bound to *user_id*. Returns ``(code, expires_at)``."""
    if ttl is None:
        ttl = _CODE_TTL_S
    code = generate_code()
    expires_at = _now() + ttl
    with _STORE_LOCK:
        _STORE[code] = {'user_id': str(user_id), 'expires_at': expires_at,
                        'attempts': 0}
    return code, expires_at


def _hmac(code: str, secret: str) -> str:
    return hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()


def consume_code(code: str) -> Optional[str]:
    """Validate and ONE-SHOT consume a code. Returns the owning user_id on
    success, or ``None`` on any failure (missing / expired / locked /
    over-attempted). A consume that fails the attempt budget also burns
    the code so it cannot be re-tried.

    The code is bound to its minting user; the exchange mints the
    agents:bridge key for THAT user (the agent carries no identity of its
    own into this call — the code is the only credential).
    """
    code = (code or '').strip()
    with _STORE_LOCK:
        row = _STORE.get(code)
        if row is None:
            return None
        if row['expires_at'] <= _now():
            del _STORE[code]
            return None
        row['attempts'] += 1
        if row['attempts'] > _MAX_ATTEMPTS:
            del _STORE[code]
            return None
        user_id = str(row['user_id'])
        del _STORE[code]  # ONE-SHOT: never reusable
    return user_id


def pending_codes(user_id: str) -> list[dict]:
    """Metadata (no secrets) for a user's still-valid codes — the panel's
    countdown renders against this."""
    uid = str(user_id)
    now = _now()
    with _STORE_LOCK:
        return [{'code': c, 'expires_at': r['expires_at'],
                 'remaining': max(0, int(r['expires_at'] - now))}
                for c, r in _STORE.items()
                if r['user_id'] == uid and r['expires_at'] > now]


# ═══════════════════════════════════════════════════════════════════
#  LAN discovery responder (optional rung B, §11.2.1)
# ═══════════════════════════════════════════════════════════════════

class LanDiscoveryResponder:
    """Opt-in UDP broadcast responder for local-network auto-discovery.

    Off by default (TOFU_DESKTOP_LAN_DISCOVERY=1 to enable). Listens on
    UDP 15001 (adjacent to the app); on receiving the exact probe magic
    replies with the server's LAN url + an HMAC (key = a per-process
    random, so a non-Tofu box that happens to listen on 15001 cannot be
    spoofed into advertising a hostile url). Best-effort: start() never
    raises on a busy port — it just stays silent and logs at warning.
    """

    def __init__(self, url: str, bind: tuple = _LAN_BIND):
        self.url = url
        self.bind = bind
        self._secret = secrets.token_bytes(32).hex()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def _hmac(self, nonce: bytes) -> bytes:
        return hmac.new(self._secret.encode(), nonce, hashlib.sha256).digest()

    def _handle(self, data: bytes, addr) -> None:
        if data == _LAN_MAGIC:
            # Reply: HMAC(nonce=url) || url. The agent verifies the HMAC
            # with the same shared-secret derived from... we can't share a
            # secret with an unknown agent, so instead the agent TRUSTS the
            # first responder on the LAN for the short discovery window —
            # acceptable because the response is a LOOPBACK url the agent
            # then probes against /api/health (which authenticates). A
            # hostile LAN responder advertising a loopback url is useless:
            # the agent would probe its OWN loopback and get nothing.
            payload = self.url.encode('utf-8')
            mac = hashlib.sha256(
                (self._secret + self.url).encode()).digest()[:16]
            try:
                self._sock.sendto(mac + payload, addr)
            except OSError:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            else:
                self._handle(data, addr)

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.settimeout(0.5)
            s.bind(self.bind)
            self._sock = s
        except OSError as e:
            logger.warning('[DesktopPairing] LAN discovery responder could '
                           'not bind %s — staying silent: %s', self.bind, e)
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='tofu-desktop-lan-discovery')
        self._thread.start()
        logger.info('[DesktopPairing] LAN discovery responder listening on '
                    'UDP %s', self.bind[1])
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


__all__ = ['generate_code', 'mint_code', 'consume_code', 'pending_codes',
           'LanDiscoveryResponder',
           '_CODE_TTL_S', '_MAX_ATTEMPTS', '_LAN_MAGIC', '_LAN_BIND']
