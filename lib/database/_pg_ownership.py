"""PostgreSQL ownership / lock / heartbeat / host-identity.

Extracted from lib/database/_bootstrap.py (2026-07-11, Decoupling D, §10-signed
off by owner). Self-contained leaf: owns the process-ownership flag, the
startup flock, the tofu heartbeat, the instance-stamp / copy-detection, and the
host-identity + owner-host markers. All FIVE mutable module globals
(_PG_STARTED_BY_US, _startup_lock_fd, _flock_enforced, _heartbeat_thread,
_HOST_IDENTITY_CACHE) live HERE with ALL their accessors, so no `global`
mutation straddles a module boundary. _bootstrap.py re-imports everything from
here (facade) so `_bootstrap.<name>` and `_bootstrap as b; b._<name>` keep
resolving for _core.py, pg_admin.py, desktop/, and the pg_* test suites.

The only two call-outs into core (_audit, _pg_real_connect_ok) are resolved
LAZILY (in-body import) to avoid an import cycle with _bootstrap.
"""

import getpass
import json
import os
import shutil
import threading
import time

from lib.compat import IS_LINUX, IS_MACOS, IS_WINDOWS
from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


def _audit(*a, **k):
    from lib.database._bootstrap import _audit as _core_audit
    return _core_audit(*a, **k)


def _pg_real_connect_ok(*a, **k):
    from lib.database._bootstrap import _pg_real_connect_ok as _f
    return _f(*a, **k)


# Explicit re-export surface (import * skips _underscore names without this).
# Every moved symbol the _bootstrap facade must expose so `_bootstrap.<name>`
# and `b._<private>` (pg_* tests) resolve. The two lazy shims above are NOT
# listed (they belong to core; re-exporting would shadow the real ones).
__all__ = [
    '_PG_STARTED_BY_US', '_HEARTBEAT_FILE', '_HEARTBEAT_TTL_S', '_HEARTBEAT_REFRESH_S',
    '_heartbeat_thread', '_heartbeat_stop_event', '_heartbeat_lock',
    '_STARTUP_LOCK_FILE', '_startup_lock_fd', '_startup_lock_mu', '_startup_lock_path',
    '_try_acquire_startup_lock', '_release_startup_lock', '_flock_enforced',
    '_flock_probe_mu', '_probe_flock_enforced', '_flock_required',
    '_verify_flock_support_or_warn', '_heartbeat_path', '_read_heartbeat',
    '_heartbeat_is_fresh', '_write_heartbeat', '_clear_heartbeat', '_heartbeat_loop',
    '_start_heartbeat_thread', 'stop_heartbeat', '_INSTANCE_ID_FILE', '_instance_id_path',
    '_canonical_pgdata_path', '_read_instance_stamp', '_write_instance_stamp',
    '_pgdata_was_copied', '_clear_ownership_markers', '_heal_if_copied',
    '_standalone_mode', '_heal_if_standalone_remote_owner', '_mark_pg_owned_locally',
    'is_pg_owned_locally', '_find_pg_binary', '_get_username', '_read_pg_host_from_pidfile',
    '_pidfile_pid_is_live_local_postgres', '_get_local_ip', '_HOST_IDENTITY_CACHE',
    '_OWNER_ID_FILE', '_get_host_identity', '_owner_is_self', '_write_owner_host',
    '_pg_already_running_on_another_machine', '_find_free_port', '_fix_unix_socket_conf',
]


_PG_STARTED_BY_US = False


# ─────────────────────────────────────────────────────────────────────
#  Tofu-level heartbeat
#
#  The shared FUSE-mounted pgdata is occasionally inherited from a
#  previous host that didn't shut down cleanly: its postmaster may still
#  be TCP-reachable, but no tofu process there is actively using it.
#  A new server.py on this host would otherwise read .pg_owner_host,
#  see the remote PG answers, and route every DB call across the
#  network — only to time out when the abandoned remote eventually
#  drops or stalls.
#
#  The heartbeat file (`pgdata/.tofu_heartbeat`) is written by the
#  process that actually owns the local PG, refreshed every
#  _HEARTBEAT_REFRESH_S seconds, and cleared on clean shutdown. A new
#  startup considers the previous owner alive iff the heartbeat is
#  fresher than _HEARTBEAT_TTL_S. Otherwise it auto-heals: clears the
#  ownership markers and starts PG locally.
# ─────────────────────────────────────────────────────────────────────

_HEARTBEAT_FILE = '.tofu_heartbeat'
_HEARTBEAT_TTL_S = 120
_HEARTBEAT_REFRESH_S = 30

_heartbeat_thread = None
_heartbeat_stop_event = threading.Event()
_heartbeat_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────
#  Cross-host startup lock (anti-concurrent-pg_ctl race guard)
#
#  When two tofu hosts share the same FUSE-mounted pgdata, both can
#  simultaneously conclude "PG is down, I'll start it" and race to
#  pg_ctl start. Each new postmaster sees the OTHER host's PID in
#  postmaster.pid, immediate-shutdowns with "lock file contains wrong
#  PID", and leaves truncated WAL records — after which every further
#  startup on either side PANICs with "could not locate a valid
#  checkpoint record". The heartbeat alone can't prevent this: it only
#  defends sequential handoff (A dies → B takes over), not concurrent
#  startup within the same 60–120s window.
#
#  The fix: use an advisory POSIX flock() on a lock file INSIDE pgdata.
#  Since pgdata is the shared FUSE mount, the lock is visible to every
#  host that could race with us. The lock is acquired BEFORE any
#  pg_ctl start call and held for the process's lifetime via a retained
#  file descriptor — a process crash releases it, but no graceful exit
#  is needed. If another host already holds it, we abort the start
#  attempt and let the caller fall back to SQLite (or retry next cycle).
#
#  flock() over FUSE is best-effort; if the backend doesn't support it
#  we get IOError/OSError at acquire time and we LOG but do NOT skip
#  the start — we preserve the pre-fix behavior so hosts without FUSE
#  flock support aren't newly regressed. Audit-log emits a signal so
#  multi-host collisions are easy to grep from outside.
# ─────────────────────────────────────────────────────────────────────

