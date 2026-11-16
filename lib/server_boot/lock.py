"""Single-instance startup lock + heartbeat wedge detection (extracted from server.py).

Co-location requirement: ``tests/server_boot/test_instance_lock_reclaim.py`` and
``tests/server_boot/test_instance_lock_wedge_reclaim.py`` import this module and assign
``_pid_is_live_server`` / ``_holder_wedge_age`` directly, then call
``_acquire_instance_lock``. All these helpers must therefore live in this one
module object. ``server.py`` re-exports them for ``asgi.py`` and any remaining
direct users.
"""

import json
import logging
import os
import time

from lib.runtime.runtime_paths import data_root as _tofu_data_root
from runtime_guards import pid_is_alive as _pid_alive


def _read_instance_lock_entry(lock_path):
    """Read the ``<pid>@<host>`` first line of the single-instance lock file.

    Returns ``(pid:int|None, host:str|None)``. A missing/empty/malformed file
    yields ``(None, None)`` (or ``(None, host)`` if only the pid is unparseable).
    """
    try:
        with open(lock_path, 'r') as f:
            entry = (f.readline() or '').strip()
    except OSError:
        return None, None
    if not entry or '@' not in entry:
        return None, None
    pid_str, _, host = entry.partition('@')
    host = host.strip() or None
    try:
        return int(pid_str), host
    except (ValueError, TypeError):
        return None, host


def _pid_is_live_server(pid):
    """True iff *pid* is alive AND its ``/proc/<pid>/cmdline`` still looks like
    one of our server entries: ``server.py``, or ``local_kernel.py``, which
    serves the same application for the app that launched it.

    A dead pid → False, including an exited one that its parent has not reaped
    yet: signal 0 still reaches such a zombie and its cmdline reads empty, so
    an unreaped server would otherwise block every replacement boot. A live
    pid whose cmdline is provably NOT ``server.py`` (PID reuse) → False. If
    liveness or the cmdline cannot be established (no /proc, permission denied,
    empty cmdline) this conservatively returns True so we NEVER reclaim a lock
    whose owner might still be a running server.
    Mirrors stop.sh's ``kill -0`` + ``ps -o args`` server.py check.
    """
    if not _pid_alive(pid):
        return False
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', 'replace')
    except (OSError, ValueError):
        return True  # cannot inspect → assume a live server, refuse to reclaim
    if not cmdline.strip():
        return True  # ambiguous → conservative
    return 'server.py' in cmdline or 'local_kernel.py' in cmdline


# ── Loop-heartbeat sidecar (cross-process wedge detection for lock reclaim) ──
# A ``flock`` proves neither liveness nor HEALTH: a server whose event loop is
# wedged in a FUSE syscall (the proven root cause of the 5-minute restart
# stalls) is still alive, still ``server.py``, still holds the flock — so
# ``_pid_is_live_server`` reports True and the reclaim refuses, blocking the
# operator's restart. The fix is a second signal: the live loop persists a
# wall-clock heartbeat to a sidecar; a RESTARTING process reads it to tell a
# healthy holder (fresh heartbeat → refuse) from a wedged one (stale → reclaim).
#
# The sidecar lives on LOCAL disk, NOT under data/ (the FUSE mount that
# wedges): the reader runs in the restarting process DURING the exact FUSE
# stall we're detecting and must never block. Local xfs (``/tmp/tofu``) reads
# cannot block, and a loop wedged in a FUSE syscall simply stops REFRESHING
# the local file → its age grows → that IS the wedged signal. Wall-clock (not
# monotonic) because a DIFFERENT process interprets it.
_HEARTBEAT_FILE = 'server.heartbeat'


def _heartbeat_dir():
    """Local-disk directory for the loop-heartbeat sidecar (see block comment).

    Overridable via ``TOFU_HEARTBEAT_DIR``; defaults to the dedicated local
    runtime directory ``/tmp/tofu/heartbeat``.
    """
    d = (os.environ.get('TOFU_HEARTBEAT_DIR', '') or '').strip()
    return d or '/tmp/tofu/heartbeat'


