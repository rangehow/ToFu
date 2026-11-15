"""Persistent per-project file index ("locate DB") for search_files / find_files.

Why this exists
───────────────
On FUSE/network filesystems (DolphinFS, cross-DC mounts) the dominant cost of
``search_files`` / ``find_files`` is NOT reading file contents — it is the
directory walk itself: every query re-issues tens of thousands of
readdir/stat RPCs before ripgrep/fd can even start.  On large or cold trees
that walk alone regularly exceeds the 60s tool timeout.

This module builds the walk ONCE, in the background, and keeps the result
both in memory (LRU) and on disk (an automatically selected local cache when
eligible, otherwise the sessions directory). Query-time cost then becomes:

* find_files  → pure in-memory glob over the cached relpath list (µs–ms).
* search_files → the index yields the exact candidate file list (ignore rules
  and include-globs applied in memory); ripgrep is then invoked on EXPLICIT
  file paths, so it performs zero directory traversal and immediately
  parallelizes content reads.

Freshness model
───────────────
* Writes made through the project's own write tools update the index
  synchronously via :func:`note_write` (hooked from the write-freshness
  choke point), so agent-authored changes are visible immediately.
* Anything else (git checkout, external editors) is picked up by a
  stale-while-revalidate refresh. A changed candidate snapshot refreshes again
  after ``STALE_AFTER_S``; byte-identical path+size snapshots back off to a
  bounded stable interval. Reads past the base interval carry explicit age
  evidence. Entries older than ``MAX_AGE_S`` are no longer trusted and the
  caller falls back to the live subprocess walk.
* An ignore-rule file write (``.gitignore``/``.ignore`` at any depth,
  ``.git/info/exclude``) invalidates the index outright (ignore rules are
  baked into the candidate list). Tracked-set changes (``git add``/``rm``,
  checkouts) are picked up by the refresh like any external change.
* A query whose target prefix holds no indexed file — a pruned subtree such
  as ``.tofu/worktrees/x`` or ``node_modules/pkg``, or a directory created
  after the snapshot — declines to the live walk (:func:`covers`). An
  explicit path is always searched, exactly as rg/fd search an explicit root.

Coverage (``tree_index_coverage.py``) is Git's view of the work tree: every
tracked file plus every untracked file Git does not ignore, under Git's exact
ignore semantics (last match wins, deeper files outrank shallower ones,
``info/exclude`` and ``core.excludesFile`` rank below them, an excluded
directory is never entered). ``.ignore`` files outrank Git as in ripgrep and
hold the project's model-facing search scope; ``IGNORE_DIRS`` names (``node_modules``,
``.tofu``, ...) keep untracked bulk unwalked while their tracked files stay
listed. A NON-git tree honours the ignore files inside it (rg≥13 ignores them
there; that divergence is exclusion-only). Hidden entries are indexed like any
other file; symlinks and ``.git`` entries are skipped. A directory listed only
for its tracked files makes :func:`covers` decline queries inside it, so the
live walk answers them.

Everything here is best-effort: no public function raises, and every failure
mode degrades to the pre-index code path (live subprocess walk).
"""

from __future__ import annotations

import array
import bisect
import hashlib
import os
import struct
import threading
import time
import sys
from concurrent.futures import FIRST_COMPLETED, wait
from collections import deque

from lib.log import get_logger
from lib.observability import InstrumentedThreadPoolExecutor
from lib.project_mod import glob_match
from lib.project_mod import tree_index_coverage as _coverage
from lib.project_mod.config import IGNORE_DIRS, SESSIONS_DIR
from lib.project_mod.tree_index_coverage import IGNORE_RULE_FILES, excluded, verdict
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════
#  Tunables (env-overridable)
# ═══════════════════════════════════════════════════════

def _env_float(name, default, minimum=0.0, maximum=86_400.0):
    try:
        v = float(os.environ.get(name, '') or default)
        return max(minimum, min(maximum, v))
    except (TypeError, ValueError):
        return default


def _disabled():
    return os.environ.get('TOFU_TREE_INDEX_DISABLE', '').strip() == '1'


# Entries older than this are served while a background refresh is kicked.
def _stale_after_s():
    return _env_float('TOFU_TREE_INDEX_STALE_S', 45.0)


# Entries older than this are NOT served; caller falls back to the live walk.
def _max_age_s():
    return _env_float('TOFU_TREE_INDEX_MAX_AGE_S', 900.0)


