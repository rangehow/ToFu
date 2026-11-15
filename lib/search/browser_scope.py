"""Explicit browser selection, task scope and authenticated file handoffs.

Entry points: select_search_browser captures one immutable SearchBrowserBinding;
its activate() method carries that decision into workers without a registry
lookup. bind_search_browser combines selection and activation for direct calls.
owner_browser_refusal decides whether an executing task may borrow the owner's
real browser at all; a refused binding carries its reason, which
withheld_browser_notice explains once a fallback wanted the browser.
Provider identity and one-time file response cleanup share this scope.
No HTTP context, model authorization or optional tofu-search imports live here.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple

from lib.browser.log_safety import text_for_log, url_for_log
from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

_search_browser_binding = ContextVar('tofu_search_browser_binding', default=None)
_search_browser_file_handoffs = ContextVar(
    'tofu_search_browser_file_handoffs', default=None)
_search_browser_current_file = ContextVar(
    'tofu_search_browser_current_file', default=None)
_browser_file_handoff_escape_enabled = ContextVar(
    'tofu_browser_file_handoff_escape_enabled', default=False)
_search_browser_withheld_use = ContextVar(
    'tofu_search_browser_withheld_use', default=None)
_MAX_TASK_FILE_HANDOFFS = 16
_OWNER_SOURCE_VERDICT = '_ownerBrowserSourceVerdict'

# Why task execution may not borrow the owner's real, logged-in browser. That
# browser carries the owner's sessions, so only the owner's own attended,
# human-authored request may use it; no delegation grant covers it.
OWNER_BROWSER_REFUSALS = {
    'delegated_worker': 'this is delegated project work (a Worker or an '
                        'autonomous Coordinator continuation)',
    'delegated_subagent': 'this is a delegated sub-agent',
    'unattended': 'this task runs unattended',
    'nonhuman_request': "this turn answers automated input, not the owner's own message",
    'unverified_request': "the owner's own request could not be verified",
}
WITHHELD_BROWSER_NEXT_ACTION = (
    'Do not retry through another browser or cookie path; if the page or file '
    "needs the owner's login, report its URL to the owner.")


class BrowserFileHandoffReady(BaseException):
    """Non-error escape from tofu-search's legacy text-only provider seam.

    ``tofu-search`` intentionally catches every ordinary provider ``Exception``
    and then tries another transport. A completed one-time file response is
    not a provider failure, so continuing would issue a second GET. This
    ``BaseException`` subclass crosses that legacy catch only while
    :func:`browser_file_handoff_boundary` is active; the owning fetch handler
    catches it immediately and claims the exact transfer ID.
    """

    def __init__(self, source_url: str, transfer_id: str):
        self.source_url = str(source_url or '')
        self.transfer_id = str(transfer_id or '')
        super().__init__('Authenticated browser file response is ready')


@contextmanager
def browser_file_handoff_boundary():
    """Allow a completed browser file to stop the legacy text fallback chain."""
    token = _browser_file_handoff_escape_enabled.set(True)
    try:
        yield
    finally:
        _browser_file_handoff_escape_enabled.reset(token)


class _TaskFileHandoffs:
    """Small thread-safe transfer-ID registry shared across task fan-out."""

    def __init__(self):
        self._lock = threading.Lock()
        self._transfer_ids: dict[str, None] = {}

    def remember(self, transfer_id: str) -> tuple[str, ...]:
        evicted = []
        with self._lock:
            self._transfer_ids.pop(transfer_id, None)
            while len(self._transfer_ids) >= _MAX_TASK_FILE_HANDOFFS:
                oldest = next(iter(self._transfer_ids))
                self._transfer_ids.pop(oldest, None)
                evicted.append(oldest)
            self._transfer_ids[transfer_id] = None
        return tuple(evicted)

    def discard(self, transfer_id: str) -> None:
        with self._lock:
            self._transfer_ids.pop(transfer_id, None)

    def drain(self) -> tuple[str, ...]:
        with self._lock:
            transfer_ids = tuple(self._transfer_ids)
            self._transfer_ids.clear()
        return transfer_ids


class _WithheldUse:
    """Whether one activation wanted the owner's browser after it was withheld."""

    __slots__ = ('wanted',)

    def __init__(self):
        self.wanted = False


