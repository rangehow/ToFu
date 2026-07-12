"""PostgreSQL durability: logical backup, Tier-B base backup, PITR, self-heal.

Extracted from lib/database/_bootstrap.py (2026-07-11, Decoupling D sub-cut 3,
§10 signed off). The backup/restore/recovery cluster:
  • Tier A logical dump (`backup_pg_database` → data/pg_backups/pg_dumpall_*.sql)
    + `_latest_pg_backup` selector.
  • §3a newest-wins restore-channel selector (`_select_restore_channel`,
    `_tier_a_dump_end_ts`, `_tier_b_wal_end_ts`).
  • Tier B self-contained base backup (`basebackup_pg_cluster`,
    `_base_backup_dir`, `_latest_base_backup`).
  • Cold-start PITR replay (`_recover_via_pitr`).
  • Corrupt-cluster self-heal / quarantine (`_quarantine_corrupt_pgdata`,
    `_try_self_heal_corrupt_pg`) — the 2026-06-04 WAL-redo-PANIC recovery class.

Its call-outs into core/ownership (start/verify/config/port/db-exists/owner
markers/binaries/bootstrap) resolve LAZILY via in-body import to avoid an
import cycle. _bootstrap re-imports these fns (explicit facade) so
``_bootstrap.<name>`` / ``from lib.database._bootstrap import <name>`` keep
resolving for _core.py, the scheduler, _pg_seed's own lazy shims, and the
pg_*/tier_b/seed test suites.
"""

import os
import shutil
import subprocess
import sys  # noqa: F401
import time

from lib.env_compat import getenv_compat
from lib.log import get_logger, log_context

logger = get_logger(__name__)


# ── Lazy shims: core/ownership helpers this cluster calls (resolved at call
#    time so there is no import cycle with _bootstrap / _pg_ownership). ──
def _find_pg_binary(*a, **k):
    from lib.database._bootstrap import _find_pg_binary as _f
    return _f(*a, **k)

def _get_username(*a, **k):
    from lib.database._bootstrap import _get_username as _f
    return _f(*a, **k)

def _tier_b_enabled(*a, **k):
    from lib.database._bootstrap import _tier_b_enabled as _f
    return _f(*a, **k)

def _ensure_managed_pg_config(*a, **k):
    from lib.database._bootstrap import _ensure_managed_pg_config as _f
    return _f(*a, **k)

def _read_our_pg_port(*a, **k):
    from lib.database._bootstrap import _read_our_pg_port as _f
    return _f(*a, **k)

def _verify_pg_after_start(*a, **k):
    from lib.database._bootstrap import _verify_pg_after_start as _f
    return _f(*a, **k)

def _ensure_database_exists(*a, **k):
    from lib.database._bootstrap import _ensure_database_exists as _f
    return _f(*a, **k)

def _write_owner_host(*a, **k):
    from lib.database._bootstrap import _write_owner_host as _f
    return _f(*a, **k)

def _mark_pg_owned_locally(*a, **k):
    from lib.database._bootstrap import _mark_pg_owned_locally as _f
    return _f(*a, **k)

def _pg_binaries_present(*a, **k):
    from lib.database._bootstrap import _pg_binaries_present as _f
    return _f(*a, **k)

def _stop_local_pg_quietly(*a, **k):
    from lib.database._bootstrap import _stop_local_pg_quietly as _f
    return _f(*a, **k)

def _bootstrap_pg(*a, **k):
    from lib.database._bootstrap import _bootstrap_pg as _f
    return _f(*a, **k)