def _stable_refresh_max_s():
    """Maximum refresh interval after unchanged path+size snapshots.

    Keep at least one refresh opportunity before MAX_AGE whenever the operator
    has configured a coherent max age. The 180-second default is deliberately
    conservative: it removes 45-second rebuild loops without turning this
    eventually-consistent accelerator into a long-lived authority.
    """
    base = _stale_after_s()
    configured = _env_float(
        'TOFU_TREE_INDEX_STABLE_REFRESH_MAX_S',
        180.0,
        minimum=1.0,
        maximum=900.0,
    )
    freshness_ceiling = max(base, _max_age_s() * 0.8)
    return max(base, min(configured, freshness_ceiling))


def _max_entries():
    return resolve_resource_budget(
        'TOFU_TREE_INDEX_MAX_ENTRIES', minimum=10_000, maximum=600_000)


def _walk_jobs():
    return resolve_resource_budget(
        'TOFU_TREE_INDEX_WALK_JOBS', minimum=2, maximum=16)


def _mem_roots():
    return resolve_resource_budget(
        'TOFU_TREE_INDEX_MEM_ROOTS', minimum=1, maximum=8)


# Bump whenever the coverage policy changes: a persisted snapshot built under
# an older policy would otherwise serve the wrong candidate set until refresh.
# TFIX002: hidden entries included, ``.ignore`` files honoured.
# TFIX003: Git's view (tracked files, exact ignore precedence), partial dirs.
_DISK_MAGIC = b'TFIX003\n'


# ═══════════════════════════════════════════════════════
#  In-memory index entry
# ═══════════════════════════════════════════════════════

class TreeIndex:
    """Columnar snapshot of one project's file list.

    ``paths`` is a SORTED list of project-relative '/'-joined file paths;
    ``sizes`` / ``mtimes`` are parallel ``array('q')`` columns.  Kept as
    columnar arrays so a 500k-file index loads from disk in ~0.1s and stays
    compact in RAM. Synchronized writes mutate those columns under ``_lock``;
    the revision pair prevents an older asynchronous persist from committing
    after such a mutation. ``partial_dirs`` are pruned directories listed
    only for their tracked files (persisted with the snapshot).
    """

    __slots__ = ('root', 'paths', 'sizes', 'mtimes', 'built_at', 'complete',
                 'policy', 'partial_dirs', 'refresh_after_s', 'unchanged_refreshes',
                 'mutation_revision', 'persisted_revision')

    def __init__(self, root, paths, sizes, mtimes, built_at, complete,
                 policy=None, *, partial_dirs=frozenset(), refresh_after_s=None,
                 unchanged_refreshes=0, persisted_revision=-1):
        self.root = root
        self.paths = paths
        self.sizes = sizes
        self.mtimes = mtimes
        self.built_at = built_at
        self.complete = complete
        # Ignore rules of the build, retained so the write hook admits exactly
        # the files a rebuild would; resolved lazily for a snapshot from disk.
        self.policy = policy
        self.partial_dirs = frozenset(partial_dirs)
        self.refresh_after_s = float(
            _stale_after_s() if refresh_after_s is None else refresh_after_s)
        self.unchanged_refreshes = max(0, int(unchanged_refreshes))
        self.mutation_revision = 0
        self.persisted_revision = int(persisted_revision)

    def age(self):
        return time.time() - self.built_at

    def __len__(self):
        return len(self.paths)


# root → TreeIndex (LRU); root → build-in-progress flag.
_lock = threading.RLock()
_build_condition = threading.Condition(_lock)
_mem = {}            # OrderedDict semantics via move_to_end on hit
_building = set()

# One owned generation serves overlapping roots. Root builds remain admitted;
# each walk feeds directory microtasks through its launch-probed window.
_builder = None
_scanner = None
_scanner_admissions = 0


def _new_builder():
    return InstrumentedThreadPoolExecutor(
        max_workers=resolve_resource_budget('TOFU_AGENT_WORKERS', maximum=sys.maxsize),
        metric_pool='tree-index', thread_name_prefix='tree-index')


def _new_scanner():
    return InstrumentedThreadPoolExecutor(
        max_workers=_walk_jobs(), metric_pool='tree-index-scan', thread_name_prefix='tree-index-scan')


