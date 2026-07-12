"""PostgreSQL server management — auto-bootstrap, start, stop, remote discovery.

Extracted from _core.py for modularity. Called from _core at import time.
Cross-platform: works on Linux, macOS, and Windows.
"""

import os
import shutil
import subprocess
import sys
import time

from lib.compat import IS_MACOS, IS_WINDOWS
from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)

# ── Ownership / lock / heartbeat / host-identity relocated to
#    _pg_ownership.py (2026-07-11, Decoupling D). Re-imported here so every
#    caller of `_bootstrap.<name>` / `from lib.database._bootstrap import <name>`
#    (incl. _core.py, pg_admin.py, desktop/, and the pg_* test suites that do
#    `_bootstrap as b; b._<private>`) keeps resolving. The sibling resolves its
#    two core call-outs (_audit, _pg_real_connect_ok) lazily → no import cycle.
from lib.database import _pg_ownership as _pg_ownership  # noqa: F401,E402
from lib.database._pg_ownership import (  # noqa: F401,E402
    _PG_STARTED_BY_US, _HEARTBEAT_FILE, _HEARTBEAT_TTL_S, _HEARTBEAT_REFRESH_S,
    _heartbeat_thread, _heartbeat_stop_event, _heartbeat_lock,
    _STARTUP_LOCK_FILE, _startup_lock_fd, _startup_lock_mu, _startup_lock_path,
    _try_acquire_startup_lock, _release_startup_lock, _flock_enforced,
    _flock_probe_mu, _probe_flock_enforced, _flock_required,
    _verify_flock_support_or_warn, _heartbeat_path, _read_heartbeat,
    _heartbeat_is_fresh, _write_heartbeat, _clear_heartbeat, _heartbeat_loop,
    _start_heartbeat_thread, stop_heartbeat, _INSTANCE_ID_FILE, _instance_id_path,
    _canonical_pgdata_path, _read_instance_stamp, _write_instance_stamp,
    _pgdata_was_copied, _clear_ownership_markers, _heal_if_copied,
    _standalone_mode, _heal_if_standalone_remote_owner, _mark_pg_owned_locally,
    is_pg_owned_locally, _find_pg_binary, _get_username, _read_pg_host_from_pidfile,
    _pidfile_pid_is_live_local_postgres, _get_local_ip, _HOST_IDENTITY_CACHE,
    _OWNER_ID_FILE, _get_host_identity, _owner_is_self, _write_owner_host,
    _pg_already_running_on_another_machine, _find_free_port, _fix_unix_socket_conf,
)
# _PG_STARTED_BY_US + its accessors (_mark_pg_owned_locally / is_pg_owned_locally)
# now live in _pg_ownership; shutdown_pool() in _core.py reads them via the
# is_pg_owned_locally() FUNCTION (re-exported above), never the raw global.

# ── Seed/dump/legacy migration relocated to _pg_seed.py (2026-07-11,
#    Decoupling D sub-cut 2). Re-imported here so _bootstrap.<name> keeps
#    resolving. _pg_seed calls back into core lazily (no import cycle).
from lib.database import _pg_seed as _pg_seed  # noqa: F401,E402
from lib.database._pg_seed import (  # noqa: F401,E402
    _pgdata_major_compatible, _dump_live_cluster, _count_convs,
    _ensure_legacy_up_for_seed, _seed_local_pgdata_from_legacy,
)


# ── Backup / base-backup / PITR / self-heal relocated to _pg_backup.py
#    (2026-07-11, Decoupling D sub-cut 3). Re-imported here so _bootstrap.<name>
#    / `from lib.database._bootstrap import <name>` keep resolving for _core.py,
#    the scheduler, _pg_seed's own lazy shims, and the pg_*/tier_b/seed suites.
#    _pg_backup calls back into core lazily (no import cycle).
from lib.database import _pg_backup as _pg_backup  # noqa: F401,E402
from lib.database._pg_backup import (  # noqa: F401,E402
    backup_pg_database, _latest_pg_backup, _tier_b_wal_end_ts,
    _tier_a_dump_end_ts, _select_restore_channel, _base_backup_dir,
    _latest_base_backup, basebackup_pg_cluster, _recover_via_pitr,
    _quarantine_corrupt_pgdata, _try_self_heal_corrupt_pg,
)


# ─────────────────────────────────────────────────────────────────────
#  Managed PostgreSQL tuning block
#
#  Historically the durability + sizing settings (max_connections,
#  wal_level, fsync, …) were appended to postgresql.conf MANUALLY, once,
#  under a "# ── ChatUI Custom Config ──" header. Nothing in the codebase
#  maintained them, so:
#    • bumping the app-side TOFU_DB_MAX_CONNS did NOT raise PG's own
#      max_connections ceiling (a 1000-user deployment would still hit
#      PG "too many clients" at 200);
#    • durability settings could silently drift between deployments.
#
#  This function makes the config code-managed: every owned-PG startup
#  rewrites a single delimited block (idempotently). PG reads the LAST
#  occurrence of a setting in the file, so appending our block also
#  overrides any older manual entries above it.
# ─────────────────────────────────────────────────────────────────────

_MANAGED_BLOCK_BEGIN = '# ── Tofu managed config (auto-generated; do not edit) BEGIN ──'
_MANAGED_BLOCK_END = '# ── Tofu managed config END ──'

# PG server-side max_connections. Provisioned ABOVE the app-side semaphore
# ceiling (_MAX_TOTAL_CONNS, default 1000) so the application — not PG's
# hard FATAL limit — is always the binding constraint. The extra headroom
# absorbs superuser/maintenance/replication connections. Override via env.
_MANAGED_PG_MAX_CONNECTIONS = int(
    getenv_compat('TOFU_PG_MAX_CONNECTIONS', default='1100'))


def _tier_b_enabled():
    """True when Tier B (WAL archive + PITR) is opted in."""
    return getenv_compat('TOFU_DB_TIER_B', default='0').lower() in ('1', 'true', 'yes')


def _pgdata_is_resolved_primary(pgdata):
    """True when *pgdata* is the LOCAL primary the split resolves to (post-flip).

    Tier B archiving must engage ONLY against the local primary — never the
    legacy FUSE cluster while resolution is still on legacy (pre-seed). Writing
    archive_mode into the soon-to-be-retired legacy cluster would waste FUSE
    writes and muddy the §3a WAL-tail timestamp.
    """
    try:
        from lib.database.db_paths import resolve_pgdata_dir
        from lib.runtime_paths import data_root
        return os.path.abspath(pgdata) == os.path.abspath(resolve_pgdata_dir(data_root()))
    except Exception as e:
        logger.debug('[DB] primary-resolution probe failed: %s', e)
        return False


