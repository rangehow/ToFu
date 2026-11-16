//! Low-priority bounded scheduling for authority-local maintenance.
//!
//! The scheduler owns one scope worker, admits an explicit bounded set of
//! tenant/owner scopes, and attempts at most one already-bounded authority
//! transaction per job for restored mounts, history, and expired
//! reconstructible tool results. Logical jobs prepare on pinned snapshots
//! outside the writer; each commit validates the original OCC witnesses.
//! Scope rounds never wait for the foreground
//! authority mutex: contention always defers them to a later round. The
//! overlay background drain is the one exception to the no-wait rule: on
//! its own fast poll cadence it takes the mutex in single-chunk slices
//! (`drain_overlay_in_background`), because deferring under continuous load
//! pushed every overlay merge into the foreground commit path (2026-09-21
//! measurement: ~46-58 µs/append amortized; with slices it is ~0.1).
//! That cadence, with catalog location derivation and history retirement
//! slices, runs on its own `tofu-db-overlay-drain` driver thread: a scope
//! round's search rebuild takes minutes on a loaded volume, and sharing the
//! scope worker left the drain unpolled for whole rounds (2026-11-11).
//! While that driver is alive it owns automatic merging: commits never merge
//! the overlay inline and only report WAL pressure to it
//! (`entity_overlay::OverlayDrainSignal`).
//! A committed search invalidation rings the worker's condvar, ending idle
//! backoff for that scope at the base cadence (`search_dirty::SearchInvalidations`).

use std::collections::BTreeSet;
use std::io;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, TryLockError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::authority::AuthorityDatabase;
use crate::generated_tofudb_ir::{
    LEAN_HISTORY_RETAINED_SEGMENTS, LEAN_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS,
    MAINTENANCE_WORKER_STACK_BYTES, MAX_HISTORY_RETAINED_SEGMENTS,
    MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS, MAX_MAINTENANCE_SCOPES, MAX_MAINTENANCE_WORKERS,
    OBSERVED_HISTORY_RETAINED_SEGMENTS, OBSERVED_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS,
    TOOL_RESULT_PRUNE_ROWS_PER_MAINTENANCE_ROUND,
};
use crate::resource_probe::DaemonResourceBudget;
use crate::turn_search_projection::TurnSearchProjection;

#[path = "maintenance_preparation.rs"]
mod prepared;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct MaintenanceScope {
    pub tenant_id: u64,
    pub owner_user_id: u64,
}