def background_builder_snapshot():
    """Return the demand builder's current resource state."""
    with _lock:
        builder = _builder
        scanner = _scanner
        builder_threads = tuple(getattr(builder, '_threads', ()))
        scanner_threads = tuple(getattr(scanner, '_threads', ()))
        return {
            'activeBuilds': len(_building),
            'executorActive': builder is not None,
            'residentThreads': sum(
                thread.is_alive() for thread in builder_threads),
            'scanExecutorActive': scanner is not None,
            'scanResidentThreads': sum(
                thread.is_alive() for thread in scanner_threads),
            'scanWorkerCapacity': max(
                int(getattr(scanner, '_max_workers', _walk_jobs())),
                len(scanner_threads)),
            'scanIdleReuseHint': int(getattr(scanner, '_max_workers', _walk_jobs())),
            'retainedRoots': len(_mem),
            'retainedEntries': sum(len(entry.paths) for entry in _mem.values()),
            'rootCapacity': _mem_roots(),
            'entryRetentionTarget': _max_entries(),
            'stableRefreshMaxSeconds': _stable_refresh_max_s(),
            'maxScheduledRefreshSeconds': max(
                (entry.refresh_after_s for entry in _mem.values()),
                default=0.0,
            ),
            'unchangedRefreshes': sum(
                entry.unchanged_refreshes for entry in _mem.values()),
        }


# ═══════════════════════════════════════════════════════
#  Background tree walk
# ═══════════════════════════════════════════════════════

def _scan_one_dir(abs_dir, rel_dir, depth, out, contexts):
    """Scandir ONE directory; append admitted files to *out*; RETURN child dirs.

    Runs inside the walk pool.  ``out`` appends ``(rel, size, mtime)`` tuples;
    DirEntry caches the stat from readdir where the FS supports readdirplus,
    so this stays at ~1-2 RPCs per directory on FUSE.  Returns a list of
    ``(abs_dir, rel_dir, depth, child_contexts)`` tuples for the BFS.

    ``contexts`` is ``(policy, gitignore, dot_ignore, tracked_only)``: the
    build's :class:`IgnorePolicy`, the rule lists in force (highest precedence
    first) and whether this directory lies in a pruned one, where only tracked
    files are listed and only directories holding tracked files are entered.
    Rule files found in the listing extend the lists for this subtree at no
    extra RPC; they are ordinary searchable files.
    """
    policy, gitignore, dot_ignore, tracked_only = contexts
    rows = []
    rule_entries = {}
    try:
        with os.scandir(abs_dir) as it:
            for entry in it:
                if entry.name in IGNORE_RULE_FILES:
                    rule_entries[entry.name] = entry
                rows.append(entry)
    except OSError as e:
        logger.debug('[TreeIndex] scandir failed for %s: %s', abs_dir, e)
        raise

    if rule_entries and rel_dir:  # the policy holds the root's own files
        gitignore, dot_ignore = policy.enter(
            rel_dir, rule_entries, tracked_only, gitignore, dot_ignore)

    subdirs = []
    for entry in rows:
        name = entry.name
        if name == '.git':
            continue
        try:
            if entry.is_symlink():
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if not is_dir and not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        rel_e = (rel_dir + '/' + name) if rel_dir else name
        rel_b, name_b = os.fsencode(rel_e), os.fsencode(name)
        dot = verdict(dot_ignore, rel_b, name_b, is_dir) if dot_ignore else None
        if dot:
            continue  # .ignore outranks Git, as in ripgrep, and drops tracked files too
        pruned = tracked_only or (is_dir and name in IGNORE_DIRS) or (
            dot is None and excluded(gitignore, rel_b, name_b, is_dir))
        if is_dir:
            if pruned:
                if not policy.has_tracked_below(rel_b):
                    continue
                if not tracked_only:
                    policy.partial_dirs.append(rel_e)
            subdirs.append((entry.path, rel_e, depth + 1,
                            (policy, gitignore, dot_ignore, pruned)))
        elif not pruned or policy.is_tracked(rel_b):
            try:
                st = entry.stat(follow_symlinks=False)
                out.append((rel_e, int(st.st_size), int(st.st_mtime)))
            except OSError:
                out.append((rel_e, 0, 0))
    return subdirs


def _walk_tree(root, deadline=None, *, scan_executor=None, policy=None):
    """Parallel BFS walk of *root*.  Returns ``(rows, complete)``.

    Complete means every admitted directory was read successfully. The legacy
    deadline argument is ignored: elapsed time cannot make a valid tree partial.
    Directory Futures remain windowed; the frontier retains descriptors.
    *policy* defaults to :func:`tree_index_coverage.root_policy` of *root*.
    """
    if policy is None:
        policy = _coverage.root_policy(root)
    out = []
    truncated = False

    owns_executor = scan_executor is None
    pool = scan_executor or _new_scanner()
    pending = set()
    frontier = deque([(root, '', 0, (policy, policy.gitignore, policy.dot_ignore, False))])
    scan_window = _walk_jobs()
    try:
        while (frontier or pending) and not truncated:
            # Demand executors create workers for submitted work. Keep the
            # rest of this directory frontier as descriptors, not Futures or
            # OS threads; accepted project builds still progress independently.
            while frontier and len(pending) < scan_window:
                abs_d, rel_d, depth, ctx = frontier.popleft()
                pending.add(pool.submit(_scan_one_dir, abs_d, rel_d, depth, out, ctx))
            if truncated or not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    subdirs = fut.result() or []
                except Exception as e:
                    logger.debug('[TreeIndex] walk task failed: %s', e)
                    truncated = True
                    continue
                for abs_d, rel_d, depth, ctx in subdirs:
                    frontier.append((abs_d, rel_d, depth, ctx))
    finally:
        # Shared scan workers can outlive this build. Drain only this walk's
        # accepted futures before returning so none can mutate ``out`` after
        # rows are sorted or discarded.
        for future in pending:
            future.cancel()
        if pending:
            wait(pending)
        if owns_executor:
            pool.shutdown(wait=True, cancel_futures=True)
    return out, not truncated


