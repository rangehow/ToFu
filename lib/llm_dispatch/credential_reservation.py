"""Experiment-reserved credentials that fallback dispatch never selects.

Entry points: :func:`fallback_dispatch` (context manager, also usable as a
decorator) marks the current execution context as fallback traffic;
:func:`reserved_slot` is read by ``LLMDispatcher._selection_scope``.
Data: ``contracts/experiment_credential_reservations_v1.json``, declared from
the experiment owners' launchers (docs/NETWORK.md, "Experiment-reserved
credentials"). Dependencies: standard library only.

A content-filter refusal moves one request to the configured fallback model.
That traffic is Tofu's own choice, not a model the owner picked for the
conversation, so it must not compete with experiments for the per-AppId
minute limit of a key those experiments depend on. Selection is narrowed
through a context variable, like ``provider_pin``: dispatch on the same
thread inherits it, and every selection API reads the same scope, so the
picker, capability checks and cooldown summaries agree.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ['fallback_dispatch', 'reservations', 'reserved_slot', 'active_reservations']

_RESERVATIONS_PATH = (Path(__file__).resolve().parents[2]
                      / 'contracts' / 'experiment_credential_reservations_v1.json')

_active: contextvars.ContextVar[frozenset[tuple[str, str]] | None] = (
    contextvars.ContextVar('tofu_fallback_credential_reservations', default=None))


@lru_cache(maxsize=1)
def reservations() -> frozenset[tuple[str, str]]:
    """``(gateway_host, key_tail)`` pairs declared for experiments."""
    document = json.loads(_RESERVATIONS_PATH.read_text(encoding='utf-8'))
    return frozenset(
        (str(row['gateway_host']).lower(), str(row['key_tail']))
        for row in document['reservations'])


def active_reservations() -> frozenset[tuple[str, str]] | None:
    """The reservations the current context must avoid; None outside fallback."""
    return _active.get()


def reserved_slot(slot, reserved: frozenset[tuple[str, str]]) -> bool:
    """True when *slot*'s gateway host and key tail match a reservation.

    The tail is the gateway's own name for the AppId (FRIDAY reports
    ``App:**<tail>``), so it identifies the quota the experiments use.
    """
    api_key = str(getattr(slot, 'api_key', '') or '')
    if not api_key:
        return False
    host = (urlsplit(str(getattr(slot, 'base_url', '') or '')).hostname or '').lower()
    return any(host == reserved_host and api_key.endswith(tail)
               for reserved_host, tail in reserved)


@contextlib.contextmanager
def fallback_dispatch():
    """Dispatch inside this block never selects an experiment-reserved key.

    The declaration is read before any dispatcher lock is taken. Nesting is
    harmless: the inner scope binds the same set and restores on exit.
    """
    token = _active.set(reservations())
    try:
        yield
    finally:
        _active.reset(token)