def _heartbeat_path():
    """Absolute path of the heartbeat sidecar file."""
    return os.path.join(_heartbeat_dir(), _HEARTBEAT_FILE)


def _write_heartbeat(pid=None, ts=None, path=None, *, phase='serving'):
    """Atomically stamp ``{pid, ts}`` (wall-clock) into the sidecar.

    ``phase='booting'`` is stamped by the executable immediately after taking
    the instance lock, before importing the database or starting background
    writers.  A contender gives that phase a longer grace period than a stale
    serving-loop heartbeat, so a legitimate schema migration cannot be
    mistaken for a wedged server.  Best-effort: a write failure NEVER raises.
    Atomic (temp + ``os.replace``) means a concurrent reader never sees a
    half-written file. Returns True on success, False on any failure.
    """
    pid = os.getpid() if pid is None else pid
    ts = time.time() if ts is None else ts
    path = path or _heartbeat_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = '%s.%d.tmp' % (path, pid)
        with open(tmp, 'w') as f:
            f.write(json.dumps({'pid': pid, 'ts': ts, 'phase': phase}))
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError) as e:
        logging.getLogger('server').debug('[Heartbeat] write failed (%s) — '
                                          'letting the sidecar age', e)
        return False


def _read_heartbeat(path=None):
    """Read ``(pid:int|None, ts:float|None)`` from the sidecar.

    A missing / unreadable / unparseable file yields ``(None, None)`` — the
    normal case when no server is running and also the fail-safe for the
    reclaim decision (ambiguity → never claim wedge).
    """
    path = path or _heartbeat_path()
    try:
        with open(path) as f:
            data = json.loads(f.read() or '{}')
        pid = data.get('pid')
        ts = data.get('ts')
        return (int(pid) if pid is not None else None,
                float(ts) if ts is not None else None)
    except (OSError, ValueError, TypeError) as e:
        logging.getLogger('server').debug('[Heartbeat] read failed/absent: %s', e)
        return None, None


def _read_heartbeat_state(path=None):
    """Return the heartbeat record while keeping ``_read_heartbeat`` stable.

    Old sidecars without ``phase`` are serving heartbeats. Invalid records are
    ambiguous and therefore return ``None`` (the reclaim path fails safe).
    """
    path = path or _heartbeat_path()
    try:
        with open(path) as f:
            data = json.loads(f.read() or '{}')
        pid = int(data['pid'])
        ts = float(data['ts'])
        phase = str(data.get('phase') or 'serving')
        if phase not in ('booting', 'serving'):
            return None
        return {'pid': pid, 'ts': ts, 'phase': phase}
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _heartbeat_stale_threshold():
    """Seconds after which a heartbeat proves the loop is wedged.

    Conservative: ``max(30s, 3 × TOFU_LOOP_HEARTBEAT_SECS)`` — well beyond any
    healthy GC pause or momentary busy stretch, so a genuinely-running server
    is never falsely reclaimed.
    """
    try:
        bump = float(os.environ.get('TOFU_LOOP_HEARTBEAT_SECS', '') or '1')
    except (ValueError, TypeError):
        bump = 1.0
    if bump <= 0:
        bump = 1.0
    return max(30.0, bump * 3.0)


def _boot_heartbeat_stale_threshold():
    """Grace for a lock holder that has not reached the serving loop yet.

    Production startup can legitimately spend tens of seconds migrating and
    verifying a 20 GB SQLite authority.  Keep the bound finite so a process
    genuinely wedged during import remains reclaimable without weakening the
    normal 30-second serving-loop detector.
    """
    try:
        value = float(os.environ.get('TOFU_BOOT_HEARTBEAT_GRACE_SECS', '') or '180')
    except (ValueError, TypeError):
        value = 180.0
    return max(60.0, min(900.0, value))


_SERVE_MODE_FILE = '.last_serve_mode'


def _serve_mode_path():
    """Absolute path of the serve-mode sidecar (data/.last_serve_mode)."""
    return os.path.join(_tofu_data_root(), _SERVE_MODE_FILE)