def _build_sync(root, *, scan_executor=None):
    """Build (or rebuild) the index for *root*; swap into memory; persist.

    Runs on the builder pool.  Never raises.
    """
    t0 = time.perf_counter()
    try:
        policy = _coverage.root_policy(root)
        rows, complete = _walk_tree(root, scan_executor=scan_executor, policy=policy)
        if not complete:
            logger.warning('[TreeIndex] walk of %s incomplete after a read failure '
                           '(entries=%d) — index NOT installed; queries fall back to live walk',
                           root, len(rows))
            return
        rows.sort(key=lambda r: r[0])
        paths = [r[0] for r in rows]
        sizes = array.array('q', (r[1] for r in rows))
        mtimes = array.array('q', (r[2] for r in rows))
        partial_dirs = frozenset(policy.partial_dirs)
        policy.tracked = []  # the write hook needs only paths and rules
        policy.nested_complete = True
        with _lock:
            previous = _mem.get(root)
            unchanged = bool(
                previous is not None
                and previous.complete
                and previous.paths == paths
                and previous.sizes == sizes
                and previous.partial_dirs == partial_dirs
            )
            if unchanged:
                unchanged_refreshes = previous.unchanged_refreshes + 1
                refresh_after_s = min(
                    _stable_refresh_max_s(),
                    max(_stale_after_s(), previous.refresh_after_s * 2),
                )
            else:
                unchanged_refreshes = 0
                refresh_after_s = _stale_after_s()
            entry = TreeIndex(
                root, paths, sizes, mtimes, time.time(), True,
                policy,
                partial_dirs=partial_dirs,
                refresh_after_s=refresh_after_s,
                unchanged_refreshes=unchanged_refreshes,
            )
            _mem[root] = entry
            _mem_move_to_end(root)
            _evict_mem_over_cap()
        _persist(entry)
        elapsed = time.perf_counter() - t0
        logger.info(
            '[TreeIndex] built %s: %d files in %.1fs '
            '(partial_dirs=%d unchanged=%d refresh_after=%.0fs)',
            root, len(paths), elapsed, len(partial_dirs), unchanged_refreshes,
            refresh_after_s,
        )
    except Exception as e:
        logger.warning('[TreeIndex] build failed for %s: %s', root, e, exc_info=True)
    finally:
        with _lock:
            _building.discard(root)


def _finish_builder_job(root, builder, scanner):
    """Release one root lease; later warm calls may already own this generation."""
    global _builder, _scanner, _scanner_admissions
    builder_to_shutdown = None
    scanner_to_shutdown = None
    with _lock:
        _building.discard(root)
        if scanner is not None and _scanner is scanner:
            _scanner_admissions = max(0, _scanner_admissions - 1)
        _build_condition.notify_all()
        if not _building and builder is not None and _builder is builder:
            _builder = None
            builder_to_shutdown = builder
        if not _building and not _scanner_admissions \
                and scanner is not None and _scanner is scanner:
            _scanner = None
            scanner_to_shutdown = scanner
    if scanner_to_shutdown is not None:
        scanner_to_shutdown.shutdown(wait=False, cancel_futures=False)
    if builder_to_shutdown is not None:
        # This normally runs on the final builder worker. Waiting here would
        # attempt to join the current thread; non-waiting shutdown lets it
        # return naturally after the accepted batch has completed.
        builder_to_shutdown.shutdown(wait=False, cancel_futures=False)