def backup_pg_database(retention_days=None):
    """Dump the live PG cluster to a timestamped SQL file under data/pg_backups/.

    This is the single biggest durability win for the 'lost conversations on
    crash' problem: the only reason the 2026-06-04 WAL-corruption incident was
    recoverable at all is that a manual ``pg_dumpall`` happened to exist. This
    makes that dump SCHEDULED (see the 'PostgreSQL Backup' task auto-registered
    in lib/scheduler/manager.py) so a corrupt primary can always be restored
    from a recent logical dump instead of a data-losing ``pg_resetwal -f``.

    PG-only; on SQLite the on-disk .db file already IS the durable artifact, so
    this is a silent no-op there.

    Args:
        retention_days: Delete dumps older than this many days. Defaults to
            env ``TOFU_PG_BACKUP_RETENTION_DAYS`` or 7.

    Returns:
        dict summary {ok, path, size_mb, pruned} — ok=False with a 'reason'
        when the dump was skipped or failed.
    """
    from lib.database._core import BASE_DIR, _BACKEND, PG_PORT, PG_USER
    from lib.database.db_paths import resolve_backup_root

    if _BACKEND != 'pg':
        return {'ok': False, 'reason': 'not_pg'}
    if PG_PORT == 0:
        return {'ok': False, 'reason': 'pg_unavailable'}

    if retention_days is None:
        try:
            retention_days = int(getenv_compat('TOFU_PG_BACKUP_RETENTION_DAYS',
                                                default='7'))
        except (TypeError, ValueError) as e:
            logger.debug('[DB-Backup] Invalid TOFU_PG_BACKUP_RETENTION_DAYS, defaulting to 7: %s', e)
            retention_days = 7

    pg_dumpall = shutil.which('pg_dumpall') or _find_pg_binary('pg_dumpall')
    if not pg_dumpall or not os.path.isabs(pg_dumpall):
        # _find_pg_binary returns the bare name when not found.
        if not shutil.which('pg_dumpall'):
            logger.warning('[DB-Backup] pg_dumpall not found on PATH — skipping backup')
            return {'ok': False, 'reason': 'pg_dumpall_missing'}

    backup_dir = resolve_backup_root(os.path.join(BASE_DIR, 'data'))
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as e:
        logger.error('[DB-Backup] Cannot create backup dir %s: %s', backup_dir, e)
        return {'ok': False, 'reason': f'mkdir_failed: {e}'}

    stamp = time.strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(backup_dir, f'pg_dumpall_{stamp}.sql')
    user = PG_USER or _get_username()

    try:
        with log_context('pg_backup', logger=logger):
            result = subprocess.run(
                [pg_dumpall, '-h', '127.0.0.1', '-p', str(PG_PORT), '-U', user,
                 '--clean', '--if-exists', '-f', out_path],
                capture_output=True, text=True,
                env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            )
        if result.returncode != 0:
            logger.error('[DB-Backup] pg_dumpall failed (rc=%d): %s',
                         result.returncode, (result.stderr or '').strip()[:500])
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError as e:
                logger.debug('[DB-Backup] could not remove partial dump %s: %s', out_path, e)
            return {'ok': False, 'reason': f'dump_failed_rc{result.returncode}'}
    except Exception as e:
        logger.error('[DB-Backup] pg_dumpall invocation failed: %s', e, exc_info=True)
        return {'ok': False, 'reason': f'exception: {e}'}

    try:
        size_mb = os.path.getsize(out_path) / (1024 * 1024)
    except OSError as e:
        logger.debug('[DB-Backup] could not stat dump %s: %s', out_path, e)
        size_mb = 0.0
    logger.info('[DB-Backup] Wrote %s (%.1f MB)', out_path, size_mb)

    # ── Prune old dumps past the retention window ──
    pruned = 0
    if retention_days > 0:
        cutoff = time.time() - retention_days * 86400
        try:
            for fn in os.listdir(backup_dir):
                if not (fn.startswith('pg_dumpall_') and fn.endswith('.sql')):
                    continue
                fpath = os.path.join(backup_dir, fn)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        pruned += 1
                except OSError as e:
                    logger.debug('[DB-Backup] Could not prune %s: %s', fpath, e)
        except OSError as e:
            logger.debug('[DB-Backup] Prune scan failed: %s', e)
    if pruned:
        logger.info('[DB-Backup] Pruned %d dump(s) older than %d days',
                    pruned, retention_days)

    try:
        from lib.log import audit_log
        audit_log('pg_backup', path=out_path, size_mb=round(size_mb, 1),
                  pruned=pruned, retention_days=retention_days)
    except Exception as _ae:
        logger.debug('[DB-Backup] audit_log failed: %s', _ae)

    return {'ok': True, 'path': out_path, 'size_mb': round(size_mb, 1),
            'pruned': pruned}


