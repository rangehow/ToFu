"""Protected steps: process-lifetime markers that a server restart must not interrupt.

A protected step changes or publishes the checkout the server runs from:
``scripts/git_land.py`` landing a branch and ``export.py`` runs. Such a run may
be a foreground command of a conversation, a background command, a terminal
or another agent, so conversation activity alone cannot see it.
``protected_step(kind)`` keeps one marker under ``<data root>/protected-steps/``
while the step runs; ``active_protected_steps()`` reports the live ones to the
restart-protection signal (``lib.tasks_pkg.manager.list_running_tasks``), which
Settings restart, automatic restart, deferred-restart quiescence and the Global
Router's ``restart_server`` share.

A local marker counts while its process (PID and start time) is alive, so a
crashed run never blocks restarts. A marker written on another host counts
until its writer removes it: its liveness cannot be observed here, and a
restart must not boot from a half-landed checkout (fail closed; the restart
refusal names the marker so a stale one can be removed by hand).

A pending restart takes precedence over new protected steps
(``restart_precedence``; its locks live in ``protected-steps-locks/``). Continuous landings otherwise keep one step always
running, so every restart is refused or its hours-long engine certification is
invalidated by the next landing. The restart holds ``restart-precedence.lock``
exclusively for its whole lifetime, then waits on ``landing.lock`` until the
steps already running finish; a new step first waits for the precedence lock
(shared), then holds ``landing.lock`` shared while it runs. Both waits are
kernel ``flock`` waits: no polling interval, no timeout. A restart that hands
over retains its locks until its process exits, so a landing never starts
between handoff and shutdown; a restart that fails releases them and the
waiting steps resume.

Entry points: protected_step, active_protected_steps, protected_steps_root,
restart_precedence, pending_restart.
Dependencies: lib.runtime.runtime_paths (data root), runtime_guards (liveness).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import secrets
import socket
import time

_KINDS = frozenset({'git_land', 'export'})


def protected_steps_root() -> str:
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), 'protected-steps')


def _start_time(pid: int) -> str | None:
    from runtime_guards import proc_stat_fields
    fields = proc_stat_fields(pid)
    return fields[19] if fields is not None and len(fields) > 19 else None


_PRECEDENCE_LOCK = 'restart-precedence.lock'
_LANDING_LOCK = 'landing.lock'
_PENDING_RESTART = 'restart-pending.json'


def _locks_root() -> str:
    # Beside, not inside, the marker directory: markers are the only entries there.
    root = protected_steps_root() + '-locks'
    os.makedirs(root, exist_ok=True)
    return root


def _open_lock(name: str) -> int:
    return os.open(os.path.join(_locks_root(), name), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)


def _flock(fd: int, mode: int, on_wait=None) -> None:
    import fcntl
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        if on_wait is not None:
            on_wait()
        fcntl.flock(fd, mode)


def pending_restart() -> dict | None:
    """The restart that currently has precedence, or None."""
    import fcntl
    root = protected_steps_root() + '-locks'
    if _retained_here() is not None:
        return _retained.info
    try:
        fd = os.open(os.path.join(root, _PRECEDENCE_LOCK), os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                with open(os.path.join(root, _PENDING_RESTART), encoding='utf-8') as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                return {'detail': 'a restart (details unreadable)'}
        return None
    finally:
        os.close(fd)


_retained: 'RestartPrecedence | None' = None


def _retained_here() -> 'RestartPrecedence | None':
    """This process's retained precedence over the current data root's steps."""
    return _retained if _retained is not None and _retained.root == _locks_root() else None


class RestartPrecedence:
    """Held by one restart: new protected steps wait until ``release`` or process exit."""

    def __init__(self, fds: list[int], info_path: str):
        self._fds, self._info_path, self.retained, self.info = fds, info_path, False, {}
        self.root = os.path.dirname(info_path)

    def retain_until_exit(self) -> None:
        """The restart handed over: keep precedence until this process is replaced.

        Another restart in this same process reuses it (``restart_precedence``):
        a second lock on a new descriptor would wait for this process itself.
        """
        global _retained
        self.retained = True
        _retained = self

    def abandon(self) -> None:
        """The retained handoff failed after all: release now."""
        global _retained
        if _retained is self:
            _retained = None
        self.retained = False
        self.release()

    def release(self) -> None:
        if self.retained:
            return
        try:
            os.unlink(self._info_path)
        except FileNotFoundError:
            pass
        while self._fds:
            os.close(self._fds.pop())


@contextmanager
def restart_precedence(detail: str, *, on_wait=None):
    """Give one restart precedence over protected steps for the block (or, retained, the process).

    Waits for restarts ahead of it, then for the protected steps already
    running. ``on_wait(message)`` reports a wait once, before it blocks.
    """
    if _retained_here() is not None:
        yield _retained
        return
    fds = [_open_lock(_PRECEDENCE_LOCK)]
    holder = RestartPrecedence(fds, os.path.join(_locks_root(), _PENDING_RESTART))
    try:
        import fcntl
        _flock(fds[0], fcntl.LOCK_EX, on_wait and (
            lambda: on_wait('Waiting for another pending restart to finish.')))
        info = holder.info = {'host': socket.gethostname(), 'pid': os.getpid(),
                              'startedAt': time.time(), 'detail': str(detail)[:300]}
        staged = holder._info_path + f'.{os.getpid()}.tmp'
        with open(staged, 'w', encoding='utf-8') as handle:
            json.dump(info, handle)
        os.replace(staged, holder._info_path)
        fds.append(_open_lock(_LANDING_LOCK))
        _flock(fds[1], fcntl.LOCK_EX, on_wait and (lambda: on_wait(
            'Waiting for running protected steps to finish: ' + ', '.join(
                f"{step['kind']} {step.get('detail') or step['id']}" for step in active_protected_steps())
            + '; new ones wait for this restart.')))
        yield holder
    finally:
        holder.release()


@contextmanager
def protected_step(kind: str, *, detail: str = '', on_wait=None):
    """Mark this process as inside a protected step until the block exits.

    A pending restart goes first: the step waits for it, reporting
    ``on_wait(message)`` once, then starts.
    """
    if kind not in _KINDS:
        raise ValueError(f'unknown protected step: {kind!r}')
    import fcntl
    if _retained_here() is not None:
        raise RuntimeError(f'this process handed over a restart ({_retained.info.get("detail")}); '
                           f'a {kind} cannot start before it is replaced')
    root = protected_steps_root()
    os.makedirs(root, exist_ok=True)
    landing_fd = _open_lock(_LANDING_LOCK)
    try:
        precedence_fd = _open_lock(_PRECEDENCE_LOCK)
        try:
            def waiting():
                if on_wait is not None:
                    pending = pending_restart() or {}
                    on_wait(f"A restart is pending ({pending.get('detail') or 'details unreadable'}, "
                            f"pid {pending.get('pid')}); this {kind} waits for it, then continues.")
            _flock(precedence_fd, fcntl.LOCK_SH, waiting)
            _flock(landing_fd, fcntl.LOCK_SH)
        finally:
            os.close(precedence_fd)
        with _marker(root, kind, detail) as marker:
            yield marker
    finally:
        os.close(landing_fd)


@contextmanager
def _marker(root: str, kind: str, detail: str):
    host, pid = socket.gethostname(), os.getpid()
    identity = f'{kind}-{host}-{pid}-{secrets.token_hex(4)}'
    path = os.path.join(root, identity + '.json')
    marker = {'version': 1, 'id': identity, 'kind': kind, 'host': host, 'pid': pid,
              'startTime': _start_time(pid), 'startedAt': time.time(), 'detail': str(detail)[:300]}
    staged = path + '.tmp'
    with open(staged, 'w', encoding='utf-8') as handle:
        json.dump(marker, handle)
    os.replace(staged, path)
    try:
        yield marker
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _alive(marker: dict) -> bool:
    if marker.get('host') != socket.gethostname():
        return True
    from runtime_guards import pid_is_alive
    pid = marker.get('pid')
    if type(pid) is not int or not pid_is_alive(pid):
        return False
    expected = marker.get('startTime')
    return expected is None or _start_time(pid) in (None, expected)


def active_protected_steps() -> list[dict]:
    """Live protected steps, oldest first; an unreadable marker counts as live."""
    root = protected_steps_root()
    try:
        names = sorted(name for name in os.listdir(root) if name.endswith('.json'))
    except FileNotFoundError:
        return []
    steps = []
    for name in names:
        path = os.path.join(root, name)
        try:
            with open(path, encoding='utf-8') as handle:
                marker = json.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            marker = None
        if not isinstance(marker, dict) or not isinstance(marker.get('startedAt'), (int, float)):
            marker = {'id': name[:-5], 'kind': 'unknown', 'host': '', 'pid': None,
                      'startedAt': 0.0, 'unreadable': True}
        elif not _alive(marker):
            continue
        steps.append({**marker, 'path': path})
    return sorted(steps, key=lambda step: step['startedAt'])


__all__ = ['RestartPrecedence', 'active_protected_steps', 'pending_restart', 'protected_step',
           'protected_steps_root', 'restart_precedence']