impl MaintenanceScope {
    pub fn new(tenant_id: u64, owner_user_id: u64) -> io::Result<Self> {
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input("maintenance scope IDs must be positive"));
        }
        Ok(Self {
            tenant_id,
            owner_user_id,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MaintenanceSchedulerConfig {
    maximum_scopes: usize,
    idle_interval: Duration,
    worker_stack_bytes: usize,
    history_retained_segments: usize,
    search_admission: SearchAdmission,
}

/// Foreground evidence consulted before a search rebuild.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SearchAdmission {
    /// The worker's long-lived process-wide sampler; its two-second progress
    /// permit bounds each deferral.
    ForegroundPressure,
    /// Specifications declare an idle foreground. Concurrent tests in one
    /// process commit on foreground threads, which the sampler cannot tell
    /// apart from user load.
    #[cfg(test)]
    IdleForeground,
}

impl SearchAdmission {
    fn defer(self, sampler: &mut crate::fsync_stats::ForegroundSampler) -> bool {
        match self {
            Self::ForegroundPressure => crate::fsync_stats::defer_search_projection(sampler),
            #[cfg(test)]
            Self::IdleForeground => false,
        }
    }
}

impl MaintenanceSchedulerConfig {
    pub fn from_resource_budget(resource_budget: DaemonResourceBudget) -> Self {
        debug_assert_eq!(MAX_MAINTENANCE_WORKERS, 1);
        let interval_milliseconds = if resource_budget.used_lean_fallback {
            LEAN_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS
        } else {
            OBSERVED_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS
        };
        let history_retained_segments = if resource_budget.used_lean_fallback {
            LEAN_HISTORY_RETAINED_SEGMENTS
        } else {
            OBSERVED_HISTORY_RETAINED_SEGMENTS
        };
        Self {
            maximum_scopes: resource_budget
                .maximum_connections
                .clamp(1, MAX_MAINTENANCE_SCOPES),
            idle_interval: Duration::from_millis(interval_milliseconds),
            worker_stack_bytes: MAINTENANCE_WORKER_STACK_BYTES,
            history_retained_segments,
            search_admission: SearchAdmission::ForegroundPressure,
        }
    }

    fn validate(self) -> io::Result<Self> {
        if self.maximum_scopes == 0 || self.maximum_scopes > MAX_MAINTENANCE_SCOPES {
            return Err(invalid_input("invalid maintenance scope capacity"));
        }
        if self.idle_interval.is_zero()
            || self.idle_interval
                > Duration::from_millis(LEAN_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS)
        {
            return Err(invalid_input("invalid maintenance idle interval"));
        }
        if self.worker_stack_bytes != MAINTENANCE_WORKER_STACK_BYTES {
            return Err(invalid_input("invalid maintenance worker stack size"));
        }
        if self.history_retained_segments == 0
            || self.history_retained_segments > MAX_HISTORY_RETAINED_SEGMENTS
        {
            return Err(invalid_input("invalid retained history segment bound"));
        }
        Ok(self)
    }

    pub fn maximum_scopes(self) -> usize {
        self.maximum_scopes
    }

    pub fn idle_interval(self) -> Duration {
        self.idle_interval
    }

    pub fn worker_stack_bytes(self) -> usize {
        self.worker_stack_bytes
    }

    pub fn history_retained_segments(self) -> usize {
        self.history_retained_segments
    }

    #[cfg(test)]
    fn for_test(idle_interval: Duration, maximum_scopes: usize) -> Self {
        Self {
            maximum_scopes,
            idle_interval,
            worker_stack_bytes: MAINTENANCE_WORKER_STACK_BYTES,
            history_retained_segments: MAX_HISTORY_RETAINED_SEGMENTS,
            search_admission: SearchAdmission::IdleForeground,
        }
    }

    #[cfg(test)]
    fn for_history_test(
        idle_interval: Duration,
        maximum_scopes: usize,
        history_retained_segments: usize,
    ) -> Self {
        Self {
            maximum_scopes,
            idle_interval,
            worker_stack_bytes: MAINTENANCE_WORKER_STACK_BYTES,
            history_retained_segments,
            search_admission: SearchAdmission::IdleForeground,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MaintenanceMetrics {
    pub rounds: u64,
    pub foreground_deferrals: u64,
    pub no_work_rounds: u64,
    pub conflict_deferrals: u64,
    pub failed_rounds: u64,
    pub committed_transactions: u64,
    pub completed_mounts: u64,
    pub history_compactions: u64,
    pub published_background_packs: u64,
    pub retired_history_segments: u64,
    pub retained_history_segments: u32,
    pub materialized_rows: u64,
    pub materialized_bytes: u64,
    pub current_idle_interval_milliseconds: u64,
    pub search_dirty_conversations: u64,
    pub search_rebuilt_conversations: u64,
    pub search_removed_conversations: u64,
    pub search_acknowledged_tokens: u64,
    pub search_source_pages: u64,
    pub search_indexed_turns: u64,
    pub search_skipped_oversized_turns: u64,
    pub search_source_bytes: u64,
    pub search_foreground_deferrals: u64,
    pub search_failed_rounds: u64,
    pub search_retry_deferrals: u64,
    pub search_maximum_failure_backoff_milliseconds: u64,
    pub search_page_budget_exhaustions: u64,
    /// History the search index retired (`TurnSearchProjection::retire_history_slice`)
    /// and the segments it retained after its latest slice.
    pub search_retired_history_segments: u64,
    pub search_retained_history_segments: u32,
    pub search_history_retirement_failures: u64,
    /// The longest single hold of the index mutex any retirement slice took.
    pub search_history_retirement_max_hold_us: u64,
    /// Search items retracted because their source changed or vanished, and
    /// the logical (owner-budget) bytes of the projection rows they held.
    pub search_retracted_items: u64,
    pub search_retracted_logical_bytes: u64,
    pub tool_result_artifacts_pruned: u64,
    pub tool_result_prune_backlog_rounds: u64,
    pub attempt_events_pruned: u64,
    pub attempt_event_prune_attempts: u64,
    pub attempt_event_prune_backlog_rounds: u64,
    /// Background overlay drain: wake-ups of the fast poll cadence and the
    /// bounded chunks they committed (see `drain_overlay_in_background`).
    pub overlay_background_wakes: u64,
    pub overlay_background_chunks: u64,
    /// Terminal rounds whose record could not be stored because the
    /// scheduler state lock was poisoned; counted lock-free so the dropout
    /// itself stays observable (waiters are still woken, never stranded).
    pub dropped_terminal_records: u64,
    /// Tombstone compaction (L2): rounds that rewrote pages, the pages they
    /// rewrote and the tombstones they reclaimed
    /// (docs/TOFUDB_TOMBSTONE_COMPACTION.md).
    pub tombstone_compaction_rounds: u64,
    pub tombstone_pages_rewritten: u64,
    pub tombstone_entries_reclaimed: u64,
}

/// Scheduled retention horizon for the cold tier (matches the turn
/// tombstone horizon): settled attempt event chunks and conversation sync
/// events older than this are pruned by maintenance rounds through the
/// certified operator operations (`turn.events.prune`, `turn.sync.prune`).
const PRUNE_RETENTION_MS: u64 = 7 * 24 * 60 * 60 * 1_000;
const MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_ROUND: usize = 8_192;
const MAX_SYNC_PRUNE_ROWS_PER_ROUND: usize = 8_192;

/// Background overlay drain cadence: the fast poll keeps the overlay near
/// its soft thresholds under continuous load, where the scope round-robin's
/// try_lock almost always defers to the foreground (2026-09-21 swarm
/// measurement: with only the idle rounds draining, every overlay merge
/// landed inside foreground commits at ~46-58 µs/append amortized).
const OVERLAY_BACKGROUND_POLL_INTERVAL: Duration = Duration::from_millis(25);
/// One wake commits at most this many single bounded chunks, releasing the
/// authority mutex between chunks so a foreground commit never waits more
/// than one ~1-3 ms chunk behind the drain.
const OVERLAY_BACKGROUND_MAX_CHUNKS_PER_WAKE: u32 = 4;

/// Bounded background overlay drain: while the overlay sits above its soft
/// thresholds (or the WAL window under pressure still owes its one pass),
/// commit single bounded chunks, releasing the authority mutex between
/// chunks. Returns the chunks committed (0 when nothing asks for a drain).
/// With the driver alive the commit path merges nothing, so a burst this
/// drain cannot keep up with leaves a larger overlay (memory and replay, not
/// a stalled commit) until it catches up. Drain captures keep the same
/// prefix/version rules as foreground merges; physical page preparation runs
/// outside the authority mutex.
fn drain_overlay_in_background(
    database: &Arc<Mutex<AuthorityDatabase>>,
    trigger: &crate::entity_overlay::EntityOverlaySoftTrigger,
) -> u64 {
    if !trigger.needs_drain() {
        return 0;
    }
    let mut chunks = 0_u64;
    while chunks < u64::from(OVERLAY_BACKGROUND_MAX_CHUNKS_PER_WAKE)
        && !trigger.below_soft_thresholds()
    {
        let outcome = merge_overlay_outside_authority(database);
        match outcome {
            Ok(true) => chunks += 1,
            Ok(false) => break,
            // Rotation backpressure owns the WAL lane briefly; the next
            // poll retries. Anything else is a real merge failure.
            Err(error)
                if error.kind() == io::ErrorKind::ResourceBusy
                    || error.kind() == io::ErrorKind::WouldBlock =>
            {
                break
            }
            Err(error) => {
                crate::engine_log::warn(
                    "overlay_background_drain",
                    &format!("background overlay merge slice failed: {error}"),
                );
                break;
            }
        }
    }
    chunks
}

/// Capture and publication are short authority holds. The potentially cold
/// page traversal/encoding runs between them with only an immutable pin.
fn merge_overlay_outside_authority(database: &Arc<Mutex<AuthorityDatabase>>) -> io::Result<bool> {
    let prepared = {
        let guard = database
            .lock()
            .map_err(|_| io::Error::other("maintenance authority mutex is poisoned"))?;
        let _hold = crate::authority_watch::AuthorityHoldTimer::start("overlay.capture");
        guard.prepare_entity_overlay_merge()?
    };
    let Some(prepared) = prepared else {
        return Ok(false);
    };
    let encoded = prepared.encode()?;
    let mut guard = database
        .lock()
        .map_err(|_| io::Error::other("maintenance authority mutex is poisoned"))?;
    let _hold = crate::authority_watch::AuthorityHoldTimer::start("overlay.publish");
    guard.finish_entity_overlay_merge(encoded)
}

#[derive(Clone, Copy, Debug, Default)]
struct ScheduledPruneOutcome {
    events: usize,
    attempts: usize,
    events_has_more: bool,
    sync_rows: usize,
    sync_has_more: bool,
}

fn parse_prune_outcome(bytes: &[u8]) -> (usize, usize, bool) {
    let value = serde_json::from_slice::<serde_json::Value>(bytes).ok();
    let events = value
        .as_ref()
        .and_then(|v| v.get("deleted_rows")?.as_u64())
        .unwrap_or(0) as usize;
    let attempts = value
        .as_ref()
        .and_then(|v| v.get("deleted_attempts")?.as_u64())
        .unwrap_or(0) as usize;
    let remaining = value
        .as_ref()
        .and_then(|v| v.get("remaining")?.as_u64())
        .map(|v| v > 0)
        .unwrap_or(false);
    (events, attempts, remaining)
}

#[derive(Clone, Debug)]
struct ErrorSnapshot {
    kind: io::ErrorKind,
    message: String,
}

impl ErrorSnapshot {
    fn capture(error: &io::Error) -> Self {
        Self {
            kind: error.kind(),
            message: error.to_string(),
        }
    }

    fn into_error(self) -> io::Error {
        io::Error::new(self.kind, self.message)
    }
}

struct SchedulerState {
    stopping: bool,
    next_scope: usize,
    metrics: MaintenanceMetrics,
    next_idle_interval: Duration,
    terminal_error: Option<ErrorSnapshot>,
    /// Scope-round timer, re-armed from the idle backoff after every round.
    round_deadline: Instant,
    round_completed_at: Instant,
    /// One bit per admitted scope whose committed search invalidation has not
    /// started a round yet.
    search_wakes: u64,
}

const _: () = assert!(MAX_MAINTENANCE_SCOPES <= u64::BITS as usize);

impl SchedulerState {
    /// A committed search invalidation ends idle backoff, but a woken round
    /// still waits one base interval after the previous round: the cadence
    /// that continuous maintenance work already uses.
    fn round_due(&self, base_interval: Duration) -> Instant {
        if self.search_wakes == 0 {
            self.round_deadline
        } else {
            self.round_deadline
                .min(self.round_completed_at + base_interval)
        }
    }

    /// Woken scopes go first, in round-robin order; selection consumes the wake.
    fn take_round_scope(&mut self, scopes: usize) -> usize {
        let index = (0..scopes)
            .map(|offset| (self.next_scope + offset) % scopes)
            .find(|index| self.search_wakes & (1 << index) != 0)
            .unwrap_or(self.next_scope);
        self.search_wakes &= !(1 << index);
        self.next_scope = (index + 1) % scopes;
        index
    }
}

struct Shared {
    stop: Arc<std::sync::atomic::AtomicBool>,
    scopes: Vec<MaintenanceScope>,
    config: MaintenanceSchedulerConfig,
    state: Mutex<SchedulerState>,
    wake: Condvar,
    /// Lock-free overlay probe + soft thresholds, resolved best-effort at
    /// startup and lazily by the drain driver when the authority mutex was
    /// contended — setup never blocks on that mutex (a foreground holder
    /// starting the scheduler must not deadlock). The background drain
    /// runs on its own fast cadence, independent of the scope round-robin.
    overlay_trigger: std::sync::OnceLock<crate::entity_overlay::EntityOverlaySoftTrigger>,
    /// Whether the drain driver should look for retirable history: armed at
    /// start and by every scope round, kept while slices retire, cleared
    /// once nothing is retirable. A slice consumes the arm before it starts,
    /// so an arm a concurrent round sets is never lost.
    history_backlog: std::sync::atomic::AtomicBool,
    /// Lock-free count of terminal records dropped to a poisoned state lock;
    /// folded into the metrics snapshot on read.
    dropped_terminal_records: std::sync::atomic::AtomicU64,
}

#[derive(Clone)]
pub struct MaintenanceObserver {
    shared: Arc<Shared>,
}

impl MaintenanceObserver {
    pub fn metrics(&self) -> io::Result<MaintenanceMetrics> {
        self.shared
            .state
            .lock()
            .map(|state| {
                let mut metrics = state.metrics;
                metrics.dropped_terminal_records = self
                    .shared
                    .dropped_terminal_records
                    .load(std::sync::atomic::Ordering::Relaxed);
                metrics
            })
            .map_err(|_| io::Error::other("maintenance scheduler state is poisoned"))
    }

    pub fn require_healthy(&self) -> io::Result<()> {
        let state = self
            .shared
            .state
            .lock()
            .map_err(|_| io::Error::other("maintenance scheduler state is poisoned"))?;
        state
            .terminal_error
            .clone()
            .map_or(Ok(()), |error| Err(error.into_error()))
    }
}

pub struct MaintenanceScheduler {
    shared: Arc<Shared>,
    /// The scope worker and the overlay drain driver.
    workers: Mutex<Vec<JoinHandle<()>>>,
}

impl MaintenanceScheduler {
    pub fn start(
        database: Arc<Mutex<AuthorityDatabase>>,
        scopes: Vec<MaintenanceScope>,
        config: MaintenanceSchedulerConfig,
    ) -> io::Result<Self> {
        Self::start_with_search(database, None, scopes, config)
    }

    pub fn start_with_search(
        database: Arc<Mutex<AuthorityDatabase>>,
        search_projection: Option<Arc<Mutex<TurnSearchProjection>>>,
        scopes: Vec<MaintenanceScope>,
        config: MaintenanceSchedulerConfig,
    ) -> io::Result<Self> {
        let config = config.validate()?;
        let scopes = scopes.into_iter().collect::<BTreeSet<_>>();
        if scopes.is_empty() || scopes.len() > config.maximum_scopes {
            return Err(invalid_input("maintenance scopes exceed admission bounds"));
        }
        let started = Instant::now();
        let shared = Arc::new(Shared {
            stop: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            scopes: scopes.into_iter().collect(),
            config,
            state: Mutex::new(SchedulerState {
                stopping: false,
                next_scope: 0,
                metrics: MaintenanceMetrics {
                    current_idle_interval_milliseconds: duration_milliseconds(config.idle_interval),
                    ..MaintenanceMetrics::default()
                },
                next_idle_interval: config.idle_interval,
                terminal_error: None,
                // Deferred search activation starts on this already-owned
                // worker, after readiness, without waiting for an unrelated
                // retention idle interval.
                round_deadline: if search_projection.is_some() {
                    started
                } else {
                    started + config.idle_interval
                },
                round_completed_at: started,
                search_wakes: 0,
            }),
            wake: Condvar::new(),
            overlay_trigger: std::sync::OnceLock::new(),
            // A restart must not wait for the first scope round, whose search
            // pass can take minutes, before it retires anything.
            history_backlog: std::sync::atomic::AtomicBool::new(true),
            dropped_terminal_records: std::sync::atomic::AtomicU64::new(0),
        });
        // Best-effort probe snapshot: an uncontended startup resolves it
        // here; under contention the worker resolves it on a later poll.
        if let Ok(guard) = database.try_lock() {
            let _ = shared
                .overlay_trigger
                .set(guard.entity_overlay_soft_trigger());
        }
        if let Some(projection) = &search_projection {
            // Scopes are already sorted and unique, so each sequence index is
            // the scope index. The worker attaches it to the authority.
            let scheduler = Arc::downgrade(&shared);
            let invalidations = Arc::new(crate::search_dirty::SearchInvalidations::new(
                shared
                    .scopes
                    .iter()
                    .map(|scope| (scope.tenant_id, scope.owner_user_id)),
                move |index| wake_search_scope(&scheduler, index),
            ));
            // Install only cancellation and coverage metadata, retaining poison
            // as the worker's retryable index failure. Optional search health
            // must not prevent source maintenance or service startup.
            let mut projection = projection
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            projection.install_stop(Arc::clone(&shared.stop));
            projection.install_search_invalidations(invalidations);
        }
        let drain_shared = Arc::clone(&shared);
        let drain_database = Arc::clone(&database);
        let drain_search_projection = search_projection.clone();
        let drain = thread::Builder::new()
            .name("tofu-db-overlay-drain".to_owned())
            .stack_size(config.worker_stack_bytes)
            .spawn(move || {
                run_overlay_drain(
                    &drain_database,
                    drain_search_projection.as_ref(),
                    &drain_shared,
                )
            })?;
        let worker_shared = Arc::clone(&shared);
        let worker = match thread::Builder::new()
            .name("tofu-db-maintenance".to_owned())
            .stack_size(config.worker_stack_bytes)
            .spawn(move || run_worker(&database, search_projection.as_ref(), &worker_shared))
        {
            Ok(worker) => worker,
            Err(error) => {
                request_stop(&shared);
                let _ = drain.join();
                return Err(error);
            }
        };
        Ok(Self {
            shared,
            workers: Mutex::new(vec![worker, drain]),
        })
    }

    pub fn observer(&self) -> MaintenanceObserver {
        MaintenanceObserver {
            shared: Arc::clone(&self.shared),
        }
    }

    pub fn stop_and_join(&self) -> io::Result<MaintenanceMetrics> {
        if !request_stop(&self.shared) {
            return Err(io::Error::other("maintenance scheduler state is poisoned"));
        }
        let workers = std::mem::take(
            &mut *self
                .workers
                .lock()
                .map_err(|_| io::Error::other("maintenance scheduler worker is poisoned"))?,
        );
        // Join every thread before reporting, so none outlives the call.
        let mut panicked = false;
        for worker in workers {
            panicked |= worker.join().is_err();
        }
        if panicked {
            return Err(io::Error::other("maintenance scheduler worker panicked"));
        }
        self.observer().require_healthy()?;
        self.observer().metrics()
    }
}

impl Drop for MaintenanceScheduler {
    fn drop(&mut self) {
        request_stop(&self.shared);
        if let Ok(mut workers) = self.workers.lock() {
            for worker in workers.drain(..) {
                let _ = worker.join();
            }
        }
    }
}

/// Asks both threads to stop and wakes them; false when the state lock was
/// poisoned (the stop flag and the wake are still delivered).
fn request_stop(shared: &Shared) -> bool {
    shared
        .stop
        .store(true, std::sync::atomic::Ordering::Release);
    let stopping = shared
        .state
        .lock()
        .map(|mut state| state.stopping = true)
        .is_ok();
    shared.wake.notify_all();
    stopping
}

/// Commit-side doorbell; runs under the committing caller's authority hold,
/// which the worker never waits for while holding the scheduler state.
fn wake_search_scope(shared: &std::sync::Weak<Shared>, index: usize) {
    let Some(shared) = shared.upgrade() else {
        return;
    };
    if let Ok(mut state) = shared.state.lock() {
        state.search_wakes |= 1 << index;
    }
    shared.wake.notify_all();
}

/// Per-scope failure feedback for the reconstructible search projection only.
/// The existing launch-derived idle interval and maintenance ceiling bound
/// retries; foreground writes, retention, and overlay drain never wait here.
#[derive(Clone, Copy, Debug, Default)]
struct SearchRetryState {
    retry_at: Option<Instant>,
    delay: Duration,
    consecutive_failures: u32,
}

impl SearchRetryState {
    fn ready(&self, now: Instant) -> bool {
        self.retry_at.is_none_or(|retry_at| now >= retry_at)
    }

    fn failed(&mut self, now: Instant, idle_interval: Duration) -> Duration {
        self.delay = if self.consecutive_failures == 0 {
            idle_interval
        } else {
            increased_idle_interval(self.delay)
        };
        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
        self.retry_at = Some(now + self.delay);
        self.delay
    }

    fn recovered(&mut self) {
        *self = Self::default();
    }
}

fn maximum_search_failure_backoff(states: &[SearchRetryState]) -> u64 {
    states
        .iter()
        .map(|state| duration_milliseconds(state.delay))
        .max()
        .unwrap_or(0)
}

/// Block stores whose published catalog location table the overlay poll has
/// derived for a newly installed catalog, off the owning mutex
/// (docs/TOFUDB_BLOCK_READ_PATH.md, "Published catalog location table"): the
/// authority's and, once opened, the search index's. Weak, so the worker never
/// extends a store past its engine; a dropped or reopened store is resolved
/// again with one non-blocking attempt per poll.
#[derive(Default)]
struct CatalogLocationStores {
    authority: std::sync::Weak<crate::block::BlockStore>,
    search: std::sync::Weak<crate::block::BlockStore>,
    /// The derivation in flight, on its own thread so overlay drain polls
    /// never wait for it (seconds for the live 229M-block table). Joined
    /// before the next one starts and when the worker exits.
    derivation: Option<std::thread::JoinHandle<()>>,
}

impl Drop for CatalogLocationStores {
    fn drop(&mut self) {
        self.join_derivation();
    }
}

impl CatalogLocationStores {
    fn advance(
        &mut self,
        database: &Mutex<AuthorityDatabase>,
        search_projection: Option<&Arc<Mutex<TurnSearchProjection>>>,
    ) {
        if self.authority.strong_count() == 0 {
            if let Ok(guard) = database.try_lock() {
                self.authority = Arc::downgrade(&guard.block_store_shared());
            }
        }
        if let Some(projection) = search_projection {
            if self.search.strong_count() == 0 {
                if let Some(blocks) = projection
                    .try_lock()
                    .ok()
                    .and_then(|guard| guard.block_store_shared())
                {
                    self.search = Arc::downgrade(&blocks);
                }
            }
        }
        if self
            .derivation
            .as_ref()
            .is_some_and(|derivation| !derivation.is_finished())
        {
            return;
        }
        self.join_derivation();
        let due: Vec<_> = [self.authority.upgrade(), self.search.upgrade()]
            .into_iter()
            .flatten()
            .filter(|blocks| blocks.catalog_location_derivation_due())
            .collect();
        if due.is_empty() {
            return;
        }
        // Reads keep the exact catalog path until a derivation succeeds; a
        // refused spawn or a failed derivation is retried on a later poll.
        self.derivation = std::thread::Builder::new()
            .name("tofu-db-locations".to_owned())
            .spawn(move || {
                crate::fsync_stats::set_current_component(
                    crate::fsync_stats::COMPONENT_MAINTENANCE,
                );
                for blocks in due {
                    let _ = blocks.advance_catalog_locations();
                }
            })
            .ok();
    }

    /// Joins a finished or running derivation; its panic resumes on this
    /// worker, as it did when the worker derived inline.
    fn join_derivation(&mut self) {
        if let Some(derivation) = self.derivation.take() {
            if let Err(panic) = derivation.join() {
                if !std::thread::panicking() {
                    std::panic::resume_unwind(panic);
                }
            }
        }
    }
}

/// Marks the drain driver alive from its first trigger until it exits, by any
/// path (stop, poisoned state, terminal round or panic), so commits resume
/// their inline backstop as soon as no driver merges for them.
struct DrainDriverLiveness<'a>(Option<&'a crate::entity_overlay::EntityOverlaySoftTrigger>);

impl<'a> DrainDriverLiveness<'a> {
    fn hold(&mut self, trigger: &'a crate::entity_overlay::EntityOverlaySoftTrigger) {
        if self.0.is_none() {
            trigger.signal().set_driver_alive(true);
            self.0 = Some(trigger);
        }
    }
}

impl Drop for DrainDriverLiveness<'_> {
    fn drop(&mut self) {
        if let Some(trigger) = self.0 {
            trigger.signal().set_driver_alive(false);
        }
    }
}

/// The `tofu-db-overlay-drain` driver: the fast poll cadence of the
/// background overlay drain, the catalog location derivations and the
/// history retirement slices. It runs beside the scope worker because a
/// scope round (search rebuild, backfill) takes minutes on a loaded volume:
/// on one shared thread the drain went unpolled for whole rounds and the
/// commit-path backstop merged cold pages inside foreground authority holds
/// instead (2026-11-11: 30 drain wakes in 47 minutes, while 1,606 merges
/// landed in commits in ten of them).
fn run_overlay_drain(
    database: &Arc<Mutex<AuthorityDatabase>>,
    search_projection: Option<&Arc<Mutex<TurnSearchProjection>>>,
    shared: &Shared,
) {
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_MAINTENANCE);
    let mut next_overlay_poll = Instant::now();
    let mut catalog_location_stores = CatalogLocationStores::default();
    let mut next_history_slice = Instant::now();
    let mut liveness = DrainDriverLiveness(None);
    loop {
        {
            let mut state = match shared.state.lock() {
                Ok(state) => state,
                Err(_) => return,
            };
            loop {
                if state.stopping {
                    return;
                }
                let remaining = next_overlay_poll.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    break;
                }
                state = match shared.wake.wait_timeout(state, remaining) {
                    Ok((state, _)) => state,
                    Err(_) => return,
                };
            }
        }
        next_overlay_poll = Instant::now() + OVERLAY_BACKGROUND_POLL_INTERVAL;
        let trigger = match shared.overlay_trigger.get() {
            Some(trigger) => Some(trigger),
            None => {
                // Lazy resolution after a contended startup: one
                // non-blocking attempt per poll, never queued behind
                // foreground commits.
                match database.try_lock() {
                    Ok(guard) => {
                        let built = guard.entity_overlay_soft_trigger();
                        drop(guard);
                        let _ = shared.overlay_trigger.set(built);
                        shared.overlay_trigger.get()
                    }
                    Err(_) => None,
                }
            }
        };
        if let Some(trigger) = trigger {
            liveness.hold(trigger);
            let chunks = drain_overlay_in_background(database, trigger);
            update_metrics(
                shared,
                |metrics| {
                    metrics.overlay_background_wakes =
                        metrics.overlay_background_wakes.saturating_add(1);
                    metrics.overlay_background_chunks =
                        metrics.overlay_background_chunks.saturating_add(chunks);
                },
                Backoff::Keep,
            );
        }
        catalog_location_stores.advance(database, search_projection);
        // History retires on this fast cadence too, not only in scope
        // rounds, whose search and backfill work takes minutes on a
        // loaded volume while the WAL keeps adding segments. A slice
        // waits for the authority like the background drain, and the
        // next one starts no sooner than this one took (50% duty).
        if Instant::now() >= next_history_slice
            && shared
                .history_backlog
                .swap(false, std::sync::atomic::Ordering::AcqRel)
        {
            let started = Instant::now();
            let outcome = match database.lock() {
                Ok(guard) => retire_history_slice(
                    database,
                    guard,
                    crate::authority_watch::AuthorityHoldTimer::probe("maintenance.round"),
                    shared.config.history_retained_segments,
                )
                .ok(),
                Err(_) => Some(RoundOutcome::Terminal(io::Error::other(
                    "maintenance authority mutex is poisoned",
                ))),
            };
            let backlog = match &outcome {
                Some(RoundOutcome::HistoryCompacted {
                    retained_segments, ..
                }) => *retained_segments as usize > shared.config.history_retained_segments,
                // Another publication owned the lane; retry after the pause.
                Some(RoundOutcome::Conflict) => true,
                _ => false,
            };
            if backlog {
                shared
                    .history_backlog
                    .store(true, std::sync::atomic::Ordering::Release);
            }
            match outcome {
                Some(RoundOutcome::HistoryCompacted {
                    retired_segments,
                    retained_segments,
                }) => record_history_compaction(
                    shared,
                    retired_segments,
                    retained_segments,
                    Backoff::Keep,
                ),
                Some(RoundOutcome::Conflict) => update_metrics(
                    shared,
                    |metrics| {
                        metrics.conflict_deferrals = metrics.conflict_deferrals.saturating_add(1);
                    },
                    Backoff::Keep,
                ),
                Some(RoundOutcome::Terminal(error)) => {
                    record_terminal_round(shared, error);
                    return;
                }
                _ => {}
            }
            next_history_slice = Instant::now() + started.elapsed();
        }
    }
}