def _restore_disk_candidate_before_build(root):
    """Restore an evicted local snapshot inside the background worker.

    Direct ``warm`` callers do not pass through :func:`acquire`, so an LRU
    miss previously scheduled a full project walk even when a fresh local
    blob existed.  A stale-but-still-trusted blob is installed for immediate
    stale-while-revalidate service, but only a base-fresh blob suppresses the
    walk.  Disk I/O therefore stays off the request thread.
    """
    with _lock:
        current = _mem.get(root)
        if current is not None:
            return bool(
                current.complete
                and current.age() <= current.refresh_after_s
            )
    restored = _load_disk(root)
    if (restored is None or not restored.complete
            or restored.age() > _max_age_s()):
        return False
    with _lock:
        current = _mem.get(root)
        if current is None:
            _mem[root] = restored
            _mem_move_to_end(root)
            _evict_mem_over_cap()
            current = _mem.get(root)
        else:
            _mem_move_to_end(root)
        fresh = bool(
            current is not None
            and current.complete
            and current.age() <= current.refresh_after_s
        )
    if fresh:
        logger.debug(
            '[TreeIndex] restored fresh disk index without project walk for %s',
            root,
        )
    return fresh


def _run_builder_job(root, builder, scanner):
    try:
        if not _restore_disk_candidate_before_build(root):
            _build_sync(root, scan_executor=scanner)
    finally:
        # _build_sync also clears the root lease. Keep this idempotent wrapper
        # as the lifecycle authority so injected/test builders cannot strand
        # the executor generation when their build function returns early.
        _finish_builder_job(root, builder, scanner)


def _mem_move_to_end(root):
    # Plain dicts preserve insertion order; emulate LRU touch.
    entry = _mem.pop(root, None)
    if entry is not None:
        _mem[root] = entry


def _evict_mem_over_cap():
    root_capacity = _mem_roots()
    entry_capacity = _max_entries()
    retained_entries = sum(len(entry.paths) for entry in _mem.values())
    # Retain the newest usable index even when it alone exceeds the target.
    # Returned index objects remain owned by their callers after LRU removal.
    while (len(_mem) > 1 and (
            len(_mem) > root_capacity or retained_entries > entry_capacity)):
        victim = next(iter(_mem))
        entry = _mem.get(victim)
        _checkpoint_dirty_eviction(entry)
        entry = _mem.pop(victim, None)
        retained_entries -= len(entry.paths) if entry is not None else 0
        logger.debug(
            '[TreeIndex] LRU-evicted in-memory index for %s '
            '(roots=%d/%d entries=%d/%d)',
            victim, len(_mem), root_capacity,
            retained_entries, entry_capacity,
        )


def _checkpoint_dirty_eviction(entry):
    """Persist a write-fresh entry before LRU eviction when its blob is stale.

    ``note_write`` normally removes the superseded blob, which makes a later
    memory miss honest but would otherwise force the read tool and background
    builder back onto the project filesystem. The revision comparison also
    detects a stale blob left by an unlink failure. Checkpointing while
    ``_lock`` is held snapshots the current parallel columns consistently.
    Clean entries already have a blob and incur no write; a failed checkpoint
    preserves the established rebuild fallback.
    """
    if not isinstance(entry, TreeIndex) or not entry.complete:
        return False
    if entry.persisted_revision == entry.mutation_revision:
        try:
            if os.path.isfile(_disk_path(entry.root)):
                return False
        except OSError:
            pass
    if not _persist(entry):
        return False
    logger.debug(
        '[TreeIndex] checkpointed write-fresh index before LRU eviction for %s',
        entry.root,
    )
    return True


def warm(root):
    """Kick a background build/refresh for *root* when stale.  Non-blocking.

    Called from project registration (set_project / ensure_project_state) and
    lazily from the read tools, so the index usually exists by the time a
    grep/find query needs it.  The common fresh-hit path is a pure dict
    lookup — no filesystem RPC at all.
    """
    global _builder, _scanner, _scanner_admissions
    if _disabled() or not root:
        return
    builder = None
    scanner = None
    try:
        root = os.path.abspath(root)
        with _lock:
            if root in _building:
                return False
            entry = _mem.get(root)
            if (entry is not None and entry.complete
                    and entry.age() <= entry.refresh_after_s):
                return  # fresh enough — nothing to do
            _building.add(root)
        try:
            if os.path.isdir(root):
                with _lock:
                    builder = _builder
                    scanner = _scanner
                    if builder is None:
                        builder = _new_builder()
                        _builder = builder
                    if scanner is None:
                        scanner = _new_scanner()
                        _scanner = scanner
                        _scanner_admissions = 0
                    _scanner_admissions += 1
                    builder.submit(
                        _run_builder_job, root, builder, scanner)
            else:
                with _lock:
                    _building.discard(root)
        except Exception:
            _finish_builder_job(root, builder, scanner)
            raise
    except Exception as e:
        logger.debug('[TreeIndex] warm(%s) skipped: %s', root, e)