def _record_serve_mode(mode, path=None):
    """Persist the protocol we are ACTUALLY serving ('http'|'https') so the
    watchdog (deploy/tofu_guard.sh) can (a) probe /api/health with the right
    scheme and (b) replay the same TLS decision on auto-relaunch — a
    cron-env relaunch re-runs _detect_reverse_proxy blind and came up TLS
    behind a plain-HTTP proxy (the 2026-08-03 'socket hang up' incident).
    Best-effort: a write failure must never block startup."""
    if mode not in ('http', 'https'):
        raise ValueError('serve mode must be http|https, got %r' % (mode,))
    path = path or _serve_mode_path()
    try:
        from lib.json_store import write_text_atomic
        write_text_atomic(path, mode + '\n')
    except Exception as e:
        logging.getLogger('server').warning(
            '[TLS] could not record serve mode to %s: %s', path, e)


def _holder_wedge_age(pid, now=None, path=None):
    """Return the heartbeat AGE (seconds) iff the sidecar PROVES *pid*'s event
    loop is wedged, else None.

    "Proves" = the heartbeat belongs to *pid* (its recorded pid matches, so we
    never judge a live server by a stale file from a DIFFERENT process) AND its
    wall-clock age exceeds ``_heartbeat_stale_threshold()``. Every ambiguous
    case — missing / unparseable file, mismatched pid, or a future-dated ts
    (clock skew) — returns None so the caller keeps today's refuse-to-reclaim
    behaviour. The age is returned (not just a bool) so the caller can log the
    concrete staleness.
    """
    state = _read_heartbeat_state(path)
    if state is None or state['pid'] != pid:
        return None
    now = time.time() if now is None else now
    age = now - state['ts']
    threshold = (_boot_heartbeat_stale_threshold()
                 if state['phase'] == 'booting'
                 else _heartbeat_stale_threshold())
    if age < 0 or age <= threshold:
        return None
    return age


def _reclaim_stale_instance_lock(lock_path, hostname, logger):
    """Decide whether a flock-contended instance lock is a STALE *local* lock we
    may reclaim, and if so unlink it so a fresh inode can be flock'd.

    Robustness rationale (the crux of the OOM-restart bug): ``flock`` is bound
    to an open file *description*, NOT to process liveness. When the previous
    server is SIGKILL'd (e.g. OOM) its atexit/lock-release never runs, and
    orphaned child processes may keep the fd — and thus the flock — open
    indefinitely; on a FUSE mount the advisory lock is not reliably released on
    unclean death either. So a contended flock does NOT prove "a server is
    running". We mirror stop.sh: read the recorded ``<pid>@<host>`` and ONLY
    when ``host == this machine`` AND that pid is not a live ``server.py`` do we
    ``unlink`` the lock path. Unlinking yields a brand-new inode on the retry;
    the orphan's surviving fd points at the now-unlinked OLD inode, so its
    lingering flock is harmless and our flock on the new inode succeeds.

    Cross-host staleness is deliberately NOT handled here (that is the PG
    heartbeat-takeover's domain) — a foreign-host lock is left untouched and the
    caller refuses to start.

    Returns True iff a stale local lock was unlinked (caller should retry the
    flock), else False.
    """
    pid, host = _read_instance_lock_entry(lock_path)
    if pid is None and host is None:
        logger.critical('[Lock] contended instance lock has no readable <pid>@<host> entry — '
                        'refusing to reclaim (a live peer may hold it)')
        return False
    if host and host != hostname:
        logger.critical('[Lock] instance lock held by another host: pid=%s host=%s (we are %s) — '
                        'refusing to reclaim a foreign lock (cross-host is PG-heartbeat territory)',
                        pid, host, hostname)
        return False
    if pid is not None and _pid_is_live_server(pid):
        # Heartbeat age is diagnostic, not ownership release. A blocked syscall
        # can return later with pending writes; unlinking its flock inode would
        # permit two live owners of one application/storage authority.
        wedge_age = _holder_wedge_age(pid)
        if wedge_age is None:
            logger.critical('[Lock] instance lock held by a LIVE local server (pid=%s host=%s) — '
                            'another instance is genuinely running', pid, host)
            return False
        logger.critical('[Lock] instance lock held by a WEDGED local server '
                        '(pid=%s host=%s) — loop heartbeat stale %.1fs (threshold=%.1fs); '
                        'preserving live owner until it exits or explicitly releases the lock', pid, host,
                        wedge_age, _heartbeat_stale_threshold())
        return False
    else:
        logger.warning('[Lock] reclaiming stale lock pid=%s host=%s (dead)', pid, host)
    try:
        os.unlink(lock_path)
    except OSError as e:
        logger.critical('[Lock] failed to unlink stale lock %s: %s', lock_path, e)
        return False
    return True