_STARTUP_LOCK_FILE = '.tofu_pg_start.lock'
_startup_lock_fd = None         # Lock fd, retained for process life
_startup_lock_mu = threading.Lock()


def _startup_lock_path(pgdata):
    return os.path.join(pgdata, _STARTUP_LOCK_FILE)


def _try_acquire_startup_lock(pgdata):
    """Try to acquire an exclusive cross-host lock on the pgdata startup lock.

    Returns:
        True  — lock held (or flock unsupported / degraded to no-op).
        False — another host holds the lock; caller MUST NOT call pg_ctl start.
    """
    global _startup_lock_fd
    with _startup_lock_mu:
        if _startup_lock_fd is not None:
            # Already held by this process.
            return True

        path = _startup_lock_path(pgdata)
        try:
            os.makedirs(pgdata, exist_ok=True)
        except OSError as e:
            logger.warning('[DB] Could not ensure pgdata exists for startup lock: %s', e)
            return True  # Degrade to pre-fix behavior: let the caller try.

        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            logger.warning('[DB] Could not open startup-lock file %s: %s — '
                           'proceeding without cross-host lock', path, e)
            return True

        if IS_WINDOWS:
            # No portable fcntl.flock on Windows. Windows FUSE shares are
            # rare for pgdata, so we degrade to no-op rather than ship a
            # half-reliable msvcrt.locking code path.
            _startup_lock_fd = fd
            logger.debug('[DB] Startup lock: Windows — no flock, acquired fd=%d '
                         'as a no-op placeholder', fd)
            return True

        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            # Non-Linux/macOS POSIX without fcntl — degrade to no-op.
            _startup_lock_fd = fd
            logger.debug('[DB] Startup lock: fcntl unavailable — degraded to no-op')
            return True
        except (OSError, IOError) as e:
            # Two possible causes:
            #   1. EWOULDBLOCK — another process (possibly on another
            #      host) already holds it. Treat as concurrent-start.
            #   2. ENOLCK / EOPNOTSUPP — FUSE backend doesn't implement
            #      advisory locks. Degrade to no-op with a warning so the
            #      pre-fix behavior is preserved on unsupported backends.
            import errno as _errno
            err_code = getattr(e, 'errno', None)
            if err_code in (_errno.EWOULDBLOCK, _errno.EAGAIN, _errno.EACCES):
                logger.warning('[DB] Startup lock HELD by another process/host '
                               '(pgdata=%s): %s — skipping our pg_ctl start to '
                               'avoid WAL race', pgdata, e)
                try:
                    os.close(fd)
                except OSError as _ce:
                    logger.debug('[DB] Close after lock-contention failed: %s', _ce)
                try:
                    from lib.log import audit_log as _audit
                    _audit('pg_concurrent_start_detected',
                           pgdata=pgdata, our_host=_get_local_ip(),
                           err=str(e)[:200])
                except Exception as _audit_err:
                    logger.debug('[DB] audit_log for pg_concurrent_start_detected '
                                 'failed: %s', _audit_err)
                return False
            # flock not supported by this FS — keep behavior, log loudly once.
            logger.warning('[DB] flock() on %s not supported by filesystem '
                           '(errno=%s: %s) — cross-host race guard DISABLED. '
                           'Multiple tofu hosts sharing this pgdata may '
                           'corrupt WAL. Mount pgdata on a filesystem that '
                           'supports POSIX advisory locks (ext4/xfs/NFSv4/'
                           'most FUSE) to re-enable.',
                           path, err_code, e)
            _startup_lock_fd = fd
            return True

        _startup_lock_fd = fd
        logger.info('[DB] Acquired cross-host startup lock on %s', path)
        return True


def _release_startup_lock():
    """Release the startup lock if held. Safe to call multiple times."""
    global _startup_lock_fd
    with _startup_lock_mu:
        fd = _startup_lock_fd
        _startup_lock_fd = None
    if fd is None:
        return
    try:
        if not IS_WINDOWS:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError) as e:
                logger.debug('[DB] flock release raised (harmless): %s', e)
    finally:
        try:
            os.close(fd)
        except OSError as e:
            logger.debug('[DB] Close of startup lock fd failed: %s', e)


# Cached flock-enforcement verdict for the pgdata mount, set once per process
# by _probe_flock_enforced(): True = real advisory locks, False = silent no-op
# / unsupported, None = not yet probed.
_flock_enforced = None
_flock_probe_mu = threading.Lock()