fn run_worker(
    database: &Arc<Mutex<AuthorityDatabase>>,
    search_projection: Option<&Arc<Mutex<TurnSearchProjection>>>,
    shared: &Shared,
) {
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_MAINTENANCE);
    // The scope-round deadline is recomputed from the (adaptive) idle
    // interval after each round; the drain driver's polls never reset it.
    let mut search_pressure = crate::fsync_stats::foreground_hotness_sampler();
    // Exactly one bounded entry per admitted scope; dropped with this worker.
    let mut search_retries = vec![SearchRetryState::default(); shared.scopes.len()];
    // A round deferred by a busy foreground waits for the authority next time.
    let mut wait_for_authority = false;
    loop {
        let (scope_index, scope) = {
            let mut state = match shared.state.lock() {
                Ok(state) => state,
                Err(_) => return,
            };
            loop {
                if state.stopping {
                    return;
                }
                let now = Instant::now();
                let round_due = state.round_due(shared.config.idle_interval);
                if now >= round_due {
                    let index = state.take_round_scope(shared.scopes.len());
                    state.metrics.rounds = state.metrics.rounds.saturating_add(1);
                    break (index, shared.scopes[index]);
                }
                state = match shared
                    .wake
                    .wait_timeout(state, round_due.saturating_duration_since(now))
                {
                    Ok((state, _)) => state,
                    Err(_) => return,
                };
            }
        };
        let mut search_did_work = false;
        if let Some(search_projection) = search_projection {
            if !search_retries[scope_index].ready(Instant::now()) {
                update_metrics(
                    shared,
                    |metrics| {
                        metrics.search_retry_deferrals =
                            metrics.search_retry_deferrals.saturating_add(1);
                    },
                    Backoff::Keep,
                );
            } else {
                match crate::turn_search_worker::process_turn_search_batch_with_admission_and_stop(
                    database,
                    search_projection,
                    scope,
                    || shared.config.search_admission.defer(&mut search_pressure),
                    || {
                        shared
                            .state
                            .lock()
                            .map(|state| state.stopping)
                            .unwrap_or(true)
                    },
                ) {
                    Ok(search) => {
                        search_retries[scope_index].recovered();
                        let maximum_backoff = maximum_search_failure_backoff(&search_retries);
                        search_did_work = search.dirty_conversations != 0;
                        update_metrics(
                            shared,
                            |metrics| {
                                metrics.search_maximum_failure_backoff_milliseconds =
                                    maximum_backoff;
                                metrics.search_dirty_conversations = metrics
                                    .search_dirty_conversations
                                    .saturating_add(search.dirty_conversations);
                                metrics.search_rebuilt_conversations = metrics
                                    .search_rebuilt_conversations
                                    .saturating_add(search.rebuilt_conversations);
                                metrics.search_removed_conversations = metrics
                                    .search_removed_conversations
                                    .saturating_add(search.removed_conversations);
                                metrics.search_acknowledged_tokens = metrics
                                    .search_acknowledged_tokens
                                    .saturating_add(search.acknowledged_tokens);
                                metrics.search_source_pages = metrics
                                    .search_source_pages
                                    .saturating_add(search.source_pages);
                                metrics.search_indexed_turns = metrics
                                    .search_indexed_turns
                                    .saturating_add(search.indexed_turns);
                                metrics.search_skipped_oversized_turns = metrics
                                    .search_skipped_oversized_turns
                                    .saturating_add(search.skipped_oversized_turns);
                                metrics.search_source_bytes = metrics
                                    .search_source_bytes
                                    .saturating_add(search.source_bytes);
                                metrics.search_foreground_deferrals = metrics
                                    .search_foreground_deferrals
                                    .saturating_add(search.foreground_deferrals);
                                metrics.search_page_budget_exhaustions = metrics
                                    .search_page_budget_exhaustions
                                    .saturating_add(search.page_budget_exhaustions);
                                metrics.search_retracted_items = metrics
                                    .search_retracted_items
                                    .saturating_add(search.retracted_items);
                                metrics.search_retracted_logical_bytes = metrics
                                    .search_retracted_logical_bytes
                                    .saturating_add(search.retracted_logical_bytes);
                            },
                            Backoff::Keep,
                        )
                    }
                    Err(_) if shared.stop.load(std::sync::atomic::Ordering::Acquire) => return,
                    Err(error) => {
                        let delay = search_retries[scope_index]
                            .failed(Instant::now(), shared.config.idle_interval);
                        let failures = search_retries[scope_index].consecutive_failures;
                        if failures.is_power_of_two() {
                            crate::engine_log::warn("search_retry", &format!(
                                "failure_kind={:?} consecutive_failures={} retry_after_ms={} error={error}",
                                error.kind(), failures, duration_milliseconds(delay)));
                        }
                        let maximum_backoff = maximum_search_failure_backoff(&search_retries);
                        update_metrics(
                            shared,
                            |metrics| {
                                metrics.search_failed_rounds =
                                    metrics.search_failed_rounds.saturating_add(1);
                                metrics.search_maximum_failure_backoff_milliseconds =
                                    maximum_backoff;
                            },
                            Backoff::Keep,
                        );
                    }
                }
            }
        }
        if let Some(search_projection) = search_projection {
            retire_search_history(shared, search_projection);
        }
        if shared
            .state
            .lock()
            .map(|state| state.stopping)
            .unwrap_or(true)
        {
            return;
        }
        let recovery_did_work =
            match crate::project_brain::recovery::backfill(database, scope, &|| {
                shared.stop.load(std::sync::atomic::Ordering::Acquire)
            }) {
                Ok(committed) => {
                    if committed {
                        update_metrics(
                            shared,
                            |metrics| {
                                metrics.committed_transactions =
                                    metrics.committed_transactions.saturating_add(1);
                            },
                            Backoff::Keep,
                        );
                    }
                    committed
                }
                Err(error) => {
                    crate::engine_log::warn(
                        "project_recovery_backfill",
                        &format!("summary backfill deferred: {error}"),
                    );
                    false
                }
            };
        if shared.stop.load(std::sync::atomic::Ordering::Acquire) {
            return;
        }
        let outcome = run_round(
            database,
            scope,
            shared.config.history_retained_segments,
            wait_for_authority,
        );
        wait_for_authority = matches!(outcome, RoundOutcome::ForegroundBusy);
        shared
            .history_backlog
            .store(true, std::sync::atomic::Ordering::Release);
        match outcome {
            RoundOutcome::ForegroundBusy => update_metrics(
                shared,
                |metrics| {
                    metrics.foreground_deferrals = metrics.foreground_deferrals.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::OverlayMerged => update_metrics(
                shared,
                |metrics| {
                    metrics.overlay_background_chunks =
                        metrics.overlay_background_chunks.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::NoWork => update_metrics(
                shared,
                |metrics| {
                    metrics.no_work_rounds = metrics.no_work_rounds.saturating_add(1);
                },
                if search_did_work || recovery_did_work {
                    Backoff::Reset
                } else {
                    Backoff::Increase
                },
            ),
            RoundOutcome::Conflict => update_metrics(
                shared,
                |metrics| {
                    metrics.conflict_deferrals = metrics.conflict_deferrals.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::Committed {
                rows,
                bytes,
                mount_completed,
                tool_result_artifacts_pruned,
                tool_result_prune_has_more,
                attempt_events_pruned,
                attempt_event_prune_attempts,
                attempt_event_prune_has_more,
                deferred,
            } => update_metrics(
                shared,
                |metrics| {
                    metrics.committed_transactions =
                        metrics.committed_transactions.saturating_add(1);
                    if deferred {
                        metrics.conflict_deferrals = metrics.conflict_deferrals.saturating_add(1);
                    }
                    metrics.materialized_rows =
                        metrics.materialized_rows.saturating_add(rows as u64);
                    metrics.materialized_bytes = metrics.materialized_bytes.saturating_add(bytes);
                    if mount_completed {
                        metrics.completed_mounts = metrics.completed_mounts.saturating_add(1);
                    }
                    metrics.tool_result_artifacts_pruned = metrics
                        .tool_result_artifacts_pruned
                        .saturating_add(tool_result_artifacts_pruned as u64);
                    if tool_result_prune_has_more {
                        metrics.tool_result_prune_backlog_rounds =
                            metrics.tool_result_prune_backlog_rounds.saturating_add(1);
                    }
                    metrics.attempt_events_pruned = metrics
                        .attempt_events_pruned
                        .saturating_add(attempt_events_pruned as u64);
                    metrics.attempt_event_prune_attempts = metrics
                        .attempt_event_prune_attempts
                        .saturating_add(attempt_event_prune_attempts as u64);
                    if attempt_event_prune_has_more {
                        metrics.attempt_event_prune_backlog_rounds =
                            metrics.attempt_event_prune_backlog_rounds.saturating_add(1);
                    }
                },
                Backoff::Reset,
            ),
            RoundOutcome::TombstoneCompacted { pages, entries } => update_metrics(
                shared,
                |metrics| {
                    metrics.tombstone_compaction_rounds =
                        metrics.tombstone_compaction_rounds.saturating_add(1);
                    metrics.tombstone_pages_rewritten = metrics
                        .tombstone_pages_rewritten
                        .saturating_add(pages as u64);
                    metrics.tombstone_entries_reclaimed = metrics
                        .tombstone_entries_reclaimed
                        .saturating_add(entries as u64);
                    metrics.committed_transactions =
                        metrics.committed_transactions.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::HistoryCompacted {
                retired_segments,
                retained_segments,
            } => record_history_compaction(
                shared,
                retired_segments,
                retained_segments,
                Backoff::Reset,
            ),
            RoundOutcome::PayloadPackPublished => update_metrics(
                shared,
                |metrics| {
                    metrics.published_background_packs =
                        metrics.published_background_packs.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::Terminal(error) => {
                record_terminal_round(shared, error);
                return;
            }
        }
        // Re-arm the scope-round deadline from the interval the round's
        // backoff updates just selected; overlay polls never touch it.
        match shared.state.lock() {
            Ok(mut state) => {
                state.round_completed_at = Instant::now();
                state.round_deadline = state.round_completed_at + state.next_idle_interval;
            }
            Err(_) => return,
        }
        // Specifications wait on completed rounds instead of sleeping.
        #[cfg(test)]
        shared.wake.notify_all();
    }
}

enum RoundOutcome {
    ForegroundBusy,
    NoWork,
    PayloadPackPublished,
    OverlayMerged,
    Conflict,
    Committed {
        rows: u32,
        bytes: u64,
        mount_completed: bool,
        tool_result_artifacts_pruned: usize,
        tool_result_prune_has_more: bool,
        attempt_events_pruned: usize,
        attempt_event_prune_attempts: usize,
        attempt_event_prune_has_more: bool,
        deferred: bool,
    },
    TombstoneCompacted {
        pages: u32,
        entries: u32,
    },
    HistoryCompacted {
        retired_segments: u32,
        retained_segments: u32,
    },
    Terminal(io::Error),
}

// Tombstone compaction (L2, docs/TOFUDB_TOMBSTONE_COMPACTION.md): one hint
// per round, a bounded leaf budget per slice and a bounded slice count per
// round, so catch-up never holds the authority past a user-visible window.
const TOMBSTONE_COMPACTION_LEAVES_PER_SLICE: usize = 32;
const TOMBSTONE_COMPACTION_SLICES_PER_ROUND: usize = 4;

fn run_tombstone_compaction(
    database: &mut AuthorityDatabase,
    scope: MaintenanceScope,
) -> io::Result<Option<(u32, u32)>> {
    // Feature-gated with L1 (`TOFU_DB_TOMBSTONE_RECLAIM`): when reclamation
    // is off the round is a no-op and hints simply age out.
    if !database.entity_tombstone_reclaim_is_enabled() {
        return Ok(None);
    }
    let Some((start, end, dead_leaves)) = database.take_entity_tombstone_compaction_hint()? else {
        return Ok(None);
    };
    // Hints are entity-key encoded; a scope only vacuums its own owner.
    let owner_prefix = [
        scope.tenant_id.to_be_bytes(),
        scope.owner_user_id.to_be_bytes(),
    ]
    .concat();
    if !start.starts_with(&owner_prefix) {
        // Requeue for the owning scope (or the next scope round-robin):
        // hints are advisory and coalescing, never lost.
        database.record_entity_tombstone_compaction_hint(&start, &end, dead_leaves)?;
        return Ok(None);
    }
    let mut pages = 0_u32;
    let mut entries = 0_u32;
    let mut window = (start, end);
    let mut remaining = true;
    for _ in 0..TOMBSTONE_COMPACTION_SLICES_PER_ROUND {
        let progress = database.reclaim_entity_tombstones_in_range(
            &window.0,
            &window.1,
            TOMBSTONE_COMPACTION_LEAVES_PER_SLICE,
        )?;
        pages = pages.saturating_add(progress.pages_rewritten);
        entries = entries.saturating_add(progress.entries_reclaimed);
        match progress.resume {
            Some(next) => window = next,
            None => {
                remaining = false;
                break;
            }
        }
    }
    if remaining {
        // Partial sweep: hand the remaining window back so a later round
        // resumes instead of restarting.
        database.record_entity_tombstone_compaction_hint(&window.0, &window.1, dead_leaves)?;
    }
    Ok(Some((pages, entries)))
}

/// One automatic history retirement slice, entered holding the authority.
/// Hands the hold back untouched when nothing is retirable, so a scope round
/// continues with its other work under the same hold.
fn retire_history_slice<'a>(
    database_arc: &'a Arc<Mutex<AuthorityDatabase>>,
    mut database: MutexGuard<'a, AuthorityDatabase>,
    round_hold: crate::authority_watch::AuthorityHoldTimer<'static>,
    history_retained_segments: usize,
) -> Result<
    RoundOutcome,
    (
        MutexGuard<'a, AuthorityDatabase>,
        crate::authority_watch::AuthorityHoldTimer<'static>,
    ),
> {
    match database.try_prepare_history_compaction(history_retained_segments) {
        Ok(Some(prepared)) => {
            drop(round_hold);
            drop(database);
            let outcome = crate::engine::encode_history_compaction(&prepared);
            // The reserved physical lane must complete even when foreground
            // traffic remains hot. Only metadata publication needs this hold.
            let mut database = match database_arc.lock() {
                Ok(database) => database,
                Err(_) => {
                    return Ok(RoundOutcome::Terminal(io::Error::other(
                        "maintenance authority mutex is poisoned",
                    )))
                }
            };
            let _hold = crate::authority_watch::AuthorityHoldTimer::probe("history.install");
            Ok(
                match database.finish_history_compaction(prepared, outcome) {
                    Ok(metrics) => RoundOutcome::HistoryCompacted {
                        retired_segments: metrics.retired_segments,
                        retained_segments: metrics.retained_segments,
                    },
                    Err(error) => round_engine_error(error),
                },
            )
        }
        Ok(None) => Err((database, round_hold)),
        Err(error) => Ok(round_engine_error(error)),
    }
}

fn run_round(
    database: &Arc<Mutex<AuthorityDatabase>>,
    scope: MaintenanceScope,
    history_retained_segments: usize,
    wait_for_authority: bool,
) -> RoundOutcome {
    let database_arc = database;
    // Only the first attempt yields to a busy foreground. Under a mutex that
    // is almost always held a fresh try_lock almost never wins, so retrying
    // with it starved every lane below (2026-11-11: 660 of 671 rounds).
    let acquired = if wait_for_authority {
        database.lock().ok()
    } else {
        match database.try_lock() {
            Ok(database) => Some(database),
            Err(TryLockError::WouldBlock) => return RoundOutcome::ForegroundBusy,
            Err(TryLockError::Poisoned(_)) => None,
        }
    };
    let Some(database) = acquired else {
        return RoundOutcome::Terminal(io::Error::other("maintenance authority mutex is poisoned"));
    };
    let round_hold = crate::authority_watch::AuthorityHoldTimer::probe("maintenance.round");
    let (mut database, round_hold) = match retire_history_slice(
        database_arc,
        database,
        round_hold,
        history_retained_segments,
    ) {
        Ok(outcome) => return outcome,
        Err(held) => held,
    };
    match database.prepare_entity_overlay_merge() {
        Ok(Some(prepared)) => {
            drop(round_hold);
            drop(database);
            let encoded = match prepared.encode() {
                Ok(encoded) => encoded,
                Err(error) => return round_engine_error(error),
            };
            let mut guard = match database_arc.lock() {
                Ok(guard) => guard,
                Err(_) => {
                    return RoundOutcome::Terminal(io::Error::other(
                        "maintenance authority mutex is poisoned",
                    ))
                }
            };
            let _hold = crate::authority_watch::AuthorityHoldTimer::start("overlay.publish");
            return match guard.finish_entity_overlay_merge(encoded) {
                Ok(true) => RoundOutcome::OverlayMerged,
                Ok(false) => RoundOutcome::NoWork,
                Err(error) => round_engine_error(error),
            };
        }
        Ok(None) => {}
        Err(error) => return round_engine_error(error),
    }
    let now_ms = match SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|duration| u64::try_from(duration.as_millis()).ok())
        .filter(|now_ms| *now_ms > 0)
    {
        Some(now_ms) => now_ms,
        None => {
            return RoundOutcome::Terminal(io::Error::other(
                "maintenance wall clock is outside the supported range",
            ));
        }
    };
    match run_tombstone_compaction(&mut database, scope) {
        Ok(Some((pages, entries))) => return RoundOutcome::TombstoneCompacted { pages, entries },
        Ok(None) => {}
        Err(error) => return round_engine_error(error),
    }
    drop(round_hold);
    drop(database);
    match prepared::run(database_arc, scope, now_ms) {
        RoundOutcome::NoWork => {}
        outcome => return outcome,
    }
    let mut database = match database_arc.try_lock() {
        Ok(database) => database,
        Err(TryLockError::WouldBlock) => return RoundOutcome::ForegroundBusy,
        Err(TryLockError::Poisoned(_)) => {
            return RoundOutcome::Terminal(io::Error::other(
                "maintenance authority mutex is poisoned",
            ))
        }
    };
    let round_hold = crate::authority_watch::AuthorityHoldTimer::probe("maintenance.round");
    let preparation = match database.try_prepare_pack_adoption() {
        Ok(preparation) => preparation,
        Err(error) => return round_engine_error(error),
    };
    match preparation {
        crate::engine::PackAdoptionPreparation::Empty => RoundOutcome::NoWork,
        crate::engine::PackAdoptionPreparation::Busy => RoundOutcome::Conflict,
        crate::engine::PackAdoptionPreparation::Ready(mut prepared) => {
            // The fsync chain (manifest block, WAL cover, CONTROL
            // chain) runs WITHOUT the authority mutex held; only the
            // cheap prepare/finish phases hold it, so foreground
            // commits never wait on a catalog fsync.
            drop(round_hold);
            drop(database);
            let outcome = crate::engine::publish_prepared_pack_adoption(&mut prepared);
            let mut database = match database_arc.try_lock() {
                Ok(database) => database,
                Err(TryLockError::WouldBlock) => match database_arc.lock() {
                    Ok(database) => database,
                    Err(_) => {
                        return RoundOutcome::Terminal(io::Error::other(
                            "maintenance authority mutex is poisoned",
                        ))
                    }
                },
                Err(TryLockError::Poisoned(_)) => {
                    return RoundOutcome::Terminal(io::Error::other(
                        "maintenance authority mutex is poisoned",
                    ))
                }
            };
            let _hold = crate::authority_watch::AuthorityHoldTimer::probe("pack.install");
            match database.finish_pack_adoption(prepared, outcome) {
                Ok(true) => RoundOutcome::PayloadPackPublished,
                Ok(false) => RoundOutcome::NoWork,
                // Abandoned before any CONTROL write: nothing landed and the
                // authority stays usable, so a later round adopts again.
                Err(error) if crate::wal::abandoned_uncovered(&error) => RoundOutcome::Conflict,
                Err(error) => RoundOutcome::Terminal(error),
            }
        }
    }
}

/// Retires every retirable history slice of the search index, one slice per
/// pair of short holds, until none is left or the worker stops. The index is
/// derived data, so the retained bound is the source's own
/// (`history_retained_segments`); a failure is logged and retried next round.
fn retire_search_history(
    shared: &Shared,
    search_projection: &Arc<Mutex<crate::turn_search_projection::TurnSearchProjection>>,
) {
    loop {
        if shared.stop.load(std::sync::atomic::Ordering::Acquire) {
            return;
        }
        match crate::turn_search_projection::TurnSearchProjection::retire_history_slice(
            search_projection,
            shared.config.history_retained_segments,
        ) {
            Ok(Some(retired)) => update_metrics(
                shared,
                |metrics| {
                    metrics.search_retired_history_segments = metrics
                        .search_retired_history_segments
                        .saturating_add(u64::from(retired.metrics.retired_segments));
                    metrics.search_retained_history_segments = retired.metrics.retained_segments;
                    metrics.search_history_retirement_max_hold_us = metrics
                        .search_history_retirement_max_hold_us
                        .max(u64::try_from(retired.longest_hold.as_micros()).unwrap_or(u64::MAX));
                },
                Backoff::Keep,
            ),
            Ok(None) => return,
            Err(error) => {
                crate::engine_log::warn(
                    "search_history",
                    &format!("history retirement failed; the next round retries: kind={:?} error={error}", error.kind()),
                );
                update_metrics(
                    shared,
                    |metrics| {
                        metrics.search_history_retirement_failures =
                            metrics.search_history_retirement_failures.saturating_add(1);
                    },
                    Backoff::Keep,
                );
                return;
            }
        }
    }
}

fn record_history_compaction(
    shared: &Shared,
    retired_segments: u32,
    retained_segments: u32,
    backoff: Backoff,
) {
    update_metrics(
        shared,
        |metrics| {
            metrics.history_compactions = metrics.history_compactions.saturating_add(1);
            metrics.retired_history_segments = metrics
                .retired_history_segments
                .saturating_add(retired_segments as u64);
            metrics.retained_history_segments = retained_segments;
        },
        backoff,
    );
}

/// Records a terminal outcome and stops the worker; its caller returns.
fn record_terminal_round(shared: &Shared, error: io::Error) {
    match shared.state.lock() {
        Ok(mut state) => {
            state.metrics.failed_rounds = state.metrics.failed_rounds.saturating_add(1);
            state.terminal_error = Some(ErrorSnapshot::capture(&error));
            state.stopping = true;
        }
        Err(_) => {
            // A poisoned state lock must not strand waiters or
            // silently lose the terminal record: count the
            // dropout lock-free and still wake everyone below.
            shared
                .dropped_terminal_records
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    }
    shared.wake.notify_all();
}

/// Classify an engine error escaping a maintenance round. Rotation
/// backpressure is transient by design — the rotation driver completes the
/// install and the retried round proceeds — so it defers the round exactly
/// like a `WouldBlock` publication conflict. Recording it as terminal let
/// the listener health gate recycle the whole daemon on one expected
/// backpressure signal (2026-09-22: two daemon exits, one failed round
/// each, inside slow-mount rotation windows). A full volume or exhausted
/// quota defers the same way while the authority stays usable: recycling
/// frees no space and every exit feeds the storage restart breaker. An
/// authority that needs reopening stays terminal, because only the recycle
/// reopens it (docs/STORAGE_OPERATIONS.md, "Full volume or exhausted quota").
fn round_engine_error(error: io::Error) -> RoundOutcome {
    if error.kind() == io::ErrorKind::WouldBlock
        || crate::engine::is_rotation_backpressure(&error)
        || crate::wal::abandoned_uncovered(&error)
        || (crate::vfs::is_storage_full(&error) && !crate::engine::requires_reopen(&error))
    {
        RoundOutcome::Conflict
    } else {
        RoundOutcome::Terminal(error)
    }
}

enum Backoff {
    Keep,
    Reset,
    Increase,
}

fn update_metrics(shared: &Shared, update: impl FnOnce(&mut MaintenanceMetrics), backoff: Backoff) {
    if let Ok(mut state) = shared.state.lock() {
        update(&mut state.metrics);
        state.next_idle_interval = match backoff {
            Backoff::Keep => state.next_idle_interval,
            Backoff::Reset => shared.config.idle_interval,
            Backoff::Increase => increased_idle_interval(state.next_idle_interval),
        };
        state.metrics.current_idle_interval_milliseconds =
            duration_milliseconds(state.next_idle_interval);
    }
}

fn increased_idle_interval(current: Duration) -> Duration {
    current.saturating_mul(2).min(Duration::from_millis(
        MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS,
    ))
}

fn duration_milliseconds(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(test)]
mod tests {
    include!("maintenance_preparation_tests.rs");
    use super::*;
    use crate::entity::EntityKey;
    use std::time::Instant;

    fn key(index: u16) -> EntityKey {
        scoped_key(7, 11, index)
    }

    #[test]
    fn search_failure_feedback_bounds_retries_and_preserves_other_scopes() {
        let start = Instant::now();
        let interval = Duration::from_millis(250);
        let mut retries = [SearchRetryState::default(); 2];
        let mut attempts = 0;
        for tick in 0..240_u32 {
            let now = start + interval * tick;
            if retries[0].ready(now) {
                retries[0].failed(now, interval);
                attempts += 1;
            }
            assert!(
                retries[1].ready(now),
                "one failing scope cannot delay another"
            );
        }
        assert!(
            attempts <= 9,
            "persistent failure must not retry every maintenance round"
        );
        assert_eq!(attempts, 8);
        let previous_delay = retries[0].delay;
        retries[0].recovered();
        assert!(retries[0].ready(start));
        assert_eq!(maximum_search_failure_backoff(&retries), 0);
        assert_eq!(retries[0].failed(start, interval), interval);
        assert!(previous_delay > interval);
        eprintln!(
            "search_retry_state_bytes={} maximum_scopes={} maximum_state_bytes={}",
            std::mem::size_of::<SearchRetryState>(),
            MAX_MAINTENANCE_SCOPES,
            std::mem::size_of::<SearchRetryState>() * MAX_MAINTENANCE_SCOPES
        );
    }

    #[test]
    fn search_retry_ceiling_does_not_suspend_repair_forever() {
        let mut retry = SearchRetryState::default();
        let mut now = Instant::now();
        let ceiling = Duration::from_millis(MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS);
        for _ in 0..100 {
            let delay = retry.failed(now, Duration::from_millis(250));
            assert!(delay <= ceiling);
            assert!(!retry.ready(now));
            now += delay;
            assert!(retry.ready(now));
        }
        assert_eq!(retry.delay, ceiling);
    }

    #[test]
    fn search_failure_backoff_preserves_maintenance_and_resumes_after_repair() {
        let (directory, database) = authority_with_mount();
        let projection_directory = tempfile::tempdir().unwrap();
        {
            let guard = database.lock().unwrap();
            let mut transaction = guard.begin(7, 11).unwrap();
            crate::search_dirty::mark(
                &guard,
                &mut transaction,
                crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE,
                "already-deleted",
            )
            .unwrap();
            drop(guard);
            database.lock().unwrap().commit(transaction).unwrap();
        }
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 1024 * 1024).unwrap(),
        ));
        let poisoned = Arc::clone(&projection);
        assert!(thread::spawn(move || {
            let _guard = poisoned.lock().unwrap();
            panic!("injected derived-index failure");
        })
        .join()
        .is_err());
        let scheduler = MaintenanceScheduler::start_with_search(
            Arc::clone(&database),
            Some(Arc::clone(&projection)),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.search_failed_rounds >= 2
                && metrics.search_retry_deferrals > 0
                && metrics.completed_mounts == 1
        });
        assert!(metrics.search_maximum_failure_backoff_milliseconds >= 20);
        scheduler.observer().require_healthy().unwrap();
        projection.clear_poison();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.search_removed_conversations == 1
        });
        assert_eq!(metrics.search_acknowledged_tokens, 1);
        assert_eq!(metrics.search_maximum_failure_backoff_milliseconds, 0);
        scheduler.stop_and_join().unwrap();
        drop(database);
        let reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let mut transaction = reopened.begin(7, 11).unwrap();
        assert_eq!(
            reopened.entity_get(&mut transaction, &key(3)).unwrap(),
            Some(vec![3; 64])
        );
    }

    #[test]
    fn idle_round_publishes_a_completed_pack_without_business_writes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let payload = vec![0x57; 2 * 1024 * 1024];
        let id = database.commit_block_payload_for_test(&payload).unwrap();
        let store = database.block_store_shared();
        let (segment_id, ids) = store
            .begin_background_pack(1, crate::engine::MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES)
            .unwrap()
            .unwrap();
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(segment_id, ids.clone(), |id| store.get(id))
            .unwrap();
        let reference = crate::payload_manifest::PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store.verify_payload_segment(&reference).unwrap();
        store.note_packed(&ids).unwrap();
        store
            .complete_background_pack(segment_id, reference)
            .unwrap();
        let state = database.engine_mut_for_test().state().clone();
        let database = Arc::new(Mutex::new(database));
        let foreground = database.lock().unwrap();
        assert!(matches!(
            run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64, false),
            RoundOutcome::ForegroundBusy
        ));
        drop(foreground);
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.published_background_packs == 1
        });
        assert_eq!(metrics.committed_transactions, 0);
        scheduler.stop_and_join().unwrap();
        {
            let mut database = database.lock().unwrap();
            let engine = database.engine_mut_for_test();
            assert_eq!(engine.payload_segment_count(), 1);
            assert_eq!(engine.state().durable_sequence, state.durable_sequence);
            assert_eq!(
                engine.state().checkpoint_sequence,
                state.checkpoint_sequence
            );
            assert_eq!(
                engine.state().active_log_generation,
                state.active_log_generation
            );
        }
        assert!(store.pending_payload(id).unwrap().is_none());
        drop(store);
        drop(database);
        let reopened = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(reopened.read_block_for_test(id).unwrap(), payload);
    }

    /// Stages `payload` and completes one background pack holding it, as the
    /// packer does, so the next round finds an adoption to publish.
    fn complete_one_background_pack(database: &mut AuthorityDatabase, payload: &[u8]) {
        let id = database.commit_block_payload_for_test(payload).unwrap();
        let store = database.block_store_shared();
        let (segment_id, reserved) = store
            .begin_background_pack(1, crate::engine::MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES)
            .unwrap()
            .expect("the staged payload is reservable");
        assert!(reserved.contains(&id));
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(segment_id, reserved.clone(), |id| store.get(id))
            .unwrap();
        let reference = crate::payload_manifest::PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store.verify_payload_segment(&reference).unwrap();
        store.note_packed(&reserved).unwrap();
        store
            .complete_background_pack(segment_id, reference)
            .unwrap();
    }

    /// 2026-11-13 02:06: a DolphinFS fsync stalled past the barrier deadline
    /// while a pack adoption waited for its WAL cover. A scope round's
    /// adoption abandoned that way published nothing, so the round defers and
    /// a later round adopts again; a terminal round would stop the worker and
    /// its record would recycle the daemon.
    #[test]
    fn a_round_whose_pack_adoption_misses_the_barrier_deadline_defers_and_adopts_later() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let barrier = crate::wal::WalBarrierShared::buffered_for_test(4 * 1024 * 1024);
        database.attach_wal_barrier(&barrier).unwrap();
        let vfs = database.wal_vfs().unwrap();
        complete_one_background_pack(&mut database, b"stalled packed payload");
        // Written, so a Normal-tier command was acknowledged; its sync stalls.
        barrier.write_for_test(vfs.as_ref()).unwrap();
        let generation = database.engine_mut_for_test().state().generation;
        let database = Arc::new(Mutex::new(database));
        let outcome = {
            let _expired = crate::wal::ExpiredPublicationCovers::on_this_thread();
            run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64, false)
        };
        match &outcome {
            RoundOutcome::Conflict => {}
            RoundOutcome::Terminal(error) => {
                panic!("an adoption abandoned before publication ended the worker: {error}")
            }
            _ => panic!("the round must defer its abandoned adoption"),
        }
        {
            let mut guard = database.lock().unwrap();
            let engine = guard.engine_mut_for_test();
            engine.require_usable_authority().unwrap();
            assert_eq!(
                engine.state().generation,
                generation,
                "nothing was published"
            );
            assert_eq!(engine.payload_segment_count(), 0);
        }
        // Once the sync completes, a later round adopts again.
        barrier.sync_for_test(vfs.as_ref()).unwrap();
        complete_one_background_pack(&mut database.lock().unwrap(), b"next packed payload");
        barrier.write_for_test(vfs.as_ref()).unwrap();
        barrier.sync_for_test(vfs.as_ref()).unwrap();
        assert!(matches!(
            run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64, false),
            RoundOutcome::PayloadPackPublished
        ));
        assert_eq!(
            database
                .lock()
                .unwrap()
                .engine_mut_for_test()
                .payload_segment_count(),
            1
        );
    }

    /// History slices wait on the same WAL cover as pack adoption, before any
    /// CONTROL write. A slice abandoned at a cover that missed the barrier
    /// deadline published nothing, so the round defers and a later round
    /// retires; a terminal round would stop the worker and recycle the daemon.
    #[test]
    fn a_round_whose_history_slice_misses_the_barrier_deadline_defers_and_retires_later() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..4_u16 {
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(&mut transaction, key(index), vec![index as u8; 64])
                .unwrap();
            database.commit(transaction).unwrap();
            database.checkpoint_for_test().unwrap();
        }
        assert_eq!(database.history_segment_count_for_test(), 4);
        let barrier = crate::wal::WalBarrierShared::buffered_for_test(4 * 1024 * 1024);
        database.attach_wal_barrier(&barrier).unwrap();
        let vfs = database.wal_vfs().unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut transaction, key(9), vec![9; 64])
            .unwrap();
        database.commit(transaction).unwrap();
        // Written, so a Normal-tier command was acknowledged; its sync stalls.
        barrier.write_for_test(vfs.as_ref()).unwrap();
        let generation = database.engine_mut_for_test().state().generation;
        let database = Arc::new(Mutex::new(database));
        let outcome = {
            let _expired = crate::wal::ExpiredPublicationCovers::on_this_thread();
            run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 1, false)
        };
        match &outcome {
            RoundOutcome::Conflict => {}
            RoundOutcome::Terminal(error) => {
                panic!("a history slice abandoned before publication ended the worker: {error}")
            }
            _ => panic!("the round must defer its abandoned history slice"),
        }
        {
            let mut guard = database.lock().unwrap();
            assert_eq!(guard.history_segment_count_for_test(), 4);
            let engine = guard.engine_mut_for_test();
            engine.require_usable_authority().unwrap();
            assert_eq!(
                engine.state().generation,
                generation,
                "nothing was published"
            );
        }
        // Once the sync completes, a later round retires history.
        barrier.sync_for_test(vfs.as_ref()).unwrap();
        assert!(matches!(
            run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 1, false),
            RoundOutcome::HistoryCompacted { .. }
        ));
        assert!(database.lock().unwrap().history_segment_count_for_test() < 4);
    }

    /// Publishes a one-segment catalog in `database`, lets a whole-store pass
    /// publish its location table, then installs a second catalog that adds a
    /// segment holding `second-<seed>`: the published table is one behind.
    fn stale_catalog_location_table(
        database: &mut AuthorityDatabase,
        seed: u128,
    ) -> Arc<crate::block::BlockStore> {
        use crate::block::BlockId;
        use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
        let blocks = database.block_store_shared();
        let store = blocks.payload_segment_store().unwrap();
        let segment = |number: u128, payloads: &[Vec<u8>]| {
            let payloads: std::collections::BTreeMap<BlockId, Vec<u8>> = payloads
                .iter()
                .map(|payload| (BlockId::for_payload(payload), payload.clone()))
                .collect();
            let created = store
                .create(
                    uuid::Uuid::from_u128(number),
                    payloads.keys().copied().collect(),
                    |id| Ok(payloads[&id].clone()),
                )
                .unwrap();
            PayloadSegmentReference::new(
                created.metadata(),
                created.first_block_id(),
                created.last_block_id(),
            )
            .unwrap()
        };
        let first = segment(
            seed,
            &[
                format!("first-a-{seed}").into_bytes(),
                format!("first-b-{seed}").into_bytes(),
            ],
        );
        database
            .engine_mut_for_test()
            .publish_payload_manifest_for_test(PayloadManifest::new(vec![first]).unwrap())
            .unwrap();
        drop(blocks.traversal_reader(|| Ok(())).unwrap());
        let second = segment(seed + 1, &[format!("second-{seed}").into_bytes()]);
        database
            .engine_mut_for_test()
            .publish_payload_manifest_for_test(PayloadManifest::new(vec![first, second]).unwrap())
            .unwrap();
        assert_ne!(
            blocks.published_catalog_locations_for_test().unwrap().0,
            blocks.installed_catalog_publication_for_test(),
            "fixture: the published table is one catalog behind"
        );
        blocks
    }

    /// docs/TOFUDB_BLOCK_READ_PATH.md, "Published catalog location table":
    /// once a whole-store pass has published a table, the worker derives the
    /// table of each catalog installed later, off the owning mutex, for the
    /// authority and for the search index alike.
    #[test]
    fn worker_derives_the_published_location_tables_of_the_authority_and_the_search_index() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let authority_blocks = stale_catalog_location_table(&mut database, 1);
        let projection_directory = tempfile::tempdir().unwrap();
        let mut projection =
            TurnSearchProjection::initialize(projection_directory.path(), 1024 * 1024).unwrap();
        let search_blocks = stale_catalog_location_table(projection.database_mut_for_test(), 3);
        let database = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(projection));
        let scheduler = MaintenanceScheduler::start_with_search(
            Arc::clone(&database),
            Some(Arc::clone(&projection)),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        for blocks in [&authority_blocks, &search_blocks] {
            while blocks.published_catalog_locations_for_test().unwrap().0
                != blocks.installed_catalog_publication_for_test()
            {
                assert!(
                    std::time::Instant::now() < deadline,
                    "the worker never derived the installed catalog's table"
                );
                thread::sleep(Duration::from_millis(5));
            }
        }
        scheduler.stop_and_join().unwrap();
        for (blocks, seed) in [(&authority_blocks, 1), (&search_blocks, 3)] {
            let (_, table) = blocks.published_catalog_locations_for_test().unwrap();
            assert_eq!((table.located_blocks(), table.unindexed_segments()), (3, 0));
            let second = format!("second-{seed}").into_bytes();
            assert_eq!(
                blocks
                    .get(crate::block::BlockId::for_payload(&second))
                    .unwrap(),
                second
            );
        }
    }

    #[test]
    fn rotation_backpressure_defers_a_round_instead_of_stopping_the_worker() {
        // 2026-09-22: one maintenance prune commit inside a rotation window
        // recorded the transient backpressure as terminal; the listener
        // health gate then recycled the whole daemon (exit 1) on a signal
        // that is retryable by design. Rotation backpressure must classify
        // as a conflict deferral so the worker survives the install window.
        let backpressure = crate::engine::rotation_backpressure();
        assert!(matches!(
            round_engine_error(backpressure),
            RoundOutcome::Conflict
        ));
        let publication_conflict = io::Error::new(io::ErrorKind::WouldBlock, "would block");
        assert!(matches!(
            round_engine_error(publication_conflict),
            RoundOutcome::Conflict
        ));
        let fatal = io::Error::new(io::ErrorKind::InvalidData, "corrupt");
        assert!(matches!(
            round_engine_error(fatal),
            RoundOutcome::Terminal(_)
        ));
        // Only private reservation exhaustion is rewritten by its owner;
        // an unrelated real allocation failure must retain terminal policy.
        assert!(matches!(
            round_engine_error(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "physical read allocation failed"
            )),
            RoundOutcome::Terminal(_)
        ));
    }

    #[test]
    fn a_full_volume_defers_a_round_unless_the_authority_needs_reopening() {
        // DISK-RUNWAY-2 (2026-11-12): an ENOSPC or EDQUOT round was terminal,
        // so the health gate recycled the daemon, every exit fed the storage
        // restart breaker, and no restart could free space.
        for kind in [io::ErrorKind::StorageFull, io::ErrorKind::QuotaExceeded] {
            assert!(
                matches!(
                    round_engine_error(io::Error::new(kind, "volume full")),
                    RoundOutcome::Conflict
                ),
                "{kind:?}"
            );
            // A latched authority keeps the full volume's kind, yet only the
            // recycle reopens it.
            let latched =
                crate::engine::reopen_required(kind, "WAL background barrier failed".to_owned());
            assert!(
                matches!(round_engine_error(latched), RoundOutcome::Terminal(_)),
                "{kind:?}"
            );
        }
    }

    fn scoped_key(tenant_id: u64, owner_user_id: u64, index: u16) -> EntityKey {
        EntityKey::new(
            tenant_id,
            owner_user_id,
            "maintenance_test",
            &index.to_be_bytes(),
        )
        .unwrap()
    }

    fn tomb_key(tenant: u64, owner: u64, index: u16) -> EntityKey {
        EntityKey::new(tenant, owner, "tombstone_maintenance", &index.to_be_bytes()).unwrap()
    }

    fn seed_tombstone_dense_region(database: &mut AuthorityDatabase) {
        let mut put = database.begin(7, 11).unwrap();
        for index in 0..40_u16 {
            database
                .entity_put(
                    &mut put,
                    tomb_key(7, 11, index),
                    vec![(index % 251) as u8; 4 * 1024],
                )
                .unwrap();
        }
        database.commit(put).unwrap();
        database.merge_entity_overlay_fully().unwrap();
        let pin_a = database.begin(7, 11).unwrap();
        for chunk_start in (0..40_u16).step_by(4) {
            let mut delete = database.begin(7, 11).unwrap();
            for index in chunk_start..chunk_start + 4 {
                database
                    .entity_delete(&mut delete, tomb_key(7, 11, index))
                    .unwrap();
            }
            database.commit(delete).unwrap();
            database.merge_entity_overlay_fully().unwrap();
        }
        drop(pin_a);
    }

    #[test]
    fn compaction_round_vacuums_a_reported_dense_window() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_entity_tombstone_reclaim_override_for_test(Some(true));
        seed_tombstone_dense_region(&mut database);
        // The dense scan reports the window into the hint registry.
        let start = tomb_key(7, 11, 0);
        let end = tomb_key(7, 11, 1000);
        let mut scan_tx = database.begin(7, 11).unwrap();
        let rows = database
            .entity_scan(&mut scan_tx, &start, &end, 1_000)
            .unwrap();
        assert!(rows.is_empty());
        drop(scan_tx);
        let (hint_start, hint_end, count) = database
            .take_entity_tombstone_compaction_hint()
            .unwrap()
            .expect("a dense window must be reported");
        database
            .record_entity_tombstone_compaction_hint(&hint_start, &hint_end, count)
            .unwrap();
        let database = Arc::new(Mutex::new(database));
        let outcome = run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64, false);
        let (pages, entries) = match outcome {
            RoundOutcome::TombstoneCompacted { pages, entries } => (pages, entries),
            _ => panic!("expected TombstoneCompacted"),
        };
        assert!(
            pages > 0 && entries >= 40,
            "pages={pages} entries={entries}"
        );
        let database = database.lock().unwrap();
        let mut probe = database.begin(7, 11).unwrap();
        for index in 0..40_u16 {
            assert!(database
                .entity_get(&mut probe, &tomb_key(7, 11, index))
                .unwrap()
                .is_none());
        }
    }

    #[test]
    fn compaction_round_requeues_foreign_scope_hints() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_entity_tombstone_reclaim_override_for_test(Some(true));
        let start = tomb_key(9, 3, 0);
        let end = tomb_key(9, 3, 1000);
        database
            .record_entity_tombstone_compaction_hint(start.encoded(), end.encoded(), 99)
            .unwrap();
        let database = Arc::new(Mutex::new(database));
        let outcome = run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64, false);
        assert!(
            !matches!(outcome, RoundOutcome::TombstoneCompacted { .. }),
            "foreign hints must not be vacuumed by this scope"
        );
        let database = database.lock().unwrap();
        let requeued = database.take_entity_tombstone_compaction_hint().unwrap();
        assert!(requeued.is_some(), "the foreign hint must be requeued");
    }

    fn add_mount(
        database: &mut AuthorityDatabase,
        tenant_id: u64,
        owner_user_id: u64,
        pin_id: &[u8],
    ) {
        let mut seed = database.begin(tenant_id, owner_user_id).unwrap();
        for index in 0..4 {
            database
                .entity_put(
                    &mut seed,
                    scoped_key(tenant_id, owner_user_id, index),
                    vec![index as u8; 64],
                )
                .unwrap();
        }
        database.commit(seed).unwrap();

        let ranges = vec![(
            scoped_key(tenant_id, owner_user_id, 0),
            scoped_key(tenant_id, owner_user_id, 10),
        )];
        let mut pin = database.begin(tenant_id, owner_user_id).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut pin, pin_id, &ranges)
            .unwrap();
        database.commit(pin).unwrap();
        let mut retire = database.begin(tenant_id, owner_user_id).unwrap();
        database
            .entity_retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();
        let mut restore = database.begin(tenant_id, owner_user_id).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, pin_id, &ranges)
            .unwrap();
        database.commit(restore).unwrap();
    }

    fn authority_with_expired_attempt_events(
        event_count: usize,
        publish_conversation_sync: bool,
    ) -> (tempfile::TempDir, Arc<Mutex<AuthorityDatabase>>) {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let old_ms = 1_700_000_000_000_u64;
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            &database,
            &mut seed,
            &crate::turn::AppendSettledRequest {
                conversation_id: "maintenance-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: br#"{"content":"seeded"}"#.to_vec(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "maintenance-command".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "maintenance-turn".to_owned(),
                attempt_id: Some("maintenance-attempt".to_owned()),
                created_at_ms: old_ms,
                committed_at_ms: old_ms,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true,
                    title: "Maintenance fixture".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: old_ms,
                },
            },
        )
        .unwrap();
        for index in 0..event_count {
            if index > 0 && index % 200 == 0 {
                database.commit(seed).unwrap();
                seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            }
            crate::turn::append_attempt_event(
                &database,
                &mut seed,
                crate::turn::AttemptEventAppend {
                    conversation_id: "maintenance-conversation",
                    turn_id: "maintenance-turn",
                    attempt_id: "maintenance-attempt",
                    projection_revision: 1,
                    event_type: "text-delta",
                    payload: serde_json::Value::String(format!("chunk-{index}")),
                    occurred_at_ms: old_ms,
                    publish_conversation_sync,
                },
            )
            .unwrap();
        }
        database.commit(seed).unwrap();
        (directory, Arc::new(Mutex::new(database)))
    }

    #[test]
    fn scheduler_prunes_expired_attempt_events_on_idle_rounds() {
        let (_directory, database) = authority_with_expired_attempt_events(3, false);
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.attempt_events_pruned >= 3
        });
        assert_eq!(metrics.attempt_event_prune_attempts, 1);
        scheduler.stop_and_join().unwrap();
    }

    #[test]
    fn idle_round_commits_event_retirement_without_payload_rows() {
        let (directory, database) = authority_with_expired_attempt_events(0, false);
        let namespace = crate::generated_tofudb_ir::CONVERSATION_EVENT_RETIREMENT_NAMESPACE;
        let (first, end) = EntityKey::prefix_range(7, 11, namespace, b"j").unwrap();
        {
            let mut db = database.lock().unwrap();
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::delete(
                &mut db,
                &mut tx,
                &crate::conversation_header::DeleteRequest {
                    conversation_id: "maintenance-conversation".into(),
                    deleted_at_ms: 2_000_000_000_000,
                },
            )
            .unwrap();
            db.commit(tx).unwrap();
            let mut tx = db.begin(7, 11).unwrap();
            assert_eq!(db.entity_scan(&mut tx, &first, &end, 1).unwrap().len(), 1);
        }
        match run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64, false) {
            RoundOutcome::Committed {
                attempt_events_pruned: 0,
                ..
            } => {}
            RoundOutcome::Terminal(error) => panic!("metadata retirement failed: {error}"),
            _ => panic!("metadata-only retirement must count as committed work"),
        }
        drop(database);
        let db = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = db.begin(7, 11).unwrap();
        assert!(
            db.entity_scan(&mut tx, &first, &end, 1).unwrap().is_empty(),
            "maintenance discarded metadata-only retirement progress"
        );
    }

    #[test]
    fn idle_rounds_commit_sync_marker_release_after_conversation_delete() {
        // Deleting a conversation retires its replay range, while each expired
        // entry's age marker and Attempt reference remain until retention
        // releases them. Such a batch deletes no replay row, yet it must commit,
        // or every later round rescans and discards the same page. More markers
        // than one prune page also take more than one round.
        let (directory, database) = authority_with_expired_attempt_events(1_200, true);
        let present = |db: &AuthorityDatabase, namespace: &str| {
            let mut tx = db.begin(7, 11).unwrap();
            let (start, end) = EntityKey::prefix_range(7, 11, namespace, &[]).unwrap();
            !db.entity_scan(&mut tx, &start, &end, 1).unwrap().is_empty()
        };
        let markers = crate::generated_tofudb_ir::CONVERSATION_SYNC_AGE_INDEX_NAMESPACE;
        let references = crate::generated_tofudb_ir::ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE;
        {
            let mut db = database.lock().unwrap();
            assert!(present(&db, markers) && present(&db, references));
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::delete(
                &mut db,
                &mut tx,
                &crate::conversation_header::DeleteRequest {
                    conversation_id: "maintenance-conversation".into(),
                    deleted_at_ms: 2_000_000_000_000,
                },
            )
            .unwrap();
            db.commit(tx).unwrap();
        }
        for round in 0.. {
            match run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64, false) {
                RoundOutcome::NoWork => break,
                RoundOutcome::Terminal(error) => panic!("round {round} failed: {error}"),
                _ => assert!(round < 200, "maintenance never settled"),
            }
        }
        drop(database);
        let db = AuthorityDatabase::open(directory.path()).unwrap();
        assert!(
            !present(&db, markers),
            "maintenance discarded the release of a deleted conversation's sync markers"
        );
        assert!(!present(&db, references));
    }

    fn authority_with_mount() -> (tempfile::TempDir, Arc<Mutex<AuthorityDatabase>>) {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        add_mount(&mut database, 7, 11, b"mount");
        (directory, Arc::new(Mutex::new(database)))
    }

    fn wait_for(
        observer: &MaintenanceObserver,
        predicate: impl Fn(MaintenanceMetrics) -> bool,
    ) -> MaintenanceMetrics {
        // These functional checks wait for real fsync-backed work. Keep a
        // fixed failure watchdog without treating host I/O speed as the
        // scheduler contract; pure-clock tests above verify retry cadence.
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            let metrics = observer.metrics().unwrap();
            if predicate(metrics) {
                return metrics;
            }
            assert!(
                Instant::now() < deadline,
                "maintenance scheduler timed out: {metrics:?}"
            );
            thread::sleep(Duration::from_millis(2));
        }
    }

    #[test]
    fn scheduler_waits_one_interval_and_consolidates_one_bounded_mount() {
        let (_directory, database) = authority_with_mount();
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        assert_eq!(scheduler.observer().metrics().unwrap().rounds, 0);
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.committed_transactions == 1
        });
        assert_eq!(metrics.materialized_rows, 4);
        assert_eq!(metrics.completed_mounts, 1);
        scheduler.stop_and_join().unwrap();

        let database = database.lock().unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            database.entity_get(&mut transaction, &key(3)).unwrap(),
            Some(vec![3; 64])
        );
    }

    #[test]
    fn scheduler_uses_the_single_worker_for_search_without_making_failure_terminal() {
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(authority_directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        crate::search_dirty::mark(
            &database,
            &mut transaction,
            crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            "already-deleted",
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let database = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 1024 * 1024).unwrap(),
        ));
        let scheduler = MaintenanceScheduler::start_with_search(
            Arc::clone(&database),
            Some(projection),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.search_removed_conversations == 1
        });
        assert_eq!(metrics.search_dirty_conversations, 1);
        assert_eq!(metrics.search_acknowledged_tokens, 1);
        assert_eq!(metrics.search_failed_rounds, 0);
        scheduler.stop_and_join().unwrap();
        assert!(crate::search_dirty::list(&database.lock().unwrap(), 7, 11)
            .unwrap()
            .is_empty());
    }

    /// docs/STORAGE_OPERATIONS.md, "Search projection history retirement":
    /// the worker that indexes also retires the index's history down to the
    /// source's retained bound, so superseded index pages become collectable.
    #[test]
    fn scheduler_retires_search_index_history_to_the_retained_bound() {
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize(authority_directory.path()).unwrap(),
        ));
        let mut index =
            TurnSearchProjection::initialize(projection_directory.path(), 1 << 24).unwrap();
        for generation in 1..=6 {
            index
                .publish_scope(7, 11, "conversation", generation, "")
                .unwrap();
            index.checkpoint_wal_window_for_shutdown().unwrap();
        }
        let history = |index: &mut TurnSearchProjection| {
            index
                .database_mut_for_test()
                .engine_mut_for_test()
                .history_segment_count()
        };
        assert!(history(&mut index) >= 6);
        let projection = Arc::new(Mutex::new(index));
        let scheduler = MaintenanceScheduler::start_with_search(
            Arc::clone(&database),
            Some(Arc::clone(&projection)),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_history_test(Duration::from_millis(10), 1, 2),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.search_retained_history_segments == 2
        });
        assert!(metrics.search_retired_history_segments >= 4);
        assert_eq!(metrics.search_history_retirement_failures, 0);
        assert!(metrics.search_history_retirement_max_hold_us > 0);
        scheduler.stop_and_join().unwrap();
        assert_eq!(history(&mut projection.lock().unwrap()), 2);
    }

    /// Search is admitted on an empty owner and the worker is idle behind the
    /// 60 s backoff ceiling: inside the 20 s wait bound, only a commit-side
    /// wake can start another round.
    fn backed_off_search_scheduler(
        database: &Arc<Mutex<AuthorityDatabase>>,
        projection: &Arc<Mutex<TurnSearchProjection>>,
    ) -> (MaintenanceScheduler, MaintenanceMetrics) {
        let scheduler = MaintenanceScheduler::start_with_search(
            Arc::clone(database),
            Some(Arc::clone(projection)),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        // Admission ends in a round of its own; a no-work round can precede
        // it, so the idle round must follow the end of admission.
        let deadline = Instant::now() + Duration::from_secs(20);
        while projection.lock().unwrap().owner_indexing(7, 11).unwrap() {
            assert!(Instant::now() < deadline, "search admission never finished");
            std::thread::sleep(Duration::from_millis(1));
        }
        let admitted_no_work_rounds = scheduler.observer().metrics().unwrap().no_work_rounds;
        let idle = {
            // The state lock excludes every scheduling decision, so a re-arm
            // still in flight also selects the ceiling.
            let mut state = wait_for_round(&scheduler, |metrics| {
                metrics.no_work_rounds > admitted_no_work_rounds
            });
            let ceiling = Duration::from_millis(MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS);
            state.next_idle_interval = ceiling;
            state.round_deadline = Instant::now() + ceiling;
            state.metrics
        };
        assert!(!projection.lock().unwrap().owner_indexing(7, 11).unwrap());
        (scheduler, idle)
    }

    /// Completed rounds notify the scheduler condvar in specifications; the
    /// bound only reports a missed wake and stays below the idle ceiling.
    fn wait_for_round(
        scheduler: &MaintenanceScheduler,
        predicate: impl Fn(&MaintenanceMetrics) -> bool,
    ) -> std::sync::MutexGuard<'_, SchedulerState> {
        let state = scheduler.shared.state.lock().unwrap();
        let (state, waited) = scheduler
            .shared
            .wake
            .wait_timeout_while(state, Duration::from_secs(20), |state| {
                !predicate(&state.metrics)
            })
            .unwrap();
        assert!(
            !waited.timed_out(),
            "maintenance round never started: {:?}",
            state.metrics
        );
        state
    }

    fn create_searchable_conversation(database: &Arc<Mutex<AuthorityDatabase>>, title: &str) {
        let mut database = database.lock().unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut transaction,
            &crate::conversation_header::CreateRequest {
                conversation_id: "woken".into(),
                title: title.into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 100,
                updated_at_ms: 100,
                committed_at_ms: 100,
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
    }

    #[test]
    fn search_wake_ends_idle_backoff_at_the_base_cadence_and_goes_first() {
        let completed = Instant::now();
        let ceiling = Duration::from_millis(MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS);
        let base = Duration::from_millis(250);
        let mut state = SchedulerState {
            stopping: false,
            next_scope: 1,
            metrics: MaintenanceMetrics::default(),
            next_idle_interval: ceiling,
            terminal_error: None,
            round_deadline: completed + ceiling,
            round_completed_at: completed,
            search_wakes: 0,
        };
        assert_eq!(state.round_due(base), completed + ceiling);
        state.search_wakes = 1 << 2;
        assert_eq!(state.round_due(base), completed + base);
        state.round_deadline = completed + base / 2;
        assert_eq!(
            state.round_due(base),
            completed + base / 2,
            "a wake never delays a due round"
        );
        assert_eq!(state.take_round_scope(3), 2);
        assert_eq!((state.search_wakes, state.next_scope), (0, 0));
        assert_eq!(state.round_due(base), completed + base / 2);
        assert_eq!(
            state.take_round_scope(3),
            0,
            "without wakes the round robin resumes"
        );
        assert_eq!(state.next_scope, 1);
    }

    #[test]
    fn committed_search_invalidation_wakes_a_backed_off_worker() {
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize(authority_directory.path()).unwrap(),
        ));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 16 * 1024 * 1024)
                .unwrap(),
        ));
        let (scheduler, idle) = backed_off_search_scheduler(&database, &projection);
        create_searchable_conversation(&database, "wakeneedle");
        let woken = wait_for_round(&scheduler, |metrics| {
            metrics.search_acknowledged_tokens == 1
        })
        .metrics;
        assert!(woken.rounds > idle.rounds);
        assert_eq!(woken.search_rebuilt_conversations, 1);
        assert_eq!(woken.search_failed_rounds, 0);
        scheduler.stop_and_join().unwrap();
    }

    #[test]
    fn acknowledged_search_invalidation_is_not_ready_until_consumed() {
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize(authority_directory.path()).unwrap(),
        ));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 16 * 1024 * 1024)
                .unwrap(),
        ));
        let (scheduler, _) = backed_off_search_scheduler(&database, &projection);
        {
            // Every round first takes the index lock, so the woken worker
            // cannot consume the token before this coverage read.
            let index = projection.lock().unwrap();
            create_searchable_conversation(&database, "fenceneedle");
            assert!(
                index.owner_indexing(7, 11).unwrap(),
                "an acknowledged invalidation was reported ready before consumption"
            );
        }
        let woken = wait_for_round(&scheduler, |metrics| {
            metrics.search_acknowledged_tokens == 1
        })
        .metrics;
        // Rounds are sequential: once the second later round starts, the round
        // after the ACK has observed the empty dirty queue and published READY.
        drop(wait_for_round(&scheduler, |metrics| {
            metrics.rounds >= woken.rounds + 2
        }));
        scheduler.stop_and_join().unwrap();
        let index = projection.lock().unwrap();
        assert!(!index.owner_indexing(7, 11).unwrap());
        let hits = index
            .search(&crate::turn_search_projection::ConversationSearchRequest {
                tenant_id: 7,
                owner_user_id: 11,
                query: "fenceneedle".into(),
                limit: 10,
                snippet_radius: 10,
            })
            .unwrap();
        assert_eq!(
            hits.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["woken"]
        );
    }

    #[test]
    fn scheduler_prunes_expired_tool_results_in_one_bounded_owner_transaction() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let now_ms = u64::try_from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis(),
        )
        .unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for content in ["expired-a", "expired-b"] {
            crate::artifact::tool_put(
                &database,
                &mut seed,
                content,
                "text/plain",
                now_ms - 1_000,
                now_ms - 1,
            )
            .unwrap();
        }
        crate::artifact::tool_put(
            &database,
            &mut seed,
            "still-live",
            "text/plain",
            now_ms,
            now_ms + 10_000,
        )
        .unwrap();
        database.commit(seed).unwrap();
        let database = Arc::new(Mutex::new(database));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.tool_result_artifacts_pruned == 2
        });
        assert_eq!(metrics.committed_transactions, 1);
        assert_eq!(metrics.tool_result_prune_backlog_rounds, 0);
        scheduler.stop_and_join().unwrap();

        let database = database.lock().unwrap();
        let mut verify = database.begin(7, 11).unwrap();
        let progress = crate::artifact::tool_prune(
            &database,
            &mut verify,
            now_ms,
            TOOL_RESULT_PRUNE_ROWS_PER_MAINTENANCE_ROUND,
        )
        .unwrap();
        assert_eq!(progress.deleted, 0);
    }

    #[test]
    fn scheduler_retires_old_history_while_preserving_foreground_wal() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..3_u16 {
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(&mut transaction, key(index), vec![index as u8; 64])
                .unwrap();
            database.commit(transaction).unwrap();
            database.checkpoint_for_test().unwrap();
        }
        assert_eq!(database.history_segment_count_for_test(), 3);
        let mut active = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut active, key(9), b"foreground".to_vec())
            .unwrap();
        database.commit(active).unwrap();
        let foreground_sequence = database
            .engine_mut_for_test()
            .committed_transactions()
            .last()
            .map(|transaction| transaction.sequence)
            .unwrap();
        let active_generation = database.engine_mut_for_test().state().active_log_generation;
        let checkpoint_sequence = database.engine_mut_for_test().state().checkpoint_sequence;
        let database = Arc::new(Mutex::new(database));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_history_test(Duration::from_millis(10), 1, 1),
        )
        .unwrap();
        let observer = scheduler.observer();
        // History retires when the scheduler starts, so the first scope round
        // can merge the foreground write's overlay before the stop. Waiting
        // for that round (joined by the stop) checks the WAL after a merge
        // in every run instead of racing it.
        let metrics = wait_for(&observer, |metrics| {
            metrics.history_compactions == 1 && metrics.rounds >= 1
        });
        assert_eq!(metrics.retired_history_segments, 2);
        assert_eq!(metrics.retained_history_segments, 1);
        scheduler.stop_and_join().unwrap();
        let stopped = observer.metrics().unwrap();
        assert_eq!(stopped.committed_transactions, 0, "{stopped:?}");
        {
            let mut database = database.lock().unwrap();
            assert_eq!(database.history_segment_count_for_test(), 1);
            let engine = database.engine_mut_for_test();
            // The foreground commit stays the oldest WAL record; every later
            // one is an overlay merge the scheduler published.
            let retained: Vec<u64> = engine
                .committed_transactions()
                .iter()
                .map(|transaction| transaction.sequence)
                .collect();
            assert_eq!(retained.first(), Some(&foreground_sequence), "{retained:?}");
            assert_eq!(
                retained.len() as u64,
                1 + stopped.overlay_background_chunks,
                "{retained:?} {stopped:?}"
            );
            assert_eq!(engine.state().active_log_generation, active_generation);
            assert_eq!(engine.state().checkpoint_sequence, checkpoint_sequence);
        }
        drop(database);
        let reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = reopened.begin(7, 11).unwrap();
        assert_eq!(
            reopened.entity_get(&mut read, &key(9)).unwrap(),
            Some(b"foreground".to_vec())
        );
    }

    #[test]
    fn foreground_contention_defers_one_round_then_waits_for_the_authority() {
        let (_directory, database) = authority_with_mount();
        let foreground = database.lock().unwrap();
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let observer = scheduler.observer();
        let deferred = wait_for(&observer, |metrics| metrics.foreground_deferrals > 0);
        assert_eq!(deferred.committed_transactions, 0);
        // Held across twenty idle intervals: try_lock retries used to defer
        // every one of them, so a busy foreground starved maintenance.
        thread::sleep(Duration::from_millis(200));
        assert_eq!(observer.metrics().unwrap().foreground_deferrals, 1);
        drop(foreground);
        let committed = wait_for(&observer, |metrics| metrics.committed_transactions == 1);
        assert_eq!(committed.foreground_deferrals, 1);
        scheduler.stop_and_join().unwrap();
    }

    #[test]
    fn history_retires_on_the_poll_cadence_without_waiting_for_scope_rounds() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // Two segments per slice, so retiring five takes three slices.
        database
            .engine_mut_for_test()
            .set_checkpoint_materialize_batch_bytes(
                2 * crate::history::HISTORY_SEGMENT_MAX_BYTES as u64,
            )
            .unwrap();
        for index in 0..6_u16 {
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(&mut transaction, key(index), vec![index as u8; 64])
                .unwrap();
            database.commit(transaction).unwrap();
            database.checkpoint_for_test().unwrap();
        }
        assert_eq!(database.history_segment_count_for_test(), 6);
        let database = Arc::new(Mutex::new(database));
        // Scope rounds a second apart (the longest admitted interval): the
        // poll path, armed at start and by the first round, retires every
        // slice before a second round is due.
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_history_test(Duration::from_secs(1), 1, 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.retired_history_segments == 5
        });
        assert!(metrics.rounds <= 1, "{metrics:?}");
        assert_eq!(metrics.history_compactions, 3);
        assert_eq!(metrics.retained_history_segments, 1);
        scheduler.stop_and_join().unwrap();
        assert_eq!(database.lock().unwrap().history_segment_count_for_test(), 1);
    }

    #[test]
    fn history_retires_from_start_before_any_scope_round() {
        // 2026-11-12: a restarted engine retired nothing for 27 minutes; the
        // poll path was armed only by a finished scope round, and the first
        // round's search pass ran that long.
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .engine_mut_for_test()
            .set_checkpoint_materialize_batch_bytes(
                2 * crate::history::HISTORY_SEGMENT_MAX_BYTES as u64,
            )
            .unwrap();
        for index in 0..6_u16 {
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(&mut transaction, key(index), vec![index as u8; 64])
                .unwrap();
            database.commit(transaction).unwrap();
            database.checkpoint_for_test().unwrap();
        }
        let database = Arc::new(Mutex::new(database));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_history_test(Duration::from_secs(1), 1, 1),
        )
        .unwrap();
        // No scope round runs during this specification.
        scheduler.shared.state.lock().unwrap().round_deadline =
            Instant::now() + Duration::from_secs(600);
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.retired_history_segments == 5
        });
        assert_eq!(metrics.rounds, 0, "{metrics:?}");
        assert_eq!(metrics.history_compactions, 3);
        scheduler.stop_and_join().unwrap();
        assert_eq!(database.lock().unwrap().history_segment_count_for_test(), 1);
    }

    #[test]
    fn scope_and_worker_resources_are_hard_bounded() {
        let (_directory, database) = authority_with_mount();
        assert!(MaintenanceScope::new(0, 1).is_err());
        let lean = MaintenanceSchedulerConfig::from_resource_budget(
            DaemonResourceBudget::from_snapshot(Default::default()),
        );
        assert_eq!(lean.maximum_scopes(), 4);
        assert_eq!(lean.idle_interval(), Duration::from_secs(1));
        assert_eq!(lean.worker_stack_bytes(), 512 * 1024);
        assert_eq!(lean.history_retained_segments(), 16);
        let observed = MaintenanceSchedulerConfig::from_resource_budget(
            DaemonResourceBudget::from_snapshot(crate::resource_probe::LaunchResourceSnapshot {
                logical_cpus: Some(8),
                memory_capacity_bytes: Some(8 * 1024 * 1024 * 1024),
                memory_headroom_bytes: Some(4 * 1024 * 1024 * 1024),
                volume_free_bytes: Some(100 * 1024 * 1024 * 1024),
            }),
        );
        assert_eq!(observed.history_retained_segments(), 64);
        for production in [lean, observed] {
            assert_eq!(
                production.search_admission,
                SearchAdmission::ForegroundPressure
            );
        }
        let scopes = (1..=MAX_MAINTENANCE_SCOPES + 1)
            .map(|owner| MaintenanceScope::new(7, owner as u64).unwrap())
            .collect();
        assert!(MaintenanceScheduler::start(
            database,
            scopes,
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), MAX_MAINTENANCE_SCOPES),
        )
        .is_err());
        assert_eq!(MAX_MAINTENANCE_WORKERS, 1);
    }

    #[test]
    fn search_admission_defers_only_for_sampled_foreground_pressure() {
        let created = Instant::now();
        let mut pressured = crate::fsync_stats::foreground_hotness_sampler();
        {
            let _foreground_wait = crate::fsync_stats::ForegroundWait::start();
            thread::sleep(Duration::from_millis(300));
        }
        let deferred = SearchAdmission::ForegroundPressure.defer(&mut pressured);
        // Past two seconds the fairness permit legitimately admits the batch.
        if created.elapsed() < Duration::from_secs(2) {
            assert!(deferred);
        }
        assert!(!SearchAdmission::IdleForeground.defer(&mut pressured));
        assert_eq!(
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1).search_admission,
            SearchAdmission::IdleForeground
        );
    }

    #[test]
    fn empty_authority_exponentially_backs_off() {
        let directory = tempfile::tempdir().unwrap();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize(directory.path()).unwrap(),
        ));
        let scheduler = MaintenanceScheduler::start(
            database,
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| metrics.no_work_rounds >= 2);
        assert_eq!(metrics.current_idle_interval_milliseconds, 40);
        assert_eq!(
            increased_idle_interval(Duration::from_secs(40)),
            Duration::from_secs(60)
        );
        assert_eq!(
            increased_idle_interval(Duration::from_secs(60)),
            Duration::from_secs(60)
        );
        scheduler.stop_and_join().unwrap();
    }

    #[test]
    fn distinct_owner_scopes_are_serviced_round_robin() {
        let directory = tempfile::tempdir().unwrap();
        let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
        add_mount(&mut authority, 7, 11, b"owner-11");
        add_mount(&mut authority, 7, 12, b"owner-12");
        let database = Arc::new(Mutex::new(authority));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![
                MaintenanceScope::new(7, 12).unwrap(),
                MaintenanceScope::new(7, 11).unwrap(),
            ],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 2),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.completed_mounts == 2
        });
        assert_eq!(metrics.committed_transactions, 2);
        assert_eq!(metrics.materialized_rows, 8);
        scheduler.stop_and_join().unwrap();

        let authority = database.lock().unwrap();
        for owner_user_id in [11, 12] {
            let mut transaction = authority.begin(7, owner_user_id).unwrap();
            assert_eq!(
                authority
                    .entity_get(&mut transaction, &scoped_key(7, owner_user_id, 3))
                    .unwrap(),
                Some(vec![3; 64])
            );
        }
    }

    fn put_overlay_record(database: &mut AuthorityDatabase, index: u32) {
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, "records", &index.to_be_bytes()).unwrap(),
                index.to_be_bytes().to_vec(),
            )
            .unwrap();
        database.commit(transaction).unwrap();
    }

    fn get_overlay_record(database: &AuthorityDatabase, index: u32) -> Option<Vec<u8>> {
        let mut read = database.begin(7, 11).unwrap();
        database
            .entity_get(
                &mut read,
                &EntityKey::new(7, 11, "records", &index.to_be_bytes()).unwrap(),
            )
            .unwrap()
    }

    fn overlay_entries(database: &Arc<Mutex<AuthorityDatabase>>) -> usize {
        database.lock().unwrap().entity_overlay_entries_for_test()
    }

    /// Blocks one cold source open so the test observes the real maintenance
    /// helper's authority availability while physical preparation is stalled.
    struct BlockingMergeProbe {
        armed: std::sync::atomic::AtomicBool,
        entered: std::sync::mpsc::Sender<()>,
        release: Mutex<std::sync::mpsc::Receiver<()>>,
    }
    impl BlockingMergeProbe {
        fn pause_read(&self) -> io::Result<()> {
            if self.armed.swap(false, std::sync::atomic::Ordering::AcqRel) {
                self.entered
                    .send(())
                    .map_err(|_| io::Error::other("merge probe receiver closed"))?;
                self.release
                    .lock()
                    .unwrap()
                    .recv_timeout(Duration::from_secs(10))
                    .map_err(|_| io::Error::other("merge probe release missing"))?;
            }
            Ok(())
        }
    }
    struct BlockingMergeReadVfs {
        inner: Arc<crate::vfs::DeterministicVfs>,
        probe: Arc<BlockingMergeProbe>,
    }
    struct BlockingMergeReadFile {
        inner: Box<dyn crate::vfs::VfsFile>,
        probe: Arc<BlockingMergeProbe>,
    }
    impl crate::vfs::VfsFile for BlockingMergeReadFile {
        fn len(&self) -> io::Result<u64> {
            self.inner.len()
        }
        fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
            self.probe.pause_read()?;
            self.inner.read_all(maximum)
        }
        fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
            self.probe.pause_read()?;
            self.inner.read_exact_at(offset, bytes)
        }
        fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
            self.inner.write_all_at(offset, bytes)
        }
        fn set_len(&mut self, length: u64) -> io::Result<()> {
            self.inner.set_len(length)
        }
        fn sync_data(&mut self) -> io::Result<()> {
            self.inner.sync_data()
        }
        fn sync_all(&mut self) -> io::Result<()> {
            self.inner.sync_all()
        }
        fn try_lock_exclusive(&mut self) -> io::Result<()> {
            self.inner.try_lock_exclusive()
        }
    }

    impl crate::vfs::Vfs for BlockingMergeReadVfs {
        fn open(
            &self,
            path: &std::path::Path,
            request: crate::vfs::OpenRequest,
        ) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
            Ok(Box::new(BlockingMergeReadFile {
                inner: self.inner.open(path, request)?,
                probe: Arc::clone(&self.probe),
            }))
        }
        fn create_dir(&self, path: &std::path::Path) -> io::Result<()> {
            self.inner.create_dir(path)
        }
        fn rename(
            &self,
            source: &std::path::Path,
            destination: &std::path::Path,
        ) -> io::Result<()> {
            self.inner.rename(source, destination)
        }
        fn remove_file(&self, path: &std::path::Path) -> io::Result<()> {
            self.inner.remove_file(path)
        }
        fn metadata(&self, path: &std::path::Path) -> io::Result<crate::vfs::FileKind> {
            self.inner.metadata(path)
        }
        fn read_directory(&self, path: &std::path::Path) -> io::Result<Vec<std::path::PathBuf>> {
            self.inner.read_directory(path)
        }
        fn read_directory_bounded(
            &self,
            path: &std::path::Path,
            maximum: usize,
        ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
            self.inner.read_directory_bounded(path, maximum)
        }
        fn match_directory_prefix_bounded(
            &self,
            path: &std::path::Path,
            prefix: &str,
            scanned: usize,
            matched: usize,
        ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
            self.inner
                .match_directory_prefix_bounded(path, prefix, scanned, matched)
        }
        fn sync_directory(&self, path: &std::path::Path) -> io::Result<()> {
            self.inner.sync_directory(path)
        }
    }

    #[test]
    fn stalled_background_merge_read_releases_authority_for_new_commits() {
        use crate::vfs::Vfs;
        let (entered_sender, entered_receiver) = std::sync::mpsc::channel();
        let (release_sender, release_receiver) = std::sync::mpsc::channel();
        let vfs = Arc::new(BlockingMergeReadVfs {
            inner: Arc::new(crate::vfs::DeterministicVfs::new(None)),
            probe: Arc::new(BlockingMergeProbe {
                armed: std::sync::atomic::AtomicBool::new(false),
                entered: entered_sender,
                release: Mutex::new(release_receiver),
            }),
        });
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let source: Arc<dyn Vfs> = vfs.clone();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), source).unwrap();
        database.set_write_overlay_for_test(true);
        for index in 0..20 {
            put_overlay_record(&mut database, index);
        }
        database.merge_entity_overlay_fully().unwrap();
        database.checkpoint_wal_window().unwrap();
        put_overlay_record(&mut database, 100);
        database.configure_hot_data_caches(0, 0).unwrap();
        let database = Arc::new(Mutex::new(database));
        vfs.probe
            .armed
            .store(true, std::sync::atomic::Ordering::Release);
        let worker_database = Arc::clone(&database);
        let worker = std::thread::spawn(move || merge_overlay_outside_authority(&worker_database));
        entered_receiver
            .recv_timeout(Duration::from_secs(5))
            .expect("merge must reach the cold physical source read");
        {
            let mut foreground = database
                .try_lock()
                .expect("cold merge read must not hold authority");
            put_overlay_record(&mut foreground, 101);
            assert_eq!(
                get_overlay_record(&foreground, 101),
                Some(101_u32.to_be_bytes().to_vec())
            );
        }
        release_sender.send(()).unwrap();
        assert!(worker.join().unwrap().unwrap());
        let guard = database.lock().unwrap();
        assert_eq!(
            get_overlay_record(&guard, 100),
            Some(100_u32.to_be_bytes().to_vec())
        );
        assert_eq!(
            get_overlay_record(&guard, 101),
            Some(101_u32.to_be_bytes().to_vec())
        );
        assert_eq!(
            guard
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
    }

    #[test]
    fn background_drain_commits_bounded_chunks_below_the_hard_trigger() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        // 5,000 entries stay below every commit-path trigger (12,000 scan
        // entries, 8 MiB budget, 48 MiB WAL watermark), so only the
        // background drain can merge them.
        for index in 0..5_000_u32 {
            put_overlay_record(&mut database, index);
        }
        let database = Arc::new(Mutex::new(database));
        assert_eq!(overlay_entries(&database), 5_000);
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        assert!(trigger.needs_drain());
        // Cold slices start small, then measured cost determines growth.
        // Every wake remains bounded and every nonempty wake makes progress.
        let wake_limit = 5_000;
        let mut remaining = wake_limit;
        for _ in 0..wake_limit {
            if trigger.below_soft_thresholds() {
                break;
            }
            let chunks = drain_overlay_in_background(&database, &trigger);
            assert!((1..=u64::from(OVERLAY_BACKGROUND_MAX_CHUNKS_PER_WAKE)).contains(&chunks));
            let next = overlay_entries(&database);
            assert!(next < remaining);
            remaining = next;
        }
        assert!(trigger.below_soft_thresholds());
        // The retained entries resolve exactly through the overlay, and the
        // next wake finds nothing to do.
        assert_eq!(drain_overlay_in_background(&database, &trigger), 0);
        let guard = database.lock().unwrap();
        for index in [0_u32, 1, 2_048, 4_999] {
            assert_eq!(
                get_overlay_record(&guard, index),
                Some(index.to_be_bytes().to_vec()),
            );
        }
    }

    #[test]
    fn background_drain_bounds_each_wake_for_the_worst_sub_trigger_backlog() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        // The largest backlog the commit path can hold without firing its
        // own drain: one entry below the 12,000 scan trigger.
        for index in 0..11_999_u32 {
            put_overlay_record(&mut database, index);
        }
        let database = Arc::new(Mutex::new(database));
        assert_eq!(overlay_entries(&database), 11_999);
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        // One wake is bounded by four chunks, not a fixed number of rows:
        // wall-clock AIMD may shrink subsequent chunks under parallel load.
        assert_eq!(drain_overlay_in_background(&database, &trigger), 4);
        let mut remaining = overlay_entries(&database);
        assert!((3_807..11_999).contains(&remaining));
        let wake_limit = remaining;
        for _ in 0..wake_limit {
            if trigger.below_soft_thresholds() {
                break;
            }
            let chunks = drain_overlay_in_background(&database, &trigger);
            assert!((1..=u64::from(OVERLAY_BACKGROUND_MAX_CHUNKS_PER_WAKE)).contains(&chunks));
            let next = overlay_entries(&database);
            assert!(next < remaining, "every wake must make bounded progress");
            remaining = next;
        }
        assert!(trigger.below_soft_thresholds());
        assert_eq!(drain_overlay_in_background(&database, &trigger), 0);
        let guard = database.lock().unwrap();
        for index in [0_u32, 8_191, 8_192, 11_998] {
            assert_eq!(
                get_overlay_record(&guard, index),
                Some(index.to_be_bytes().to_vec()),
            );
        }
    }

    #[test]
    fn background_drain_is_a_noop_below_the_soft_threshold() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        for index in 0..1_000_u32 {
            put_overlay_record(&mut database, index);
        }
        let database = Arc::new(Mutex::new(database));
        assert_eq!(overlay_entries(&database), 1_000);
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        assert!(!trigger.needs_drain());
        assert_eq!(drain_overlay_in_background(&database, &trigger), 0);
        // Nothing merged: the entries are still pending in the overlay and
        // reads resolve through it exactly.
        assert_eq!(overlay_entries(&database), 1_000);
        let guard = database.lock().unwrap();
        assert_eq!(
            get_overlay_record(&guard, 999),
            Some(999_u32.to_be_bytes().to_vec())
        );
    }

    #[test]
    fn a_long_scope_round_never_starves_the_background_overlay_drain() {
        // 2026-11-11: search rebuilds held the scope worker for minutes per
        // round; the 25 ms drain poll ran 30 times in 47 minutes and the
        // commit-path backstop merged cold pages inside foreground holds. A
        // round blocked on the search index stands in for a long rebuild.
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(authority_directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        let database = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 16 * 1024 * 1024)
                .unwrap(),
        ));
        let (scheduler, idle) = backed_off_search_scheduler(&database, &projection);
        let observer = scheduler.observer();
        let index = projection.lock().unwrap();
        create_searchable_conversation(&database, "drainneedle");
        let blocked = wait_for(&observer, |metrics| metrics.rounds > idle.rounds);
        {
            // Above the soft drain threshold, below every commit-path trigger.
            let mut guard = database.lock().unwrap();
            for record in 0..5_000_u32 {
                put_overlay_record(&mut guard, record);
            }
        }
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        assert!(trigger.needs_drain());
        let drained = wait_for(&observer, |_| trigger.below_soft_thresholds());
        assert_eq!(
            drained.rounds, blocked.rounds,
            "the scope round was still in progress"
        );
        assert!(drained.overlay_background_chunks > blocked.overlay_background_chunks);
        drop(index);
        scheduler.stop_and_join().unwrap();
        let guard = database.lock().unwrap();
        for record in [0_u32, 4_999] {
            assert_eq!(
                get_overlay_record(&guard, record),
                Some(record.to_be_bytes().to_vec())
            );
        }
    }

    #[test]
    fn the_drain_driver_takes_wal_pressure_merges_off_the_commit_path() {
        // 2026-11-11: past the WAL watermark every commit merged one chunk
        // inside its writer hold (324 times in ten minutes in production),
        // however small the overlay. With the driver alive a commit only
        // reports the pressure, and the driver drains any pending entries,
        // also below the soft levels, while a scope round is busy.
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(authority_directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        let database = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 16 * 1024 * 1024)
                .unwrap(),
        ));
        let (scheduler, idle) = backed_off_search_scheduler(&database, &projection);
        let observer = scheduler.observer();
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        wait_for(&observer, |_| trigger.signal().driver_alive());
        let index = projection.lock().unwrap();
        create_searchable_conversation(&database, "pressureneedle");
        let blocked = wait_for(&observer, |metrics| metrics.rounds > idle.rounds);
        {
            let mut guard = database.lock().unwrap();
            for record in 0..100_u32 {
                put_overlay_record(&mut guard, record);
            }
            guard.set_overlay_wal_watermark_for_test(0);
            let pending = guard.entity_overlay_entries_for_test();
            put_overlay_record(&mut guard, 100);
            assert_eq!(
                guard.entity_overlay_entries_for_test(),
                pending + 1,
                "the commit merged inline"
            );
        }
        assert!(trigger.signal().wal_pressure_reported());
        let drained = wait_for(&observer, |_| !trigger.needs_drain());
        assert_eq!(
            drained.rounds, blocked.rounds,
            "the scope round was still in progress"
        );
        drop(index);
        scheduler.stop_and_join().unwrap();
        assert!(
            !trigger.signal().driver_alive(),
            "a stopped driver hands merging back to commits"
        );
        let guard = database.lock().unwrap();
        assert_eq!(guard.entity_overlay_entries_for_test(), 0);
        for record in [0_u32, 100] {
            assert_eq!(
                get_overlay_record(&guard, record),
                Some(record.to_be_bytes().to_vec())
            );
        }
    }

    #[test]
    fn the_drain_driver_makes_one_pressure_pass_per_wal_window() {
        // 2026-11-12 (GATE-PUBLISH-CARRY drill): chasing every delta committed
        // under WAL pressure with a near-empty merge grew the active WAL to
        // 681 MB; the graceful stop then never finished. The driver makes one
        // pass per WAL window; the next window's pass takes what came after.
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        let database = Arc::new(Mutex::new(database));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_secs(1), 1),
        )
        .unwrap();
        // No scope round runs during this specification; only the driver drains.
        scheduler.shared.state.lock().unwrap().round_deadline =
            Instant::now() + Duration::from_secs(600);
        let observer = scheduler.observer();
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        wait_for(&observer, |_| trigger.signal().driver_alive());
        {
            let mut guard = database.lock().unwrap();
            for record in 0..100_u32 {
                put_overlay_record(&mut guard, record);
            }
            guard.set_overlay_wal_watermark_for_test(0);
            put_overlay_record(&mut guard, 100);
        }
        wait_for(&observer, |_| {
            database.lock().unwrap().entity_overlay_entries_for_test() == 0
        });
        {
            let mut guard = database.lock().unwrap();
            for record in 101..111_u32 {
                put_overlay_record(&mut guard, record);
            }
        }
        let polled = observer.metrics().unwrap().overlay_background_wakes;
        let later = wait_for(&observer, |metrics| {
            metrics.overlay_background_wakes >= polled + 8
        });
        assert_eq!(
            database.lock().unwrap().entity_overlay_entries_for_test(),
            10,
            "the driver chased deltas committed later in the same window: {later:?}"
        );
        {
            let mut guard = database.lock().unwrap();
            guard.engine_mut_for_test().checkpoint().unwrap();
            put_overlay_record(&mut guard, 111);
        }
        let drained = wait_for(&observer, |_| {
            database.lock().unwrap().entity_overlay_entries_for_test() == 0
        });
        assert_eq!(drained.rounds, 0, "{drained:?}");
        scheduler.stop_and_join().unwrap();
        let guard = database.lock().unwrap();
        for record in [0_u32, 100, 110, 111] {
            assert_eq!(
                get_overlay_record(&guard, record),
                Some(record.to_be_bytes().to_vec())
            );
        }
    }

    #[test]
    fn poisoned_authority_is_a_terminal_observable_failure() {
        let directory = tempfile::tempdir().unwrap();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize(directory.path()).unwrap(),
        ));
        let poison_target = Arc::clone(&database);
        assert!(thread::spawn(move || {
            let _guard = poison_target.lock().unwrap();
            panic!("intentional authority mutex poison");
        })
        .join()
        .is_err());
        let scheduler = MaintenanceScheduler::start(
            database,
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let observer = scheduler.observer();
        let metrics = wait_for(&observer, |metrics| metrics.failed_rounds == 1);
        assert_eq!(metrics.committed_transactions, 0);
        assert_eq!(
            observer.require_healthy().unwrap_err().kind(),
            io::ErrorKind::Other
        );
        assert_eq!(
            scheduler.stop_and_join().unwrap_err().kind(),
            io::ErrorKind::Other
        );
    }
}