def _build_managed_pg_config(archive_enabled=False):
    """Return the body (settings only) of the managed postgresql.conf block.

    Args:
        archive_enabled: When True, emit Tier B ``archive_mode=on`` +
            ``archive_command``. Caller passes this only for the resolved LOCAL
            primary when TOFU_DB_TIER_B is opted in (never the legacy cluster).

    Durability is deliberately kept SAFE (fsync + synchronous_commit on,
    full_page_writes on) — the cluster lives on a shared FUSE mount where a
    torn page on crash would corrupt the whole cluster, exactly the failure
    that produced the 'lost conversations' incident. ``wal_level=replica``
    (up from the old ``minimal``) is what makes PITR / base-backup-based
    recovery possible, so a future corrupt primary is recoverable to a
    point-in-time instead of needing a data-losing ``pg_resetwal -f``.
    """
    return [
        f'max_connections = {_MANAGED_PG_MAX_CONNECTIONS}',
        'superuser_reserved_connections = 10',
        # Server-side backstop for leaked transactions: PG kills any backend
        # left 'idle in transaction' past this. Matched to the app-side idle
        # reaper (TOFU_DB_IDLE_RELEASE_S, default 120s) so a connection parked
        # mid-transaction by a long-lived worker is reclaimed even though the
        # app-side reaper deliberately skips non-IDLE connections.
        'idle_in_transaction_session_timeout = 120s',
        # ── Durability (do NOT relax on a FUSE-mounted cluster) ──
        'fsync = on',
        'synchronous_commit = on',
        'full_page_writes = on',
        # ── WAL: replica level enables base-backup + PITR recovery ──
        'wal_level = replica',
        'max_wal_senders = 10',
        'wal_compression = on',
        'max_wal_size = 2GB',
        'min_wal_size = 160MB',
        'checkpoint_completion_target = 0.9',
        # ── Memory sizing for a ~1000-connection workload ──
        'shared_buffers = 512MB',
        'effective_cache_size = 2GB',
        'work_mem = 8MB',
        'maintenance_work_mem = 128MB',
    ] + ([
        # ── Tier B: continuous WAL archiving to the DolphinFS durability
        # target (only on the resolved local primary; see B1/B2). The shim is
        # FUSE-stall-safe (hard timeout, non-zero-to-retain). Invoked as a
        # module so it inherits our env/paths and takes no shell string.
        'archive_mode = on',
        "archive_command = '%s -m lib.database.wal_archive archive %%p %%f'"
        % (sys.executable or 'python3'),
    ] if archive_enabled else [])


def _ensure_managed_pg_config(pgdata):
    """Idempotently write the managed tuning block into postgresql.conf.

    Returns:
        bool: True if the on-disk config CHANGED (caller should restart PG
        for ``max_connections`` / ``wal_level`` — which need a restart — to
        take effect), False if it was already up to date or on error.
    """
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return False

    # Tier B archiving engages ONLY when opted in AND this pgdata is the
    # resolved local primary (never the legacy cluster pre-flip).
    archive_enabled = _tier_b_enabled() and _pgdata_is_resolved_primary(pgdata)
    settings = _build_managed_pg_config(archive_enabled=archive_enabled)
    block_lines = [_MANAGED_BLOCK_BEGIN, *settings, _MANAGED_BLOCK_END]
    new_block = '\n'.join(block_lines) + '\n'

    try:
        with open(conf_path, encoding='utf-8') as f:
            content = f.read()

        import re
        # Strip any prior managed block (between the BEGIN/END markers).
        pattern = re.compile(
            re.escape(_MANAGED_BLOCK_BEGIN) + r'.*?' + re.escape(_MANAGED_BLOCK_END) + r'\n?',
            re.DOTALL)
        stripped = pattern.sub('', content)

        desired = stripped.rstrip('\n') + '\n\n' + new_block
        if desired == content:
            logger.debug('[DB] Managed PG config already current — no change')
            return False

        # Write atomically (temp file + replace) so a crash mid-write can't
        # leave a truncated postgresql.conf that bricks startup.
        tmp_path = conf_path + '.tofu.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(desired)
        os.replace(tmp_path, conf_path)
        logger.info('[DB] Wrote managed PG config block '
                    '(max_connections=%d, wal_level=replica, fsync=on) — '
                    'restart required for connection/WAL settings',
                    _MANAGED_PG_MAX_CONNECTIONS)
        return True
    except Exception as e:
        logger.warning('[DB] Could not write managed PG config block: %s', e)
        return False


def _restart_local_pg(pgdata, base_dir):
    """Restart the locally-owned PG with ``pg_ctl restart -m fast``.

    Used after _ensure_managed_pg_config reports a change to a restart-only
    setting (max_connections / wal_level). Best-effort: on failure the
    running PG keeps its previous (still-valid) config.

    Returns:
        bool: True on a successful restart.
    """
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    try:
        result = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path,
             'restart', '-m', 'fast', '-w', '-t', '30'],
            capture_output=True, text=True, timeout=45
        )
        if result.returncode != 0:
            logger.error('[DB] pg_ctl restart (for managed config) failed: %s',
                         (result.stderr or '').strip()[:300])
            return False
        logger.info('[DB] Restarted local PG to apply managed config')
        return True
    except Exception as e:
        logger.error('[DB] pg_ctl restart raised: %s', e, exc_info=True)
        return False


def _read_our_pg_port(pgdata):
    """Read the port from OUR postgresql.conf, if it exists."""
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return None
    try:
        port = None
        with open(conf_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('port') and '=' in stripped:
                    if stripped.startswith('#'):
                        continue
                    val = stripped.split('=', 1)[1].strip().split('#')[0].strip()
                    port = int(val)
        return port
    except Exception as e:
        logger.debug('[DB] Could not parse port from postgresql.conf: %s', e)
        return None


def _verify_pg_data_directory(host, port, pgdata, pg_user):
    """Check that the PG on host:port uses OUR pgdata directory."""
    db_user = pg_user or _get_username()
    psql_bin = _find_pg_binary('psql')
    try:
        result = subprocess.run(
            [psql_bin, '-h', host, '-p', str(port), '-U', db_user,
             '-d', 'template1', '-t', '-A',
             '-c', 'SHOW data_directory;'],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '5', 'PGGSSENCMODE': 'disable'}
        )
        if result.returncode == 0:
            remote_pgdata = result.stdout.strip()
            our_pgdata = os.path.realpath(pgdata)
            remote_real = os.path.realpath(remote_pgdata) if remote_pgdata else ''
            if remote_real and remote_real != our_pgdata:
                logger.warning(
                    '[DB] data_directory mismatch: PG on %s:%d uses %s, '
                    'but ours is %s', host, port, remote_pgdata, pgdata)
                return False
            logger.debug('[DB] data_directory verified: PG on %s:%d → %s', host, port, remote_pgdata)
            return True
        else:
            logger.debug('[DB] Could not verify data_directory on %s:%d: %s',
                        host, port, result.stderr.strip()[:200])
            return False  # fail-safe: cannot verify → refuse to match
    except FileNotFoundError:
        logger.debug('[DB] psql binary not found — cannot verify data_directory')
        return False  # fail-safe: no psql → refuse to match
    except Exception as e:
        logger.debug('[DB] data_directory check failed on %s:%d: %s', host, port, e)
        return False  # fail-safe: error → refuse to match


def _pg_has_database(host, port, dbname, pg_user):
    """Check if a PostgreSQL instance has a specific database."""
    db_user = pg_user or _get_username()
    psql_bin = _find_pg_binary('psql')
    try:
        result = subprocess.run(
            [psql_bin, '-h', host, '-p', str(port), '-U', db_user,
             '-d', 'template1', '-t', '-A',
             '-c', f"SELECT 1 FROM pg_database WHERE datname = '{dbname}';"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '5', 'PGGSSENCMODE': 'disable'}
        )
        if result.returncode == 0:
            has_it = result.stdout.strip() == '1'
            logger.debug('[DB] Database "%s" on %s:%d: %s',
                        dbname, host, port, 'exists' if has_it else 'NOT FOUND')
            return has_it
        else:
            logger.debug('[DB] Could not check database existence on %s:%d: %s',
                        host, port, result.stderr.strip()[:200])
            return True
    except Exception as e:
        logger.debug('[DB] Database existence check failed on %s:%d: %s', host, port, e)
        return True