class SearchBrowserBinding(NamedTuple):
    """One captured owner/device/profile decision, including explicit absence.

    This is routing data, not an authorization grant. Browser commands still
    revalidate live ownership and access through the existing browser boundary.
    ``refusal`` (an ``OWNER_BROWSER_REFUSALS`` key) records that task execution
    was denied the owner's browser; such a binding never carries a device.
    """

    user_id: str
    client_id: str
    profile: str
    refusal: str = ''

    @contextmanager
    def activate(self):
        """Borrow this exact choice with fresh, exception-safe file handoffs."""
        handoff_registry = _TaskFileHandoffs()
        token = _search_browser_binding.set(self)
        handoff_token = _search_browser_file_handoffs.set(handoff_registry)
        current_file_token = _search_browser_current_file.set(None)
        withheld_token = _search_browser_withheld_use.set(_WithheldUse())
        try:
            yield self
        finally:
            # A completed response that the task never claimed has no remaining
            # user-visible value. Reclaim it at the task boundary instead of
            # relying on the registry TTL.
            try:
                for transfer_id in handoff_registry.drain():
                    _abort_bound_browser_file(transfer_id, binding=self)
            finally:
                _search_browser_withheld_use.reset(withheld_token)
                _search_browser_current_file.reset(current_file_token)
                _search_browser_file_handoffs.reset(handoff_token)
                _search_browser_binding.reset(token)


_UNBOUND = SearchBrowserBinding('', '', '')


def owner_browser_refusal(task) -> str:
    """Return ``''`` when ``task`` executes the owner's own attended request.

    Otherwise return the ``OWNER_BROWSER_REFUSALS`` code that withholds the
    owner's browser: project Workers and Coordinator continuations, swarm/Goal
    sub-agents, unattended runs, and Turns that answer automated input (timers,
    channel events, swarm continuations, reports). Inherited configuration such
    as a pinned ``browserClientId`` or ``browserEnabled`` never widens this.
    """
    from lib.tasks_pkg.invocation import is_child_invocation
    if task.get('_parentTaskId') or is_child_invocation(task):
        return 'delegated_subagent'
    from lib.project_dispatch.admission import is_dispatch_task
    if is_dispatch_task(task):
        return 'delegated_worker'
    try:
        from lib.identity import PrincipalContext
        principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
        owner = require_user_id(task.get('_userId'), context='task owner')
    except (AttributeError, TypeError, ValueError):
        return 'unattended'
    if (task.get('_attended') is not True or principal.kind != 'user'
            or principal.owner_user_id != owner):
        return 'unattended'
    return _human_source_refusal(task, owner)


def _human_source_refusal(task, owner: int) -> str:
    """The task's reply Turn must answer a genuine human input Turn of ``owner``.

    A Turn's input never changes, so a verified verdict is pinned on the task
    per Turn (like the channel review ceiling); a failed read is not pinned.
    """
    conversation_id = str(task.get('convId') or '')
    turn_id = str(task.get('_turnId') or (task.get('config') or {}).get('_turnId') or '')
    if not conversation_id or not turn_id:
        return 'nonhuman_request'
    identity = [owner, conversation_id, turn_id]
    pinned = task.get(_OWNER_SOURCE_VERDICT)
    if isinstance(pinned, dict) and pinned.get('identity') == identity:
        return pinned['refusal']
    from lib.turn_initiation import require_human_turn
    from lib.turn_lifecycle import get_turn
    try:
        reply = get_turn(conversation_id, turn_id, user_id=owner, include_projection=False)
        source_id = reply.get('parentTurnId') if reply.get('actor') == 'assistant' else None
        source = get_turn(conversation_id, source_id, user_id=owner) if source_id else None
        try:
            require_human_turn(source, user_id=owner)
            refusal = '' if source.get('conversationId') == conversation_id else 'nonhuman_request'
        except ValueError:
            refusal = 'nonhuman_request'
    except Exception as exc:
        logger.warning('[Bridge] owner request verification failed for owner=%s: %s',
                       owner, type(exc).__name__)
        return 'unverified_request'
    task[_OWNER_SOURCE_VERDICT] = {'identity': identity, 'refusal': refusal}
    return refusal