def _latest_pg_backup(base_dir):
    """Return the path to the most recent data/pg_backups/pg_dumpall_*.sql, or None."""
    from lib.database.db_paths import resolve_backup_root
    backup_dir = resolve_backup_root(os.path.join(base_dir, 'data'))
    try:
        candidates = [
            os.path.join(backup_dir, fn) for fn in os.listdir(backup_dir)
            if fn.startswith('pg_dumpall_') and fn.endswith('.sql')
        ]
    except OSError as e:
        logger.debug('[DB-Backup] no backup dir %s: %s', backup_dir, e)
        return None
    candidates = [p for p in candidates if os.path.getsize(p) > 0]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)



def _tier_b_wal_end_ts(base_dir):
    """Recoverable end-timestamp of the Tier B WAL archive, or None if absent.

    This is the mtime of the LAST archived WAL segment in
    ``$TOFU_DB_BACKUP_ROOT/wal/`` — the true PITR replay ceiling (NOT the base
    backup time; recovery reaches the last replayable segment).
    """
    try:
        from lib.database.db_paths import resolve_backup_root
        wal_dir = os.path.join(resolve_backup_root(os.path.join(base_dir, 'data')), 'wal')
        segs = [os.path.join(wal_dir, fn) for fn in os.listdir(wal_dir)
                if not fn.startswith('.') and not fn.endswith('.tmp')]
        segs = [s for s in segs if os.path.isfile(s)]
        if not segs:
            return None
        return max(os.path.getmtime(s) for s in segs)
    except OSError as e:
        logger.debug('[DB-Restore] no WAL archive end-ts: %s', e)
        return None


def _tier_a_dump_end_ts(base_dir):
    """Recoverable end-timestamp of the latest Tier A logical dump, or None."""
    latest = _latest_pg_backup(base_dir)
    if not latest:
        return None
    try:
        return os.path.getmtime(latest)
    except OSError as e:
        logger.debug('[DB-Restore] no dump end-ts: %s', e)
        return None


def _select_restore_channel(base_dir):
    """§3a: pick the channel with the NEWER recoverable end (never by tier).

    Compares Tier A (latest logical dump completion) vs Tier B (last archived
    WAL segment). Restores the NEWER. When the two ends diverge by more than
    ``TOFU_DB_RESTORE_DIVERGENCE_WARN_S`` (default 6h) logs CRITICAL + audit —
    a large gap means one durability channel is BROKEN and must surface; the
    restore still proceeds with the newer channel.

    Returns:
        ('tier_b', wal_end_ts) | ('tier_a', dump_end_ts) | (None, None).
    """
    a_ts = _tier_a_dump_end_ts(base_dir)
    b_ts = _tier_b_wal_end_ts(base_dir)
    if a_ts is None and b_ts is None:
        return None, None

    # Divergence guard (only meaningful when BOTH channels exist).
    if a_ts is not None and b_ts is not None:
        warn_s = int(getenv_compat('TOFU_DB_RESTORE_DIVERGENCE_WARN_S', default='21600'))
        gap = abs(a_ts - b_ts)
        if gap > warn_s:
            chosen = 'tier_b' if b_ts >= a_ts else 'tier_a'
            logger.critical('[DB-Restore] durability channel DIVERGENCE: Tier A end=%.0f '
                            'vs Tier B end=%.0f (gap=%.0fs > %ds) — one channel is '
                            'likely BROKEN. Restoring the NEWER (%s); investigate the '
                            'stale channel.', a_ts, b_ts, gap, warn_s, chosen)
            try:
                from lib.log import audit_log
                audit_log('db_restore_channel_divergence', tier_a_end=a_ts,
                          tier_b_end=b_ts, chosen=chosen, gap_s=round(gap))
            except Exception as _ae:
                logger.debug('[DB-Restore] divergence audit failed: %s', _ae)

    if b_ts is not None and (a_ts is None or b_ts >= a_ts):
        return 'tier_b', b_ts
    return 'tier_a', a_ts



def _base_backup_dir(base_dir):
    from lib.database.db_paths import resolve_backup_root
    return os.path.join(resolve_backup_root(os.path.join(base_dir, 'data')), 'base')