def _pg_real_connect_ok(host, port, pg_user, pg_dbname, timeout_s=5):
    """Probe a PG host with a *real* connection, not just pg_isready.

    pg_isready returns OK as soon as postmaster accepts a TCP connection,
    even if the backend process that actually services queries is hung
    (common with "half-alive" containers on shared FUSE storage where
    the postmaster's FUSE-bound disk I/O is unreachable). A real
    psycopg2.connect() is what the app uses, so it's what we probe.

    Returns True if a fresh connection + trivial SELECT succeeds.
    """
    try:
        import psycopg2
    except ImportError:
        logger.debug('[DB] psycopg2 not importable — cannot do real-connect probe')
        return False
    db_user = pg_user or _get_username()
    dsn = f"host={host} port={port} dbname={pg_dbname or 'template1'} user={db_user}"
    try:
        conn = psycopg2.connect(
            dsn,
            connect_timeout=timeout_s,
            application_name='tofu-probe',
            gssencmode='disable',
        )
    except Exception as e:
        logger.debug('[DB] Real-connect probe to %s:%d failed: %s', host, port, e)
        return False
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close()
        return True
    except Exception as e:
        logger.debug('[DB] Real-connect probe query to %s:%d failed: %s', host, port, e)
        return False
    finally:
        try:
            conn.close()
        except Exception as _e:
            logger.debug('[DB] Real-connect probe close failed: %s', _e)


def _verify_pg_after_start(pg_port, pgdata, pg_user, total_wait_s=12):
    """Verify the PG we just started is truly ours and stays alive.

    pg_ctl start can succeed (rc=0) and yet the postmaster shuts itself
    down moments later. Three failure modes we must detect here:

    1. WAL recovery PANIC (e.g. "invalid resource manager ID in
       checkpoint record"). pg_ctl returns 0 because the postmaster
       process itself launched fine; the startup sub-process aborts
       seconds later and the postmaster shuts down.
    2. Concurrent-start race: another host's postmaster wrote a
       different PID to postmaster.pid AFTER our pg_ctl rc=0 but BEFORE
       we noticed. Our postmaster will discover this within ~60s and
       perform an "immediate shutdown because data directory lock file
       is invalid".
    3. data_directory mismatch: rare, but if a port collision dance
       lands us on someone else's PG, we should not declare success.

    Approach: poll over ~total_wait_s. At each tick verify (a) postmaster.pid
    still references a live local PG process, AND (b) a real psycopg2
    connect+SELECT 1 succeeds, AND (c) data_directory matches our pgdata.
    Require two consecutive successful checks before declaring victory.

    Returns True if PG is healthy, False otherwise. On failure the caller
    is expected to NOT take ownership and to fall back / retry.
    """
    deadline = time.monotonic() + total_wait_s
    consecutive_ok = 0
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    last_err = None
    while time.monotonic() < deadline:
        # Check 1 — pidfile + PID alive locally
        try:
            with open(pidfile) as _f:
                pid_str = _f.readline().strip()
            pid = int(pid_str)
        except FileNotFoundError as _e_audit:
            logger.debug('[_bootstrap] _verify_pg_after_start caught %s: %s', type(_e_audit).__name__, _e_audit)
            last_err = 'postmaster.pid disappeared'
            consecutive_ok = 0
            time.sleep(0.5)
            continue
        except (OSError, ValueError) as e:
            # OSError: permission/filesystem; ValueError: int(pid_str) on garbage.
            logger.debug('[DB:bootstrap] postmaster.pid unreadable: %s', e)
            last_err = f'postmaster.pid unreadable: {e}'
            consecutive_ok = 0
            time.sleep(0.5)
            continue
        try:
            from lib.compat import is_process_alive
            if not is_process_alive(pid):
                last_err = f'postmaster PID {pid} not alive (likely PANIC during recovery)'
                consecutive_ok = 0
                time.sleep(0.5)
                continue
        except ImportError as e:
            logger.debug('[DB] is_process_alive unavailable for verify: %s', e)
        # Check 2 — real psycopg2 connect + trivial query
        if not _pg_real_connect_ok('127.0.0.1', pg_port, pg_user, None, timeout_s=3):
            last_err = 'real psycopg2 SELECT 1 failed'
            consecutive_ok = 0
            time.sleep(1.0)
            continue
        # Check 3 — data_directory matches ours
        try:
            if not _verify_pg_data_directory('127.0.0.1', pg_port, pgdata, pg_user):
                last_err = 'data_directory mismatch (someone else\'s PG)'
                consecutive_ok = 0
                time.sleep(1.0)
                continue
        except Exception as e:
            logger.debug('[DB] _verify_pg_data_directory raised during verify: %s', e)
            last_err = f'data_directory probe raised: {e}'
            consecutive_ok = 0
            time.sleep(1.0)
            continue
        consecutive_ok += 1
        if consecutive_ok >= 2:
            return True
        time.sleep(0.5)
    logger.error('[DB] Post-start verification FAILED after %.1fs: %s',
                 total_wait_s, last_err)
    return False


def _stop_local_pg_quietly(pgdata):
    """Best-effort pg_ctl stop -m fast, used to undo a failed start."""
    try:
        subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast', '-w', '-t', '10'],
            capture_output=True, text=True, timeout=15
        )
        logger.info('[DB] Stopped local PG after failed post-start verification')
    except Exception as e:
        logger.debug('[DB] Quiet stop after failed verify raised: %s', e)


def _scan_for_our_pg(host, port_range, pgdata, pg_user):
    """Scan a range of ports for a PG instance that owns our pgdata."""
    for port in port_range:
        try:
            result = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', host, '-p', str(port), '-d', 'template1'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0:
                continue
            if _verify_pg_data_directory(host, port, pgdata, pg_user):
                logger.info('[DB] Found our PG on %s:%d (port scan recovery)', host, port)
                return port
        except Exception as e:
            logger.debug('[DB] Port scan probe %d failed: %s', port, e)
            continue
    return None