def _probe_flock_enforced(pgdata):
    """Actively verify the pgdata mount truly ENFORCES POSIX advisory locks.

    The plain ``flock(LOCK_EX)`` call in ``_try_acquire_startup_lock`` only
    catches filesystems that FAIL locking with an errno (ENOLCK/EOPNOTSUPP).
    The dangerous case is a filesystem that silently treats every flock as a
    NO-OP: the call "succeeds", so two hosts both believe they hold the lock
    and start postmasters on the same pgdata → WAL/pg_subtrans corruption.

    This probe opens the SAME file twice (two independent open-file
    descriptions) and confirms that holding LOCK_EX on the first BLOCKS a
    LOCK_EX|LOCK_NB on the second. On a real locking FS the second call raises
    EWOULDBLOCK; a no-op FS grants both. Result is cached for the process.

    Returns:
        True  — advisory locks are genuinely enforced on this mount.
        False — locks are a silent no-op or unsupported (cross-host guard is
                NOT reliable here).
        None  — could not determine (probe itself errored unexpectedly); the
                caller should treat this conservatively, like False, for
                warning/refusal purposes.
    """
    global _flock_enforced
    with _flock_probe_mu:
        if _flock_enforced is not None:
            return _flock_enforced
        if IS_WINDOWS:
            _flock_enforced = False
            return False
        try:
            import fcntl
        except ImportError as e:
            logger.debug('[PgOwnership] fcntl unavailable, disabling flock fallback: %s', e)
            _flock_enforced = False
            return False

        probe_path = os.path.join(pgdata, '.tofu_flock_probe')
        fd1 = fd2 = None
        try:
            os.makedirs(pgdata, exist_ok=True)
            fd1 = os.open(probe_path, os.O_CREAT | os.O_RDWR, 0o644)
            fd2 = os.open(probe_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                import errno as _errno
                if getattr(e, 'errno', None) in (_errno.EWOULDBLOCK, _errno.EAGAIN):
                    _flock_enforced = True  # second lock correctly blocked
                else:
                    # Unexpected errno — be conservative and call it unenforced.
                    logger.debug('[DB] flock probe second-lock errno=%s: %s',
                                 getattr(e, 'errno', None), e)
                    _flock_enforced = False
            else:
                # Second LOCK_EX succeeded while the first was held → no-op FS.
                _flock_enforced = False
            return _flock_enforced
        except OSError as e:
            logger.warning('[DB] flock-enforcement probe could not run on %s: %s '
                           '— cannot confirm cross-host locking', pgdata, e)
            _flock_enforced = None
            return None
        finally:
            for _fd in (fd1, fd2):
                if _fd is not None:
                    try:
                        fcntl.flock(_fd, fcntl.LOCK_UN)
                    except OSError as _e:
                        logger.debug('[DB] flock probe unlock failed: %s', _e)
                    try:
                        os.close(_fd)
                    except OSError as _e:
                        logger.debug('[DB] flock probe close failed: %s', _e)
            try:
                os.remove(probe_path)
            except OSError as _e:
                logger.debug('[DB] flock probe cleanup failed: %s', _e)


def _flock_required():
    """Policy: should PG be REFUSED when advisory locks aren't enforced?

    Driven by env ``TOFU_PG_REQUIRE_FLOCK`` (legacy ``CHATUI_PG_REQUIRE_FLOCK``
    also honored). Truthy (1/true/yes/refuse) → refuse PG on a non-locking
    shared mount rather than risk a double-start. Default false → warn loudly
    but proceed (single-host deployments must not be regressed).
    """
    raw = getenv_compat('TOFU_PG_REQUIRE_FLOCK') or ''
    return raw.strip().lower() in ('1', 'true', 'yes', 'on', 'refuse', 'require')


def _verify_flock_support_or_warn(pgdata):
    """Probe flock enforcement once and surface the result loudly.

    Returns:
        True  — safe to proceed (locks enforced, OR not enforced but policy
                allows proceeding with a loud warning).
        False — caller MUST refuse to bootstrap/start PG (locks not enforced
                AND TOFU_PG_REQUIRE_FLOCK demands enforcement).
    """
    enforced = _probe_flock_enforced(pgdata)
    if enforced is True:
        logger.info('[DB] Advisory-lock enforcement verified on pgdata mount %s '
                    '— cross-host startup interlock is reliable.', pgdata)
        return True

    state = 'a SILENT NO-OP' if enforced is False else 'UNVERIFIABLE'
    if _flock_required():
        logger.critical(
            '[DB] REFUSING PostgreSQL: advisory locks on pgdata mount %s are %s, '
            'so the cross-host startup interlock CANNOT prevent two hosts from '
            'starting postmasters on the same data dir (WAL/pg_subtrans '
            'corruption). TOFU_PG_REQUIRE_FLOCK is set — falling back to SQLite. '
            'Mount pgdata on a filesystem with working POSIX advisory locks '
            '(ext4/xfs/NFSv4/most FUSE) to use PG here.', pgdata, state)
        try:
            from lib.log import audit_log as _audit
            _audit('pg_flock_unenforced_refused', pgdata=pgdata,
                   enforced=enforced, host=_get_local_ip())
        except Exception as _e:
            logger.debug('[DB] audit_log for pg_flock_unenforced_refused failed: %s', _e)
        return False

    logger.warning(
        '[DB] ⚠️  Advisory locks on pgdata mount %s are %s. The cross-host '
        'startup interlock degrades to heuristics here: if you run TWO tofu '
        'hosts against this SAME pgdata, a double postmaster start can corrupt '
        'WAL/pg_subtrans. This is safe for a SINGLE-host deployment. To make '
        'this fatal instead, set TOFU_PG_REQUIRE_FLOCK=1. To fix, mount pgdata '
        'on a filesystem with working POSIX advisory locks.', pgdata, state)
    try:
        from lib.log import audit_log as _audit
        _audit('pg_flock_unenforced_warning', pgdata=pgdata,
               enforced=enforced, host=_get_local_ip())
    except Exception as _e:
        logger.debug('[DB] audit_log for pg_flock_unenforced_warning failed: %s', _e)
    return True


def _heartbeat_path(pgdata):
    return os.path.join(pgdata, _HEARTBEAT_FILE)


def _read_heartbeat(pgdata):
    """Return parsed heartbeat dict ({host, pid, ts}) or None."""
    path = _heartbeat_path(pgdata)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.debug('[DB] Heartbeat at %s is not a dict', path)
        return None
    except FileNotFoundError as _e_audit:
        logger.debug('[_bootstrap] _read_heartbeat caught %s: %s', type(_e_audit).__name__, _e_audit)
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.debug('[DB] Could not read heartbeat at %s: %s', path, e)
        return None


def _heartbeat_is_fresh(pgdata, ttl_s=_HEARTBEAT_TTL_S):
    """Return (fresh, info_dict) — fresh=True if heartbeat exists and is
    within ttl_s seconds.

    info_dict carries {host, pid, ts, age_s} when the file is present
    (regardless of freshness) so the caller can log a useful message.
    """
    path = _heartbeat_path(pgdata)
    try:
        st = os.stat(path)
    except FileNotFoundError as _e_audit:
        logger.debug('[_bootstrap] _heartbeat_is_fresh caught %s: %s', type(_e_audit).__name__, _e_audit)
        return False, None
    except OSError as e:
        logger.debug('[DB] stat heartbeat failed: %s', e)
        return False, None

    age_s = time.time() - st.st_mtime
    info = _read_heartbeat(pgdata) or {}
    info = dict(info)
    info['age_s'] = age_s
    return age_s <= ttl_s, info


def _write_heartbeat(pgdata):
    """Write/refresh the heartbeat file. Best-effort."""
    payload = {
        'host': _get_local_ip(),
        'pid': os.getpid(),
        'ts': time.time(),
    }
    path = _heartbeat_path(pgdata)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug('[DB] Could not write heartbeat to %s: %s', path, e)


def _clear_heartbeat(pgdata):
    path = _heartbeat_path(pgdata)
    try:
        os.remove(path)
        logger.debug('[DB] Cleared heartbeat %s', path)
    except FileNotFoundError as _e_audit:
        logger.debug('[_bootstrap] _clear_heartbeat caught %s: %s', type(_e_audit).__name__, _e_audit)
    except OSError as e:
        logger.debug('[DB] Could not clear heartbeat at %s: %s', path, e)


def _heartbeat_loop(pgdata):
    logger.info('[DB] Heartbeat thread started (pgdata=%s, refresh=%ds, ttl=%ds)',
                pgdata, _HEARTBEAT_REFRESH_S, _HEARTBEAT_TTL_S)
    while not _heartbeat_stop_event.is_set():
        try:
            _write_heartbeat(pgdata)
        except Exception as e:
            logger.warning('[DB] Heartbeat refresh failed: %s', e)
        if _heartbeat_stop_event.wait(_HEARTBEAT_REFRESH_S):
            break
    logger.info('[DB] Heartbeat thread stopped')


def _start_heartbeat_thread(pgdata):
    """Start the heartbeat refresher (idempotent)."""
    global _heartbeat_thread
    with _heartbeat_lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            return
        _heartbeat_stop_event.clear()
        _write_heartbeat(pgdata)  # immediate first write
        t = threading.Thread(
            target=_heartbeat_loop, args=(pgdata,),
            name='tofu-pg-heartbeat', daemon=True,
        )
        t.start()
        _heartbeat_thread = t


def stop_heartbeat(pgdata=None):
    """Stop the heartbeat refresher and (optionally) clear the file.

    Called from server.py's clean-shutdown hook via _core.stop_local_pg_if_owned.
    """
    global _heartbeat_thread
    with _heartbeat_lock:
        _heartbeat_stop_event.set()
        t = _heartbeat_thread
        _heartbeat_thread = None
    if t is not None and t.is_alive():
        try:
            t.join(timeout=5)
        except Exception as e:
            logger.debug('[DB] Heartbeat thread join failed: %s', e)
    if pgdata is not None:
        _clear_heartbeat(pgdata)


# ─────────────────────────────────────────────────────────────────────
#  Instance-identity stamp — the copy/move detector
#
#  Ownership markers (`.pg_owner_host`, `.tofu_heartbeat`, `postmaster.pid`)
#  live INSIDE pgdata, which is exactly the directory people copy. When the
#  whole project is copied to a NEW path (a colleague's home, `tofu-meituan2`,
#  an open-source clone) the markers come along and the fresh instance trusts
#  them — silently routing every DB call back to the ORIGINAL machine's PG
#  via FUSE cross-machine discovery. Result: shared data, privacy leak, no
#  error shown.
#
#  The discriminator is the pgdata's own ABSOLUTE PATH. Legitimate cross-host
#  sharing happens at the SAME mount path (two machines, one FUSE pgdata);
#  a copy lands at a DIFFERENT path. So we stamp the canonical path (plus a
#  random instance id + creation ts) into `.pg_instance_id` whenever this
#  process takes local ownership. On a later startup, if the stamped path no
#  longer matches the current canonical path, the directory was copied/moved
#  → we ignore ALL inherited ownership markers and take over locally. This
#  strengthens — never weakens — the same-path multi-host failover logic,
#  which continues to rely on `.pg_owner_host` + heartbeat.
# ─────────────────────────────────────────────────────────────────────

_INSTANCE_ID_FILE = '.pg_instance_id'


def _instance_id_path(pgdata):
    return os.path.join(pgdata, _INSTANCE_ID_FILE)


def _canonical_pgdata_path(pgdata):
    """Return a stable canonical key for a pgdata location.

    Uses ``os.path.realpath`` (resolves symlinks + ``..``) so the same
    physical directory always produces the same string regardless of how
    it was addressed. Returns the input unchanged on error.
    """
    try:
        return os.path.realpath(pgdata)
    except OSError as e:
        logger.debug('[DB] realpath(%s) failed: %s', pgdata, e)
        return pgdata


def _read_instance_stamp(pgdata):
    """Return the parsed `.pg_instance_id` dict, or None if absent/invalid."""
    path = _instance_id_path(pgdata)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('path'):
            return data
        logger.debug('[DB] instance stamp at %s malformed: %r', path, data)
        return None
    except FileNotFoundError:
        logger.debug('[DB] instance stamp %s not present', path)
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.debug('[DB] Could not read instance stamp at %s: %s', path, e)
        return None


def _write_instance_stamp(pgdata):
    """Stamp this pgdata with its current canonical path + a fresh id.

    Idempotent for the path: if a stamp already exists for the SAME
    canonical path, the existing id/created are preserved (we only rewrite
    when the path differs or no stamp exists). Best-effort — failures are
    logged at debug and never abort startup.
    """
    import uuid
    canon = _canonical_pgdata_path(pgdata)
    existing = _read_instance_stamp(pgdata)
    if existing and _canonical_pgdata_path(existing.get('path', '')) == canon:
        return  # already stamped for this path — keep stable id
    payload = {
        'path': canon,
        'id': (existing or {}).get('id') or uuid.uuid4().hex,
        'created': (existing or {}).get('created') or time.time(),
        'restamped': time.time() if existing else None,
    }
    path = _instance_id_path(pgdata)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        logger.info('[DB] Stamped pgdata instance identity: path=%s id=%s',
                    canon, payload['id'])
    except OSError as e:
        logger.debug('[DB] Could not write instance stamp to %s: %s', path, e)


def _pgdata_was_copied(pgdata):
    """Return (was_copied, stamped_path) for this pgdata.

    ``was_copied`` is True only when a stamp EXISTS and its recorded
    canonical path differs from the current canonical path — i.e. the
    directory was copied or moved here from elsewhere. A missing stamp
    (legacy pgdata predating this mechanism, or a brand-new initdb) returns
    False so existing same-path multi-host behaviour is untouched; the stamp
    is written lazily the next time we take local ownership.
    """
    stamp = _read_instance_stamp(pgdata)
    if not stamp:
        return False, None
    stamped = _canonical_pgdata_path(stamp.get('path', ''))
    current = _canonical_pgdata_path(pgdata)
    if stamped and stamped != current:
        return True, stamped
    return False, stamped


def _clear_ownership_markers(pgdata, *, remove_pidfile=True, reason=''):
    """Remove the machine-specific ownership markers from a pgdata.

    Clears `.pg_owner_host`, the tofu heartbeat, and (optionally)
    `postmaster.pid` / `postmaster.opts`. The DATA files are never touched.
    Used by the copy/move self-heal and by the `reset-ownership` admin
    command. Best-effort; each failure is logged at warning level.
    """
    suffix = f' ({reason})' if reason else ''
    removed = []
    targets = ['.pg_owner_host', _OWNER_ID_FILE, _HEARTBEAT_FILE]
    if remove_pidfile:
        targets += ['postmaster.pid', 'postmaster.opts']
    for name in targets:
        p = os.path.join(pgdata, name)
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(name)
        except OSError as e:
            logger.warning('[DB] Could not remove ownership marker %s%s: %s',
                           name, suffix, e)
    if removed:
        logger.info('[DB] Cleared ownership markers%s: %s', suffix, ', '.join(removed))
    return removed


def _heal_if_copied(pgdata):
    """If this pgdata was copied/moved here, drop inherited ownership markers.

    Returns True when a copy was detected and markers were cleared (the
    caller should treat the directory as freshly-owned-locally), False
    otherwise. The instance stamp is re-written for the new path by the
    subsequent ``_mark_pg_owned_locally`` call.
    """
    was_copied, stamped = _pgdata_was_copied(pgdata)
    if not was_copied:
        return False
    logger.warning('[DB] pgdata was COPIED/MOVED here (stamped path=%s, '
                   'current=%s) — ignoring inherited ownership markers and '
                   'taking over locally. This prevents silently connecting to '
                   "the original machine's PostgreSQL.",
                   stamped, _canonical_pgdata_path(pgdata))
    try:
        from lib.log import audit_log as _audit
        _audit('pg_copied_pgdata_self_heal',
               stamped_path=stamped,
               current_path=_canonical_pgdata_path(pgdata))
    except Exception as e:
        logger.debug('[DB] audit_log for copy self-heal failed: %s', e)
    _clear_ownership_markers(pgdata, remove_pidfile=False, reason='copied pgdata')
    _clear_heartbeat(pgdata)
    return True


def _standalone_mode():
    """True when this deployment is a standalone single-machine copy.

    Set ``TOFU_PG_STANDALONE=1`` (``export.py`` seeds it into every exported
    ``.env``). In this mode we NEVER defer to a remote PG owner recorded in an
    inherited pgdata: such an ``.pg_owner_host`` / heartbeat comes from the
    machine the copy was made on — or a previous container sharing the same
    FUSE-mounted absolute path — NOT a live failover peer. We clear the
    inherited markers and own PG locally instead of routing every DB call
    across a dead cross-host link (the "connection ... timeout expired" crash).

    This deliberately disables same-path multi-host failover, which standalone
    deployments don't use. Same-path failover deployments must leave
    ``TOFU_PG_STANDALONE`` unset to keep the heartbeat handoff in
    ``_pg_already_running_on_another_machine`` Step 3.
    """
    return getenv_compat('TOFU_PG_STANDALONE', default='').strip().lower() in (
        '1', 'true', 'yes', 'on')


def _heal_if_standalone_remote_owner(pgdata):
    """In standalone mode, drop an inherited REMOTE-owner marker so we never
    defer to another machine's PG.

    Complements ``_heal_if_copied``: that one heals when the pgdata was copied
    to a DIFFERENT absolute path. On shared FUSE storage every container sees
    the pgdata at the SAME absolute path, so the stamp matches and copy-detect
    can't fire — yet the ``.pg_owner_host`` still points at a stale peer. The
    explicit ``TOFU_PG_STANDALONE`` flag resolves that ambiguity in favour of
    "own it locally".

    Returns True when an inherited remote marker was cleared (caller should
    treat the directory as freshly owned locally), False otherwise.
    """
    if not _standalone_mode():
        return False
    # Stable-identity guard: if .pg_owner_id says this pgdata is ours, an IP
    # flap (owner_host != local_ip) is NOT an inherited remote marker — it's
    # our own pgdata under a new container IP. Never clear in that case.
    if _owner_is_self(pgdata) is True:
        return False
    owner_host = _read_pg_host_from_pidfile(pgdata)
    if not owner_host:
        return False
    local_ip = _get_local_ip()
    if owner_host in (local_ip, 'localhost', '127.0.0.1'):
        return False  # owner is this host — nothing inherited to heal
    # Owner is remote. IP-independent safety: if our pidfile PID is a LIVE
    # local postgres, THIS host already owns pgdata (the .pg_owner_host IP can
    # be stale after a container-IP flap) — never clobber our own postmaster.
    if _pidfile_pid_is_live_local_postgres(pgdata):
        return False
    logger.warning('[DB] TOFU_PG_STANDALONE set and pgdata carries a REMOTE '
                   'owner marker (owner_host=%s, local_ip=%s) — inherited from '
                   'another machine/container, not a failover peer. Clearing it '
                   'and owning PG locally.', owner_host, local_ip)
    try:
        from lib.log import audit_log as _audit
        _audit('pg_standalone_heal_remote_owner',
               owner_host=owner_host, local_ip=local_ip,
               pgdata=_canonical_pgdata_path(pgdata))
    except Exception as e:
        logger.debug('[DB] audit_log for standalone heal failed: %s', e)
    _clear_ownership_markers(pgdata, remove_pidfile=False,
                             reason='standalone remote-owner marker')
    _clear_heartbeat(pgdata)
    return True


def _mark_pg_owned_locally(pgdata=None):
    """Record that this process is responsible for the local PG.

    When ``pgdata`` is provided, also starts the heartbeat refresher so
    other hosts (sharing the same FUSE-mounted pgdata) can tell that a
    tofu process is actively using this PG.
    """
    global _PG_STARTED_BY_US
    _PG_STARTED_BY_US = True
    if pgdata:
        _write_instance_stamp(pgdata)
        _start_heartbeat_thread(pgdata)


def is_pg_owned_locally():
    """Return True if this process started / took over a local PG server."""
    return _PG_STARTED_BY_US


def _find_pg_binary(name):
    """Locate a PostgreSQL binary by name, cross-platform.

    Uses ``shutil.which()`` which respects PATH on all platforms.
    On Windows, also checks common PostgreSQL install locations.

    Args:
        name: Binary name without extension (e.g. 'pg_ctl', 'initdb').

    Returns:
        Full path to the binary, or *name* itself if not found
        (so subprocess will raise FileNotFoundError with a clear message).
    """
    found = shutil.which(name)
    if found:
        return found
    # On macOS, try common Homebrew / MacPorts / Conda locations
    if IS_MACOS:
        mac_paths = [
            # Homebrew (Apple Silicon)
            '/opt/homebrew/bin',
            '/opt/homebrew/opt/postgresql/bin',
            # Homebrew (Intel)
            '/usr/local/bin',
            '/usr/local/opt/postgresql/bin',
            # MacPorts
            '/opt/local/bin',
            # Postgres.app
            '/Applications/Postgres.app/Contents/Versions/latest/bin',
        ]
        # Also check all Homebrew-versioned postgresql formulae
        for prefix in ['/opt/homebrew/opt', '/usr/local/opt']:
            for pg_ver in range(18, 12, -1):
                mac_paths.append(os.path.join(prefix, f'postgresql@{pg_ver}', 'bin'))
        # Check Conda envs — the user's active conda env and base
        conda_prefix = os.environ.get('CONDA_PREFIX', '')
        if conda_prefix:
            mac_paths.insert(0, os.path.join(conda_prefix, 'bin'))
        conda_base = os.environ.get('CONDA_PREFIX_1', '')  # base env when sub-env is active
        if conda_base:
            mac_paths.append(os.path.join(conda_base, 'bin'))
        for d in mac_paths:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                logger.info('[DB] Found %s at %s', name, candidate)
                return candidate
    # On Windows, try common PostgreSQL install paths
    if IS_WINDOWS:
        for pg_ver in range(18, 12, -1):
            candidate = os.path.join(
                os.environ.get('ProgramFiles', r'C:\Program Files'),
                'PostgreSQL', str(pg_ver), 'bin', f'{name}.exe'
            )
            if os.path.isfile(candidate):
                logger.info('[DB] Found %s at %s', name, candidate)
                return candidate
    # Return bare name — subprocess will raise FileNotFoundError
    return name


def _get_username(fallback='postgres'):
    """Get OS username cross-platform (Linux USER, Windows USERNAME)."""
    try:
        return getpass.getuser()
    except Exception as e:
        logger.debug('[DB] getuser() failed, using fallback %s: %s', fallback, e)
        return fallback


def _read_pg_host_from_pidfile(pgdata):
    """Read the PG owner host from .pg_owner_host on shared FUSE storage."""
    owner_file = os.path.join(pgdata, '.pg_owner_host')
    try:
        if os.path.exists(owner_file):
            with open(owner_file) as f:
                host = f.read().strip()
            if host:
                return host
    except Exception as e:
        logger.debug('[DB] Could not read .pg_owner_host: %s', e)
    return None


def _pidfile_pid_is_live_local_postgres(pgdata):
    """Return True if postmaster.pid names a PID that is a live local postgres.

    This is the IP-independent ground truth for "is OUR machine already
    running PG on this pgdata". The `.pg_owner_host` marker is derived from
    `_get_local_ip()`, which flaps when the container's IP is reassigned
    (cloud-IDE network changes) — making a host mistake its OWN postmaster
    for a remote one. A PID liveness + name check does not depend on the IP,
    so we use it as a hard guard before deleting the pidfile or starting a
    second postmaster (concurrent access to one pgdata corrupts pg_subtrans).

    Returns False if the pidfile is absent/unparseable, the PID is dead, or
    the PID belongs to a non-postgres process (genuinely stale pidfile).
    """
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    try:
        with open(pidfile) as f:
            pid = int(f.readline().strip())
    except (FileNotFoundError, ValueError) as e:
        logger.debug('[DB] pidfile liveness: cannot read PID from %s: %s', pidfile, e)
        return False
    except OSError as e:
        logger.debug('[DB] pidfile liveness: stat/read error on %s: %s', pidfile, e)
        return False
    try:
        from lib.compat import is_process_alive, is_process_named
        if not is_process_alive(pid):
            return False
        try:
            named = is_process_named(pid, 'postgres')
        except Exception as e:
            # Can't introspect the name (no /proc perms etc.) — be SAFE and
            # assume it IS our live postgres rather than risk a double-start.
            logger.warning('[DB] pidfile liveness: PID %d alive but name check '
                           'failed (%s) — assuming live postgres to avoid double-start', pid, e)
            return True
        if named:
            logger.info('[DB] pidfile liveness: PID %d is a LIVE local postgres '
                        '— this host already owns pgdata=%s', pid, pgdata)
            return True
        logger.info('[DB] pidfile liveness: PID %d alive but not postgres — stale pidfile', pid)
        return False
    except Exception as e:
        logger.warning('[DB] pidfile liveness check failed (%s) — assuming live '
                       'postgres to avoid double-start', e)
        return True


def _get_local_ip():
    """Get this machine's IP address (non-loopback)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as _e:
        logger.debug('[DB] UDP socket IP detection failed: %s', _e)
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception as _e2:
        logger.debug('[DB] gethostbyname fallback also failed: %s — returning 127.0.0.1', _e2)
        return '127.0.0.1'


_HOST_IDENTITY_CACHE = None
_OWNER_ID_FILE = '.pg_owner_id'


def _get_host_identity():
    """Return a STABLE per-host identity that does NOT flap when the container
    IP is reassigned (unlike ``_get_local_ip()``).

    ``_get_local_ip()`` uses a UDP-trick to 8.8.8.8 and returns whatever the
    container's current IP is. Cloud-IDE / FUSE deployments reassign that IP
    while the host stays the same, which made a server mistake its OWN pgdata
    for a remote one ("connection ... timeout expired" / split-brain). The
    machine-id (or hostname) stays constant across an IP flap but DIFFERS
    between genuinely different containers/hosts — exactly the semantics we
    want for "is this pgdata owned by THIS machine?".

    Order: ``TOFU_HOST_ID`` env override → ``/etc/machine-id`` →
    ``/var/lib/dbus/machine-id`` → ``socket.gethostname()``. Cached per process.
    """
    global _HOST_IDENTITY_CACHE
    if _HOST_IDENTITY_CACHE:
        return _HOST_IDENTITY_CACHE
    ident = (getenv_compat('TOFU_HOST_ID', default='') or '').strip()
    if not ident:
        for p in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
            try:
                with open(p) as f:
                    ident = f.read().strip()
                if ident:
                    break
            except OSError as e:
                logger.debug('[DB] host-identity: could not read %s: %s', p, e)
    if not ident:
        try:
            import socket
            ident = socket.gethostname().strip()
        except Exception as e:
            logger.debug('[DB] host-identity: gethostname failed: %s', e)
            ident = ''
    _HOST_IDENTITY_CACHE = ident or 'unknown-host'
    return _HOST_IDENTITY_CACHE


def _owner_is_self(pgdata):
    """IP-independent ownership check using the stable ``.pg_owner_id`` marker.

    Returns:
        True  — the stored host-identity equals ours (we own this pgdata,
                regardless of any IP flap recorded in ``.pg_owner_host``).
        False — the stored identity is a DIFFERENT host.
        None  — no ``.pg_owner_id`` marker (legacy pgdata or never written);
                caller must fall back to the IP / live-PID heuristics.
    """
    id_file = os.path.join(pgdata, _OWNER_ID_FILE)
    try:
        if os.path.exists(id_file):
            with open(id_file) as f:
                stored = f.read().strip()
            if stored:
                return stored == _get_host_identity()
    except OSError as e:
        logger.debug('[DB] Could not read %s: %s', _OWNER_ID_FILE, e)
    return None


def _write_owner_host(pgdata):
    """Write our IP to .pg_owner_host so other machines know where to connect,
    plus a stable host-identity to .pg_owner_id for IP-flap-proof self-check."""
    owner_file = os.path.join(pgdata, '.pg_owner_host')
    try:
        ip = _get_local_ip()
        with open(owner_file, 'w') as f:
            f.write(ip)
        logger.info('[DB] Wrote PG owner host: %s (id=%s)', ip, _get_host_identity())
    except Exception as e:
        logger.warning('[DB] Could not write .pg_owner_host: %s', e)
    id_file = os.path.join(pgdata, _OWNER_ID_FILE)
    try:
        with open(id_file, 'w') as f:
            f.write(_get_host_identity())
    except Exception as e:
        logger.warning('[DB] Could not write %s: %s', _OWNER_ID_FILE, e)


def _pg_already_running_on_another_machine(pgdata, pg_port):
    """Check if another machine owns the PG data directory.

    Returns:
        (True, host_ip) if another machine has PG running on this pgdata,
        (False, None) otherwise.
    """
    # Copy/move self-heal: if this pgdata was copied here from another path,
    # every inherited marker (owner_host, heartbeat, pidfile) belongs to the
    # ORIGINAL instance. Never defer to it — that is the "silently connect to
    # the source machine's PG" trap. Clear the markers and report no remote.
    if _heal_if_copied(pgdata):
        return False, None

    # Standalone single-machine copy: an inherited remote-owner marker (same
    # FUSE abs-path, different container/host) must not make us defer. Clear it
    # and own PG locally. No-op unless TOFU_PG_STANDALONE is set.
    if _heal_if_standalone_remote_owner(pgdata):
        return False, None

    pidfile = os.path.join(pgdata, 'postmaster.pid')
    if not os.path.exists(pidfile):
        logger.debug('[DB] No postmaster.pid — PG not running')
        return False, None

    try:
        with open(pidfile) as f:
            lines = f.readlines()
        if len(lines) < 2:
            logger.debug('[DB] postmaster.pid too short (%d lines) — treating as absent', len(lines))
            return False, None
        pid = int(lines[0].strip())
    except Exception as e:
        logger.warning('[DB] Cannot parse postmaster.pid: %s', e)
        return False, None

    # IP-independent identity check FIRST: the stable .pg_owner_id marker
    # (machine-id / hostname) does not flap when the container IP is
    # reassigned, unlike .pg_owner_host. If it says this pgdata is ours, we
    # own it — no matter what IP the (possibly stale) .pg_owner_host records.
    owner_self = _owner_is_self(pgdata)
    if owner_self is True:
        logger.info('[DB] .pg_owner_id matches this host (id=%s) — pgdata is OURS '
                    '(ignoring any IP flap in .pg_owner_host)', _get_host_identity())
        return False, None

    owner_host = _read_pg_host_from_pidfile(pgdata)
    local_ip = _get_local_ip()
    is_remote_owner = (
        owner_host is not None
        and owner_host not in (local_ip, 'localhost', '127.0.0.1')
    )
    # A DIFFERENT-host identity marker is authoritative proof of remoteness
    # even if the flapping IPs happen to coincide.
    if owner_self is False:
        is_remote_owner = True

    logger.info('[DB] postmaster.pid: PID=%d, owner_host=%s, local_ip=%s, owner_self=%s, is_remote=%s',
                pid, owner_host, local_ip, owner_self, is_remote_owner)

    # IP-independent ground truth: if the pidfile PID is a live local
    # postgres, THIS host already owns pgdata — regardless of what the
    # `.pg_owner_host` IP marker says. _get_local_ip() flaps when the
    # container IP is reassigned, which previously made a host mistake its
    # OWN postmaster for a remote one, delete the pidfile, and start a
    # SECOND postmaster on the same pgdata → pg_subtrans corruption. Trust
    # the PID over the IP.
    if is_remote_owner and _pidfile_pid_is_live_local_postgres(pgdata):
        logger.warning('[DB] postmaster.pid PID=%d is a LIVE local postgres but '
                       'owner_host=%s != local_ip=%s — IP flap detected. Treating '
                       'as OURS (not remote) to avoid a double-start.',
                       pid, owner_host, local_ip)
        return False, None

    if is_remote_owner:
        # Use a real psycopg2 connect probe — pg_isready can give false
        # positives on "half-alive" containers (TCP accept works but real
        # queries hang) which is exactly the container-switch scenario on
        # shared FUSE storage.
        reachable = _pg_real_connect_ok(owner_host, pg_port, None, None, timeout_s=5)
        logger.info('[DB] PG owned by remote host %s (real_connect=%s) — deferring to it', owner_host, reachable)
        return True, owner_host

    try:
        from lib.compat import is_process_alive, is_process_named
        if not is_process_alive(pid):
            raise ProcessLookupError(f'PID {pid} not alive')
        try:
            if is_process_named(pid, 'postgres'):
                logger.debug('[DB] PID %d is local postgres — already running', pid)
                return False, None
            else:
                logger.info('[DB] PID %d exists locally but is not postgres — stale pidfile', pid)
                return False, None
        except Exception as e:
            logger.warning('[DB] Cannot check PID %d command: %s — assuming stale', pid, e)
            return False, None
    except ProcessLookupError:
        logger.info('[DB] PID %d not found locally, owner=%s (us) — stale pidfile', pid, owner_host or 'unknown')
        return False, None
    except PermissionError:
        logger.info('[DB] Cannot signal PID %d (PermissionError) — assuming local PG running', pid)
        return False, None


def _find_free_port(start=15432, end=15500):
    """Find an available TCP port in [start, end) for PostgreSQL."""
    import socket
    for port in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            result = s.connect_ex(('127.0.0.1', port))
            s.close()
            if result != 0:
                return port
        except Exception as e:
            logger.debug('[DB] Port %d probe error (assuming free): %s', port, e)
            return port
    logger.warning('[DB] No free port found in %d–%d, falling back to %d', start, end, start)
    return start


def _fix_unix_socket_conf(pgdata):
    """Patch postgresql.conf to disable Unix sockets if needed.

    Disables Unix sockets on:
      - FUSE filesystems (Linux: /mnt/ paths) — FUSE doesn't support AF_UNIX
      - Windows — Unix domain sockets are only partially supported
    On macOS with local disk, Unix sockets are fine — skip patching.
    """
    # Decide if we need to disable unix sockets
    if IS_WINDOWS:
        reason = 'Windows (Unix sockets not reliably supported)'
    elif IS_LINUX and pgdata.startswith('/mnt/'):
        reason = 'FUSE filesystem does not support Unix sockets'
    else:
        # macOS and Linux on local disk — Unix sockets are fine
        return

    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return
    try:
        with open(conf_path) as f:
            content = f.read()
        if "unix_socket_directories = ''" in content:
            return
        import re
        new_content, count = re.subn(
            r"unix_socket_directories\s*=\s*'[^']*'",
            "unix_socket_directories = ''",
            content
        )
        if count > 0:
            with open(conf_path, 'w') as f:
                f.write(new_content)
            logger.info('[DB] Patched postgresql.conf: disabled unix_socket_directories (%s)', reason)
    except Exception as e:
        logger.warning('[DB] Could not patch unix_socket_directories in postgresql.conf: %s', e)