def select_search_browser(
    *,
    task,
    user_id='',
    client_id='',
    required_capabilities=(),
) -> SearchBrowserBinding:
    """Select one caller's browser once; an unavailable choice stays inert.

    ``task`` is required: the executing task for tool execution, or ``None``
    for the authenticated owner's own HTTP request naming ``user_id``. A task
    names its owner itself and borrows the owner's real browser only when
    ``owner_browser_refusal`` allows it; otherwise the binding records the
    refusal and no device is looked up.

    An explicit client remains authoritative and will surface an upgrade error
    if it lacks a capability. Without an explicit selection, prefer the
    freshest compatible owned device; if none is compatible, retain the
    freshest owned device so the strict caller can distinguish upgrade from
    offline instead of silently borrowing another owner's/global device.
    """
    if task is not None:
        if user_id not in ('', None):
            raise TypeError('Task execution names its browser owner through the task')
        user_id = task.get('_userId', '')
    uid = '' if user_id is None or (isinstance(user_id, str) and user_id == '') else str(
        require_user_id(user_id, context='search browser owner'))
    if not uid:
        return _UNBOUND
    refusal = '' if task is None else owner_browser_refusal(task)
    if refusal:
        logger.debug('[Bridge] owner browser withheld from task: %s', refusal)
        return SearchBrowserBinding(uid, '', '', refusal)
    try:
        from lib.browser.queue import get_connected_clients
        clients = get_connected_clients(owner_user_id=uid)
        by_id = {str(row.get('client_id') or ''): row for row in clients}
        if client_id:
            selected = by_id.get(str(client_id or ''))
        else:
            required = {str(value) for value in required_capabilities if value}
            compatible = [
                row for row in clients
                if required <= set(row.get('capabilities') or ())
            ]
            candidates = compatible or clients
            selected = (
                max(candidates, key=lambda row: row.get('last_poll', 0))
                if candidates else None)
        return SearchBrowserBinding(
            uid, str((selected or {}).get('client_id') or ''),
            str((selected or {}).get('profile') or ''))
    except Exception as exc:
        logger.debug('[Bridge] task browser binding failed: %s', exc)
        return SearchBrowserBinding(uid, '', '')


@contextmanager
def bind_search_browser(*, task, user_id='', client_id='', required_capabilities=()):
    """Select and borrow a caller's browser, restoring any enclosing scope."""
    binding = select_search_browser(
        task=task, user_id=user_id, client_id=client_id,
        required_capabilities=required_capabilities)
    with binding.activate():
        yield binding


def current_browser_identity() -> SearchBrowserBinding:
    """Capture only the caller's explicit binding before provider fan-out."""
    return _search_browser_binding.get() or _UNBOUND


def withheld_browser_reason(refusal: str) -> str:
    """One model-facing sentence naming why the owner's browser was withheld."""
    reason = OWNER_BROWSER_REFUSALS.get(refusal) or OWNER_BROWSER_REFUSALS['unverified_request']
    return (f"The owner's logged-in browser was not used: {reason}; only the "
            "owner's own attended requests may use it.")


def note_withheld_browser_use() -> bool:
    """Record that a fallback wanted the owner's browser; True when it is withheld."""
    binding = _search_browser_binding.get()
    if binding is None or not binding.refusal:
        return False
    use = _search_browser_withheld_use.get()
    if use is not None:
        use.wanted = True
    return True


def withheld_browser_notice() -> str:
    """Explain a withheld browser once this activation's fallback wanted it."""
    binding = _search_browser_binding.get()
    use = _search_browser_withheld_use.get()
    if binding is None or not binding.refusal or use is None or not use.wanted:
        return ''
    return f'{withheld_browser_reason(binding.refusal)} {WITHHELD_BROWSER_NEXT_ACTION}'


def _abort_bound_browser_file(transfer_id: str, *, binding=None) -> None:
    binding = binding or _search_browser_binding.get() or _UNBOUND
    user_id, client_id = binding.user_id, binding.client_id
    if not user_id or not client_id or not transfer_id:
        return
    try:
        from lib.browser.file_transfer import file_transfer_store
        file_transfer_store.abort(
            transfer_id,
            owner_user_id=user_id,
            client_id=client_id,
            internal=True,
        )
    except Exception as exc:
        logger.debug('[Bridge] browser file handoff cleanup failed: %s', exc)