def invalidate(root):
    """Drop memory + disk state for *root* (e.g. .gitignore changed)."""
    if not root:
        return
    root = os.path.abspath(root)
    with _lock:
        _mem.pop(root, None)
        _build_condition.notify_all()
    _drop_persisted_snapshot(root)


def _drop_persisted_snapshot(root):
    """Remove one reconstructible disk snapshot without touching live memory."""
    try:
        os.unlink(_disk_path(root))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug('[TreeIndex] disk invalidate failed for %s: %s', root, e)


# ═══════════════════════════════════════════════════════
#  Reconstructible disk persistence; placement/retention: tree_index_storage
# ═══════════════════════════════════════════════════════

def _index_dir():
    from lib.project_mod.tree_index_storage import index_directory

    path = index_directory(SESSIONS_DIR)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.debug('[TreeIndex] cannot create index dir %s: %s', path, e)
    return path


def _disk_path(root):
    digest = hashlib.sha1(root.encode('utf-8', 'surrogateescape')).hexdigest()[:20]
    return os.path.join(_index_dir(), f'{digest}.tfix')


def _persist(entry):
    """Write the index atomically and report success. Little-endian blob:

    magic(8) built_at(d) count(I) root_len(H) root sizes(count×8) mtimes(count×8)
    paths_len(I) paths partial_len(I) partial_dirs
    """
    tmp = None
    try:
        with _lock:
            if _mem.get(entry.root) is not entry:
                return False
            root = entry.root
            built_at = entry.built_at
            mutation_revision = entry.mutation_revision
            paths = tuple(entry.paths)
            sizes_blob = entry.sizes.tobytes()
            mtimes_blob = entry.mtimes.tobytes()
            partial_dirs = sorted(entry.partial_dirs)
        paths_blob = '\n'.join(paths).encode('utf-8', 'surrogateescape')
        partial_blob = '\n'.join(partial_dirs).encode('utf-8', 'surrogateescape')
        root_b = root.encode('utf-8', 'surrogateescape')
        header = struct.pack(
            '<8sdIH', _DISK_MAGIC, built_at, len(paths), len(root_b))
        blob = (header + root_b + sizes_blob + mtimes_blob
                + struct.pack('<I', len(paths_blob)) + paths_blob
                + struct.pack('<I', len(partial_blob)) + partial_blob)
        final = _disk_path(root)
        from lib.project_mod.tree_index_storage import prepare_write

        if not prepare_write(final, len(blob)):
            return False
        tmp = (final + f'.tmp.{os.getpid()}.{threading.get_ident()}.'
               f'{time.monotonic_ns()}')
        with open(tmp, 'wb') as f:
            f.write(blob)
        with _lock:
            if (_mem.get(root) is not entry
                    or entry.mutation_revision != mutation_revision):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False
            os.replace(tmp, final)
            tmp = None
            entry.persisted_revision = mutation_revision
        return True
    except Exception as e:
        logger.debug('[TreeIndex] persist failed for %s: %s', entry.root, e)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def _load_disk(root):
    """Load a persisted index for *root* (root-string validated)."""
    try:
        path = _disk_path(root)
        with open(path, 'rb') as f:
            header_size = struct.calcsize('<8sdIH')
            header = f.read(header_size)
            magic, built_at, count, root_len = struct.unpack(
                '<8sdIH', header)
            if magic != _DISK_MAGIC:
                return None
            blob = header + f.read()
        off = struct.calcsize('<8sdIH')
        disk_root = blob[off:off + root_len].decode('utf-8', 'surrogateescape')
        if disk_root != root:
            return None
        off += root_len
        nbytes = count * 8
        if len(blob) < off + 2 * nbytes + 4:
            return None  # Malformed count, not a source-size admission decision.
        sizes = array.array('q')
        sizes.frombytes(blob[off:off + nbytes])
        off += nbytes
        mtimes = array.array('q')
        mtimes.frombytes(blob[off:off + nbytes])
        off += nbytes
        (paths_len,) = struct.unpack_from('<I', blob, off)
        off += 4
        if off + paths_len + 4 > len(blob):
            return None
        paths = (blob[off:off + paths_len].decode('utf-8', 'surrogateescape').split('\n')
                 if count else [])
        off += paths_len
        (partial_len,) = struct.unpack_from('<I', blob, off)
        off += 4
        if off + partial_len != len(blob):
            return None
        partial_dirs = (blob[off:].decode('utf-8', 'surrogateescape').split('\n')
                        if partial_len else ())
        if len(paths) != count:
            return None
        if count and (not paths or paths[0] == '' and count > 1):
            return None
        # The write hook resolves the ignore rules on its first use.
        return TreeIndex(
            root, paths, sizes, mtimes, built_at, True,
            partial_dirs=partial_dirs, persisted_revision=0,
        )
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug('[TreeIndex] disk load failed for %s: %s', root, e)
        return None