def _retire_previous_host(fd, lock_path, hostname, logger):
    """A lock that named another host hands its serving era to this one.

    Recovery here cannot observe that host's processes; without this record
    every Attempt it entered stays running and Stop cannot settle it. The record
    is written before this process stamps the lock, so a failed write leaves the
    old entry for the next boot to retire.
    """
    try:
        fd.seek(0)
        entry = (fd.readline() or '').strip()
        era_line = (fd.readline() or '').strip()
    except OSError:
        return
    pid, _, host = entry.partition('@')
    host = host.strip()
    if host and host == hostname:
        _retire_previous_era(entry, era_line, lock_path, host, logger)
        return
    if not host:
        return
    from lib.process_owner import record_host_retired
    try:
        record_host_retired(host, os.path.join(os.path.dirname(os.path.abspath(lock_path)),
                                               'process-exits'),
                            evidence={'lock': entry})
        logger.warning('[Lock] host %s handed this data root to %s (its server pid %s); '
                       'recorded its retirement', host, hostname, pid)
    except (OSError, ValueError) as e:
        logger.warning('[Lock] could not record retirement of host %s: %s', host, e)


def _retire_previous_era(entry, era_line, lock_path, host, logger):
    """A same-host lock stamped by another boot or PID namespace hands over that era.

    A container restarted under the same hostname cannot observe its previous
    PID namespace, and a host retirement never covers the reading host. A stamp
    without an era line predates era stamps and proves nothing.
    """
    import json
    from lib.process_owner import process_owner, record_era_retired
    try:
        era = json.loads(era_line) if era_line else None
    except ValueError:
        return
    current = process_owner()
    if (not isinstance(era, dict) or current is None
            or (era.get('boot'), era.get('pid_namespace'))
            == (current['boot'], current['pid_namespace'])):
        return
    try:
        record_era_retired({**era, 'host': host}, os.path.join(
            os.path.dirname(os.path.abspath(lock_path)), 'process-exits'),
            evidence={'lock': entry, 'era': era})
        logger.warning('[Lock] serving era %s of host %s ended (its server %s); recorded its '
                       'retirement', era, host, entry)
    except (OSError, ValueError) as e:
        logger.warning('[Lock] could not record retirement of era %s: %s', era, e)


def _lock_stamp(hostname):
    """``<pid>@<host>`` first line (every reader's format), then this process's era."""
    import json
    from lib.process_owner import process_owner
    current = process_owner()
    era = ('' if current is None else json.dumps(
        {'boot': current['boot'], 'pid_namespace': current['pid_namespace']}) + '\n')
    return '%d@%s\n%s' % (os.getpid(), hostname, era)