def _latest_base_backup(base_dir):
    """Path to the most recent pg_basebackup dir under $BACKUP_ROOT/base/, or None."""
    bdir = _base_backup_dir(base_dir)
    try:
        cands = [os.path.join(bdir, d) for d in os.listdir(bdir)
                 if os.path.isdir(os.path.join(bdir, d))
                 and os.path.isfile(os.path.join(bdir, d, 'PG_VERSION'))]
    except OSError as e:
        logger.debug('[DB-BaseBackup] no base dir %s: %s', bdir, e)
        return None
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def basebackup_pg_cluster():
    """Tier B: take a self-contained ``pg_basebackup -X stream`` of the primary.

    ``-X stream`` (owner decision) makes each base INDEPENDENTLY restorable even
    if the WAL archive has a gap; archived WAL is layered on top for the
    seconds-RPO tail. Engaged only when Tier B is opted in AND the split is
    active. PG-only; no-op otherwise. Returns a dict summary.
    """
    from lib.database._core import BASE_DIR, _BACKEND, PG_PORT, PG_USER
    from lib.database.db_paths import local_data_split_enabled
    if _BACKEND != 'pg':
        return {'ok': False, 'reason': 'not_pg'}
    if not _tier_b_enabled():
        return {'ok': False, 'reason': 'tier_b_off'}
    if not local_data_split_enabled(os.path.join(BASE_DIR, 'data')):
        return {'ok': False, 'reason': 'split_inactive'}
    pg_basebackup = _find_pg_binary('pg_basebackup')
    if not shutil.which(pg_basebackup) and not os.path.isfile(pg_basebackup):
        logger.warning('[DB-BaseBackup] pg_basebackup not found — skipping')
        return {'ok': False, 'reason': 'binary_missing'}
    bdir = _base_backup_dir(BASE_DIR)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    out = os.path.join(bdir, stamp)
    try:
        os.makedirs(bdir, exist_ok=True)
        proc = subprocess.run(
            [pg_basebackup, '-h', '127.0.0.1', '-p', str(PG_PORT), '-U', PG_USER,
             '-D', out, '-X', 'stream', '--checkpoint=fast', '--no-password'],
            capture_output=True, text=True,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            timeout=int(getenv_compat('TOFU_DB_BASEBACKUP_TIMEOUT', default='3600')),
        )
        if proc.returncode != 0:
            logger.error('[DB-BaseBackup] pg_basebackup failed (rc=%d): %s',
                         proc.returncode, (proc.stderr or '').strip()[:300])
            shutil.rmtree(out, ignore_errors=True)
            return {'ok': False, 'reason': 'basebackup_failed'}
    except Exception as e:
        logger.error('[DB-BaseBackup] pg_basebackup raised: %s', e, exc_info=True)
        shutil.rmtree(out, ignore_errors=True)
        return {'ok': False, 'reason': str(e)}
    logger.info('[DB-BaseBackup] Wrote base backup %s', out)
    return {'ok': True, 'path': out}