# ═══════════════════════════════════════════════════════
#  Query entry point
# ═══════════════════════════════════════════════════════

def _wait_for_build(root):
    """Wait for the admitted cold build to finish and return its installed entry."""
    with _build_condition:
        while root in _building:
            _build_condition.wait()
        entry = _mem.get(root)
        if entry is not None:
            _mem_move_to_end(root)
        return entry


def _read_candidate(root):
    with _lock:
        entry = _mem.get(root)
        if entry is not None:
            _mem_move_to_end(root)
    if entry is None:
        entry = _load_disk(root)
        if entry is not None:
            with _lock:
                _mem[root] = entry
                _mem_move_to_end(root)
                _evict_mem_over_cap()
    return entry


def _usable_candidate(root, entry):
    if entry is None or not entry.complete:
        return None
    age = entry.age()
    if age > _max_age_s():
        return None
    if age > entry.refresh_after_s:
        warm(root)  # stale-while-revalidate: serve now, refresh behind us
    return entry


def acquire(root, *, wait_for_build=True):
    """Return a usable :class:`TreeIndex` for *root*, or ``None``.

    Only an already-running cold build is shared by concurrent readers;
    starting one here would conceal a write invalidation. An existing stale
    snapshot remains immediately usable while its refresh proceeds; failed
    builds retain the live fallback.
    """
    if _disabled() or not root:
        return None
    try:
        root = os.path.abspath(root)
        entry = _usable_candidate(root, _read_candidate(root))
        if entry is not None:
            return entry
        with _lock:
            building = root in _building
        warm(root)
        if not wait_for_build or not building:
            return None
        entry = _wait_for_build(root)
        return _usable_candidate(root, entry)
    except Exception as e:
        logger.debug('[TreeIndex] acquire(%s) failed: %s', root, e)
        return None


def freshness_evidence(entry):
    """Return visible age evidence once adaptive retention exceeds the base.

    Existing file contents are still read live by rg. The evidence concerns
    only externally changed candidate paths and display-grade size metadata;
    Tofu-owned writes update the in-memory candidate list synchronously.
    """
    if entry is None:
        return None
    age = max(0.0, entry.age())
    base = _stale_after_s()
    if age <= base:
        return None
    return {
        'ageSeconds': age,
        'baseRefreshSeconds': base,
        'scheduledRefreshSeconds': entry.refresh_after_s,
    }


# ═══════════════════════════════════════════════════════
#  Write-tool hook
# ═══════════════════════════════════════════════════════

def _rel_admissible(entry, rel):
    """Would a rebuild admit *rel* as a new (untracked) file?

    The write hook must not insert files a rebuild would exclude: a write
    into ``node_modules/``, ``.git/``, an ignored directory or a directory
    listed only for its tracked files stays invisible. Files already indexed
    (tracked ones included) are updated without asking. A failed rule lookup
    admits nothing; the next refresh reconciles.
    """
    policy = entry.policy
    if policy is None:
        try:
            policy = entry.policy = _coverage.root_policy(entry.root, tracked=False)
        except Exception as e:
            logger.debug('[TreeIndex] ignore rules unavailable for %s: %s', entry.root, e)
            return False
    return policy.admits_untracked(rel)