def _acquire_instance_lock(lock_path, logger, hostname=None, allow_reclaim=True,
                           *, mark_booting=False):
    """Acquire the exclusive single-instance lock at *lock_path*.

    Returns ``(ok, fd)``: ``(True, <open flocked fd>)`` on success — the caller
    MUST keep the fd open for the whole process lifetime — or ``(False, None)``
    when a live instance genuinely holds it. On a platform without ``fcntl`` /
    with an unopenable lock dir it degrades to best-effort ``(True, fd|None)``
    so a missing lock never blocks startup.

    Self-healing: on flock contention we do NOT assume a live server (see
    ``_reclaim_stale_instance_lock`` for why). If the recorded owner is a dead
    LOCAL pid we unlink the stale lock and retry ONCE on a fresh inode. A
    single bounded retry (``allow_reclaim=False``) guarantees no reclaim loop;
    if the retry still fails we log CRITICAL and refuse (caller surfaces the
    ``TOFU_SKIP_LOCK=1`` escape hatch).
    """
    if hostname is None:
        import socket as _s
        hostname = _s.gethostname()
    try:
        import fcntl
    except ImportError:
        logger.warning('[Lock] fcntl unavailable on this platform — skipping instance lock')
        try:
            return True, open(lock_path, 'a+')
        except OSError:
            return True, None
    try:
        if not os.path.exists(lock_path):
            open(lock_path, 'a').close()
        fd = open(lock_path, 'r+')
    except OSError as e:
        logger.warning('[Lock] cannot open lock file %s (%s) — proceeding without instance lock', lock_path, e)
        return True, None
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        fd.close()
        if allow_reclaim and _reclaim_stale_instance_lock(lock_path, hostname, logger):
            ok2, fd2 = _acquire_instance_lock(
                lock_path, logger, hostname=hostname, allow_reclaim=False,
                mark_booting=mark_booting)
            if ok2 and fd2 is not None:
                logger.info('[Lock] reclaimed stale lock and acquired fresh instance lock (pid=%d)', os.getpid())
            else:
                logger.critical('[Lock] reclaimed stale lock but STILL could not acquire flock — '
                                'refusing to start. Set TOFU_SKIP_LOCK=1 to override.')
            return ok2, fd2
        return False, None
    _retire_previous_host(fd, lock_path, hostname, logger)
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(_lock_stamp(hostname))
        fd.flush()
    except OSError as e:
        logger.debug('[Lock] could not stamp lock identity: %s', e)
    if mark_booting:
        _write_heartbeat(pid=os.getpid(), phase='booting')
    return True, fd


class ProjectOwnedByAnotherServer(RuntimeError):
    """A server on its own data root was given a project that another live server owns."""

    def __init__(self, project_path, lock_path, pid, host, cmdline):
        self.project_path, self.lock_path = project_path, lock_path
        self.pid, self.host, self.cmdline = pid, host, cmdline
        super().__init__(
            'TOFU_PROJECT_PATH=%s is owned by the live server %s@%s (%s), which holds %s. A server '
            "on its own data root must not serve another server's project: set TOFU_PROJECT_PATH "
            'to a scratch or workspace path.'
            % (project_path, pid, host or '?', cmdline or 'command line unreadable', lock_path))


def _proc_cmdline(pid):
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            return f.read().replace(b'\x00', b' ').decode('utf-8', 'replace').strip() or None
    except (OSError, ValueError):
        return None


def refuse_project_owned_by_another_server(project_path, own_lock_path, hostname=None):
    """Raise ``ProjectOwnedByAnotherServer`` when another live server owns *project_path*.

    A project's server is the one whose instance lock is ``<project>/data/.server.lock``, the
    file the manager (``server_manager.read_lock_status``) and ``supervisor.py`` read. A server
    whose own lock is that file is that server, and the instance lock already serializes it. Any
    other server, such as a scratch server on its own data root that inherited the launching
    shell's ``TOFU_PROJECT_PATH``, would otherwise put its project-scoped state into the live
    server's checkout. Ownership follows ``_reclaim_stale_instance_lock``: a dead local pid owns
    nothing; an owner on another host, or one whose identity cannot be read, counts as live.
    """
    project_lock = os.path.join(os.path.realpath(project_path), 'data', '.server.lock')
    if os.path.realpath(project_lock) == os.path.realpath(own_lock_path):
        return
    pid, host = _read_instance_lock_entry(project_lock)
    if pid is None or pid == os.getpid():
        return
    if hostname is None:
        import socket as _s
        hostname = _s.gethostname()
    if host and host != hostname:
        raise ProjectOwnedByAnotherServer(project_path, project_lock, pid, host, None)
    if _pid_is_live_server(pid):
        raise ProjectOwnedByAnotherServer(project_path, project_lock, pid, host or hostname,
                                          _proc_cmdline(pid))