def _ensure_database_exists(host, port, pg_dbname, pg_user, pgdata):
    """Run ``createdb`` if the target database doesn't exist yet."""
    if not _verify_pg_data_directory(host, port, pgdata, pg_user):
        logger.error('[DB] REFUSING to createdb on %s:%d — it is NOT our PG instance '
                     '(data_directory mismatch). This prevents data leakage.',
                     host, port)
        return

    db_user = pg_user or _get_username()
    createdb_bin = _find_pg_binary('createdb')
    # Try the given host first; if 'localhost' DNS fails (macOS quirk),
    # retry with 127.0.0.1 as fallback.
    hosts_to_try = [host]
    if host == 'localhost':
        hosts_to_try.append('127.0.0.1')
    elif host == '127.0.0.1':
        hosts_to_try.append('localhost')
    for _h in hosts_to_try:
        try:
            result = subprocess.run(
                [createdb_bin, '-h', _h, '-p', str(port),
                 '-U', db_user, pg_dbname],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                if 'already exists' in result.stderr:
                    logger.debug('[DB] Database "%s" already exists on %s:%d',
                                pg_dbname, _h, port)
                    return
                elif 'could not translate host name' in result.stderr and _h != hosts_to_try[-1]:
                    logger.debug('[DB] createdb DNS failed for %s, retrying with %s', _h, hosts_to_try[-1])
                    continue
                else:
                    logger.warning('[DB] createdb on %s:%d failed: %s',
                                  _h, port, result.stderr.strip())
            else:
                logger.info('[DB] Created missing database "%s" on %s:%d',
                           pg_dbname, _h, port)
            return
        except FileNotFoundError:
            logger.debug('[DB] createdb binary not found (looked for: %s) — skipping', createdb_bin)
            return
        except Exception as e:
            logger.warning('[DB] createdb check failed: %s', e)
            return


def _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname):
    """Bootstrap a brand-new PostgreSQL data directory and start the server.

    Returns:
        dict with updated PG_HOST, PG_PORT, PG_DSN on success, or None on failure.
    """
    logger.info('[DB] Bootstrapping new PostgreSQL data directory at %s ...', pgdata)

    os.makedirs(os.path.dirname(pgdata), exist_ok=True)
    os.makedirs(os.path.join(base_dir, 'logs'), exist_ok=True)

    # initdb
    initdb_bin = _find_pg_binary('initdb')
    try:
        result = subprocess.run(
            [initdb_bin, '-D', pgdata, '--encoding=UTF8', '--locale=C',
             '--auth=trust', '--username=' + (pg_user or _get_username())],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            logger.error('[DB] initdb failed: %s', result.stderr)
            return None
        logger.info('[DB] initdb completed successfully')
    except FileNotFoundError:
        hint = 'conda install postgresql'
        if IS_MACOS:
            hint = 'brew install postgresql@18, or conda install postgresql'
        elif IS_WINDOWS:
            hint = 'install PostgreSQL and add PG bin/ to PATH'
        logger.error('[DB] initdb not found (looked for: %s) — install PostgreSQL '
                     '(e.g. %s)', initdb_bin, hint)
        return None
    except Exception as e:
        logger.error('[DB] initdb failed: %s', e, exc_info=True)
        return None

    # Pick a free port and configure
    free_port = _find_free_port(start=pg_port)
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    try:
        with open(conf_path, 'a') as f:
            f.write('\n# ── Tofu auto-bootstrap overrides ──\n')
            f.write(f'port = {free_port}\n')
            f.write("listen_addresses = '*'\n")
            f.write("unix_socket_directories = ''\n")
        logger.info('[DB] Configured PG port=%d in postgresql.conf', free_port)
    except Exception as e:
        logger.error('[DB] Cannot write postgresql.conf: %s', e)
        return None

    # Apply the managed tuning block (max_connections / durability / WAL).
    # Written BEFORE the first start, so no restart is needed here.
    _ensure_managed_pg_config(pgdata)

    # Start PG — but first acquire the cross-host startup lock so we
    # don't race another tofu host that shares this pgdata.
    if not _try_acquire_startup_lock(pgdata):
        logger.warning('[DB] Skipping initdb-time pg_ctl start: another host '
                       'holds the cross-host startup lock. Falling back.')
        return None
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    pg_ctl_bin = _find_pg_binary('pg_ctl')
    try:
        start_cmd = [pg_ctl_bin, '-D', pgdata, '-l', log_path, 'start']
        if IS_WINDOWS:
            # On Windows, pg_ctl start needs -w (wait) to be reliable
            start_cmd.insert(-1, '-w')
        result = subprocess.run(
            start_cmd,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.error('[DB] pg_ctl start failed: %s', result.stderr)
            _release_startup_lock()
            return None
        logger.info('[DB] PostgreSQL started on port %d', free_port)
    except FileNotFoundError:
        hint = 'conda install postgresql'
        if IS_MACOS:
            hint = 'brew install postgresql@18, or conda install postgresql'
        elif IS_WINDOWS:
            hint = 'install PostgreSQL and add PG bin/ to PATH'
        logger.error('[DB] pg_ctl not found (looked for: %s) — install PostgreSQL '
                     '(e.g. %s)', pg_ctl_bin, hint)
        _release_startup_lock()
        return None
    except Exception as e:
        logger.error('[DB] pg_ctl start failed: %s', e, exc_info=True)
        _release_startup_lock()
        return None

    # Post-start verification: if the postmaster PANIC-shuts within a
    # few seconds (WAL corruption, concurrent-start race), fail fast.
    if not _verify_pg_after_start(free_port, pgdata, pg_user, total_wait_s=12):
        logger.error('[DB] Freshly initdb\'d PG failed post-start verification — '
                     'stopping it and aborting bootstrap. See logs/postgresql.log.')
        _stop_local_pg_quietly(pgdata)
        _release_startup_lock()
        return None

    time.sleep(1)

    # Create the database
    db_user = pg_user or _get_username()
    createdb_bin = _find_pg_binary('createdb')
    # Use 127.0.0.1 instead of 'localhost' — on macOS, DNS resolution of
    # 'localhost' can fail when network is misconfigured (e.g. iPhone tethering,
    # VPN) with: "could not translate host name 'localhost' to address".
    for _createdb_host in ('127.0.0.1', 'localhost'):
        try:
            result = subprocess.run(
                [createdb_bin, '-h', _createdb_host, '-p', str(free_port),
                 '-U', db_user, pg_dbname],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                if 'already exists' in result.stderr:
                    logger.info('[DB] Database "%s" already exists', pg_dbname)
                    break  # success
                elif ('could not translate host name' in result.stderr
                      and _createdb_host == '127.0.0.1'):
                    # Shouldn't happen with 127.0.0.1, but just in case
                    continue
                else:
                    logger.error('[DB] createdb failed: %s', result.stderr)
                    return None
            else:
                logger.info('[DB] Created database "%s"', pg_dbname)
                break  # success
        except Exception as e:
            logger.error('[DB] createdb failed: %s', e, exc_info=True)
            return None

    # ── Restore from pg_backup.sql if export.py left one behind ──
    # export.py's personal-mode export NEVER raw-copies pgdata/ (hot-copy
    # across FUSE causes TOAST chunk corruption). Instead it does a
    # pg_dumpall → data/pg_backup.sql. On the destination's first boot,
    # we've just finished initdb + createdb above — so this is exactly
    # the moment to feed the dump into psql. On success we delete the
    # dump file so subsequent boots skip straight through.
    _restore_from_sql_dump_if_present(base_dir, free_port, db_user, pg_dbname)

    # Build DSN
    dsn = f"host=127.0.0.1 port={free_port} dbname={pg_dbname}"
    if pg_user:
        dsn += f" user={pg_user}"
    if pg_password:
        dsn += f" password={pg_password}"
    _write_owner_host(pgdata)
    _mark_pg_owned_locally(pgdata)
    logger.info('[DB] Bootstrap complete — DSN: host=127.0.0.1 port=%d dbname=%s',
                free_port, pg_dbname)
    return {'PG_HOST': '127.0.0.1', 'PG_PORT': free_port, 'PG_DSN': dsn}


def _restore_from_sql_dump_if_present(base_dir, pg_port, pg_user, pg_dbname):
    """If ``data/pg_backup.sql`` exists (left by export.py), restore it.

    The dump was produced by ``pg_dumpall --clean --if-exists`` so it's
    safe to apply to a freshly-initdb'd cluster that only has the default
    ``template1`` / ``postgres`` / ``$USER`` databases.

    After a successful restore the dump file is DELETED so we never
    restore the same snapshot twice (which would clobber any new data
    written by the user on the destination after the first boot).

    Silent no-op if the dump is missing, empty, or ``psql`` is unavailable.
    """
    dump_path = os.path.join(base_dir, 'data', 'pg_backup.sql')
    if not os.path.isfile(dump_path):
        return
    try:
        size = os.path.getsize(dump_path)
    except OSError as e:
        logger.warning('[DB] Could not stat pg_backup.sql: %s — skipping restore', e)
        return
    if size == 0:
        logger.info('[DB] pg_backup.sql is empty — removing and skipping restore')
        try:
            os.remove(dump_path)
        except OSError as _e:
            logger.debug('[DB] Could not remove empty dump: %s', _e)
        return

    psql_bin = _find_pg_binary('psql')
    if not shutil.which(psql_bin) and not os.path.isfile(psql_bin):
        logger.warning('[DB] psql not found — cannot restore %s '
                       '(destination will come up with an empty DB). '
                       'Install PostgreSQL client to enable auto-restore.',
                       dump_path)
        return

    # ⚠️ DATA-LOSS GUARD (2026-06-28 incident hardening): this dump is a
    # ``pg_dumpall --clean --if-exists`` — applying it DROPs and recreates
    # EVERY database in the dump. That is safe ONLY against a freshly-initdb'd
    # cluster (the intended export→first-boot flow). If the target already
    # holds real conversations (e.g. self-heal Stage 2 restored over a cluster
    # that actually had data, or a stale dump was left in place), a blind
    # restore would silently replace newer data with the snapshot. Refuse to
    # clobber a populated target: quarantine the dump aside instead of
    # applying it, and log loudly so an operator can decide.
    try:
        probe = subprocess.run(
            [psql_bin, '-h', '127.0.0.1', '-p', str(pg_port), '-U', pg_user,
             '-d', pg_dbname, '-tAc',
             "SELECT count(*) FROM conversations"],
            capture_output=True, text=True,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            timeout=30,
        )
        existing_convs = int((probe.stdout or '0').strip() or '0') if probe.returncode == 0 else 0
    except Exception as e:
        # Table absent / DB empty / probe failed → treat as a clean target
        # (the normal first-boot case). Don't block the intended restore.
        logger.debug('[DB] restore pre-check probe failed (assuming empty target): %s', e)
        existing_convs = 0

    if existing_convs > 0:
        quarantine = dump_path + '.skipped-nonempty-target'
        logger.critical(
            '[DB] REFUSING to apply %s: target DB %r already has %d '
            'conversations. A --clean restore would DROP and replace them '
            '(potential data loss). Moving the dump aside to %s; apply it '
            'manually if you are SURE. Set TOFU_FORCE_DUMP_RESTORE=1 to '
            'override.',
            dump_path, pg_dbname, existing_convs, quarantine)
        if os.environ.get('TOFU_FORCE_DUMP_RESTORE') != '1':
            try:
                os.replace(dump_path, quarantine)
            except OSError as e:
                logger.error('[DB] Could not quarantine dump %s: %s', dump_path, e)
            return
        logger.warning('[DB] TOFU_FORCE_DUMP_RESTORE=1 — applying restore over '
                       'a populated DB at operator request')

    logger.info('[DB] Restoring data from %s (%.1f MB) — this may take a moment…',
                dump_path, size / (1024 * 1024))
    try:
        # Connect to the postgres admin DB; pg_dumpall --clean expects
        # to be able to DROP the target databases before recreating them.
        # -v ON_ERROR_STOP=1 makes a partial restore fail loudly instead
        # of leaving a half-restored DB.
        result = subprocess.run(
            [psql_bin, '-h', '127.0.0.1', '-p', str(pg_port), '-U', pg_user,
             '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-q', '-f', dump_path],
            capture_output=True, text=True,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            # No timeout — large dumps can take minutes on FUSE.
        )
    except Exception as e:
        logger.error('[DB] psql restore invocation failed: %s', e, exc_info=True)
        return

    if result.returncode != 0:
        # Leave the dump file in place so the user can retry manually.
        logger.error('[DB] Restore from %s FAILED (rc=%d). Dump preserved for '
                     'manual retry. stderr=%.1000s',
                     dump_path, result.returncode, (result.stderr or '').strip())
        return

    logger.info('[DB] Restore from %s completed successfully', dump_path)
    try:
        os.remove(dump_path)
        logger.info('[DB] Removed %s (restore complete, one-shot)', dump_path)
    except OSError as e:
        logger.warning('[DB] Could not remove restored dump %s: %s', dump_path, e)


def _pg_binaries_present():
    """Quick check: is pg_ctl available at all on this host?

    Returns True only if the core PG binaries are discoverable. This lets
    us bail out of the whole bootstrap flow early with a friendly
    "fallback to SQLite" message, instead of emitting a string of ERROR
    logs as we probe ports, scan directories, and finally try pg_ctl.
    """
    # _find_pg_binary returns the bare name as a fallback — but that only
    # works as a launch argument if PATH has the real binary. So we also
    # verify with shutil.which() that SOMETHING is there.
    pg_ctl = _find_pg_binary('pg_ctl')
    if os.path.isabs(pg_ctl) and os.path.isfile(pg_ctl):
        return True
    # Bare name — check PATH
    return shutil.which(pg_ctl) is not None


def _try_explicit_pg_target(pgdata, base_dir, pg_host, pg_port, build_dsn):
    """Step 1 of ``_ensure_pg_running``: handle an explicit env-set PG target.

    When ``TOFU_PG_HOST`` names a remote host, OR ``TOFU_PG_PORT`` is set (even
    for localhost), the user manages PG externally — connect directly rather
    than bootstrap.  A LOCAL explicit port that names OUR OWN pgdata is special:
    an unreachable target then means "our local PG is currently down" (e.g. the
    Restart button stopped it and the new server raced ahead), so we fall
    through to the local start path instead of failing to SQLite.

    Returns
    -------
    (handled: bool, result: dict | None)
        handled=True  → the caller must ``return result`` (result is the PG
                        info dict on success, or None on a hard failure).
        handled=False → no explicit target, OR an explicit-LOCAL-ours target is
                        down → the caller falls through to Step 2 (local start).
    """
    explicit_host = getenv_compat('TOFU_PG_HOST')
    explicit_port = getenv_compat('TOFU_PG_PORT', default=None)
    is_explicit_external = (
        (explicit_host and explicit_host not in ('localhost', '127.0.0.1', '::1'))
        or explicit_port is not None  # any explicit port = user-managed PG
    )
    if not is_explicit_external:
        return False, None

    target_host = explicit_host or pg_host
    target_port = int(explicit_port) if explicit_port else pg_port
    # A local explicit port (e.g. TOFU_PG_PORT pointing at 127.0.0.1)
    # almost always names OUR OWN pgdata started by a previous server.py —
    # NOT a truly external/unmanaged PG. If that cluster is OURS and we
    # have the binaries to manage it, an unreachable target just means
    # "our local PG is currently down" (e.g. the user clicked Restart,
    # which stops PG, and the new server raced ahead of it). In that case
    # we must START it ourselves rather than give up and fall back to a
    # near-empty SQLite. Only a genuinely external target (remote host, or
    # a local port whose pgdata isn't ours) is strictly connect-or-fail.
    target_is_local = target_host in ('localhost', '127.0.0.1', '::1')
    target_is_ours = (
        target_is_local
        and _read_our_pg_port(pgdata) == target_port
        and _pg_binaries_present()
    )
    logger.info('[DB] Using explicit PG target from env: %s:%d (manageable_local=%s)',
                target_host, target_port, target_is_ours)
    # Try psycopg2 directly (no pg_isready binary needed — works in CI)
    try:
        import psycopg2
        test_dsn = build_dsn(target_host, target_port)
        conn = psycopg2.connect(test_dsn, connect_timeout=5)
        conn.close()
        logger.info('[DB] Explicit PG target %s:%d is reachable', target_host, target_port)
        # Manage our own cluster's tuning so the connection / WAL settings
        # stay in sync (otherwise PG keeps its initdb defaults — e.g.
        # max_connections=200 — below the app-side TOFU_DB_MAX_CONNS
        # ceiling, producing 'too many clients' FATALs).
        if target_is_local and _read_our_pg_port(pgdata) == target_port:
            _mark_pg_owned_locally(pgdata)
            if _ensure_managed_pg_config(pgdata):
                _restart_local_pg(pgdata, base_dir)
        return True, {'PG_HOST': target_host, 'PG_PORT': target_port,
                      'PG_DSN': test_dsn}
    except ImportError:
        logger.error('[DB] psycopg2 not installed — cannot connect to explicit PG')
        return True, None
    except Exception as e:
        if target_is_ours:
            logger.warning('[DB] Explicit local PG target %s:%d is down (%s) — '
                           'it names OUR pgdata, so attempting to START it '
                           'locally instead of falling back to SQLite.',
                           target_host, target_port, e)
            # Fall through to the local start/bootstrap path (Step 2+).
            return False, None
        logger.error('[DB] Explicit PG target %s:%d not reachable: %s',
                     target_host, target_port, e)
        return True, None


def _boot_stop_pg_quietly(pgdata):
    """Best-effort ``pg_ctl stop -m fast`` for a pgdata we just started."""
    try:
        subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast', '-w', '-t', '20'],
            capture_output=True, text=True, timeout=30)
    except Exception as e:
        logger.debug('[DB-Seed] quiet stop of %s failed: %s', pgdata, e)


def _ensure_pg_running(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname):
    """Ensure PostgreSQL is accessible. Start locally or discover remote instance.

    Returns:
        dict with PG_HOST, PG_PORT, PG_DSN on success, or None on failure.
    """
    def _build_dsn(host, port):
        dsn = f"host={host} port={port} dbname={pg_dbname}"
        if pg_user:
            dsn += f" user={pg_user}"
        if pg_password:
            dsn += f" password={pg_password}"
        return dsn

    # ── Step -1: One-time local-primary seed migration ──
    # When the local-primary split is engaged but `pgdata` here is the LEGACY
    # FUSE path (the gate held because local is not yet populated), attempt the
    # one-time seed of the empty local cluster from this legacy one. On success
    # the local dir becomes a verified populated cluster and the NEXT boot's
    # resolve_pgdata_dir flips to it (the two-restart dance). On skip/failure
    # legacy stays canonical — we proceed to start THIS legacy cluster below,
    # so serving is never blocked. The seed itself is idempotent + verify-gated.
    try:
        from lib.database.db_paths import (
            local_data_split_enabled, legacy_pgdata_dir, pgdata_is_populated,
        )
        _data_dir = os.path.join(base_dir, 'data')
        _legacy = legacy_pgdata_dir(_data_dir)
        # Only when split is on AND we were handed the legacy path (gate held).
        if (local_data_split_enabled(_data_dir)
                and os.path.abspath(pgdata) == os.path.abspath(_legacy)):
            _local_root = getenv_compat('TOFU_DB_LOCAL_ROOT', default='').strip() \
                or '/tmp/tofu'
            _local_pgdata = os.path.join(os.path.abspath(_local_root), 'pgdata')
            if not pgdata_is_populated(_local_pgdata):
                _seed_local_pgdata_from_legacy(
                    _local_pgdata, _legacy, base_dir, pg_port,
                    pg_user, pg_password, pg_dbname)
    except Exception as _se:
        logger.error('[DB-Seed] seed hook raised (continuing on legacy): %s',
                     _se, exc_info=True)

    # ── Step 0: Early bail if PG binaries are simply not installed ──
    # Unless the user has explicitly set TOFU_PG_HOST to a remote, there's
    # no point probing anything — we can't start, query, or verify PG.
    # This turns a noisy "ERROR: pg_ctl not found" trace into a single
    # friendly INFO line, and the caller seamlessly falls back to SQLite.
    _explicit_host = getenv_compat('TOFU_PG_HOST')
    _explicit_remote = (_explicit_host
                        and _explicit_host not in ('localhost', '127.0.0.1', '::1'))
    if not _explicit_remote and not _pg_binaries_present():
        logger.info(
            '[DB] PostgreSQL client binaries (pg_ctl, initdb, psql) not found '
            'on this host — SKIPPING PG bootstrap and falling back to SQLite. '
            'This is normal when PG is not installed. '
            'To enable PG (better concurrency for 100+ users): '
            'conda install -c conda-forge postgresql>=18'
        )
        return None

    # ── Step 1: Explicit host/port override (see _try_explicit_pg_target) ──
    # An env-set remote host or any explicit port = user-managed PG: connect
    # directly. handled=True → return its result; handled=False → fall through
    # to the local start path (no explicit target, or our-own-local PG is down).
    _handled, _explicit_result = _try_explicit_pg_target(
        pgdata, base_dir, pg_host, pg_port, _build_dsn)
    if _handled:
        return _explicit_result

    # ── Step 2: Read OUR port from OUR postgresql.conf ──
    our_port = _read_our_pg_port(pgdata)
    if our_port is not None:
        pg_port = our_port
        logger.info('[DB] Read port=%d from our postgresql.conf', our_port)

        try:
            # Use 127.0.0.1 — 'localhost' DNS can fail on macOS with certain
            # network configs (iPhone tethering, VPN, etc.)
            _local = '127.0.0.1'
            result = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', _local, '-p', str(pg_port), '-d', 'template1'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                is_ours = _verify_pg_data_directory(_local, pg_port, pgdata, pg_user)
                if is_ours:
                    logger.info('[DB] PostgreSQL already running on %s:%d (verified ours)', _local, pg_port)
                    _ensure_database_exists(_local, pg_port, pg_dbname, pg_user, pgdata)
                    # Already-running local PG on our pgdata — almost
                    # certainly started by a previous server.py on this
                    # host. Take ownership so shutdown_pool stops it.
                    _mark_pg_owned_locally(pgdata)
                    # Re-apply managed tuning; restart only if it changed a
                    # restart-only setting (max_connections / wal_level).
                    if _ensure_managed_pg_config(pgdata):
                        _restart_local_pg(pgdata, base_dir)
                    return {'PG_HOST': _local, 'PG_PORT': pg_port,
                            'PG_DSN': _build_dsn(_local, pg_port)}
                else:
                    # NOT our PG — NEVER reuse another project's PG just because
                    # it has a database with the same name. This prevents cross-project
                    # data leakage and PID-file duel crashes.
                    logger.warning(
                        '[DB] PG on %s:%d is NOT ours (data_directory mismatch) '
                        '— REFUSING to reuse. Scanning nearby ports for our PG.',
                        _local, pg_port)
                    found_our_port = _scan_for_our_pg(_local, range(15432, 15440), pgdata, pg_user)
                    if found_our_port:
                        _ensure_database_exists(_local, found_our_port, pg_dbname, pg_user, pgdata)
                        _mark_pg_owned_locally(pgdata)
                        return {'PG_HOST': _local, 'PG_PORT': found_our_port,
                                'PG_DSN': _build_dsn(_local, found_our_port)}
        except Exception as _e:
            logger.debug('[DB] pg_isready localhost:%d check failed: %s', pg_port, _e)

    # ── Step 2b: Scan nearby ports for our PG by data_directory ──
    # Only match by data_directory verification — NEVER by database name alone.
    # This prevents cross-project data leakage when exported copies share the
    # same database name but must use independent PG instances.
    _local = '127.0.0.1'
    found_our_port = _scan_for_our_pg(_local, range(15432, 15440), pgdata, pg_user)
    if found_our_port:
        _ensure_database_exists(_local, found_our_port, pg_dbname, pg_user, pgdata)
        _mark_pg_owned_locally(pgdata)
        return {'PG_HOST': _local, 'PG_PORT': found_our_port,
                'PG_DSN': _build_dsn(_local, found_our_port)}

    # ── Step 3: Check if another machine owns the pgdata ──
    #
    # Defer to the remote ONLY if a fresh tofu heartbeat proves another
    # tofu process is actively using that PG right now. A bare TCP-alive
    # postmaster is not enough: it could be the stale tail of a previous,
    # unclean exit on another host — in which case we must take over
    # rather than route every DB call across a dying link (see
    # .tofu/skills/pg-cross-host-heartbeat-takeover.md).
    is_remote, remote_host = _pg_already_running_on_another_machine(pgdata, pg_port)
    if is_remote and remote_host:
        fresh, hb_info = _heartbeat_is_fresh(pgdata)
        if fresh:
            remote_ok = _pg_real_connect_ok(remote_host, pg_port, pg_user, pg_dbname, timeout_s=5)
            if remote_ok:
                logger.info('[DB] PostgreSQL is running on remote machine %s '
                            '(heartbeat fresh, age=%.1fs, pid=%s) — connecting as client',
                            remote_host, hb_info.get('age_s', -1) if hb_info else -1,
                            hb_info.get('pid') if hb_info else None)
                _ensure_database_exists(remote_host, pg_port, pg_dbname, pg_user, pgdata)
                return {'PG_HOST': remote_host, 'PG_PORT': pg_port,
                        'PG_DSN': _build_dsn(remote_host, pg_port)}
            logger.warning('[DB] Heartbeat was fresh but real-connect to %s:%d failed — '
                          'treating as dead and taking over locally', remote_host, pg_port)
        else:
            if hb_info is None:
                logger.info('[DB] Remote PG owner %s present but no tofu heartbeat file '
                            '— previous owner exited uncleanly; taking over locally',
                            remote_host)
            else:
                logger.info('[DB] Remote PG owner %s has a STALE heartbeat '
                            '(age=%.1fs > ttl=%ds, last_pid=%s) — previous owner is gone; '
                            'taking over locally',
                            remote_host, hb_info.get('age_s', -1),
                            _HEARTBEAT_TTL_S, hb_info.get('pid'))

    # ── Step 3b: pgdata ↔ binary major-version sanity check ──
    # A pgdata created by a different PG major than the installed binary
    # FATALs on start (config-param mismatch) → scheduler retry-storm. Detect
    # it (see _pgdata_major_compatible) and fall back to SQLite cleanly.
    if not _pgdata_major_compatible(pgdata):
        return None

    # ── Step 4/5: Start PG locally or bootstrap ──
    # Before any local start/takeover, verify the pgdata mount truly enforces
    # advisory locks. A silent-no-op filesystem makes the cross-host startup
    # interlock useless (two hosts both "acquire" and double-start → WAL
    # corruption). Warn loudly, or refuse entirely if TOFU_PG_REQUIRE_FLOCK.
    if not _verify_flock_support_or_warn(pgdata):
        return None

    if not os.path.isdir(pgdata):
        logger.info('[DB] No pgdata directory — bootstrapping new PostgreSQL instance')
        result = _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname)
        if not result:
            logger.error('[DB] Bootstrap failed — refusing to connect to '
                         'default 127.0.0.1:%d (may be another project)', pg_port)
        return result

    # Clean up stale pidfile
    #
    # Container-switch scenario: a user uses web-based VS Code and moves
    # between containers, so the machine IP changes but only ONE container
    # is live at any time. The `.pg_owner_host` marker from the previous
    # container will point at an IP that no longer runs PG. Treat such a
    # marker as stale — probe reachability first before deferring to it.
    #
    # Rule:
    #   - Remote host reachable on PG port → concurrent multi-host scenario,
    #     defer to remote (preserves the original cross-machine safety net).
    #   - Remote host NOT reachable → previous owner is dead (container gone
    #     or machine switched), auto-heal by removing stale markers and
    #     starting PG locally. This makes container switches a no-op.
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    if os.path.exists(pidfile):
        owner_host = _read_pg_host_from_pidfile(pgdata)
        local_ip = _get_local_ip()
        # IP-independent guard FIRST: if the pidfile PID is a live local
        # postgres, this host already owns the cluster. _get_local_ip() can
        # flap (container IP reassignment), which previously caused a host to
        # see its OWN postmaster as "remote", delete the pidfile, and start a
        # SECOND postmaster on the same pgdata → pg_subtrans corruption.
        # Reuse the running instance instead of taking over.
        if _pidfile_pid_is_live_local_postgres(pgdata):
            conf_port = _read_our_pg_port(pgdata) or pg_port
            if _verify_pg_data_directory('127.0.0.1', conf_port, pgdata, pg_user):
                logger.warning('[DB] Step 4: postmaster.pid is a LIVE local postgres '
                               '(owner_host marker=%s, local_ip=%s) — reusing it '
                               'instead of taking over (IP-flap safe).',
                               owner_host, local_ip)
                _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, pgdata)
                _write_owner_host(pgdata)
                _mark_pg_owned_locally(pgdata)
                return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                        'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
            logger.warning('[DB] Step 4: pidfile PID is live postgres but data_directory '
                           'verify on 127.0.0.1:%d failed — proceeding with caution.', conf_port)
        if owner_host and owner_host not in (local_ip, 'localhost', '127.0.0.1'):
            # Heartbeat is the authoritative signal: only defer if another
            # tofu is actively running there. Bare TCP-alive postmaster
            # is not enough (an unclean exit can leave it answering for
            # hours).
            fresh, hb_info = _heartbeat_is_fresh(pgdata)
            remote_alive = fresh and _pg_real_connect_ok(
                owner_host, pg_port, pg_user, pg_dbname, timeout_s=5)
            if remote_alive:
                logger.warning('[DB] Step 4 safety net: postmaster.pid belongs to '
                               'remote host %s (we are %s) and tofu heartbeat is '
                               'fresh (age=%.1fs, pid=%s) — refusing to delete. '
                               'Connecting to remote host.',
                               owner_host, local_ip,
                               hb_info.get('age_s', -1) if hb_info else -1,
                               hb_info.get('pid') if hb_info else None)
                _ensure_database_exists(owner_host, pg_port, pg_dbname, pg_user, pgdata)
                return {'PG_HOST': owner_host, 'PG_PORT': pg_port,
                        'PG_DSN': _build_dsn(owner_host, pg_port)}
            # Stale or missing heartbeat — previous owner is gone (unclean
            # exit, container switched, machine rebooted). Auto-heal: remove
            # ownership markers and proceed to start PG locally. Data
            # files are untouched.
            if hb_info is None:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s exited '
                               'uncleanly (no heartbeat file) — taking over locally.',
                               owner_host)
            elif not fresh:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s heartbeat is '
                               'stale (age=%.1fs > ttl=%ds, last_pid=%s) — '
                               'taking over locally.',
                               owner_host, hb_info.get('age_s', -1),
                               _HEARTBEAT_TTL_S, hb_info.get('pid'))
            else:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s heartbeat '
                               'fresh but PG unreachable — taking over locally.',
                               owner_host)
            owner_file = os.path.join(pgdata, '.pg_owner_host')
            try:
                if os.path.exists(owner_file):
                    os.remove(owner_file)
                    logger.info('[DB] Removed stale .pg_owner_host (was %s)', owner_host)
            except Exception as _e:
                logger.warning('[DB] Could not remove stale .pg_owner_host: %s', _e)
            _clear_heartbeat(pgdata)
        else:
            logger.warning('[DB] Removing stale postmaster.pid before starting PG '
                          '(owner: %s, us: %s)', owner_host, local_ip)
        # Cross-host HARD interlock — the real anti-corruption barrier.
        #
        # We are about to delete a postmaster.pid and start our own
        # postmaster. Removing the pidfile defeats PostgreSQL's OWN
        # single-postmaster guard, so THIS is the catastrophic step: if
        # every IP/PID/heartbeat heuristic above was wrong and the "dead"
        # owner is actually a LIVE peer on another host, deleting its
        # pidfile and starting a second postmaster on the shared FUSE
        # pgdata corrupts WAL / pg_subtrans. A live peer holds this flock
        # for its entire lifetime, so we acquire it BEFORE the deletion:
        # if another host holds it, two postmasters physically cannot
        # coexist — we refuse to take over and fall back. (The later
        # pg_ctl-start acquisition is idempotent — a no-op once held here.)
        if not _try_acquire_startup_lock(pgdata):
            logger.warning('[DB] Refusing to remove postmaster.pid / take over: '
                           'another host holds the cross-host startup lock on '
                           'pgdata=%s — a live peer owns this PG. Falling back '
                           '(SQLite / retry next cycle).', pgdata)
            return None
        try:
            os.remove(pidfile)
        except FileNotFoundError:
            # Already gone (race with another cleanup path) — fine.
            logger.debug('[DB] postmaster.pid already removed')
        except PermissionError as e:
            if IS_WINDOWS:
                logger.error('[DB] Cannot remove stale pidfile (file locked by another process '
                             '— PG may still be running): %s', e)
            else:
                logger.error('[DB] Cannot remove stale pidfile: %s', e)
            _release_startup_lock()
            return None
        except Exception as e:
            logger.error('[DB] Cannot remove stale pidfile: %s', e)
            _release_startup_lock()
            return None

    _fix_unix_socket_conf(pgdata)

    # Check if configured port is taken (possibly by our own orphaned PG)
    conf_port = _read_our_pg_port(pgdata) or pg_port
    try:
        check = subprocess.run(
            [_find_pg_binary('pg_isready'), '-h', '127.0.0.1', '-p', str(conf_port), '-d', 'template1'],
            capture_output=True, text=True, timeout=3
        )
        if check.returncode == 0:
            # PG is already responding on our port — check if it's ours
            if _verify_pg_data_directory('127.0.0.1', conf_port, pgdata, pg_user):
                logger.info('[DB] PG already running on 127.0.0.1:%d (our data_directory) '
                           '— reusing after pidfile cleanup', conf_port)
                _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, pgdata)
                _write_owner_host(pgdata)
                _mark_pg_owned_locally(pgdata)
                if _ensure_managed_pg_config(pgdata):
                    _restart_local_pg(pgdata, base_dir)
                return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                        'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
            # Not ours — reassign to a different port
            free_port = _find_free_port(start=conf_port + 1)
            if free_port is None:
                logger.error('[DB] No free port found — cannot start PG')
                _release_startup_lock()
                return None
            logger.info('[DB] Port %d is occupied by another PG — reassigning to %d',
                       conf_port, free_port)
            _conf_path = os.path.join(pgdata, 'postgresql.conf')
            try:
                with open(_conf_path) as _f:
                    _lines = _f.readlines()
                with open(_conf_path, 'w') as _f:
                    for _line in _lines:
                        _s = _line.strip()
                        if _s.startswith('port') and '=' in _s and not _s.startswith('#'):
                            _f.write(f'port = {free_port}\n')
                        else:
                            _f.write(_line)
                pg_port = free_port
                logger.info('[DB] Updated postgresql.conf: port = %d', free_port)
            except Exception as _e:
                logger.error('[DB] Failed to update postgresql.conf port: %s', _e)
                _release_startup_lock()
                return None
    except Exception as _e:
        logger.debug('[DB] Port availability check failed: %s', _e)

    logger.info('[DB] Starting PostgreSQL server from %s ...', pgdata)
    # Cross-host startup lock — prevents two tofu hosts on the same
    # FUSE-mounted pgdata from racing into pg_ctl start at the same time
    # (which corrupts WAL with mutual-PID-eviction).
    if not _try_acquire_startup_lock(pgdata):
        logger.warning('[DB] Another tofu host is currently starting/owning PG '
                       'on this pgdata — skipping our pg_ctl start. Caller will '
                       'fall back to SQLite (or retry next cycle).')
        return None
    try:
        log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
        result = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path, 'start'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info('[DB] PostgreSQL started successfully on this machine')
            # Verify it stays up — pg_ctl rc=0 does NOT mean recovery
            # succeeded. If WAL is corrupted or another host's pidfile
            # races us, the postmaster will shut itself down within
            # seconds. Catch that here instead of letting the scheduler
            # storm-retry.
            if not _verify_pg_after_start(pg_port, pgdata, pg_user, total_wait_s=12):
                logger.error('[DB] PG started (rc=0) but failed post-start '
                             'verification — likely WAL corruption or concurrent '
                             'start by another host. Stopping local PG and '
                             'attempting automatic self-heal. See logs/postgresql.log.')
                _stop_local_pg_quietly(pgdata)
                # Automatic corruption recovery (pg_resetwal → restore-from-backup)
                # BEFORE giving up. We still hold the cross-host startup lock, so
                # no peer can race us during recovery. Released after either way.
                healed = _try_self_heal_corrupt_pg(
                    pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname)
                _release_startup_lock()
                return healed
            _ensure_database_exists('127.0.0.1', pg_port, pg_dbname, pg_user, pgdata)
            _write_owner_host(pgdata)
            _mark_pg_owned_locally(pgdata)
            return {'PG_HOST': '127.0.0.1', 'PG_PORT': pg_port,
                    'PG_DSN': _build_dsn('127.0.0.1', pg_port)}
        else:
            logger.error('[DB] Failed to start PostgreSQL: %s', result.stderr)
            _release_startup_lock()
            return None
    except FileNotFoundError as e:
        # pg_ctl / initdb binary not present — PostgreSQL is simply not
        # installed on this host. This is a normal "PG not available →
        # fallback to SQLite" path, NOT a bug. Log at INFO level so it's
        # clear the system is intentionally degrading.
        logger.info('[DB] PostgreSQL binaries not found on this host (%s). '
                    'This is normal — tofu will automatically use SQLite. '
                    'To enable PG (better concurrency): '
                    '  conda install -c conda-forge postgresql>=18',
                    e)
        _release_startup_lock()
        return None
    except Exception as e:
        logger.error('[DB] Failed to start PostgreSQL: %s', e, exc_info=True)
        _release_startup_lock()
        return None


def _stop_pg(pgdata):
    """Stop PostgreSQL server on shutdown."""
    # Stop the heartbeat first so a peer host that starts up during
    # the pg_ctl stop window sees "no heartbeat" and takes over cleanly.
    stop_heartbeat(pgdata)
    if os.path.isdir(pgdata):
        try:
            subprocess.run(
                [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast'],
                capture_output=True, text=True, timeout=30
            )
            logger.info('[DB] PostgreSQL stopped')
        except Exception as e:
            logger.warning('[DB] Error stopping PostgreSQL: %s', e)
    # Always release the cross-host startup lock on shutdown, regardless
    # of whether pg_ctl stop succeeded — a peer host is better off taking
    # over a potentially-stuck PG than being locked out forever.
    _release_startup_lock()