def note_write(abs_path):
    """Upsert *abs_path* into any in-memory index that contains its root.

    Hooked from the write-freshness choke point (every project write tool
    calls it), so agent-authored changes are reflected in the next query
    without waiting for the background refresh.  A vanished file (delete via
    run_command raced us) is dropped instead.  Ignore-rule sources
    (``.gitignore``/``.ignore`` at any depth, ``.git/info/exclude``) drop the
    whole index — their semantics are baked into the candidate list.
    """
    if _disabled() or not abs_path:
        return
    try:
        ap = os.path.abspath(abs_path)
    except OSError:
        return
    with _lock:
        snapshot = list(_mem.values())
    for entry in snapshot:
        root = entry.root
        if ap != root and not ap.startswith(root + os.sep):
            continue
        rel = os.path.relpath(ap, root).replace(os.sep, '/')
        if rel.rsplit('/', 1)[-1] in IGNORE_RULE_FILES \
                or rel == '.git/info/exclude':
            logger.info('[TreeIndex] ignore rules written under %s (%s) — invalidating index',
                        root, rel)
            invalidate(root)
            return
        try:
            st = os.stat(ap)
            size, mtime = int(st.st_size), int(st.st_mtime)
            remove = False
        except OSError:
            size = mtime = 0
            remove = True
        with _lock:
            idx = bisect.bisect_left(entry.paths, rel)
            found = idx < len(entry.paths) and entry.paths[idx] == rel
        if not found and (remove or not _rel_admissible(entry, rel)):
            continue  # not indexable (ignored) — nothing to record
        persisted_snapshot_is_stale = False
        with _lock:
            live = _mem.get(root)
            if live is not entry:
                continue  # entry was evicted/replaced meanwhile
            idx = bisect.bisect_left(entry.paths, rel)
            found = idx < len(entry.paths) and entry.paths[idx] == rel
            if not found and remove:
                continue
            if remove:
                if found:
                    del entry.paths[idx]
                    del entry.sizes[idx]
                    del entry.mtimes[idx]
                    persisted_snapshot_is_stale = True
            elif found:
                entry.sizes[idx] = size
                entry.mtimes[idx] = mtime
                persisted_snapshot_is_stale = True
            else:
                entry.paths.insert(idx, rel)
                entry.sizes.insert(idx, size)
                entry.mtimes.insert(idx, mtime)
                persisted_snapshot_is_stale = True
            if persisted_snapshot_is_stale:
                entry.mutation_revision += 1
                # The local blob predates this mutation. Remove it while the
                # memory authority is still locked, before cap enforcement can
                # evict this root and make a stale disk load eligible again.
                _drop_persisted_snapshot(root)
                _mem_move_to_end(root)
                _evict_mem_over_cap()


# ═══════════════════════════════════════════════════════
#  Query helpers
# ═══════════════════════════════════════════════════════

def _prefix_bounds(entry, rel_prefix):
    """Return the [lo, hi) slice of the sorted path list under *rel_prefix*."""
    if not rel_prefix or rel_prefix == '.':
        return 0, len(entry.paths)
    prefix = rel_prefix.rstrip('/') + '/'
    paths = entry.paths
    lo = bisect.bisect_left(paths, prefix)
    hi = bisect.bisect_left(paths, prefix[:-1] + '0')  # '0' == '/' + 1
    return lo, hi


def covers(entry, rel_prefix):
    """True when the snapshot lists every file default discovery admits at or
    below *rel_prefix*, and at least one exists there.

    An empty slice is not evidence of an empty directory: the target may lie
    in a subtree the coverage policy prunes (an explicit ``.tofu/worktrees/x``
    or ``node_modules/pkg`` request) or may postdate the snapshot. A target
    in a pruned directory listed only for its tracked files (``partial_dirs``)
    is incomplete. Callers answer such targets from the live walk, which
    searches explicit roots.
    """
    lo, hi = _prefix_bounds(entry, rel_prefix)
    if hi <= lo:
        return False
    if entry.partial_dirs and rel_prefix and rel_prefix != '.':
        parts = rel_prefix.strip('/').split('/')
        for depth in range(1, len(parts) + 1):
            if '/'.join(parts[:depth]) in entry.partial_dirs:
                return False
    return True


def find_matching(entry, rel_prefix, pattern, cap, *, case_sensitive=False):
    """In-memory find_files: ``[(rel, size), ...]`` capped at *cap*.

    *pattern* is a project glob validated by the caller and matched exactly
    like the live walkers do (``glob_match``): case-insensitive unless
    *case_sensitive*, against the basename unless it contains '/', in which
    case against the project-relative path.
    """
    lo, hi = _prefix_bounds(entry, rel_prefix)
    paths, sizes = entry.paths, entry.sizes
    matches = glob_match.compile_glob(
        pattern, case_sensitive=case_sensitive).matches
    out = []
    for i in range(lo, hi):
        p = paths[i]
        if matches(p):
            out.append((p, sizes[i]))
            if len(out) >= cap:
                break
    return out


def grep_candidates(entry, rel_prefix, include):
    """In-memory candidate list for grep: ``(abs_paths, total_bytes)``.

    Applies the caller-validated include glob through ``glob_match``, the
    same matcher and scope rule every live backend follows. File size is
    telemetry, never source admission.
    ``total_bytes`` is the summed candidate size from the snapshot — the
    chunk runner uses it to tell content-heavy searches (parallelize reads
    across processes) from metadata-bound ones.
    """
    lo, hi = _prefix_bounds(entry, rel_prefix)
    paths, sizes = entry.paths, entry.sizes
    root = entry.root
    matches = glob_match.compile_glob(include).matches if include else None
    out = []
    total = 0
    for i in range(lo, hi):
        sz = sizes[i]
        p = paths[i]
        if matches is not None and not matches(p):
            continue
        out.append(root + '/' + p)
        total += sz
    return out, total