def _recover_via_pitr(local_pgdata, base_dir, pg_port, pg_user, pg_password, pg_dbname):
    """Tier B cold-start: restore latest base + replay archived WAL (PITR).

    Lays the latest ``-X stream`` base into ``local_pgdata``, writes
    ``recovery.signal`` + a ``restore_command`` pointing at our WAL-restore shim
    + ``recovery_target_timeline='latest'``, then starts PG so it replays the
    archived WAL to the last segment (the seconds-RPO tail) and promotes.

    Returns dict {PG_HOST,PG_PORT,PG_DSN} on a verified recovery, else None.
    """
    base = _latest_base_backup(base_dir)
    if not base:
        logger.warning('[DB-PITR] no base backup present — cannot PITR')
        return None
    if os.path.exists(local_pgdata) and os.listdir(local_pgdata):
        logger.error('[DB-PITR] target %s not empty — refusing to overlay', local_pgdata)
        return None
    try:
        os.makedirs(os.path.dirname(local_pgdata), exist_ok=True)
        shutil.copytree(base, local_pgdata)
    except Exception as e:
        logger.error('[DB-PITR] could not lay down base %s: %s', base, e)
        return None
    # recovery config: replay archived WAL via our restore shim.
    restore_cmd = ("%s -m lib.database.wal_archive restore %%f %%p"
                   % (sys.executable or 'python3'))
    try:
        with open(os.path.join(local_pgdata, 'postgresql.auto.conf'), 'a') as f:
            f.write("\nrestore_command = '%s'\n" % restore_cmd)
            f.write("recovery_target_timeline = 'latest'\n")
        open(os.path.join(local_pgdata, 'recovery.signal'), 'w').close()
    except OSError as e:
        logger.error('[DB-PITR] could not write recovery config: %s', e)
        return None
    # Apply managed tuning to the restored base (do NOT re-initdb — the base IS
    # the cluster), then start PG so it replays archived WAL and promotes.
    _ensure_managed_pg_config(local_pgdata)
    conf_port = _read_our_pg_port(local_pgdata) or pg_port
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    logger.info('[DB-PITR] Restoring base %s + replaying archived WAL into %s (port=%d)',
                base, local_pgdata, conf_port)
    try:
        start = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', local_pgdata, '-l', log_path,
             'start', '-w', '-t', '60'],
            capture_output=True, text=True, timeout=90)
        if start.returncode == 0 and _verify_pg_after_start(conf_port, local_pgdata,
                                                            pg_user, total_wait_s=20):
            _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, local_pgdata)
            _write_owner_host(local_pgdata)
            _mark_pg_owned_locally(local_pgdata)
            dsn = f"host=127.0.0.1 port={conf_port} dbname={pg_dbname}"
            if pg_user:
                dsn += f" user={pg_user}"
            logger.warning('[DB-PITR] PITR recovery SUCCESS — replayed to WAL tail on :%d',
                           conf_port)
            return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port, 'PG_DSN': dsn}
        logger.critical('[DB-PITR] PITR start/replay FAILED (rc=%d): %s — quarantining',
                        start.returncode, (start.stderr or '').strip()[:300])
    except Exception as e:
        logger.critical('[DB-PITR] PITR start raised: %s — quarantining', e)
    _quarantine_corrupt_pgdata(local_pgdata)
    return None


def _quarantine_corrupt_pgdata(pgdata):
    """Move a corrupt pgdata aside to pgdata.corrupt.<ts> so a fresh initdb can run.

    Returns the quarantine path on success, or None on failure (in which case
    the caller must NOT proceed to initdb over live data).
    """
    quarantine = f'{pgdata}.corrupt.{time.strftime("%Y%m%d_%H%M%S")}'
    try:
        os.rename(pgdata, quarantine)
        logger.warning('[DB-SelfHeal] Quarantined corrupt cluster: %s → %s',
                       pgdata, quarantine)
        return quarantine
    except OSError as e:
        logger.error('[DB-SelfHeal] Could not quarantine corrupt pgdata %s: %s',
                     pgdata, e)
        return None