def remember_bound_browser_file(url: str, transfer_id: str) -> bool:
    """Bind an exact completed transfer to this task's fetch invocation."""
    binding = _search_browser_binding.get()
    handoffs = _search_browser_file_handoffs.get()
    if binding is None or handoffs is None:
        return False
    user_id, client_id = binding.user_id, binding.client_id
    transfer_id = str(transfer_id or '').strip()
    source = str(url or '')
    if not user_id or not client_id or not source or not transfer_id:
        return False

    previous = _search_browser_current_file.get()
    if previous and previous[1] != transfer_id:
        handoffs.discard(previous[1])
        _abort_bound_browser_file(previous[1], binding=binding)
    for evicted in handoffs.remember(transfer_id):
        _abort_bound_browser_file(evicted, binding=binding)
    _search_browser_current_file.set((source, transfer_id))
    return True


def claim_bound_browser_file(url: str):
    """Claim this task's exact completed file-transfer ID."""
    binding = _search_browser_binding.get()
    handoffs = _search_browser_file_handoffs.get()
    if binding is None or handoffs is None:
        return None
    user_id, client_id = binding.user_id, binding.client_id
    if not user_id or not client_id:
        return None
    current = _search_browser_current_file.get()
    if not current or current[0] != str(url or ''):
        return None
    transfer_id = current[1]
    _search_browser_current_file.set(None)
    handoffs.discard(transfer_id)
    try:
        from lib.browser.file_transfer import file_transfer_store
        return file_transfer_store.consume_completed(
            transfer_id,
            owner_user_id=user_id,
            client_id=client_id,
        )
    except Exception as exc:
        _abort_bound_browser_file(transfer_id, binding=binding)
        logger.info('[Bridge] could not claim completed browser file for %s: %s',
                    url_for_log(url), text_for_log(exc))
        return None


def require_bound_browser_file(url: str, *, max_bytes: int, timeout: int):
    """Stage response bytes through this task's exact browser or raise.

    The explicit server-download tool uses this strict form so an offline,
    outdated, denied, withheld or failed browser is visible as its typed
    recovery reason.  It never selects a process-global or merely recent device.
    """
    from lib.browser.file_transfer import (
        BrowserFileTransferError,
        fetch_file_via_browser,
    )

    binding = _search_browser_binding.get()
    if binding is None:
        raise BrowserFileTransferError(
            'browser_file_transfer_unbound',
            'No request-scoped browser identity is bound to this download',
            status=503,
        )
    if note_withheld_browser_use():
        raise BrowserFileTransferError(
            'owner_browser_withheld', withheld_browser_reason(binding.refusal),
            status=403)
    user_id, client_id = binding.user_id, binding.client_id
    if not user_id or not client_id:
        raise BrowserFileTransferError(
            'browser_file_transfer_offline',
            'No compatible browser extension is connected for this user',
            status=503,
        )
    existing = claim_bound_browser_file(url)
    if existing:
        return existing
    return fetch_file_via_browser(
        url,
        max_bytes=max_bytes,
        timeout=timeout,
        client_id=client_id,
        owner_user_id=user_id,
    )


def fetch_bound_browser_file(url: str, *, max_bytes: int, timeout: int):
    """Best-effort browser staging for the legacy text-fetch fallback.

    This is a host-only extension of tofu-search's provider seam: tofu-search
    continues to own server HTTP, while authenticated binary acquisition is
    routed through chatui's owner/device authority.  An unbound worker is
    inert and can never fall back to a globally recent browser.
    """
    try:
        return require_bound_browser_file(
            url, max_bytes=max_bytes, timeout=timeout)
    except Exception as exc:
        binding = _search_browser_binding.get() or _UNBOUND
        user_id, client_id = binding.user_id, binding.client_id
        logger.info(
            '[Bridge] authenticated browser file transfer failed for %s '
            '(owner=%s client=%s): %s',
            url_for_log(url), user_id, client_id[:12], text_for_log(exc),
        )
        return None


def browser_file_handoff_enabled() -> bool:
    """Whether this caller can settle a file response outside text fallback."""
    return bool(_browser_file_handoff_escape_enabled.get())