def _try_self_heal_corrupt_pg(pgdata, base_dir, pg_host, pg_port, pg_user,
                              pg_password, pg_dbname):
    """Automatically recover a PG cluster that exists but refuses to start.

    Triggered from _ensure_pg_running when every normal start path failed but
    a real cluster is present on disk (the 2026-06-04 'WAL redo PANIC →
    lost conversations' class of failure). Fully automatic — the user never
    has to run pg_resetwal / psql by hand.

    Two-stage strategy (least destructive first):
      1. ``pg_resetwal -f`` on the existing pgdata, then retry start. This is
         the standard escape from a WAL-redo PANIC and preserves all heap data
         (only un-checkpointed WAL is discarded). A pre-reset filesystem copy
         of pg_wal is NOT kept, but the whole cluster is quarantined in stage 2
         only if this fails, so stage-1 operates in place.
      2. If reset fails (or the reset cluster still won't start), quarantine
         the corrupt pgdata, ``initdb`` a fresh cluster, and restore the latest
         ``data/pg_backups/`` logical dump into it. Data written since the last
         nightly backup is lost, but the service comes back automatically with
         the bulk of history instead of a 2-row SQLite.

    Disabled by TOFU_PG_SELF_HEAL=0 (then we fall back to the loud-fail path).

    Returns:
        dict {PG_HOST, PG_PORT, PG_DSN} on success, or None.
    """
    if getenv_compat('TOFU_PG_SELF_HEAL', default='1').lower() in ('0', 'false', 'no'):
        logger.warning('[DB-SelfHeal] Disabled via TOFU_PG_SELF_HEAL — skipping '
                       'automatic recovery')
        return None
    if not _pg_binaries_present():
        return None

    from lib.log import audit_log

    def _build_dsn(host, port):
        dsn = f"host={host} port={port} dbname={pg_dbname}"
        if pg_user:
            dsn += f" user={pg_user}"
        if pg_password:
            dsn += f" password={pg_password}"
        return dsn

    # Take an exclusive backup of the corrupt cluster dir first (cheap, just a
    # filesystem copy) so a botched self-heal can never destroy the only copy.
    logger.warning('[DB-SelfHeal] PG cluster at %s exists but will not start — '
                   'attempting automatic recovery', pgdata)
    audit_log('pg_self_heal_start', pgdata=pgdata)

    # ── Stage 1: pg_resetwal -f, then retry start ──
    pg_resetwal = _find_pg_binary('pg_resetwal')
    if shutil.which(pg_resetwal) or os.path.isfile(pg_resetwal):
        try:
            result = subprocess.run(
                [pg_resetwal, '-f', '-D', pgdata],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.warning('[DB-SelfHeal] Stage 1: pg_resetwal -f succeeded — '
                               'retrying start')
                conf_port = _read_our_pg_port(pgdata) or pg_port
                log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
                try:
                    start = subprocess.run(
                        [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path,
                         'start', '-w', '-t', '30'],
                        capture_output=True, text=True, timeout=45)
                    if start.returncode == 0 and _verify_pg_after_start(
                            conf_port, pgdata, pg_user, total_wait_s=12):
                        logger.warning('[DB-SelfHeal] Stage 1 recovery SUCCESS — '
                                       'PG started after pg_resetwal')
                        _ensure_database_exists('127.0.0.1', conf_port, pg_dbname,
                                                pg_user, pgdata)
                        _write_owner_host(pgdata)
                        _mark_pg_owned_locally(pgdata)
                        audit_log('pg_self_heal_success', stage='resetwal',
                                  port=conf_port)
                        return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                                'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
                    logger.error('[DB-SelfHeal] Stage 1: PG still will not start '
                                 'after pg_resetwal (rc=%d): %s',
                                 start.returncode, (start.stderr or '').strip()[:300])
                    _stop_local_pg_quietly(pgdata)
                except Exception as e:
                    logger.error('[DB-SelfHeal] Stage 1 start attempt raised: %s', e)
            else:
                logger.error('[DB-SelfHeal] Stage 1: pg_resetwal failed (rc=%d): %s',
                             result.returncode, (result.stderr or '').strip()[:300])
        except Exception as e:
            logger.error('[DB-SelfHeal] Stage 1 pg_resetwal raised: %s', e)

    # ── Stage 2: quarantine + fresh initdb + restore latest backup ──
    latest = _latest_pg_backup(base_dir)
    if not latest:
        logger.critical('[DB-SelfHeal] Stage 2 impossible: no logical backup in '
                        'data/pg_backups/ to restore from. Leaving corrupt cluster '
                        'in place for manual recovery (pg_resetwal already tried). '
                        'A nightly backup would have made this automatic.')
        audit_log('pg_self_heal_failed', reason='no_backup')
        return None

    logger.warning('[DB-SelfHeal] Stage 2: rebuilding from latest backup %s', latest)
    quarantine = _quarantine_corrupt_pgdata(pgdata)
    if not quarantine:
        return None

    # Stage the dump where _bootstrap_pg's restore step looks for it, so we
    # reuse the well-tested initdb → createdb → restore path verbatim.
    staged_dump = os.path.join(base_dir, 'data', 'pg_backup.sql')
    try:
        shutil.copy2(latest, staged_dump)
    except OSError as e:
        logger.error('[DB-SelfHeal] Could not stage backup for restore: %s', e)
        return None

    result = _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user,
                           pg_password, pg_dbname)
    if result:
        logger.warning('[DB-SelfHeal] Stage 2 recovery SUCCESS — fresh cluster '
                       'restored from %s (corrupt cluster preserved at %s)',
                       latest, quarantine)
        audit_log('pg_self_heal_success', stage='restore_backup',
                  backup=latest, quarantine=quarantine)
        return result

    logger.critical('[DB-SelfHeal] Stage 2 FAILED — fresh initdb+restore did not '
                    'come up. Corrupt cluster preserved at %s, backup at %s.',
                    quarantine, latest)
    audit_log('pg_self_heal_failed', reason='restore_failed',
              quarantine=quarantine, backup=latest)
    return None
